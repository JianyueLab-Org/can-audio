"""通播登录名包含 callsign；不同机场可以使用同一个频率频道。"""

import request
import process
import os
import sys
# 将 opus.dll 所在目录（Python 安装目录）加入 PATH，确保 opuslib 能找到原生 Opus 库
_python_dir = os.path.dirname(sys.executable)
os.environ['PATH'] = _python_dir + os.pathsep + os.environ.get('PATH', '')

# 口令和 login.py 走同一套，共用上一层的 serverconf——两边比对的是同一个值，
# 各存一份迟早会对不上，那时的症状是通播登不上而普通用户一切正常
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import serverconf

# 必须在 import pymumble 之前。pymumble 1.6.1 建 TLS 用的是 ssl.wrap_socket()，
# 这个函数 Python 3.12 已经删除，而它的兜底分支调的是同一个函数——异常从
# pymumble 自己的线程里抛出去，这边只看到"连接错误"，于是会去查密码，而 TLS
# 握手根本没开始。四个客户端一直都调这一句，服务端这队通播机漏了：Debian 13
# 和新一点的 Ubuntu 上 python 都 >= 3.12，装上就是连不上。
import mumblecompat
import atisrouting
mumblecompat.install()

import pymumble_py3 as pymumble
from pymumble_py3 import messages
from pymumble_py3.constants import (PYMUMBLE_CONN_STATE_CONNECTED,
                                    PYMUMBLE_CONN_STATE_FAILED,
                                    PYMUMBLE_CLBK_SOUNDRECEIVED)
import threading
import numpy as np
import time
import wave
import shutil
import subprocess
import tempfile
import io
import asyncio
import edge_tts
import re

# 建完临时频道到它出现在频道表里、以及进频道到服务器确认，都要等一次网络往返。
# 固定 sleep 赌不起——服务器忙的时候 0.1 秒远远不够。
CHANNEL_TIMEOUT = 5.0

# 收一条通播线程最多等多久。裸的 join() 会让一个卡住的席位把整队拖死。
JOIN_TIMEOUT = 10.0

# 等服务器认下这次登录最多等多久。原来是固定 sleep(1) 就直接去进频道：网慢
# 的时候一秒还没认完，会把好连接误判成失败；而登录被拒时一秒又白等——两头
# 都不合适，改成轮询。
CONNECT_TIMEOUT = 10.0

class ATISBroadcaster(threading.Thread):
    def __init__(self, atis_id, frequency, atis_text, feed=None,
                 datafeed_url=None, atis_range_nm=None):
        super().__init__()
        freq_value = int(round(float(frequency) * 1000))
        channel_name = f"FREQ_{str(freq_value).zfill(6)}"
        self.channel_name = channel_name
        # 口令不写在这里，见 server/serverconf.py
        self.user = f"{serverconf.atis_account()}_atis{freq_value:06d}_{atis_id}"
        self.password = serverconf.atis_password(required=True)
        self.running = False
        self.mumble = None
        self._router = atisrouting.ATISRouter(
            atis_id, freq_value,
            datafeed_url if datafeed_url is not None else serverconf.atis_datafeed_url(),
            atis_range_nm if atis_range_nm is not None else serverconf.atis_range_nm(),
            feed=feed)
        
        # 处理ATIS文本
        print (f"原始ATIS文本: {atis_text}")
        if '|' in atis_text:
            self.english_text, self.chinese_text = [part.strip() for part in atis_text.split('|')]
            self.english_text = process.process_single_atis_text(self.english_text, is_chinese=False)
            self.chinese_text = process.process_single_atis_text(self.chinese_text, is_chinese=True)
        else:
            self.english_text = process.process_single_atis_text(atis_text.strip(), is_chinese=False)
            self.chinese_text = None
        print (f"处理后的ATIS文本: {self.english_text} ;; {self.chinese_text}")


        self.silence_duration = 1.0
        self.last_sound_time = time.time()
        self.chunk_size = 2000  # 20ms @ 48000Hz
        self.loop = asyncio.new_event_loop()

    def connect_to_server(self):
        try:
            # 如果频道是 FREQ_199998，跳过创建
            if self.channel_name == "FREQ_199998":
                print("跳过创建频道 FREQ_199998")
                return False
                
            self.mumble = pymumble.Mumble("audio.ceruleanavi.net", self.user, password=self.password, reconnect=True)
            self.mumble.set_receive_sound(True)
            self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_SOUNDRECEIVED, self._on_sound)
            self._router.attach(self.mumble)
            self.mumble.start()

            if not self._wait_until_connected():
                self._report_login_failure()
                # reconnect=True 的连接不停掉就是个僵尸：登录被拒它也一直
                # 重试，而 login.py 对认证失败按账号限流，整队机器人共用一个
                # ATIS_CID——一个僵尸就能把整个账号的语音锁死。
                self._stop_mumble()
                return False

            return self._join_channel()
        except Exception as e:
            print(f"连接错误: {e}")
            self._stop_mumble()
            return False

    def _stop_mumble(self):
        self._router.close()
        if getattr(self, "mumble", None) is None:
            return
        try:
            self.mumble.stop()
        except Exception:
            pass
        self.mumble = None

    def _wait_until_connected(self, timeout=CONNECT_TIMEOUT):
        """等服务器把这次登录认下来，认下来才算连上。

        **start() 返回不代表连上了。** Mumble 是 threading.Thread 的子类，
        start() 拉起它自己的线程就回来了；登录被拒时 ConnectionRejectedError
        死在那条线程里，这边什么都收不到。原来紧接着 sleep(1) 就去进频道，于是
        一个被拒的登录表现成"频道 FREQ_xxxxxx 不存在"或者"进频道请求没有生效"
        ——排查方向被带到权限和 ACL 上，而问题在账号。

        reconnect=True 时状态多半停在 AUTHENTICATING 反复重试，不会落到
        FAILED，所以超时也算失败。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = getattr(self.mumble, "connected", None)
            if state == PYMUMBLE_CONN_STATE_CONNECTED:
                return True
            if state == PYMUMBLE_CONN_STATE_FAILED:
                return False
            time.sleep(0.1)
        return False

    def _report_login_failure(self):
        """说清楚为什么登不上，别让人去查频道和权限。

        这队机器人用的保留账号（默认 900）**没有免验证的旁路**——login.py 里
        那条旁路已经删掉了，`test_there_is_no_shortcut_for_any_account` 钉着它
        不许加回来。也就是说 `{ATIS_CID}` 这个 cid 得在 can-web 那边真实存在、
        密码就是它的网站密码、而且 rating >= 1（那个接口拒绝未评级会员）。

        三个条件缺任何一个，现象都一模一样：登录被拒。所以这里把三个条件一起
        列出来，而不是只说一句"连接错误"。
        """
        account = serverconf.atis_account()
        print(f"账号 {self.user} 没能登上语音服务器（{CONNECT_TIMEOUT:.0f} 秒内没有认下来）。"
              f"\n  这队机器人没有免验证通道，cid={account} 是拿去 can-web 的"
              f" /api/v1/public/auth 验的，和普通用户走同一条路。三件事都要成立："
              f"\n    1. can-web 那边确实有 cid={account} 这个账号"
              f"\n    2. ATIS_PASSWORD 就是它的网站密码"
              f"\n    3. 它的 rating >= 1（未评级的会员那个接口一律拒绝）"
              f"\n  换账号改环境变量 ATIS_CID，不要去改 login.py。")

    def _join_channel(self):
        """进入本席位的频率频道，成功才返回 True。

        和连接分开，是因为这一段全是超时和轮询，单独才测得了。
        """
        try:
            channel = self._find_channel(self.channel_name)
            if channel is None:
                print(f"频道 {self.channel_name} 不存在，建一个临时的")
                self._create_channel(self.channel_name)
                channel = self._wait_for_channel(self.channel_name)

            if channel is None:
                print(f"建立频道 {self.channel_name} 后 {CHANNEL_TIMEOUT:.0f} 秒内没有出现")
                return False

            channel_id = channel["channel_id"]
            myself = self.mumble.users.myself
            if myself is None or myself["channel_id"] != channel_id:
                self._move_in(channel_id)
                if not self._wait_until_in(channel_id):
                    print(f"发出了进入频道 {self.channel_name} 的请求，"
                          f"但 {CHANNEL_TIMEOUT:.0f} 秒内没有生效")
                    return False
            return True
        except Exception as e:
            print(f"进入频道失败: {e}")
            return False

    def _in_expected_channel(self):
        """服务器上此刻是不是真的还在本席位的频道里。

        连接是 reconnect=True 建的，而 _join_channel 只在 connect_to_server 里
        跑过一次。pymumble 掉线自己连回来之后，服务器会把用户放回**根频道**，
        本地这边毫无变化——通播于是一直往根频道里播：该听到的人听不到，反而
        是那些自己切频道失败、卡在根频道的人全都听得到。这个机器人是常年挂着
        的，重连几乎必然发生。
        """
        try:
            if not self.mumble:
                return False
            channel = self._find_channel(self.channel_name)
            myself = self.mumble.users.myself
        except Exception:
            return False
        if channel is None or myself is None:
            return False
        return myself["channel_id"] == channel["channel_id"]

    def _find_channel(self, name):
        try:
            return self.mumble.channels.find_by_name(name)
        except pymumble.errors.UnknownChannelError:
            return None

    def _create_channel(self, name):
        """在根下建一个临时频道，**不要阻塞**。

        pymumble 的 channels.new_channel() 走 execute_command(blocking=True)，
        那个 acquire 没有任何超时——pymumble 自己的源码里就写着
        "TODO: manage a timeout for blocking commands"。命令一旦没被处理，这里
        就永远卡住，这条通播线程整个死掉：日志停在建频道那一行，既没有成功也
        没有任何错误，而管理器还以为它在播。

        自己发命令、不等锁；频道有没有建出来由 _wait_for_channel 轮询判断。
        """
        self.mumble.execute_command(messages.CreateChannel(0, name, True),
                                    blocking=False)

    def _move_in(self, channel_id):
        """进频道，同样不能用 move_in()——它也走 blocking=True。"""
        self.mumble.execute_command(
            messages.MoveCmd(self.mumble.users.myself_session, channel_id),
            blocking=False)

    def _wait_for_channel(self, name):
        """等服务器把新建的频道回报回来。

        建频道只是发一条消息，频道要等服务器回 ChannelState 才进本地表——这是
        一次网络往返，固定 sleep(0.1) 经常还没回来，报出来是频道不存在。
        """
        deadline = time.time() + CHANNEL_TIMEOUT
        while time.time() < deadline and self.running:
            channel = self._find_channel(name)
            if channel is not None:
                return channel
            time.sleep(0.1)
        return self._find_channel(name)

    def _wait_until_in(self, channel_id):
        """等服务器确认我们真的进了这个频道。

        move 是异步的，命令发出去不等于进去了。不确认就返回 True，通播会对着
        根频道播一整轮。
        """
        deadline = time.time() + CHANNEL_TIMEOUT
        while time.time() < deadline and self.running:
            myself = self.mumble.users.myself
            if myself is not None and myself["channel_id"] == channel_id:
                return True
            time.sleep(0.1)
        myself = self.mumble.users.myself
        return myself is not None and myself["channel_id"] == channel_id

    def check_channel_silence(self):
        return time.time() - self.last_sound_time >= self.silence_duration

    def _on_sound(self, user, soundchunk):
        try:
            user.sound.get_sound()
        except Exception:
            pass
        if user.get("name") != self.user and self._router.hears(user):
            self.last_sound_time = time.time()

    async def _text_to_audio_edge(self, text, voice="en-US-ChristopherNeural"):
        """使用edge-tts将文本转换为音频"""
        # 打印转换的文本内容
        # print (f"正在转换ATIS: {text.strip()}")
        communicate = edge_tts.Communicate(text, voice)
        # 临时文件必须每次唯一：每个席位一条广播线程，全都往 CWD 写同两个
        # 定名文件的话，两个席位同时合成时 ffmpeg 会去读半截 mp3、甚至把
        # A 机场的音频当成 B 机场的播出去——还一条错都不报。
        temp_dir = tempfile.mkdtemp(prefix="atis_tts_")
        temp_file = os.path.join(temp_dir, "edge.mp3")
        wav_file = os.path.join(temp_dir, "processed.wav")
        try:
            await communicate.save(temp_file)

            # 使用ffmpeg将MP3转换为WAV格式
            subprocess.run([
                'ffmpeg', '-y',
                '-i', temp_file,
                '-ar', '48000',
                '-ac', '1',
                '-acodec', 'pcm_s16le',
                wav_file
            ], capture_output=True)

            with wave.open(wav_file, 'rb') as wf:
                audio_data = wf.readframes(wf.getnframes())

            return audio_data
        finally:
            # 清理临时文件
            shutil.rmtree(temp_dir, ignore_errors=True)

    def text_to_audio(self, text):
        """将文本转换为音频数据"""
        print (f"正在转换ATIS: {text}")
        
        # 使用正则表达式检查是否包含中文字符
        has_chinese = bool(re.search(r'[\u4e00-\u9fff]', text))
        
        if has_chinese:  # 处理含中文的情况

            audio_data = bytearray()
            

            # 转换中文部分

            chinese_audio = self.loop.run_until_complete(
                self._text_to_audio_edge(text, "zh-CN-YunxiNeural"))
            audio_data.extend(chinese_audio)
            
            return bytes(audio_data)
        else:
            # 处理纯英文
            return self.loop.run_until_complete(
                self._text_to_audio_edge(text, "en-US-ChristopherNeural"))

    def broadcast_audio(self, audio_data):
        """广播音频数据"""
        if not audio_data:
            return False

        generation = self._router.begin_cycle()
        if generation is None:
            return False

        position = 0
        total_size = len(audio_data)

        while position < total_size and self.running:
            if self.check_channel_silence():
                chunk_size = min(self.chunk_size, total_size - position)
                chunk = audio_data[position:position + chunk_size]
                if len(chunk) < chunk_size:
                    chunk += b'\x00' * (chunk_size - len(chunk))
                
                if not self._router.enqueue(chunk, generation):
                    return False
                position += chunk_size
                time.sleep(0.02)
            else:
                self._router.discard()
                return False
        deadline = time.monotonic() + 5
        while self.running and time.monotonic() < deadline:
            if self._router.generation != generation:
                return False
            if self.mumble.sound_output.get_buffer_size() <= 0:
                return True
            time.sleep(0.02)
        self._router.discard()
        return False

    def run(self):
        """线程主函数"""
        self.running = True
        try:
            if not self.connect_to_server():
                print(f"ATIS {self.user} 连接失败")
                return
            print(f"ATIS {self.user} 开始广播")
            self._broadcast_loop()
        finally:
            self.running = False
            self._stop_mumble()
            self.loop.close()

    def _broadcast_loop(self):
        """广播循环函数"""
        print("开始ATIS广播循环")
        while self.running:
            try:
                # 每一轮开播前对着服务器确认一次，别信自己记的状态
                if not self._in_expected_channel():
                    print(f"已经不在 {self.channel_name} 里了（多半是断线重连过），"
                          f"重新进频道")
                    if not self._join_channel():
                        time.sleep(5)
                        continue

                if self.check_channel_silence():
                    # 如果有中文ATIS，先播放中文
                    if self.chinese_text:
                        print("\n开始播放中文ATIS...")
                        chinese_audio = self.text_to_audio(self.chinese_text)
                        if chinese_audio:
                            if not self.broadcast_audio(chinese_audio):
                                time.sleep(1)
                                continue

                        if not self.running:
                            break

                        print("中文播放完成，等待检查频道状态...")

                    # 播放英文ATIS
                    if self.check_channel_silence() and self.running:
                        print("\n开始播放英文ATIS...")
                        english_audio = self.text_to_audio(self.english_text)
                        if english_audio:
                            self.broadcast_audio(english_audio)

                    print("本轮播放完成，等待下一轮...")
                    # time.sleep(30)  # 两轮播放之间的间隔
                else:
                    print("检测到频道有其他音频，等待...")
                    time.sleep(5)  # 检测到其他音频时的等待时间

            except Exception as e:
                print(f"ATIS播放循环错误: {str(e)}")
                time.sleep(5)  # 发生错误时的等待时间

            if not self.running:
                break

    def stop(self):
        """停止广播"""
        self.running = False
        self._router.close()
        if self.mumble:
            self.mumble.stop()

class ATISManager:
    def __init__(self):
        self.broadcasters = {}
        self.update_interval = 5
        self._stop_flag = False
        self.update_thread = None
        self.feed = None
        self._metadata_lock = threading.Lock()
        self._station_metadata = None

    def start(self):
        """启动ATIS管理器"""
        self._stop_flag = False
        self.feed = atisrouting.acquire_feed(serverconf.atis_datafeed_url())
        self.feed.subscribe(self._on_feed)
        self.update_thread = threading.Thread(target=self._update_loop)
        self.update_thread.start()

    def _on_feed(self, data):
        stations = None if data is None else [
            {key: station.get(key) for key in ("callsign", "frequency", "text_atis")}
            for station in data.get("atis", [])]
        with self._metadata_lock:
            self._station_metadata = stations

    def _retire(self, callsign):
        """收掉一个通播席位。**join 必须带超时。**

        原来是裸的 join()：一条卡住的通播线程会把调用方一起拖死。而调用方是
        每 30 秒跑一轮的管理线程——它一死，之后所有席位都不再新建、换稿或撤下，
        整个机队就停在那里，外面还看不出任何异常。

        一个收不掉的线程只是漏一个线程，把整队拖死是另一回事。超时就放手，
        并且说清楚是哪一个，好去查它卡在哪。
        """
        broadcaster = self.broadcasters.pop(callsign, None)
        if broadcaster is None:
            return
        try:
            broadcaster.stop()
        except Exception as e:
            print(f"停止 {callsign} 出错: {e}")
        broadcaster.join(timeout=JOIN_TIMEOUT)
        if broadcaster.is_alive():
            print(f"警告: {callsign} 的通播线程 {JOIN_TIMEOUT:.0f} 秒内没有退出，"
                  f"不再等它（线程会留着，但不影响其余席位）")

    def stop(self):
        """停止所有ATIS广播"""
        self._stop_flag = True
        if self.update_thread:
            self.update_thread.join(timeout=JOIN_TIMEOUT)
            if self.update_thread.is_alive():
                print(f"警告: 管理线程 {JOIN_TIMEOUT:.0f} 秒内没有退出")
        for callsign in list(self.broadcasters.keys()):
            self._retire(callsign)
        self.broadcasters.clear()
        if self.feed is not None:
            self.feed.unsubscribe(self._on_feed)
            atisrouting.release_feed(self.feed)
            self.feed = None

    def _update_loop(self):
        """更新ATIS信息的循环"""
        while not self._stop_flag:
            try:
                with self._metadata_lock:
                    stations = self._station_metadata
                if stations is not None:
                    self._reconcile(stations)
            except Exception as e:
                print(f"更新ATIS信息时出错: {e}")
            
            # 等待下一次更新
            for _ in range(self.update_interval):
                if self._stop_flag:
                    break
                time.sleep(1)

    def _reconcile(self, stations):
        current = {station['callsign']: station for station in stations}
        for callsign in list(self.broadcasters):
            if callsign not in current:
                self._retire(callsign)
        for callsign, station in current.items():
            frequency = station.get('frequency', '0')
            khz = int(round(float(frequency) * 1000))
            if not 118000 <= khz <= 136975:
                self._retire(callsign)
                continue
            channel_name = f"FREQ_{khz:06d}"
            previous = self.broadcasters.get(callsign)
            if previous is not None and (not previous.is_alive() or previous.channel_name != channel_name):
                self._retire(callsign)
            text = ' '.join(station.get('text_atis') or [])
            if callsign not in self.broadcasters:
                broadcaster = ATISBroadcaster(
                    atis_id=callsign, frequency=frequency, atis_text=text,
                    feed=self.feed, datafeed_url=serverconf.atis_datafeed_url(),
                    atis_range_nm=serverconf.atis_range_nm())
                self.broadcasters[callsign] = broadcaster
                broadcaster.start()
            else:
                broadcaster = self.broadcasters[callsign]
                if text != broadcaster.english_text:
                    if '|' in text:
                        english, chinese = [part.strip() for part in text.split('|')]
                        broadcaster.english_text = process.process_single_atis_text(english, is_chinese=False)
                        broadcaster.chinese_text = process.process_single_atis_text(chinese, is_chinese=True)
                    else:
                        broadcaster.english_text = process.process_single_atis_text(text.strip(), is_chinese=False)
                        broadcaster.chinese_text = None

if __name__ == "__main__":
    manager = ATISManager()
    try:
        manager.start()
        # 保持主程序运行
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("正在停止ATIS服务...")
        manager.stop()
        print("ATIS服务已停止")
