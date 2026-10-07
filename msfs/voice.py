"""Mumble 语音。

对应 xPilot 的 audio 层。和 client/ xplane_client/ 里那两份最大的差别是：
**这里不自己去问频率**。XPlaneLink 已经在持续订阅 COM1 了，界面把频率喂进
set_frequency() 就行，省掉一整套轮询和"频道对不对"的反复检查。

频率到频道的换算是全网统一的约定，改了会同时打断飞行员、管制和 ATIS：

    freq_value   = int(round(频率 * 1000))        # 125.400 -> 125400
    channel_name = f"FREQ_{freq_value:06d}"       # -> FREQ_125400
"""

import logging
import threading
import time

import denoise
import micgain
import mumblecompat
from i18n import t

# pymumble 建 TLS 用的 ssl.wrap_socket 在 Python 3.12 里已被删除。它自己那个
# except AttributeError 的兜底又调回同一个函数，异常从 pymumble 的线程里抛出
# 去，外面只看到线程死了，报成"服务器拒绝连接"。必须在 import 之前补。
mumblecompat.install()

import numpy as np
import pyaudio
import pymumble_py3 as pymumble
from pymumble_py3 import messages
from pymumble_py3.constants import (PYMUMBLE_CLBK_CONNECTED,
                                    PYMUMBLE_CLBK_DISCONNECTED,
                                    PYMUMBLE_CLBK_PERMISSIONDENIED,
                                    PYMUMBLE_CLBK_SOUNDRECEIVED,
                                    PYMUMBLE_CLBK_USERREMOVED,
                                    PYMUMBLE_CONN_STATE_CONNECTED,
                                    PYMUMBLE_CONN_STATE_FAILED)

log = logging.getLogger("voice")


def _event_has(event, field):
    """UserRemove 里这个字段到底填了没有。

    Mumble.proto 是 proto2，字段是 optional，所以"没填"和"填了空值"是两件事 ——
    被踢时 reason 可以是空串，但 actor 一定在。protobuf 用 HasField 判，而测试
    里用 dict 当替身更省事，所以两种都认。
    """
    try:
        return event.HasField(field)
    except (AttributeError, ValueError):
        pass
    try:
        return field in event
    except TypeError:
        return getattr(event, field, None) is not None


def _event_get(event, field):
    try:
        return event[field]
    except (TypeError, KeyError, IndexError):
        return getattr(event, field, None)

SAMPLE_RATES = [48000, 44100, 32000, 24000, 16000]
FORMAT = pyaudio.paInt16
CHANNELS = 1
RX_TIMEOUT = 0.5          # 这么久没有新音频就把接收灯灭掉
# 建完临时频道到它出现在频道表里，要等服务器回一条 ChannelState。这是一次网络
# 往返，固定 sleep 赌不起——远程服务器上经常不够。
CHANNEL_TIMEOUT = 5.0
# 没切到目标频道时多久重试一次。切换要能自愈——刚上线那几秒 mumble 常常还没
# 就绪，一次失败就永远留在根频道是最难查的故障。
CHANNEL_RETRY_INTERVAL = 1.0
ROOT_CHANNEL = 0

# 连上过之后掉线，最多再试这么多次；都失败就整个下线，不留一个后台无限重试的
# 僵尸。pymumble 每次重试之间等 PYMUMBLE_CONNECTION_RETRY_INTERVAL（上面调成了
# 2 秒），所以三次大约是六到八秒。
RECONNECT_LIMIT = 3


def channel_name(frequency_mhz):
    """频率（MHz）对应的 Mumble 频道名。"""
    return f"FREQ_{int(round(float(frequency_mhz) * 1000)):06d}"


class BoundedReconnect:
    """pymumble 的重连是无限的，这个混入给它一个上限。

    默认的 `reconnect=True` 会让 `run()` 一直重试到进程结束。后果不是"多试几
    次"这么轻：服务端 `login.py` 对认证失败**按 CAN ID 限流**，一个不停重连的
    僵尸足以把这个账号的语音锁死，于是用户把密码改对了也连不上，直到重启客户
    端为止。界面那边同样糟——最后一次状态回报停在"已断开"，而连接其实还在后台
    挣扎，谁也说不清此刻到底是什么状态。

    **必须挂在 connect() 上，不能去数 DISCONNECTED 回调。** pymumble 的
    `run()`（mumble.py:120-143）丢连接时发一次 DISCONNECTED，然后 sleep 再
    connect()，而**失败的重连尝试是完全静默的**：`connect() >= FAILED` 那一支
    只 `sleep + continue`，一个回调都不发。所以数回调数到的是"掉了几次"而不是
    "试了几次"——服务器一直起不来时回调只有一次，后面它安安静静重试到天亮。

    **"连上了"只能以 ServerSync 为准。** `connect()` 成功时返回的是
    `AUTHENTICATING`（1）而不是 `CONNECTED`（2）：它只负责建 TLS 并把
    Authenticate 发出去，认证结果要等服务器回话。密码错的连接同样返回 1，然后
    在 `loop()` 里因为 Reject 结束——把返回值当成功就等于每次都把计数清零，
    正好又变回无限重试，而这一次撞的是上面说的那个按账号限流。所以计数只在
    `_session_established()`（由 CONNECTED 回调调用）里清零。

    次数用尽时不再自己发起连接：把 `reconnect` 置假并返回 FAILED，`run()` 当场
    抛 `ConnectionRejectedError`，调用方在 `_mumble_loop` 里看到 `gave_up` 就走
    整个下线的路径。

    **写成混入而不是 `class X(pymumble.Mumble)`，是为了不把基类钉死。** 测试是
    通过替换 `voice.pymumble.Mumble` 来放替身的（VoiceRuntimeTest 那一套），
    顶层子类会在 import 那一刻就把真的 Mumble 焊进 MRO，替身从此再也进不来。
    `bounded_mumble()` 每次现取现套，两边都成立。
    """

    # 类属性当默认值：实例第一次写的时候才会有自己的那份，不用改 __init__ 的签名
    reconnect_limit = RECONNECT_LIMIT
    reconnect_attempts = 0
    gave_up = False
    on_reconnect = None            # 回调 (第几次, 上限)，用来更新界面
    _established = False
    kicked = False                 # 被服务端踢下来了，不要连回去
    kick_reason = ""

    def _session_established(self):
        """真的建立过一次会话（收到 ServerSync）。计数从这里清零。"""
        self._established = True
        self.reconnect_attempts = 0

    def mark_kicked(self, reason=""):
        """服务端把我们踢了，这条连接不许再连回去。

        次数上限拦不住这种情况，因为它数的是**失败**的重连：被踢之前的那次登录
        是成功的，`_session_established()` 已经把计数清零了。同一个账号在两台机
        器上登录时，两边就这样互相顶掉、各自重连、各自又把对方顶掉，每一轮都是
        成功的，所以三次的预算永远用不完 —— 这是构造上的死循环，不是上限没生效。
        Murmur 那边看到的是同一个 IP 一直在连，最后按 autoban 把它整个封掉。

        所以这里不是"再试几次"的问题，是"根本不该再试"：新的会话是用户在别处主
        动建立的，旧的这一端连回去只会把新的顶掉。
        """
        self.kicked = True
        self.kick_reason = reason or ""
        self.reconnect = False

    def connect(self):
        # 被踢优先判：它和次数无关，而且是唯一一种"重连本身就是错的"情形。
        if self.kicked:
            self.gave_up = True
            self.reconnect = False
            self.connected = PYMUMBLE_CONN_STATE_FAILED
            log.warning("kicked by the server (%s), not reconnecting",
                        self.kick_reason or "no reason given")
            return self.connected

        # 判定放在发起连接之前：这样两种失败（连不上服务器、服务器拒绝认证）
        # 都被同一处挡住，也不会多试出第四次。
        if self._established and self.reconnect_attempts >= self.reconnect_limit:
            self.gave_up = True
            self.reconnect = False
            self.connected = PYMUMBLE_CONN_STATE_FAILED
            log.warning("%d reconnect attempts after the drop all failed, "
                        "giving up", self.reconnect_attempts)
            return self.connected

        if self._established:
            self.reconnect_attempts += 1
            log.info("reconnecting to the voice server (attempt %d/%d)",
                     self.reconnect_attempts, self.reconnect_limit)
            if self.on_reconnect:
                try:
                    self.on_reconnect(self.reconnect_attempts,
                                      self.reconnect_limit)
                except Exception as e:
                    log.warning("reconnect callback raised: %s", e)

        return super().connect()


def bounded_mumble(base=None):
    """把 BoundedReconnect 套在当前的 pymumble.Mumble 上，返回那个类。

    现取现套的理由见 BoundedReconnect 的说明：import 时钉死基类会让测试的替身
    进不来。`base` 参数是给测试直接指定假基类用的。
    """
    return type("BoundedReconnectMumble", (BoundedReconnect,
                                           base or pymumble.Mumble), {})


class Voice:
    """一条 Mumble 连接，跟着 COM1 走。

    回调都在后台线程触发：
        on_status(state, message)   connecting / online / reconnecting /
                                    error / offline / stopped
        on_ptt(bool) / on_rx(bool)
        on_channel(frequency, channel_name)

    状态里有两条是掉线相关的，界面必须分开处理：`reconnecting` 是暂时的，
    pymumble 正在连回来，这条连接还活着；`offline` 是重连 RECONNECT_LIMIT 次都
    失败、已经自己收摊了，整个客户端应当跟着下线。
    """

    def __init__(self, host, username, password="", settings=None,
                 on_status=None, on_ptt=None, on_rx=None, on_channel=None,
                 reconnect_limit=RECONNECT_LIMIT):
        self.host = host
        self.username = username
        self.password = password
        self.settings = settings
        # 掉线后最多重连几次。用尽就整个下线，见 BoundedReconnectMumble。
        self.reconnect_limit = reconnect_limit

        self.on_status = on_status
        self.on_ptt = on_ptt
        self.on_rx = on_rx
        self.on_channel = on_channel

        self.mumble = None
        self.frequency = None
        self.channel = None
        self.transmitting = False
        self.receiving = False
        self.running = False

        self._audio = None
        self._input = None
        self._output = None
        self._rate = 48000
        self._denoiser_factory = denoise.Denoiser
        self._denoiser = None
        self._mic_limiter = None
        self._chunk = 960
        self._last_rx = 0.0
        self._sent_frames = 0           # 本次 PTT 已发出的帧数
        self._received_frames = 0       # 本段接收已播放的帧数
        self._skip_reason = ""          # 明明按着 PTT 却没发的原因
        self._stuck_reason = ""         # 迟迟进不了频率频道的原因
        self._lock = threading.Lock()      # 一条连接一个发送队列，串行化
        # 护住 PyAudio 流的开、关、读、写。重开设备（设置里换了声卡）跑在
        # Qt 线程上，而发送线程可能正卡在 stream.read()、pymumble 的回调线程
        # 可能正在 stream.write()——C 层的流被另一条线程 close/terminate 是
        # 直接崩进程，Python 的 try/except 接不住。管制端一直有这把锁
        # （controller/voice.py 的 _stream_lock），这份原来没有。
        self._stream_lock = threading.Lock()
        self._channel_lock = threading.Lock()   # 频道切换不能并发
        self._channel_wanted = threading.Event()  # 有新频率要切
        self._channel_thread = None
        self._pending = None               # 还没切过去的频率
        self._thread = None

        # ---- 连接状态，照搬老飞行员端 client/radio.py 的做法 ----
        # 由 pymumble 的 CONNECTED / DISCONNECTED 回调翻转，不去猜 mumble 内部
        # 的线程状态。老客户端叫 _connection_established。
        self._connection_established = threading.Event()
        self._mumble_thread = None         # 我们自己拿着的 pymumble 主循环线程
        self._reject_reason = ""           # 服务器拒绝时的原因，来自 run() 的异常
        # 重连次数用尽、已经彻底下线。和"没在跑"是两回事：这个要让界面知道是
        # 掉线掉没了，而不是用户自己点的断开。
        self.gave_up = False
        self._was_dropped = False          # 掉过线，用来在连回来时报一句"已重连"
        self._denial = None                # 最近一次 PermissionDenied 的原因
        # 给 _health() 用的累计量：上一份健康报告以来收发了多少帧
        self._sent_total = 0
        self._received_total = 0
        self._last_health = 0.0            # 上次打健康报告的时间

    # ---------- 状态 ----------
    def _status(self, state, message):
        log.info("%s: %s", state, message)
        if self.on_status:
            try:
                self.on_status(state, message)
            except Exception as e:
                log.warning("status callback raised: %s", e)

    @property
    def connected(self):
        """真的连上了才算。

        pymumble 的 connected 是状态码不是布尔：0 未连接、1 认证中、2 已连接、
        3 失败。原来写 bool(...)，**失败的 3 也是真值**，于是连接被服务器拒绝
        之后我们照样当成连上了——实测日志里就是这样：Mumble 回了
        "Wrong certificate or password"，界面还报"语音已连接"。

        光看状态码也不够：pymumble 的主循环退出时**不会把 connected 改回去**，
        它就停在 2。循环已经没了、命令队列再没人抽，这里却照样报"已连接"，
        界面一路绿灯——那正是"进不了频道又不报错"的表象。

        所以和老飞行员端一样，先看那个由回调翻转的独立标记
        （client/radio.py 的 `_connection_established`）：CONNECTED 时置上、
        DISCONNECTED 时清掉，主循环线程退出时也清掉。**不要用
        `mumble.is_alive()`**——pymumble 的循环现在跑在我们自己的线程里
        （`mumble.run()`，不是 `start()`），它那个 Thread 从没启动过，
        `is_alive()` 恒为 False。
        """
        if not self.mumble:
            return False
        if not self._connection_established.is_set():
            return False
        return self.mumble.connected == PYMUMBLE_CONN_STATE_CONNECTED

    # ---------- 音频设备 ----------
    def _open_audio(self):
        self._audio = pyaudio.PyAudio()
        self._rate = self._best_rate()
        self._chunk = int(self._rate * 0.02)         # 20 ms 一帧
        if self._rate != 48000:
            log.warning("sample rate fell back to %d Hz; pymumble sends audio at "
                        "48 kHz and nothing resamples here, so it will be "
                        "pitch-shifted", self._rate)

        input_index = getattr(self.settings, "input_device_index", None)
        output_index = getattr(self.settings, "output_device_index", None)
        self._input = self._audio.open(
            format=FORMAT, channels=CHANNELS, rate=self._rate, input=True,
            frames_per_buffer=self._chunk, input_device_index=input_index)
        self._output = self._audio.open(
            format=FORMAT, channels=CHANNELS, rate=self._rate, output=True,
            frames_per_buffer=self._chunk, output_device_index=output_index)
        # 把真正打开的设备名记下来。"听不到/说不出"最常见的原因就是选错了
        # 设备（比如麦克风指到了不存在的虚拟声卡），只报采样率看不出来。
        devices = (self._rate, self._chunk,
                   self._device_name(input_index, True),
                   self._device_name(output_index, False))
        # 这行有一百多个字符，而每次打开设置都会重开一次设备。设备没换就别重复：
        # 真实日志里它出现了四次，一个字都没变。
        level = log.debug if devices == getattr(self, "_last_devices", None) else log.info
        self._last_devices = devices
        level("audio ready: %d Hz, %d samples per frame; mic=%s, speaker=%s",
              *devices)

    def _device_name(self, index, is_input):
        try:
            if index is None:
                info = (self._audio.get_default_input_device_info() if is_input
                        else self._audio.get_default_output_device_info())
                return f"[系统默认] {info.get('name', '?')}"
            return self._audio.get_device_info_by_index(index).get("name", "?")
        except Exception as e:
            return f"取不到设备信息（{e}）"

    def _process_mic(self, data):
        """一帧麦克风 PCM → 要发出去的 int16：降噪 → × 基准 × 乘数 → 限幅。

        基准来自校准（settings.mic_baseline_db，GUI 按当前输入设备设置），
        乘数是设置里的麦克风音量，只在本次启动有效。
        """
        rate = getattr(self, "_rate", None) or 48000
        samples = np.frombuffer(data, dtype=np.int16)
        if getattr(self.settings, "mic_denoise", True):
            if self._denoiser is None or self._denoiser.rate != rate:
                if self._denoiser is not None:
                    self._denoiser.close()
                self._denoiser = self._denoiser_factory(rate)
            samples = self._denoiser.process(samples)
        if self._mic_limiter is None or self._mic_limiter.rate != rate:
            self._mic_limiter = micgain.Limiter(rate)
        baseline = getattr(self.settings, "mic_baseline_db", 0.0) or 0.0
        percent = getattr(self.settings, "mic_volume", 100)
        return micgain.apply_gain(samples, baseline, percent, self._mic_limiter)

    def _best_rate(self):
        input_index = getattr(self.settings, "input_device_index", None)
        output_index = getattr(self.settings, "output_device_index", None)

        def works(rate, index, is_input):
            try:
                stream = self._audio.open(
                    format=FORMAT, channels=CHANNELS, rate=rate,
                    input=is_input, output=not is_input,
                    input_device_index=index if is_input else None,
                    output_device_index=None if is_input else index,
                    frames_per_buffer=960, start=False)
                stream.close()
                return True
            except Exception:
                return False

        for rate in SAMPLE_RATES:
            if works(rate, input_index, True) and works(rate, output_index, False):
                return rate
        log.warning("none of the candidate sample rates worked, forcing 48000 Hz")
        return 48000

    def reopen_audio(self):
        """换了设备之后重开音频流。

        全程握着 _stream_lock：发送线程可能正在 read()、接收回调可能正在
        write()，不挡住它们就 close/terminate 是 C 层崩溃。
        """
        with self._stream_lock:
            for stream in (self._input, self._output):
                try:
                    if stream:
                        stream.stop_stream()
                        stream.close()
                except Exception:
                    pass
            self._input = self._output = None
            try:
                if self._audio:
                    self._audio.terminate()
            except Exception:
                pass
            self._open_audio()

    # ---------- 生命周期 ----------
    def start(self):
        if self.running:
            return
        self.running = True
        self._status('connecting', t("voice.connecting", server=self.host))
        try:
            self._open_audio()
        except Exception as e:
            self.running = False
            self._release()
            self._status('error', t("voice.audio_failed", error=e))
            return

        try:
            # reconnect=True 仍然要，掉线自己连回来是对的；上限由
            # BoundedReconnectMumble 管，用尽了就整个下线。
            self.mumble = bounded_mumble()(self.host, self.username,
                                           password=self.password,
                                           reconnect=True)
            self.mumble.reconnect_limit = self.reconnect_limit
            self.mumble.on_reconnect = self._on_reconnect_attempt
            self.mumble.set_receive_sound(True)
            self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_SOUNDRECEIVED,
                                               self._on_sound)
            self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_PERMISSIONDENIED,
                                               self._on_permission_denied)
            # 连接标记由这两条回调翻转，和老飞行员端一样
            # （client/gui.py:398-414）。这一份原来两条都没接：DISCONNECTED 正是
            # pymumble 主循环结束时走的那条，没人听的话连接已经彻底死了而界面
            # 还是绿的，日志里一个字都没有。
            self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_CONNECTED,
                                               self._on_connected)
            self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_DISCONNECTED,
                                               self._on_disconnected)
            # 被踢和普通掉线要分开，否则谁也说不清该不该连回去 —— 见
            # _on_user_removed。DISCONNECTED 是不带理由的，所以只听它的话
            # 两种情况长得一模一样。
            self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_USERREMOVED,
                                               self._on_user_removed)

            # ---- pymumble 的主循环挂在"创建它的那个线程"上 ----
            #
            # pymumble 在 __init__ 里记下
            #
            #     self.parent_thread = threading.current_thread()
            #
            # （mumble.py:59，它自己的注释写的是 "main thread of the calling
            # application"），而主循环的条件里带着它：
            #
            #     while ... and self.parent_thread.is_alive() and not self.exit:
            #         ...
            #         while self.commands.is_cmd():
            #             self.treat_command(self.commands.pop_cmd())
            #
            # **抽命令队列就在这个循环里。** 而 gui.py 是用
            # `threading.Thread(target=voice.start).start()` 调起来的：start()
            # 把 _run / _channel_loop 拉起来就返回，那个临时线程当场结束，
            # parent_thread.is_alive() 变 False，循环退出——连接还在、频道表还
            # 在、myself 也还在，但从此没有任何一条命令发得出去。
            #
            # 表现出来是这样一段能刷一整晚的日志：
            #
            #     → 发出进频道命令 FREQ_124550：会话号=111 从频道0 到频道1
            #     ← 进频道命令已入队 FREQ_124550
            #     发出了进入 FREQ_124550 的请求，但 5 秒内没有生效，稍后重试
            #     现场诊断 ... 我在频道=0 频道表共4个 表里有没有目标=True
            #
            # 命令进了队列却没上线，服务器压根没收到，自然既不照做也不回
            # PermissionDenied——"收得到、发不动、不报错"就是这么来的。老飞行员
            # 端（client/radio.py）不犯这个错纯属走运：它的 Mumble 对象建在 GUI
            # 线程里，跑 mumble.run() 的那个线程又自己阻塞着不退。
            #
            # 老飞行员端的做法是让 Mumble 对象活在一个不会退的线程上；这里把
            # 它挑明：主线程活得和进程一样久，正好就是 pymumble 那句注释的本意。
            # 关闭不受影响——_release() 走 mumble.stop()，它自己会把 reconnect
            # 置空、exit 置真、socket 关掉，不靠 parent_thread 死。
            self.mumble.parent_thread = threading.main_thread()

            # ---- 以下几处照搬 client/radio.py（老飞行员客户端）----
            # 那一份在同一台服务器、同一个账号上是实测好用的。

            # pymumble 的 init_connection 要到 start() 之后才设 connected，
            # 中间这段空窗里读它会 AttributeError
            self.mumble.connected = 0
            # ping 从默认 10 秒改成 1 秒。老客户端这么设的；服务器对久不 ping
            # 的连接可能有自己的处置，而我们建完频道要等的正是服务器的回话。
            pymumble.mumble.PYMUMBLE_PING_DELAY = 1
            pymumble.mumble.PYMUMBLE_CONNECTION_RETRY_INTERVAL = 2

            # **不用 mumble.start()，用我们自己的线程跑 mumble.run()。**
            # 老飞行员端就是这么做的（client/gui.py 的 run_client）：Mumble 是
            # threading.Thread 的子类，start() 会另起一条我们看不见也拿不到的
            # 线程，run() 则让主循环跑在我们自己拿着的线程里——它活多久、什么
            # 时候退、退出时抛了什么，都在自己手上。
            #
            # 还有一个实际好处：服务器拒绝登录时 run() 会把
            # ConnectionRejectedError 抛出来。走 start() 的话这个异常死在
            # pymumble 自己的线程里，我们只能靠状态码倒推"大概是密码不对"。
            self._mumble_thread = threading.Thread(
                target=self._mumble_loop, name="pymumble-loop", daemon=True)
            self._mumble_thread.start()
            # 老客户端在这里先睡 1 秒再 is_ready()。is_ready() 本身是等锁的，
            # 按说不需要，但那一份就是这么写的、也确实能用，先对齐再说。
            time.sleep(1)
            self.mumble.is_ready()
        except Exception as e:
            self.running = False
            self._release()
            self._status('error', t("voice.connect_failed", error=e))
            return

        # stop() 可以在连接线程等待 is_ready() 时由 GUI 触发。连接成功后必须
        # 再看一次 running；否则旧 Voice 会在快速断开/重连后继续启动发送和切频
        # 线程，并让已经过期的 Mumble 会话继续占用账号。
        if not self.running:
            self._release()
            return

        # is_ready() 返回不代表连上了：服务器拒绝时 pymumble 的连接线程会带着
        # ConnectionRejectedError 直接死掉，而 is_ready() 照样放行。实测里
        # 用户名填错，Mumble 回 "Wrong certificate or password"，界面却报
        # "语音已连接"，然后一切都莫名其妙地不工作。
        if not self.connected:
            self.running = False
            reason = self._reject_reason or t("voice.rejected_guess")
            self._release()
            self._status('error', t("voice.rejected", username=self.username, reason=reason))
            return

        # 连上那一刻的现场快照。切频道紧接着就发生（日志里两条常在同一秒），
        # 所以这一刻哪些东西还没就绪，直接决定了后面切不切得过去。
        self._snapshot("刚连上")

        self._status('online', t("voice.online", username=self.username))
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._channel_thread = threading.Thread(target=self._channel_loop,
                                                daemon=True)
        self._channel_thread.start()

        # 连上之前记下的频率现在可以切了
        if self._pending is not None:
            self._channel_wanted.set()

    def _channel_loop(self):
        """频道切换的工作线程。

        单独一条线程，不跟发送线程挤：切换要等服务器回 ChannelState，而发送
        线程是 20 ms 一帧的节奏，把切换放进去会把 PTT 一起卡住。

        **每轮都比对目标和当前，而不是等一个事件。** 原来是事件驱动的：
        set_frequency 置位、这里消费掉。只要那一次切换没成功——比如刚上线时
        mumble 还没就绪、或者建频道等超时——事件就没了，而 _pending 没变，
        set_frequency 又会直接 return，于是永远不再重试。实测就是这样：人一直
        留在根频道，对着没人的地方发，也收不到任何东西。
        """
        while self.running:
            # 事件只用来让新频率立刻生效，不作为唯一触发条件
            self._channel_wanted.wait(timeout=CHANNEL_RETRY_INTERVAL)
            self._channel_wanted.clear()
            if not self.running:
                break

            target = self._pending
            if target is None:
                self._note_stuck("no COM1 frequency yet")
                continue
            if not self.connected:
                self._note_stuck("the voice server is not connected yet")
                continue
            if target == self.frequency:
                if self._in_expected_channel():
                    self._stuck_reason = ""      # 到位了
                    continue
                # 只比对记下来的频率是不够的。pymumble 是 reconnect=True 建的，
                # 掉线重连之后服务器把人放回根频道，而 self.frequency /
                # self.channel 还停在旧值——于是这里认为"已经到位"，再也不切，
                # 人就永远留在根频道：界面显示已连接，帧数也在涨，就是没人听得到。
                log.warning("the server no longer has us in %s (most likely a reconnect), "
                            "rejoining", self.channel)
                self.frequency = None
                self.channel = None
            try:
                self._switch_channel(target)
            except Exception as e:
                log.warning("channel switch raised: %s", e)

    def _in_expected_channel(self):
        """服务器上我们真的还在自己记着的那个频道里吗？

        这是"以为在"和"确实在"的区别。记账是本地的，频道是服务器说了算的，两者
        之间隔着一次掉线重连就会不一致。
        """
        if not self.channel or not self.mumble:
            return False
        channel = self._find_channel(self.channel)
        myself = self.mumble.users.myself
        if channel is None or myself is None:
            return False
        return myself["channel_id"] == channel["channel_id"]

    def _note_stuck(self, reason):
        """迟迟切不过去时说明原因。

        原来这两个分支是静默 continue 的：日志里既没有"建一个临时的"也没有
        任何错误，只剩下"PTT 一帧都没发"，完全看不出卡在哪一步。
        """
        if reason == self._stuck_reason:
            return
        self._stuck_reason = reason
        log.warning("still not in the frequency channel: %s", reason)

    def _release(self):
        """把音频设备和 Mumble 连接放掉。start() 失败和 stop() 都必须走这里。

        漏掉任何一条路径都不是"少收一点资源"那么轻：

        - PyAudio 还占着麦克风。用户把密码改对了再点一次连接，新的 Voice 在
          _open_audio() 就可能直接失败，界面说"打不开音频设备"——真正的原因是
          上一次登录失败，指向的地方完全不对。
        - pymumble 是 reconnect=True 建的。扔着不管，它会在后台一直重连下去，
          而服务端 login.py 对认证失败按账号限流，一个不停重试的僵尸连接足以
          把这个账号的语音锁死，之后怎么连都连不上。

        顺序必须是先停 Mumble 再收音频：接收回调跑在 pymumble 的线程上，正在
        _output.write() 的时候把 PyAudio terminate 掉是 C 层崩溃——正常断开
        时对面恰好有人说话就中招。stop() 里"先收发送线程再走这里"的理由相同。
        """
        if self.mumble:
            try:
                self.mumble.stop()
            except Exception:
                pass
            self.mumble = None

        with self._stream_lock:
            for stream in (self._input, self._output):
                try:
                    if stream:
                        stream.stop_stream()
                        stream.close()
                except Exception:
                    pass
            self._input = self._output = None
            try:
                if self._audio:
                    self._audio.terminate()
            except Exception:
                pass
            self._audio = None

    def stop(self, state='stopped', message=None):
        """收掉整条连接。

        `state`/`message` 只为了区分"用户点的断开"和"重连次数用尽自己下线的"
        （`_give_up()` 传 offline）——界面对后者要连 FSD 一起收，不能只把语音
        那一行标红。

        `message` 默认是 None 而不是那句"语音已断开"：默认参数在**导入时**就求值
        了，写成 `message=t("voice.stopped")` 的话，用户之后切到英文，这一句仍然
        是当初那门语言。所有默认文案都得这样往后推到调用时。
        """
        if message is None:
            message = t("voice.stopped")
        self.running = False

        # 先收线程再动 PyAudio。反过来的话发送线程可能正卡在 stream.read()
        # 里，C 层被 terminate 掉是直接崩，Python 的 try/except 接不住。
        self._channel_wanted.set()      # 叫醒切换线程好让它看到 running=False
        for thread in (self._thread, self._channel_thread):
            if (thread and thread.is_alive()
                    and thread is not threading.current_thread()):
                thread.join(timeout=2)
        self._thread = self._channel_thread = None

        # _release() 里的 mumble.stop() 会把 reconnect 置空、exit 置真、socket
        # 关掉，主循环这才会返回——所以收这条线程必须排在 _release() 之后。
        self._release()
        loop_thread = self._mumble_thread
        if (loop_thread and loop_thread.is_alive()
                and loop_thread is not threading.current_thread()):
            loop_thread.join(timeout=2)
        self._mumble_thread = None
        self._connection_established.clear()

        self._status(state, message)

    # ---------- 频率 ----------
    def _find_channel(self, name):
        try:
            return self.mumble.channels.find_by_name(name)
        except pymumble.errors.UnknownChannelError:
            return None

    def _create_channel(self, name):
        """在根下建一个临时频道，**不要阻塞**。

        pymumble 的 channels.new_channel() 走 execute_command(blocking=True)，
        那个 acquire 没有任何超时——它自己的源码里就写着
        "TODO: manage a timeout for blocking commands"。命令一旦没被处理，这里
        就永远卡住，而且我们还握着 _channel_lock，整条切换链全死。

        实测就是这样：日志停在"建一个临时的"，之后既没有成功也没有任何错误，
        因为线程根本没从这一行返回。

        自己发命令、不等锁；频道有没有建出来由 _wait_for_channel 轮询判断，
        那本来就是更可靠的判据——服务器拒绝建频道时也不会干等。
        """
        command = messages.CreateChannel(0, name, True)
        log.info("-> sending CreateChannel %s (parent=0 temporary=True)", name)
        self.mumble.execute_command(command, blocking=False)
        log.info("<- CreateChannel %s queued (execute_command returned, "
                 "it did not block)", name)

    def _wait_until_in(self, channel_id):
        """等服务器确认我们真的进了这个频道。"""
        deadline = time.time() + CHANNEL_TIMEOUT
        while time.time() < deadline and self.running:
            myself = self.mumble.users.myself
            if myself and myself["channel_id"] == channel_id:
                return True
            time.sleep(0.1)
        return False

    def _snapshot(self, when):
        """把 Mumble 侧的关键状态打一行出来。

        这几个值是判断"命令为什么不生效"的全部依据：会话号是 None 的话，
        MoveCmd 带的就是个无效会话，服务器会默默忽略；频道表是空的话，说明
        服务器的频道列表还没收到，这时候去 find_by_name 当然找不到。
        """
        try:
            mumble = self.mumble
            users = getattr(mumble, "users", None)
            session = getattr(users, "myself_session", None) if users else None
            myself = getattr(users, "myself", None) if users else None
            channels = getattr(mumble, "channels", None)
            count = len(channels) if channels is not None else -1
            log.info("[%s] conn state=%r session=%r myself=%s my channel=%r "
                     "channel table=%d entries",
                     when, getattr(mumble, "connected", None), session,
                     "yes" if myself else "None",
                     myself["channel_id"] if myself else None, count)
        except Exception as e:
            log.warning("[%s] could not read the state: %s", when, e)

    def _diagnose(self, name, target_id=None):
        """进不去/建不出来时，把现场状态原样打出来。

        走到这里说明命令发了、服务器没照做、也没回 PermissionDenied。剩下的可能
        性靠读代码分不出来，只能看运行时的真实状态：

        - `myself_session` 是不是 None —— 是的话 MoveCmd 带的是个无效会话号，
          服务器会**默默忽略**，既不生效也不报错
        - 频道表里到底有没有这个名字 —— 有名字却进不去，和压根没建出来是两回事
        - 我们此刻在哪个频道 —— 0 是根频道
        """
        try:
            users = self.mumble.users
            session = getattr(users, "myself_session", None)
            myself = users.myself
            here = myself["channel_id"] if myself else "（myself 是 None）"
            table = self.mumble.channels
            names = [c.get("name") for c in table.values()] if hasattr(table, "values") else []
            log.warning(
                "diagnosis: target=%s(id=%s) my session=%r my channel=%r "
                "channel table=%d entries target present=%s",
                name, target_id, session, here, len(names), name in names)
            if len(names) <= 40:
                log.warning("diagnosis: channel table: %s", sorted(n for n in names if n))
        except Exception as e:
            log.warning("diagnosis: failed while reading the state: %s", e)

    def _wait_for_channel(self, name):
        """等服务器把新建的频道回报回来。

        new_channel() 只是发一条消息就返回，频道要等服务器回 ChannelState 才
        进本地表——这是一次网络往返。原来固定 sleep(0.3) 再找，连远程服务器时
        经常还没回来，日志里就是连着两条 "Channel FREQ_121700 does not exists"，
        看着像频道建不了，其实只是没等到。
        """
        deadline = time.time() + CHANNEL_TIMEOUT
        started = time.time()
        reported = 0
        self._denial = None
        while time.time() < deadline and self.running:
            if self._denial:
                # 服务器已经明说不行了（权限、重名……），等满超时纯属浪费：
                # 管制端和 ATIS 一直有这条短路，这份原来没有
                log.warning("the server refused while waiting for %s: %s",
                            name, self._denial)
                return None
            channel = self._find_channel(name)
            if channel is not None:
                log.info("channel %s appeared after %.1f s (id=%s)", name,
                         time.time() - started, channel["channel_id"])
                return channel
            # 每秒报一次进度：频道表在不在长，能区分"服务器没回"和"回了但没这个"
            waited = int(time.time() - started)
            if waited > reported:
                reported = waited
                try:
                    log.debug("waiting for %s: %d s so far, channel table has %d entries",
                              name, waited, len(self.mumble.channels))
                except Exception:
                    pass
            time.sleep(0.1)
        return self._find_channel(name)

    def set_frequency(self, frequency):
        """COM1 变了。**必须立刻返回**。

        这个方法是从 gui.py 的 tick() 调的，而 tick() 跑在 Qt 主线程上。真正
        的切换要建频道、等服务器回 ChannelState，是一次网络往返，最坏要等满
        CHANNEL_TIMEOUT——在主线程上干这件事窗口会直接"未响应"（实测过）。

        所以这里只记下目标频率并叫醒工作线程，切换在那边做。
        """
        if frequency is None:
            return
        frequency = round(float(frequency), 3)
        if frequency == self._pending:
            return
        self._pending = frequency
        self._channel_wanted.set()

    def _switch_channel(self, frequency):
        """真正的切换。只在工作线程里跑。

        整个过程上锁：start() 里的补切和工作线程会同时进来，两边各建一次
        频道、各报一次错（真实日志里就是这样）。
        """
        if not self.connected:
            return

        with self._channel_lock:
            if frequency == self.frequency:
                return          # 等锁的时候已经被另一个调用切过去了
            name = channel_name(frequency)
            try:
                channel = self._find_channel(name)
                if channel is None:
                    log.info("channel %s does not exist, creating a temporary one", name)
                    self._create_channel(name)
                    channel = self._wait_for_channel(name)
                if channel is None:
                    log.warning("channel %s did not appear within %.0f s of being created, "
                                "retrying in %.0f s",
                                name, CHANNEL_TIMEOUT, CHANNEL_RETRY_INTERVAL)
                    self._diagnose(name)
                    return

                myself = self.mumble.users.myself
                if myself is None:
                    # 这一支以前是静默走过去的，然后**当成切换成功**记账——人还在
                    # 根频道，界面却显示已经在频率上了
                    log.warning("myself is still None, skipping the switch to %s this round",
                                  name)
                    return
                if myself["channel_id"] != channel["channel_id"]:
                    session = self.mumble.users.myself_session
                    log.info("-> sending MoveCmd %s: session=%r from channel %r to "
                             "channel %r",
                             name, session, myself["channel_id"],
                             channel["channel_id"])
                    if session is None:
                        log.warning("session is None: the server will simply ignore "
                                    "this MoveCmd — it will neither take "
                                    "effect nor report an error")
                    # move_in() 也走 execute_command(blocking=True)，和建频道
                    # 一样会无限期卡住，同样自己发命令
                    self.mumble.execute_command(
                        messages.MoveCmd(session, channel["channel_id"]),
                        blocking=False)
                    log.info("<- MoveCmd %s queued", name)
                    # 命令是异步的，确认真的进去了再记账——否则收敛循环会以为
                    # 成功而不再重试，人却还留在原地
                    if not self._wait_until_in(channel["channel_id"]):
                        log.warning("sent the request to join %s but it did not "
                                    "take effect within %.0f s, will retry",
                                    name, CHANNEL_TIMEOUT)
                        self._diagnose(name, channel["channel_id"])
                        return

                self.frequency = frequency
                self.channel = name
                self._stuck_reason = ""
                log.info("switched to %s (%.3f MHz)", name, frequency)
                if self.on_channel:
                    self.on_channel(frequency, name)
            except Exception as e:
                log.warning("switching to %s failed: %s", name, e)

    # ---------- 收发 ----------
    def set_transmitting(self, value):
        """PTT。界面和快捷键都走这里。"""
        value = bool(value)
        if value == self.transmitting:
            return
        self.transmitting = value
        if value:
            self._sent_frames = 0
            self._skip_reason = ""
            log.info("PTT down")
        else:
            # 松开时把这一次到底发出去多少帧说清楚。"语音用不了"最常见的两种
            # 情况——根本没进发送分支、和发了但对方听不到——只有这个数能分开。
            if self._sent_frames:
                log.info("PTT up, sent %d frames (about %.1f s)",
                         self._sent_frames, self._sent_frames * 0.02)
                # 发话是最容易把上行问题暴露出来的时刻。堵过才打，没堵就不出声。
                congestion = mumblecompat.send_stats()
                if congestion:
                    log.warning("while transmitting: %s", congestion)
            else:
                log.warning("PTT up but not a single frame was sent: %s",
                            self._skip_reason or "reason unknown")
        if self.on_ptt:
            self.on_ptt(value)

    def _skip(self, reason):
        """记下这一轮为什么没发。同一个原因只记一次，别刷屏。"""
        if reason != self._skip_reason:
            self._skip_reason = reason
            log.debug("not transmitting for now: %s", reason)

    def _on_permission_denied(self, event):
        """服务器拒绝了某个动作，把原因说出来。

        不接这条回报的代价，实测日志长这样：

            频道 FREQ_127100 不存在，建一个临时的
            建立频道 FREQ_127100 后 5 秒内没有出现，1 秒后重试
            （无限重复，没有任何错误）

        建频道要根频道的 MakeTempChannel（0x400）、进频道要 Enter（0x4），
        服务器缺哪一条都只是**默默不照做**——命令发出去了、没有报错、频道就是
        不出现。看上去像网络慢或者服务器卡，实际上再等一万年也不会成功，而
        真正的原因服务器早就用 PermissionDenied 告诉我们了。管制端和 ATIS
        一直接着这条，四个飞行员端漏了。
        """
        try:
            kind = self.mumble.denial_type(event.type)
        except Exception:
            kind = str(getattr(event, "type", "?"))
        # 拒绝类型直接当 i18n 的键用。这里**不能**建一张 kind → 文案的字典：
        # 字典在模块导入时就把文案定死了，之后切语言不会跟着变。
        known = ("Permission", "ChannelName", "NestingLimit",
                 "ChannelCountLimit", "UserListenerLimit")
        reason = (t("denied." + kind) if kind in known
                  else t("denied.other", kind=kind))
        if getattr(event, "reason", ""):
            reason = t("denied.with_note", reason=reason, note=event.reason)
        # 走已有的"卡住原因"这条路，界面和日志都已经在看它了
        self._denial = reason
        self._note_stuck(reason)
        self._status("denied", reason)

    def _mumble_loop(self):
        """pymumble 的主循环，跑在我们自己的线程里。

        照搬老飞行员端 client/gui.py 的 run_client()。这条线程活多久，连接就
        活多久：`Mumble.run()` 里是 `while True` 带自动重连，只有 `stop()`
        （它会把 reconnect 置空）才会让它返回。所以这条线程退出 == 会话结束，
        没有第二种解释，也就不会再出现"连接其实早死了、界面还绿着"。
        """
        gave_up = False
        try:
            self.mumble.run()
        except pymumble.errors.ConnectionRejectedError as e:
            # 走 start() 的话这个异常死在 pymumble 自己的线程里，外面只能靠状态
            # 码倒推。自己跑就能把服务器的原话带给用户。
            gave_up = getattr(self.mumble, "gave_up", False)
            if not gave_up:
                self._reject_reason = str(e) or t("voice.rejected_plain")
                log.warning("the server rejected the connection: %s", e)
        except Exception as e:
            log.warning("the Mumble main loop exited with an exception: %s", e)
        finally:
            self._connection_established.clear()
            log.info("the Mumble main loop has ended")

        # 重连次数用尽是唯一需要主动收摊的出口：其余情况要么是用户自己断开
        # （running 已经是假），要么是首次连接被拒（start() 里就报掉了）。
        if gave_up and self.running:
            self._give_up()

    def _give_up(self):
        """掉线后重连次数用尽：整个下线。

        必须真的收掉，不能只报个错。留着不管的话 PyAudio 还占着麦克风、
        pymumble 还挂在那里，而界面已经把这条连接当没有了——下次点连接会在
        "打不开音频设备"上失败，把人往声卡上引。

        这条跑在 pymumble 主循环那条线程上，所以 stop() 里两处 join 都有
        `is not threading.current_thread()` 的保护，不会自己等自己。
        """
        self.gave_up = True
        self.stop(state='offline',
                  message=t("voice.give_up", limit=self.reconnect_limit))

    def _health(self):
        """一行说清这条连接此刻的状况。

        掉线那一刻最想知道的三件事：**ping 往返**（链路本来就慢，还是突然断
        的）、**上行有没有堵过**（区分"真的传不出去"和"一次瞬时抖动"）、
        **这段时间收发了多少**（连接是活着没人说话，还是根本没通）。
        原来掉线只有一句"connection dropped"，这三样一个都没有，只能靠猜。
        """
        parts = []
        try:
            stats = self.mumble.ping_stats if self.mumble else {}
            average = stats.get("avg")
            if average:
                parts.append(f"ping {average:.0f} ms")
                worst = stats.get("max")
                if worst:
                    parts[-1] += f" (worst {worst:.0f} ms)"
        except Exception:
            pass

        congestion = mumblecompat.send_stats()
        if congestion:
            parts.append(congestion)

        parts.append(f"sent {self._sent_total} / received {self._received_total} "
                     f"frames since the last report")
        self._sent_total = self._received_total = 0
        return "; ".join(parts)

    def _on_reconnect_attempt(self, attempt, limit):
        """每发起一次重连报一次，界面上能看到"重连中 1/3"。"""
        self._status('reconnecting',
                     t("voice.reconnecting", attempt=attempt, limit=limit))

    def _on_connected(self):
        """pymumble 收到 ServerSync，连接真的建立了。"""
        self._connection_established.set()
        # 计数只在这里清零：`connect()` 返回成功只代表 TLS 建好、Authenticate
        # 发出去了，密码错的连接同样是那个返回值。详见 BoundedReconnectMumble。
        if self.mumble:
            self.mumble._session_established()
        if self.gave_up:
            return
        # 重连回来的那一次也要报，否则界面停在"重连中"，而语音其实已经好了。
        # 频道会由 _channel_loop 自己重新进（它比对的是服务器状态），这里只
        # 负责把状态说清楚。
        if self.running and getattr(self, "_was_dropped", False):
            self._was_dropped = False
            self._status('online', t("voice.reconnected", username=self.username))

    def _on_user_removed(self, user, event=None):
        """有人离开了。只关心一种：**我们自己被踢了**。

        pymumble 把这条回调的两种情形分得很清楚（见 client/API.md）：用户自己走
        的话 event 里只有 `session`；被踢或被封才会多出 `actor`、`reason` 和
        `ban`。所以"是不是被踢"是可以判的，而 DISCONNECTED 回调判不了 —— 它不
        带任何理由，被顶下线和网络抖动在那儿长得一模一样，于是客户端一律重连。

        这就是同一个账号在两台机器上登录时那场循环的成因：A 登录顶掉 B，B 重连
        顶掉 A，A 再重连……每一轮登录都是**成功**的，所以三次上限那套预算永远用
        不完（它数的是失败的重连，而成功的会话会把计数清零）。Murmur 那边看到同
        一个 IP 每隔几秒连一次，最后按 autoban 整个封掉，于是那台机器连不上了。

        被踢之后不连回去是唯一正确的选择：新的会话是用户在别处主动建立的，这一
        端连回去只会把它顶掉，然后对方再顶回来。
        """
        if not self.mumble:
            return

        # 只认自己。别人被踢与我们无关。
        try:
            myself = self.mumble.users.myself_session
        except Exception:
            myself = None
        session = None
        try:
            session = user["session"]
        except Exception:
            session = getattr(user, "session", None)
        if myself is None or session is None or session != myself:
            return

        # event 里带 actor / reason / ban 才是被踢；只有 session 是自己走的。
        reason = ""
        kicked = False
        if event is not None:
            for field in ("actor", "reason", "ban"):
                if _event_has(event, field):
                    kicked = True
            reason = _event_get(event, "reason") or ""
        if not kicked:
            return

        self.mumble.mark_kicked(reason)
        log.warning("kicked by the server: %s", reason or "no reason given")
        # 这条状态是给人看的，所以走 i18n；理由是服务端给的中文原文。
        self._status('offline',
                     t("voice.kicked", reason=reason or t("voice.kicked_plain")))

    def _on_disconnected(self):
        """一条已经建立的连接断掉了。

        **它不只在"彻底放弃"时发。** pymumble 的 `run()` 在每次丢连接时都发一次
        （mumble.py:139/142 两个分支都发），然后才决定要不要重连——原来这里一律
        报成"不再自动重连，请重新连接"，于是一次普通抖动就被说成终态错误，
        而 pymumble 正在后台好好地连回来。真正的终态是重连次数用尽，那条走
        `_give_up()`。
        """
        self._connection_established.clear()
        if not self.running:
            return                      # 我们自己 stop() 的，正常收尾
        self._was_dropped = True
        if getattr(self.mumble, "gave_up", False):
            return                      # _mumble_loop 那边会走 _give_up()
        log.warning("voice connection dropped, waiting for pymumble to reconnect "
                    "(up to %d attempts); %s", self.reconnect_limit, self._health())
        self._status('reconnecting',
                     t("voice.dropped_retrying", limit=self.reconnect_limit))

    def _on_sound(self, user, chunk):
        """pymumble 的库线程调用。"""
        try:
            myself = self.mumble.users.myself if self.mumble else None
            if myself and user["name"] == myself["name"]:
                return
            self._last_rx = time.time()
            if not self.receiving:
                self.receiving = True
                self._received_frames = 0
                self._talker = user.get("name", "?")
                log.debug("receiving audio from %s", self._talker)
                if self.on_rx:
                    self.on_rx(True)

            volume = getattr(self.settings, "speaker_volume", 100) / 100.0
            samples = np.frombuffer(chunk.pcm, dtype=np.int16)
            samples = (samples * volume).astype(np.int16)
            with self._stream_lock:
                if not self._output:
                    # 收到了但扬声器没开——"听不到别人"和"根本没人说话"是两回事
                    log.warning("audio received but the speaker is not open, "
                                "nothing will be heard")
                    return
                self._output.write(samples.tobytes())
            self._received_frames += 1
            self._received_total += 1
        except Exception as e:
            log.warning("failed to play the received audio: %s", e)

    def _run(self):
        """发送线程：按住 PTT 就把麦克风送上去，同时管接收灯的超时。"""
        while self.running:
            try:
                if self.receiving and time.time() - self._last_rx > RX_TIMEOUT:
                    self.receiving = False
                    log.info("heard %s for %.1f s (%d frames)",
                             getattr(self, "_talker", "?"),
                             self._received_frames * 0.02, self._received_frames)
                    if self.on_rx:
                        self.on_rx(False)

                if not self.transmitting:
                    time.sleep(0.02)
                    continue
                if not self.connected:
                    self._skip("语音服务器未连接")
                    time.sleep(0.02)
                    continue
                if not self._input:
                    self._skip("麦克风没有打开")
                    time.sleep(0.02)
                    continue

                myself = self.mumble.users.myself
                if not myself:
                    self._skip("服务器还没回报我们自己的用户信息")
                    time.sleep(0.05)
                    continue
                # 注意是 is None：根频道的 channel_id 就是 0，写成 not 会把
                # "在根频道"当成"没进频道"，PTT 于是一声不吭地什么都不做。
                if myself["channel_id"] is None:
                    self._skip("还没有进入任何频道")
                    time.sleep(0.05)
                    continue
                if myself["channel_id"] == ROOT_CHANNEL:
                    # 频率频道永远是根的子频道，人在根里就一定是切换没成功。
                    # 这里原来还附带 `and self.channel is None`，可掉线重连之后
                    # self.channel 停在旧值，条件不成立——于是话音真的被发进了
                    # 根频道：自己频率上没人听得到，根频道里的人倒是全听见了，
                    # 而帧数一路在涨，看起来一切正常。
                    self._skip("还留在根频道（频率频道没切成功），发出去没人听得到")
                    time.sleep(0.05)
                    continue

                try:
                    with self._stream_lock:
                        if not self._input:
                            raise RuntimeError("stream closed")
                        data = self._input.read(self._chunk,
                                                exception_on_overflow=False)
                except Exception as e:
                    self._skip(f"读麦克风出错: {e}")
                    time.sleep(0.05)
                    continue

                samples = self._process_mic(data)
                with self._lock:
                    self.mumble.sound_output.add_sound(samples.tobytes())
                self._sent_frames += 1
                self._sent_total += 1
            except Exception as e:
                log.debug("the transmit thread raised: %s", e)
                time.sleep(0.1)
