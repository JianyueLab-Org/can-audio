"""把通播播到 Mumble 上。

一个席位一条独立的 Mumble 连接，用户名 {cid}_atis{频率6位}_{callsign}。
server/login.py 按机场和通播类型分配 Murmur 用户 id，同频率的不同席位不冲突。

这条连接不打开任何本地音频设备：通播只发合成出来的语音，不碰麦克风，也不该和
管制端抢输入输出设备。
"""

import collections
import logging
import os
import re
import tempfile
import threading
import time
import wave

import numpy as np
import pymumble_py3 as pymumble
import pyttsx3
from pymumble_py3 import messages, mumble_pb2
from pymumble_py3.constants import (
    PYMUMBLE_CLBK_CONNECTED,
    PYMUMBLE_CLBK_PERMISSIONDENIED,
    PYMUMBLE_CLBK_SOUNDRECEIVED,
    PYMUMBLE_CLBK_USERREMOVED,
    PYMUMBLE_CONN_STATE_FAILED,
    PYMUMBLE_MSG_TYPES_REJECT,
)

import mumblecompat
import atisrouting
from i18n import t

# pymumble 用的 ssl.wrap_socket 在 Python 3.12 里已被删除，导入时先补上，
# 否则连接线程一起来就抛 AttributeError
mumblecompat.install()

log = logging.getLogger("atis")


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

# Mumble 服务器拒绝时给的类型，逐条翻译成人能看懂的原因。
# 全都笼统说成"用户名或密码"会把人引到错误的方向——比如认证器挂了的时候，
# 用户会一直去改密码。
# 服务器 Reject 的类型。文案在 i18n 里，这里只留一张"认得的类型"清单——
# 写成 kind → 文案的字典的话，文案会在模块导入时定死，用户之后切语言不会跟着变。
REJECT_TYPES = ("WrongUserPW", "WrongServerPW", "InvalidUsername", "UsernameInUse",
                "ServerFull", "NoCertificate", "AuthenticatorFail", "WrongVersion")

class RejectAwareMumble(pymumble.Mumble):
    """截下服务器的 Reject 消息，把拒绝类型留下来。

    pymumble 处理 Reject 时只把 reason 字段带进异常（mumble.py 的
    dispatch_control_message），而 Murmur 经常只填 type 不填 reason——于是外面
    拿到一个空字符串，只能说"没有给出原因"。这里在分发之前先把整条消息读出来，
    type 才是真正有用的那个字段。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reject_type = None
        self.reject_reason = None

    def dispatch_control_message(self, type, message):
        if type == PYMUMBLE_MSG_TYPES_REJECT:
            try:
                reject = mumble_pb2.Reject()
                reject.ParseFromString(message)
                self.reject_type = mumble_pb2.Reject.RejectType.Name(reject.type)
                self.reject_reason = reject.reason or ""
                log.warning("the server rejected the connection: type=%s reason=%r",
                            self.reject_type, self.reject_reason)
            except Exception as e:
                log.warning("could not parse the Reject message: %s", e)
        return super().dispatch_control_message(type, message)

    def run(self):
        """接住"服务器拒绝"，别让它变成一条 CRITICAL。

        pymumble 的 run() 只接 socket.error，ConnectionRejectedError 会一路穿出
        连接线程，被 applog 的 threading.excepthook 记成"未捕获异常"加一段
        traceback——一次普通的密码错误看上去就像程序崩了，排查时很容易被带偏。

        在这里接是安全的：pymumble 抛之前已经把 connected 置成 FAILED、也放掉了
        ready_lock（mumble.py 的 dispatch_control_message），状态该收拾的都收拾
        完了，异常剩下的唯一作用就是终止线程——而 run() 返回同样终止线程。
        拒绝的原因我们在 dispatch_control_message 里已经记下来了，rejection()
        照常能拿到。
        """
        try:
            super().run()
        except pymumble.errors.ConnectionRejectedError as e:
            log.info("the server rejected the connection, the connection thread "
                     "exited normally: %s", e)

    def rejection(self):
        """翻译成人能看懂的原因，没有被拒绝则返回 None。"""
        if not self.reject_type and not self.reject_reason:
            return None
        reason = (t("reject." + self.reject_type)
                  if self.reject_type in REJECT_TYPES else None)
        if reason and self.reject_reason:
            return t("reject.with_note", reason=reason, note=self.reject_reason)
        if reason:
            return reason
        if self.reject_reason:
            return self.reject_reason
        return self.reject_type


# 连上过之后掉线，最多再试这么多次；都失败就这个席位整个停播，不留一条后台无限
# 重试的僵尸连接。四个客户端同一条策略（xpc/msfs 的 voice.py、controller 的
# voice.py 也是这个数）。
RECONNECT_LIMIT = 3


class BoundedReconnect:
    """pymumble 的重连是无限的，这个混入给它一个上限。

    默认的 `reconnect=True` 会让 `run()` 一直重试到进程结束。对通播尤其糟：
    服务端 `login.py` 对认证失败**按 CAN ID 限流**，而所有通播都用同一个保留
    账号——一个席位的僵尸连接能把整台机器上其它席位的语音一起锁死。

    **必须挂在 connect() 上，不能去数 DISCONNECTED 回调。** pymumble 的
    `run()`（mumble.py:120-143）丢连接时发一次 DISCONNECTED，然后 sleep 再
    connect()，而**失败的重连尝试是完全静默的**：连接失败那一支只
    `sleep + continue`，一个回调都不发。所以数回调数到的是"掉了几次"而不是
    "试了几次"——服务器一直起不来时回调只有一次，后面它安静地重试到天亮。

    **"连上了"只能以 ServerSync 为准。** `connect()` 成功时返回的是
    `AUTHENTICATING` 而不是 `CONNECTED`：它只负责建 TLS 并把 Authenticate 发出
    去，认证结果要等服务器回话。密码错的连接同样返回那个值，然后在 `loop()` 里
    因为 Reject 结束——把返回值当成功就等于每次都把计数清零，又变回无限重试。
    所以计数只在 `_session_established()`（由 CONNECTED 回调调用）里清零。

    `on_give_up` 是给"自己不拥有那条主循环"的调用方用的：这里是
    `mumble.start()`，pymumble 的线程放弃之后自己就没了，外面收不到任何信号，
    所以放弃的那一刻要主动喊一声。xpc/msfs 自己跑 `run()`，从它的返回就能知道。

    这一份和 xpc/msfs 的 voice.py、controller 的 voice.py 是同一段逻辑的副本——
    这个仓库靠复制共享代码，改一处要把四处一起改。
    """

    reconnect_limit = RECONNECT_LIMIT
    reconnect_attempts = 0
    gave_up = False
    on_reconnect = None            # 回调 (第几次, 上限)，用来更新界面
    on_give_up = None              # 放弃时喊一声
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
        是成功的，`_session_established()` 已经把计数清零了。同一个账号在两处登
        录时，两端就这样互相顶掉、各自重连、各自又把对方顶掉，每一轮都是成功
        的，所以三次的预算永远用不完 —— 这是构造上的死循环，不是上限没生效。
        Murmur 那边看到同一个 IP 一直在连，最后按 autoban 把它整个封掉。

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

        # 判定放在发起连接之前：两种失败（连不上服务器、服务器拒绝认证）都被
        # 同一处挡住，也不会多试出第四次。
        if self._established and self.reconnect_attempts >= self.reconnect_limit:
            self.gave_up = True
            self.reconnect = False
            self.connected = PYMUMBLE_CONN_STATE_FAILED
            log.warning("%d reconnect attempts after the drop all failed, "
                        "giving up", self.reconnect_attempts)
            if self.on_give_up:
                try:
                    self.on_give_up()
                except Exception as e:
                    log.warning("give-up callback raised: %s", e)
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
    """把 BoundedReconnect 套在 RejectAwareMumble 上，返回那个类。

    现取现套而不是在顶层写死一个子类：测试里是替换类来放替身的，import 时钉死
    基类会让替身进不来。`base` 是给测试直接指定假基类用的。
    """
    return type("BoundedRejectAwareMumble",
                (BoundedReconnect, base or RejectAwareMumble), {})


TARGET_RATE = 48000                        # pymumble 要求 48kHz 单声道 16bit
FRAME_BYTES = int(TARGET_RATE * 0.02) * 2    # 一帧 20ms
PREBUFFER_SECONDS = 0.5                      # 发送时保持的缓冲长度
SILENCE_HOLD = 0.8                           # 频道里静默多久才算空闲
QUIET_WAIT_TIMEOUT = 60.0
CONNECT_TIMEOUT = 15.0
# 建完临时频道到它出现在频道表里，要等服务器回一条 ChannelState。这是一次网络
# 往返，固定 sleep 赌不起——远程服务器上 0.2 秒经常不够，表现出来就是
# "Channel FREQ_xxxxxx does not exists"。
CHANNEL_TIMEOUT = 5.0
REPEAT_GAP = 5.0                             # 两轮播报之间的间隔

# pyttsx3 在同一进程里共享同一个 SAPI 引擎，多个席位同时合成会互相打断
_TTS_LOCK = threading.Lock()

_CHINESE = re.compile(r'[一-鿿]')


def resample(audio, source_rate, target_rate):
    """线性插值重采样到 48kHz。

    SAPI 出来的语音一般是 22050Hz，往上采样不会产生混叠，线性插值对通播语音
    足够。用它换掉 scipy.signal.resample，打包体积能少一半（scipy 光自己就
    七八十兆），也省掉几条容易漏的隐式导入。
    """
    if source_rate == target_rate or len(audio) == 0:
        return audio
    target_length = int(round(len(audio) * target_rate / float(source_rate)))
    if target_length <= 0:
        return audio[:0]
    return np.interp(
        np.linspace(0, len(audio) - 1, target_length),
        np.arange(len(audio)),
        audio,
    )


# 语速。中文比英文慢一档：同样的 rate 下 SAPI 的中文音色听起来赶，而通播是
# 要人一遍听懂的，宁可慢。
RATE_ENGLISH = 150
RATE_CHINESE = 128

# 中英切换处插一段静音。没有它两种语言会连在一起，听感上像是同一句话说串了。
LANGUAGE_GAP = 5

# 夹在中文里的短英文片段（"RNAV"、"S 模式"里的 S）不单独切出去。切出去的话
# 一句话中间要换两次音色，比用中文音色念这几个字母难听得多。
MIN_ENGLISH_RUN = 4


def _segments(text):
    """按语言把稿子切成 [(是否中文, 文本)]。

    中英双语稿是"整段英文 + 整段中文"，所以粗粒度切就够：按空白分词，带汉字
    的算中文，然后把连续同类的合并回去。最后把夹在中文当中的短英文片段并进
    中文——`本场 RNAV 离场可用` 不该在 RNAV 处换两次音色。
    """
    tokens = (text or "").split()
    if not tokens:
        return []

    runs = []
    for token in tokens:
        chinese = bool(_CHINESE.search(token))
        if runs and runs[-1][0] == chinese:
            runs[-1][1].append(token)
        else:
            runs.append([chinese, [token]])

    merged = []
    for index, (chinese, words) in enumerate(runs):
        short_english = (not chinese and len(words) < MIN_ENGLISH_RUN
                         and merged and merged[-1][0])
        if short_english:
            merged[-1][1].extend(words)
            continue
        if merged and merged[-1][0] == chinese:
            merged[-1][1].extend(words)
            continue
        merged.append([chinese, list(words)])
    return [(chinese, " ".join(words)) for chinese, words in merged]


def _pick_voice(engine, chinese):
    hints = (("chinese", "zh_", "zh-", "huihui", "yaoyao", "kangkang") if chinese
             else ("english", "en_", "en-", "zira", "david", "mark", "hazel"))
    try:
        voices = engine.getProperty('voices')
    except Exception:
        return None
    for voice in voices or []:
        blob = " ".join(str(getattr(voice, attr, ""))
                        for attr in ("id", "name", "languages")).lower()
        if any(hint in blob for hint in hints):
            return voice.id
    return None


# 缓存留几条。播报循环每 REPEAT_GAP 秒就要同一段 PCM，所以缓存是必要的；但
# 键是整篇稿子，METAR 一变、模板一改就是新的一条，而一条 45 秒的通播在 48kHz
# 单声道 16bit 下是 4MB 出头。不封顶的话，值一天班就能攒出几百兆——而真正会被
# 复用的从来只有"当前这一稿"。
CACHE_ENTRIES = 4


class Synthesizer:
    """文本 → 48kHz 单声道 PCM，按文本缓存（只留最近几条）。"""

    def __init__(self):
        self._engine = None
        self._temp_dir = tempfile.mkdtemp(prefix="atis_")
        self._cache = collections.OrderedDict()

    def _ready(self):
        if self._engine is None:
            try:
                # SAPI5 走 COM，工作线程要自己初始化
                import pythoncom
                pythoncom.CoInitialize()
            except Exception:
                pass
            self._engine = pyttsx3.init()
            self._engine.setProperty('rate', RATE_ENGLISH)
            self._engine.setProperty('volume', 0.9)
        return self._engine

    def synthesize(self, text):
        if not text or not text.strip():
            return None
        if text in self._cache:
            self._cache.move_to_end(text)
            return self._cache[text]
        with _TTS_LOCK:
            pcm = self._render(text)
        if pcm:
            self._cache[text] = pcm
            while len(self._cache) > CACHE_ENTRIES:
                self._cache.popitem(last=False)
        return pcm

    def _render(self, text):
        """整篇合成。中英双语时按语言分段，各用各的音色和语速。

        原来是整篇挑一个音色，判据是"文中有没有汉字"——于是双语稿里那一大段
        英文也是中文音色念的，听着像外国人念中文。
        """
        segments = _segments(text)
        if not segments:
            return None

        try:
            engine = self._ready()

            # cleanup() 会把这个目录整个删掉，而 Broadcaster 停了之后再 start()
            # 是允许的——目录没了的话 save_to_file 会静默失败，只留一句
            # "语音合成失败"，看不出是这个原因
            os.makedirs(self._temp_dir, exist_ok=True)

            # **整篇只能进一次 runAndWait()。** 同一个引擎连着调第二次时
            # pyttsx3 的 SAPI 驱动会永久卡在里面——实测双语稿（两段）就此挂死，
            # 日志停在合成之前，"合成完成"和"语音合成失败"两条都不会出现，
            # 表现出来就是通播一声不响。
            #
            # 不过 setProperty 和 save_to_file 都是排进同一个队列、按 FIFO 执行
            # 的（pyttsx3 driver.py 的 _Proxy._push），所以一次循环里就能把每段
            # 用各自的音色和语速写成各自的 wav。
            paths = []
            for index, (chinese, part) in enumerate(segments):
                path = os.path.join(self._temp_dir, f"atis{index}.wav")
                if os.path.exists(path):
                    try:
                        os.remove(path)
                    except Exception:
                        pass
                engine.setProperty('rate',
                                   RATE_CHINESE if chinese else RATE_ENGLISH)
                voice_id = _pick_voice(engine, chinese)
                if voice_id:
                    engine.setProperty('voice', voice_id)
                engine.save_to_file(part, path)
                paths.append(path)

            engine.runAndWait()

            gap = np.zeros(int(TARGET_RATE * LANGUAGE_GAP), dtype=np.int16)
            pieces = []
            for index, path in enumerate(paths):
                if index:
                    pieces.append(gap)      # 换语言之前留一口气
                pieces.append(self._read_wav(path))

            # 结尾补 0.2 秒静音，免得最后一个音节被截掉
            pieces.append(np.zeros(int(TARGET_RATE * 0.2), dtype=np.int16))
            pcm = np.concatenate(pieces).tobytes()
            log.info("synthesised %.1f s of audio (%d segments: %s)",
                     len(pcm) / (2.0 * TARGET_RATE), len(segments),
                     ", ".join("zh" if c else "en" for c, _ in segments))
            return pcm
        except Exception as e:
            log.warning(f"speech synthesis failed: {e}")
            return None

    def _read_wav(self, path):
        """读回一段合成结果，重采样到 48kHz 单声道。出错就抛，由 _render 记账。"""
        # save_to_file 是异步落盘的，runAndWait 返回后文件可能还没写完
        for _ in range(25):
            if os.path.exists(path) and os.path.getsize(path) > 0:
                break
            time.sleep(0.2)
        else:
            raise FileNotFoundError(f"语音文件未生成: {path}")

        with wave.open(path, 'rb') as wav:
            channels, width = wav.getnchannels(), wav.getsampwidth()
            rate = wav.getframerate()
            frames = wav.readframes(wav.getnframes())

        if width != 2:
            raise ValueError(t("voice.bad_width", bits=width * 8))
        if not frames:
            raise ValueError(t("voice.empty_audio"))

        audio = np.frombuffer(frames, dtype=np.int16)
        if channels > 1:
            audio = audio.reshape(-1, channels).mean(axis=1)
        audio = resample(audio, rate, TARGET_RATE)
        audio = np.clip(audio, np.iinfo(np.int16).min,
                        np.iinfo(np.int16).max).astype(np.int16)

        try:
            os.remove(path)
        except Exception:
            pass
        return audio

    def cleanup(self):
        self._engine = None
        try:
            for name in os.listdir(self._temp_dir):
                try:
                    os.remove(os.path.join(self._temp_dir, name))
                except Exception:
                    pass
            os.rmdir(self._temp_dir)
        except Exception:
            pass


class Broadcaster:
    """一个席位的语音播出。on_state(state, message) 在后台线程调用。"""

    def __init__(self, server, cid, password, station, on_state=None,
                 reconnect_limit=RECONNECT_LIMIT,
                 datafeed_url=atisrouting.DEFAULT_URL,
                 atis_range_nm=atisrouting.DEFAULT_RANGE_NM):
        self.server = server
        self.cid = str(cid).strip()
        self.password = password
        self.station = station
        self.on_state = on_state
        # 掉线后最多重连几次，用尽就这个席位停播，见 BoundedReconnect
        self.reconnect_limit = reconnect_limit
        # 重连次数用尽、已经停播。和"停了"是两回事：界面要知道是掉线掉没的，
        # 而不是有人按了停止播出。
        self.gave_up = False

        self.user = f"{self.cid}_atis{station.frequency_khz:06d}_{station.callsign}"
        self.running = False
        self.stop_event = threading.Event()
        self.mumble = None
        self._router = atisrouting.ATISRouter(station.callsign, station.frequency_khz,
                                             datafeed_url, atis_range_nm)
        self.thread = None

        self._synth = Synthesizer()
        self._connected = False
        self._denial = None          # 服务器拒绝某个动作时的说明
        self._last_other_sound = 0.0
        self._text_lock = threading.Lock()
        self._voice_text = ""
        self._pending_text = None

    # ---------- 状态 ----------
    def _state(self, state, message):
        log.info("%s %s: %s", self.user, state, message)
        if self.on_state:
            try:
                self.on_state(state, message)
            except Exception as e:
                log.warning(f"status callback raised: {e}")

    # ---------- 生命周期 ----------
    def start(self, voice_text):
        if self.running:
            return
        with self._text_lock:
            self._voice_text = voice_text
        self.running = True
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def update_text(self, voice_text):
        """换稿。下一轮播报时生效，不打断正在播的这一遍。"""
        with self._text_lock:
            self._pending_text = voice_text

    def stop(self):
        self.running = False
        self.stop_event.set()
        thread = self.thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)
        self.thread = None
        self._disconnect()
        self._synth.cleanup()

    def _run(self):
        try:
            if not self._connect():
                return
            self._loop()
        except Exception as e:
            self._state('error', t("voice.round_failed", error=e))
        finally:
            self._disconnect()

    # ---------- 连接 ----------
    def _on_connected(self):
        self._connected = True
        # 计数只在这里清零：connect() 返回成功只代表 TLS 建好、Authenticate 发出
        # 去了，密码错的连接同样是那个返回值。详见 BoundedReconnect。
        if self.mumble is not None and hasattr(self.mumble, "_session_established"):
            self.mumble._session_established()

    def _on_reconnect_attempt(self, attempt, limit):
        """每发起一次重连报一次，界面上能看到"重连中 1/3"。"""
        self._state('reconnecting',
                    t("voice.reconnecting", attempt=attempt, limit=limit))

    def _on_give_up(self):
        """重连次数用尽：这个席位停播。

        **只停这一个席位**，同一个客户端上的其它席位各有自己的连接，不受影响。

        走回调而不是等某个循环发现：这里的连接是 `mumble.start()` 起的，
        pymumble 那条线程放弃之后自己就没了，外面收不到任何信号——播报循环最多
        要等一整轮（`_wait_for_quiet` 能等到 60 秒）才可能注意到。`stop_event`
        一置，那些 `wait` 全部立刻返回。
        """
        self.gave_up = True
        self._state('offline',
                    t("voice.give_up", limit=self.reconnect_limit,
                      callsign=self.station.callsign))
        self.stop_event.set()

    def _on_user_removed(self, user, event=None):
        """有人离开了。只关心一种：**我们自己被踢了**。

        pymumble 把这条回调的两种情形分得很清楚（见 client/API.md）：用户自己走
        的话 event 里只有 `session`；被踢或被封才会多出 `actor`、`reason` 和
        `ban`。所以"是不是被踢"是可以判的，而 DISCONNECTED 回调判不了 —— 它不
        带任何理由，被顶下线和网络抖动在那儿长得一模一样，于是客户端一律重连。

        这就是同一个账号在两处登录时那场循环的成因：A 登录顶掉 B，B 重连顶掉
        A……每一轮登录都是**成功**的，所以三次上限那套预算永远用不完（它数的是
        失败的重连，成功的会话会把计数清零）。Murmur 那边看到同一个 IP 每隔几秒
        连一次，最后按 autoban 整个封掉。

        被踢之后不连回去是唯一正确的选择：新的会话是用户在别处主动建立的，这一
        端连回去只会把它顶掉，然后对方再顶回来。
        """
        if not self.mumble:
            return

        try:
            myself = self.mumble.users.myself_session
        except Exception:
            myself = None
        try:
            session = user["session"]
        except Exception:
            session = getattr(user, "session", None)
        if myself is None or session is None or session != myself:
            return

        kicked = False
        reason = ""
        if event is not None:
            for field in ("actor", "reason", "ban"):
                if _event_has(event, field):
                    kicked = True
            reason = _event_get(event, "reason") or ""
        if not kicked:
            return

        self.mumble.mark_kicked(reason)
        log.warning("kicked by the server: %s", reason or "no reason given")
        self._state('offline',
                    t("voice.kicked", callsign=self.station.callsign,
                      reason=reason or t("voice.kicked_plain")))
        # 这个席位停播；别的席位不受影响，它们各有自己的连接。
        self.stop_event.set()

    def _on_sound(self, user, soundchunk):
        # 开了接收就必须及时取走，否则 pymumble 会一直堆内存
        try:
            user.sound.get_sound()
        except Exception:
            pass
        try:
            name = user["name"]
        except Exception:
            name = ""
        if name and name != self.user and self._router.hears(user):
            self._last_other_sound = time.time()

    def _connect(self):
        self._state('connecting', t("voice.connecting", user=self.user, server=self.server))
        try:
            # reconnect=True 仍然要，掉线自己连回来是对的；上限由
            # BoundedReconnect 管，用尽了这个席位就停播。
            self.mumble = bounded_mumble()(self.server, self.user,
                                           password=self.password,
                                           reconnect=True)
            self.mumble.reconnect_limit = self.reconnect_limit
            self.mumble.on_reconnect = self._on_reconnect_attempt
            self.mumble.on_give_up = self._on_give_up
            self.mumble.set_receive_sound(True)
            self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_CONNECTED, self._on_connected)
            # 被踢和普通掉线要分开 —— 见 _on_user_removed。
            self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_USERREMOVED,
                                               self._on_user_removed)
            self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_SOUNDRECEIVED, self._on_sound)
            self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_PERMISSIONDENIED,
                                               self._on_permission_denied)
            self._router.attach(self.mumble)
            self.mumble.start()
        except Exception as e:
            self._state('error', t("voice.connect_failed", error=e))
            return False

        deadline = time.time() + CONNECT_TIMEOUT
        while not self._connected and time.time() < deadline:
            if not self.running or self.stop_event.is_set():
                return False
            if not self.mumble.is_alive():
                # 线程没了：要么服务器发了 Reject，要么客户端自己出错了。
                # 后者以前会被一律说成"服务器拒绝"，把人往密码上引。
                reason = self.mumble.rejection()
                self._state('error',
                            t("voice.rejected", user=self.user, reason=reason) if reason
                            else t("voice.rejected_plain", server=self.server))
                return False
            time.sleep(0.1)

        if not self._connected:
            reason = self.mumble.rejection()
            self._state('error',
                        t("voice.timeout_reason", server=self.server, reason=reason)
                        if reason else t("voice.timeout", server=self.server))
            return False

        return self._join_channel()

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
        就永远卡住，连 CHANNEL_TIMEOUT 都轮不到，通播线程整个死在这一行：日志
        停在"建一个临时的"，之后既没有成功也没有任何错误。

        自己发命令、不等锁；频道有没有建出来由 _wait_for_channel 轮询判断，
        那本来就是更可靠的判据——服务器拒绝建频道时也不会干等。
        """
        self.mumble.execute_command(messages.CreateChannel(0, name, True),
                                    blocking=False)

    def _move_in(self, channel_id):
        """进频道，同样不能用 move_in()——它也走 blocking=True。"""
        self.mumble.execute_command(
            messages.MoveCmd(self.mumble.users.myself_session, channel_id),
            blocking=False)

    def _wait_until_in(self, channel_id):
        """等服务器确认我们真的进了这个频道。

        move 是异步的，命令发出去不等于进去了。不确认就报"已在播出"，通播会
        对着根频道播，而界面显示一切正常。
        """
        deadline = time.time() + CHANNEL_TIMEOUT
        while time.time() < deadline:
            if not self.running or self.stop_event.is_set():
                return False
            myself = self.mumble.users.myself
            if myself is not None and myself["channel_id"] == channel_id:
                return True
            time.sleep(0.1)
        return False

    def _join_channel(self):
        name = self.station.channel
        try:
            channel = self._find_channel(name)
            if channel is None:
                self._denial = None
                log.info("channel %s does not exist, creating a temporary one", name)
                self._create_channel(name)
                channel = self._wait_for_channel(name)

            if channel is None:
                # 分清"服务器拒绝"和"没等到"。以前两种都报成频道不存在，
                # 而前者再等多久也不会有。
                if self._denial:
                    self._state('error',
                                t("voice.channel_denied", name=name, reason=self._denial))
                else:
                    self._state('error',
                                t("voice.channel_timeout", name=name,
                                  seconds=CHANNEL_TIMEOUT))
                return False

            channel_id = channel["channel_id"]
            myself = self.mumble.users.myself
            if myself is None or myself["channel_id"] != channel_id:
                self._move_in(channel_id)
                if not self._wait_until_in(channel_id):
                    self._state('error',
                                t("voice.move_timeout", name=name,
                                  seconds=CHANNEL_TIMEOUT))
                    return False
            self._state('online', t("voice.online", frequency=self.station.frequency))
            return True
        except Exception as e:
            self._state('error', t("voice.join_failed", error=e))
            return False

    def _in_expected_channel(self):
        """服务器上我们此刻是不是真的还在这个频率的频道里。

        不能拿自己记的状态当准。连接是 reconnect=True 建的，pymumble 掉线后会
        自己连回来，而服务器把重连上来的用户放回**根频道**——本地这边什么都没
        变，`_join_channel` 又只在 `_connect` 里跑过一次。于是通播会一直往根频道
        里播：该听到的人听不到，反而是那些自己切频道失败、卡在根频道的人全都
        听得到。和 CLAUDE.md 里 PTT 发进根频道是同一类问题。
        """
        try:
            if not self.mumble:
                return False
            channel = self._find_channel(self.station.channel)
            myself = self.mumble.users.myself
        except Exception:
            return False
        if channel is None or myself is None:
            return False
        return myself["channel_id"] == channel["channel_id"]

    def _wait_for_channel(self, name):
        """等服务器把新建的频道回报回来。

        建频道是发一条消息就返回，频道要等服务器回 ChannelState 才会进本地表。
        服务器拒绝的话（缺 MakeTempChannel 权限）不会有 ChannelState，只有一条
        PermissionDenied——那种情况下等下去没有意义，收到就立刻退出。
        """
        deadline = time.time() + CHANNEL_TIMEOUT
        while time.time() < deadline:
            if not self.running or self.stop_event.is_set():
                return None
            if self._denial:
                return None
            channel = self._find_channel(name)
            if channel is not None:
                return channel
            time.sleep(0.1)
        return self._find_channel(name)

    def _on_permission_denied(self, event):
        """服务器拒绝了某个动作。

        建频道要根频道的 MakeTempChannel 权限（ACL 里的 0x400），Mumble 默认
        ACL 不一定给。没有这条回报的话，只会看到"频道不存在"，完全猜不到是被
        权限挡了。
        """
        try:
            kind = self.mumble.denial_type(event.type)
        except Exception:
            kind = str(getattr(event, "type", "?"))
        # 不要叫 messages：模块顶上 `from pymumble_py3 import messages` 是发
        # CreateChannel / MoveCmd 用的，在这里被局部变量盖住，以后谁在这个函数
        # 里加一句发命令就会莫名其妙地炸
        known = ("Permission", "ChannelName", "NestingLimit", "ChannelCountLimit")
        self._denial = (t("denied." + kind) if kind in known
                        else t("denied.other", kind=kind))
        if getattr(event, "reason", ""):
            self._denial = t("denied.with_note", reason=self._denial, note=event.reason)
        log.warning("%s: %s", self.station.callsign, self._denial)

    def _disconnect(self):
        router = getattr(self, "_router", None)
        if router is not None:
            router.close()
        if self.mumble:
            try:
                self.mumble.stop()
            except Exception as e:
                log.warning(f"disconnecting raised: {e}")
            self.mumble = None
        self._connected = False

    # ---------- 播报 ----------
    def _channel_is_quiet(self):
        return (time.time() - self._last_other_sound) >= SILENCE_HOLD

    def _wait_for_quiet(self):
        deadline = time.time() + QUIET_WAIT_TIMEOUT
        announced = False
        while self.running and not self.stop_event.is_set():
            if self._channel_is_quiet():
                return True
            if not announced:
                self._state('online', t("voice.waiting"))
                announced = True
            if self.stop_event.wait(0.2) or time.time() > deadline:
                return False
        return False

    def _transmit(self, pcm):
        """按 20ms 帧节流发送；中途有人讲话就让路。"""
        position, total = 0, len(pcm)
        generation = self._router.begin_cycle()
        if generation is None:
            return False
        while position < total and self.running and not self.stop_event.is_set():
            if not self._channel_is_quiet():
                self._state('online', t("voice.interrupted"))
                self._router.discard()
                return False
            try:
                buffered = self.mumble.sound_output.get_buffer_size()
            except Exception:
                buffered = 0
            if buffered > PREBUFFER_SECONDS:
                if self.stop_event.wait(0.05):
                    return False
                continue

            chunk = pcm[position:position + FRAME_BYTES]
            if len(chunk) < FRAME_BYTES:
                chunk += b'\x00' * (FRAME_BYTES - len(chunk))
            try:
                if not self._router.enqueue(chunk, generation):
                    return False
            except Exception as e:
                self._state('error', t("voice.send_failed", error=e))
                return False
            position += FRAME_BYTES

        # 等缓冲排空，免得下一轮的静默判断被自己的尾音干扰
        deadline = time.time() + 5
        while self.running and not self.stop_event.is_set() and time.time() < deadline:
            if self._router.generation != generation:
                return False
            try:
                if self.mumble.sound_output.get_buffer_size() <= 0:
                    break
            except Exception:
                break
            if self.stop_event.wait(0.05):
                break
        return position >= total and self._router.generation == generation

    def _loop(self):
        while self.running and not self.stop_event.is_set():
            with self._text_lock:
                if self._pending_text is not None:
                    self._voice_text = self._pending_text
                    self._pending_text = None
                    self._state('online', t("voice.script_swapped"))
                text = self._voice_text

            pcm = self._synth.synthesize(text)
            if pcm is None:
                self._state('error', t("voice.tts_failed"))
                return

            # 每一轮开播前都对着服务器确认一次，别信自己记的
            if not self._in_expected_channel():
                log.info("no longer in %s (most likely a reconnect), rejoining the "
                         "channel", self.station.channel)
                if not self._join_channel():
                    if self.stop_event.wait(REPEAT_GAP):
                        break
                    continue

            if not self._wait_for_quiet():
                break
            self._state('online', t("voice.speaking", letter=self.station.letter))
            spoken = self._transmit(pcm)

            if not self.running or self.stop_event.is_set():
                break
            if self.stop_event.wait(REPEAT_GAP if spoken else 1.0):
                break

        # 重连用尽那条已经报过 offline 了，再报一句"通播已停止"会把原因盖掉，
        # 界面上就只剩一个没有解释的停止。
        if not self.gave_up:
            self._state('stopped', t("voice.stopped"))
