"""语音连接：一条 Mumble 连接同时守听/发话多个频率。

TrackAudio 靠 AFV 天然支持多频率，我们的后端是 Mumble——一个用户只能待在一个
频道里，所以用两个机制拼出同样的效果：

    收：频道监听（Mumble 1.4 起的 listening_channel），一次可以监听多个频道
    发：VoiceTarget（whisper），一条目标里可以带多个频道

两个都要绕开 pymumble 的封装直接发 protobuf：pymumble 没有监听频道的接口，
whisper 那边 `id==1` 时只取第一个频道（mumble.py 里写死的），也带不了多个。

服务端如果是 Mumble 1.3，监听频道会被忽略——那种情况下只有主频率（真正进入的
那个频道）能听到声音，靠 listeners_confirmed 暴露给界面提示用户。
"""

import logging
import threading
import time

import numpy as np
import pyaudio
import pymumble_py3 as pymumble
from pymumble_py3 import messages, mumble_pb2
from pymumble_py3.constants import (
    PYMUMBLE_CLBK_CONNECTED,
    PYMUMBLE_CLBK_DISCONNECTED,
    PYMUMBLE_CLBK_PERMISSIONDENIED,
    PYMUMBLE_CLBK_SOUNDRECEIVED,
    PYMUMBLE_CLBK_USERREMOVED,
    PYMUMBLE_CONN_STATE_CONNECTED,
    PYMUMBLE_CONN_STATE_FAILED,
    PYMUMBLE_MSG_TYPES_REJECT,
    PYMUMBLE_MSG_TYPES_USERSTATE,
    PYMUMBLE_MSG_TYPES_VOICETARGET,
)

import mumblecompat
import radiostack
from i18n import t

# pymumble 用的 ssl.wrap_socket 在 Python 3.12 里已被删除，导入时先补上，
# 否则连接线程一起来就抛 AttributeError
mumblecompat.install()

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

# Mumble 服务器拒绝时给的类型，逐条翻译成人能看懂的原因。
# 全都笼统说成"用户名或密码"会把人引到错误的方向——比如认证器挂了的时候，
# 用户会一直去改密码。
# 服务器认得的 Reject 类型。文字在 i18n 里，这里只留类型名——**取的时候才翻**，
# 否则模块导入时语言还没定，永远是默认语言。
REJECT_TYPES = ("WrongUserPW", "WrongServerPW", "InvalidUsername", "UsernameInUse",
                "ServerFull", "NoCertificate", "AuthenticatorFail", "WrongVersion")


def reject_reason(reject_type):
    """把服务器给的拒绝类型翻成人能看懂的话，不认识就返回 None。"""
    return t(f"reject.{reject_type}") if reject_type in REJECT_TYPES else None


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
        reason = reject_reason(self.reject_type)
        if reason and self.reject_reason:
            return t("reject.with_note", reason=reason, note=self.reject_reason)
        if reason:
            return reason
        if self.reject_reason:
            return self.reject_reason
        return self.reject_type


SUPPORTED_SAMPLE_RATES = [48000, 44100, 32000, 24000, 16000]

# VoiceTarget 的编号（协议允许 1-30）。PTT 和交叉耦合各用各的：
# 共用一个编号的话，转发时重编目标会把 PTT 的目标一起改掉，管制员的话音就
# 发到错误的频率上去了。
PTT_TARGET_ID = 1
XC_TARGET_BASE = 10            # 每个 XC 频率一个编号，从这里往后排
RX_TIMEOUT = 0.5               # 多久没收到话音就认为对方松开了
CONNECT_TIMEOUT = 15.0
# 建完临时频道到它出现在频道表里、以及进频道到服务器确认，都要等一次网络往返。
# 固定 sleep 赌不起——远程服务器上 0.2 秒经常不够，表现出来就是频道"建不出来"。
CHANNEL_TIMEOUT = 5.0
PING_TIMEOUT = 5.0             # 超过这么久没有 ping 回复就认为断线
# 连上过之后掉线，最多再试这么多次；都失败就整个下线，不留一条后台无限重试的
# 僵尸连接。四个客户端同一条策略（xpc/msfs 的 voice.RECONNECT_LIMIT、
# atis/broadcast.py 也是这个数）。
RECONNECT_LIMIT = 3

# pymumble 默认 10 秒 ping 一次、断线后 10 秒才重连，掉线要很久才能被发现。
# 调快之后配合下面的 _connection_monitor，断线基本能在几秒内反映到界面上。
pymumble.mumble.PYMUMBLE_PING_DELAY = 1
pymumble.mumble.PYMUMBLE_CONNECTION_RETRY_INTERVAL = 2


class BoundedReconnect:
    """pymumble 的重连是无限的，这个混入给它一个上限。

    默认的 `reconnect=True` 会让 `run()` 一直重试到进程结束。后果不是"多试几
    次"这么轻：服务端 `login.py` 对认证失败**按 CAN ID 限流**，一个不停重连的
    僵尸足以把这个账号的语音锁死，于是用户把密码改对了也连不上，直到重启客户
    端为止。

    **必须挂在 connect() 上，不能去数 DISCONNECTED 回调。** pymumble 的
    `run()`（mumble.py:120-143）丢连接时发一次 DISCONNECTED，然后 sleep 再
    connect()，而**失败的重连尝试是完全静默的**：连接失败那一支只
    `sleep + continue`，一个回调都不发。所以数回调数到的是"掉了几次"而不是
    "试了几次"——服务器一直起不来时回调只有一次，后面它安静地重试到天亮。

    **"连上了"只能以 ServerSync 为准。** `connect()` 成功时返回的是
    `AUTHENTICATING` 而不是 `CONNECTED`：它只负责建 TLS 并把 Authenticate 发出
    去，认证结果要等服务器回话。密码错的连接同样返回那个值，然后在 `loop()` 里
    因为 Reject 结束——把返回值当成功就等于每次都把计数清零，又变回无限重试，
    而这一次撞的正好是上面说的按账号限流。所以计数只在 `_session_established()`
    （由 CONNECTED 回调调用）里清零。

    这一份和 xpc/msfs 的 voice.py、atis 的 broadcast.py 是同一段逻辑的副本——
    这个仓库靠复制共享代码，改一处要把四处一起改。
    """

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


class VoiceClient:
    """管制席位的语音连接。

    回调都在后台线程触发，界面那边要自己转到 Qt 线程：
        on_state(state, message)          connecting / online / error / stopped
        on_rx(frequency_khz, active, callsign)
        on_tx(active)
    """

    def __init__(self, server, cid, password, on_state=None, on_rx=None, on_tx=None,
                 on_connection_change=None, reconnect_limit=RECONNECT_LIMIT):
        self.server = server
        self.cid = str(cid).strip()
        self.password = password
        self.on_state = on_state
        self.on_rx = on_rx
        self.on_tx = on_tx
        self.on_connection_change = on_connection_change
        # 掉线后最多重连几次，用尽就整个下线，见 BoundedReconnect
        self.reconnect_limit = reconnect_limit
        # 重连次数用尽、已经彻底下线。和"没在跑"是两回事：界面要知道是掉线掉
        # 没了，而不是管制员自己点的断开。
        self.gave_up = False

        self.mumble = None
        self.connected = False
        # 收到过监听频道的话音才算证实服务端支持频道监听。安静不能当作不支持——
        # 频率上没人说话是常态，据此报警只会天天误报。
        self.listeners_confirmed = False
        self.running = False

        # 音频
        self.audio = None
        self.input_stream = None
        self.output_stream = None
        self.FORMAT = pyaudio.paInt16
        self.CHANNELS = 1
        self.RATE = 48000
        self.CHUNK = 960
        self._input_device = None
        self._output_device = None
        self._stream_lock = threading.Lock()

        self.mic_volume = 100
        self.speaker_volume = 100
        self.transmitting = False

        # 频率 ↔ 频道
        self._channel_ids = {}           # frequency_khz -> channel_id
        self._channel_to_khz = {}        # channel_id -> frequency_khz
        self._listening = set()
        self._tx_channels = []
        self._xc_channels = []
        self._sent_target = None         # 上次发出去的发话目标，避免重复发
        self._force_target = False       # 重连后强制重发一次发话目标
        self._xc_targets = {}            # 频道 id -> 交叉耦合用的 VoiceTarget 编号
        # sound_output.target 是全局的一个字段，PTT 和交叉耦合都要改，
        # 不串起来的话话音会发到错误的频率上
        self._audio_lock = threading.Lock()
        # 电台栈每变一次界面就新起一个线程调 sync，而 sync 会阻塞好几秒，
        # 两个同时推会把监听列表和发话目标写乱
        self._sync_lock = threading.Lock()
        # 门口最多站一个人等着（见 sync 的说明）。这两个只保护那个标记，
        # 不能用 _sync_lock——那把锁要被持有好几秒。
        self._sync_pending = False
        self._sync_pending_lock = threading.Lock()
        # 服务器最近一次拒绝某个动作的说明。建频道时用来提前退出等待：
        # 被拒之后再干等满 CHANNEL_TIMEOUT 是纯浪费，而这段等待是在
        # _sync_lock 里面的，会把后面排队的 sync 一起拖住。
        self._denial = None
        self._volumes = {}               # frequency_khz -> 0-100
        self._last_rx = {}               # frequency_khz -> 最后收到话音的时间

        self._tx_thread = None
        self._rx_monitor = None
        self._connection_thread = None
        self._last_connected = False
        self._stack = None               # 最近一次 sync 用的电台栈，重连后重推
        self._last_ping_rcv = time.time()

    # ---------- 状态回报 ----------
    def _state(self, state, message):
        log.info(f"{state}: {message}")
        if self.on_state:
            try:
                self.on_state(state, message)
            except Exception as e:
                log.warning(f"status callback raised: {e}")

    # ---------- 被踢 ----------
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
                    t("voice.kicked", reason=reason or t("voice.kicked_plain")))

    # ---------- 连接 ----------
    def connect(self):
        """阻塞式连接。成功返回 True。"""
        self._state('connecting', t("voice.connecting", cid=self.cid, server=self.server))
        try:
            self.audio = pyaudio.PyAudio()
            self.RATE = self._find_best_sample_rate()
            self.CHUNK = int(self.RATE * 0.02)

            # reconnect=True 仍然要，掉线自己连回来是对的；上限由
            # BoundedReconnect 管，用尽了就整个下线。
            self.mumble = bounded_mumble()(self.server, self.cid,
                                           password=self.password,
                                           reconnect=True)
            self.mumble.reconnect_limit = self.reconnect_limit
            self.mumble.on_reconnect = self._on_reconnect_attempt
            self.mumble.set_receive_sound(True)
            self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_CONNECTED, self._on_connected)
            # 被踢和普通掉线要分开，否则谁也说不清该不该连回去 —— 见
            # _on_user_removed。DISCONNECTED 不带理由，只听它的话两者一模一样。
            self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_USERREMOVED,
                                               self._on_user_removed)
            self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_DISCONNECTED, self._on_disconnected)
            self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_SOUNDRECEIVED, self._on_sound)
            self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_PERMISSIONDENIED,
                                               self._on_permission_denied)
            self.mumble.start()
        except Exception as e:
            self._abort_connect()
            self._state('error', t("voice.connect_failed", error=e))
            return False

        deadline = time.time() + CONNECT_TIMEOUT
        while not self.connected and time.time() < deadline:
            if not self.mumble.is_alive():
                # 线程死了说明服务器主动拒绝了，把真实原因说出来
                reason = self.mumble.rejection()
                self._abort_connect()
                self._state('error',
                            t("voice.rejected", cid=self.cid, reason=reason) if reason
                            else t("voice.rejected_plain", server=self.server))
                return False
            time.sleep(0.1)

        if not self.connected:
            reason = self.mumble.rejection()
            # 超时这条最危险：pymumble 线程还活着、reconnect=True、又从没
            # 建立过会话，BoundedReconnect 根本管不到它——不停掉就是一个
            # 顶着账号限流无限重试的僵尸，麦克风也一直被占着。
            self._abort_connect()
            self._state('error',
                        t("voice.timeout_reason", server=self.server, reason=reason)
                        if reason else t("voice.no_response", server=self.server))
            return False

        try:
            self.setup_audio()
        except Exception as e:
            self._abort_connect()
            self._state('error', t("voice.audio_failed", error=e))
            return False

        self.running = True
        # 同步初始状态，避免监控线程第一圈就误报一次状态变化
        self._last_connected = True
        self._last_ping_rcv = time.time()

        self._rx_monitor = threading.Thread(target=self._rx_monitor_loop, daemon=True)
        self._rx_monitor.start()
        self._connection_thread = threading.Thread(target=self._connection_monitor, daemon=True)
        self._connection_thread.start()

        self._state('online', t("voice.online", cid=self.cid))
        if self.on_connection_change:
            self.on_connection_change(True)
        return True

    def _abort_connect(self):
        """connect() 半路失败时把已经拿到手的资源放掉。

        依赖调用方（gui.py 目前失败后会调 disconnect()）不保险，这条不变量
        属于 connect() 自己：PyAudio 不放，下次重连在"打不开音频设备"上失败，
        指向完全错误的方向；reconnect=True 的 Mumble 不停，就是一个顶着
        login.py 按账号限流无限重试的僵尸。
        """
        self._close_streams()
        if self.audio:
            try:
                self.audio.terminate()
            except Exception:
                pass
            self.audio = None
        if self.mumble:
            try:
                self.mumble.stop()
            except Exception:
                pass
            self.mumble = None

    def _on_connected(self):
        self.connected = True
        # 计数只在这里清零：connect() 返回成功只代表 TLS 建好、Authenticate 发
        # 出去了，密码错的连接同样是那个返回值。详见 BoundedReconnect。
        if self.mumble is not None and hasattr(self.mumble, "_session_established"):
            self.mumble._session_established()

    def _on_reconnect_attempt(self, attempt, limit):
        """每发起一次重连报一次，状态栏上能看到"重连中 1/3"。"""
        self._state('reconnecting',
                    t("voice.reconnecting", attempt=attempt, limit=limit))

    def _on_disconnected(self):
        """一条已经建立的连接断掉了。

        **它不只在"彻底放弃"时发。** pymumble 的 run() 每次丢连接都发一次
        （mumble.py:139/142 两个分支都发），然后才决定要不要重连——原来这里一律
        报 error，于是一次普通抖动在管制端就是一句红字"连接已断开"，而 pymumble
        正在后台好好地连回来，红字也没人撤。真正的终态是重连次数用尽，那条走
        `_give_up()`。
        """
        self.connected = False
        if self.running and not getattr(self.mumble, "gave_up", False):
            self._state('reconnecting',
                        t("voice.dropped_retrying", limit=self.reconnect_limit))
        if self.on_connection_change:
            self.on_connection_change(False)

    def _connection_monitor(self):
        """盯着连接是否还活着。

        光看 pymumble 的 connected 标志不够——网线拔掉之后它还会长时间停在
        "已连接"。所以同时看 ping_stats 里最后一次收到 ping 回复的时间，超过
        PING_TIMEOUT 就判定断线。
        """
        while self.running:
            try:
                # 重连次数用尽了：连接线程已经结束，这里是唯一会再跑的地方，
                # 所以收摊只能挂在它上面。**必须先判这一条**——下面那些判据只会
                # 说"断线了"，而断线和"再也不会连回来了"对用户是两回事。
                if getattr(self.mumble, "gave_up", False):
                    self._give_up()
                    return

                try:
                    last_rcv = self.mumble.ping_stats.get('last_rcv', 0)
                    if last_rcv:
                        self._last_ping_rcv = last_rcv / 1000.0
                except Exception:
                    pass

                # mumble.connected 是状态码不是布尔：0 未连接、1 认证中、
                # 2 已连接、3 失败。bool() 会把"正在重连（1）"和"失败（3）"
                # 都当成活的——掉线后 pymumble 一开始重连，这里就误报"已重连"
                # 并把整个电台栈推进一条还没认证的半开连接里。
                alive = bool(self.mumble) and (
                    self.mumble.connected == PYMUMBLE_CONN_STATE_CONNECTED)
                if alive and time.time() - self._last_ping_rcv > PING_TIMEOUT:
                    alive = False
                    log.info(f"no ping reply for {time.time() - self._last_ping_rcv:.1f}s, "
                             f"treating the connection as dropped")

                if alive != self._last_connected:
                    reconnected = alive and not self._last_connected
                    self._last_connected = alive
                    self.connected = alive
                    log.info(f"connection state changed: {alive}")
                    if self.on_connection_change:
                        self.on_connection_change(alive)
                    if reconnected:
                        # 连回来了要说一声：否则状态栏停在"正在重连"，而语音其实
                        # 已经好了
                        self._state('online', t("voice.reconnected"))
                        self._resync_after_reconnect()
            except Exception as e:
                if self.running:
                    log.warning(f"the connection monitor raised: {e}")
            time.sleep(1)

    def _resync_after_reconnect(self):
        """重连之后把整个电台栈重新推一遍。

        客户端是 reconnect=True 建的，掉线后 pymumble 自己会连回来——但服务器
        把重连上来的用户放回**根频道**，而频道监听和 VoiceTarget 都是跟会话走的
        注册，一起没了。这边只把界面上的灯点回绿色，就成了 CLAUDE.md 里写的那种
        最糟的情况：显示一切正常，人却待在根频道，一个频率都收不到，PTT 也发不
        出去。

        本地缓存必须一并作废，光调 sync 是不够的：
        - `_listening` 还记着断线前监听了哪些频道，diff 出来是空的，一条监听
          消息都不会再发；
        - `_sent_target` 会让发话目标的去重逻辑以为已经设好了；
        - `_channel_ids` 里的频道号可能已经失效——临时频道空了就被服务器销毁，
          再建回来是个新号。
        """
        log.info("reconnected, pushing the radio stack again (channel, listeners "
             "and voice targets all have to be redone)")
        self._channel_ids.clear()
        self._channel_to_khz.clear()
        self._listening = set()
        self._sent_target = None
        self._xc_targets = {}
        # 光清 _sent_target 不够：断线**前**启动的一轮 sync 可能还卡在
        # _sync_lock 里，等它跑完会把 _sent_target 写回旧值，随后重连的这一轮
        # 算出同一组频道就被去重吞掉。挂一个"下一轮必须强制重发"的标记。
        self._force_target = True
        stack = self._stack
        if stack is None:
            return
        threading.Thread(target=self.sync, args=(stack,), daemon=True).start()

    def _on_permission_denied(self, event):
        """服务器拒绝某个动作时说明白原因。

        频道监听有两个服务端上限（mumble-server.ini 的 listenersperuser /
        listenersperchannel，默认不限），超了服务器会明确回 ChannelListenerLimit
        或 UserListenerLimit——没有这条回报的话，管制员只会看到某些频率一直安静，
        根本猜不到是被服务器挡了。
        """
        try:
            kind = self.mumble.denial_type(event.type)
        except Exception:
            kind = str(getattr(event, "type", "?"))

        known = ("UserListenerLimit", "ChannelListenerLimit", "Permission")
        reason = t(f"denied.{kind}") if kind in known else t("denied.other", kind=kind)
        if getattr(event, "reason", ""):
            # 括号也得跟着语言走，直接拼中文全角括号的话，英文界面上会冒出
            # "Not permitted (…)（server said…）" 这种中英混排
            reason = t("denied.with_note", reason=reason, note=event.reason)
        # 记下来给建频道那段用：被拒了就别再等下去了
        self._denial = reason
        log.warning("the server refused an operation: %s", kind)
        self._state('denied', reason)

    def _give_up(self):
        """掉线后重连次数用尽：整个下线。

        必须真的收掉，不能只报个错。留着不管的话 PyAudio 还占着麦克风、pymumble
        还挂在那里，而管制员看到的只是一句红字——他再点一次连接会在"音频设备打不
        开"上失败，被指向声卡。

        这条跑在连接监控线程上，disconnect() 里的 join 都带
        `is not threading.current_thread()`，不会自己等自己。
        """
        self.gave_up = True
        log.warning("%d reconnect attempts after the drop all failed, going "
                    "offline", self.reconnect_limit)
        self.disconnect(state='offline',
                        message=t("voice.reconnect_failed",
                                  limit=self.reconnect_limit))

    def disconnect(self, state='stopped', message=None):
        """收掉整条连接。

        `state`/`message` 只为了区分"管制员自己点的断开"和"重连次数用尽后自己
        下线的"（`_give_up()` 传 offline）——界面对后者要弹回登录页并说明原因。
        """
        self.running = False
        self.transmitting = False
        self.connected = False

        for thread in (self._tx_thread, self._rx_monitor, self._connection_thread):
            if thread and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=2)
        self._tx_thread = None
        self._rx_monitor = None
        self._connection_thread = None

        self._close_streams()
        if self.audio:
            try:
                self.audio.terminate()
            except Exception as e:
                log.warning(f"releasing the audio device raised: {e}")
            self.audio = None
        if self.mumble:
            try:
                self.mumble.stop()
            except Exception as e:
                log.warning(f"disconnecting raised: {e}")
            self.mumble = None
        self._state(state, message if message is not None else t("voice.stopped"))

    # ---------- 音频设备 ----------
    def _find_best_sample_rate(self):
        def works(rate):
            try:
                probe = self.audio.open(
                    format=self.FORMAT, channels=self.CHANNELS, rate=rate,
                    input=True, frames_per_buffer=960, start=False,
                    input_device_index=self._input_device)
                probe.close()
                return True
            except Exception:
                return False

        for rate in SUPPORTED_SAMPLE_RATES:
            if works(rate):
                if rate != 48000:
                    log.warning(f"the device does not support 48 kHz, falling back to "
                                f"{rate} Hz (audio will be pitch-shifted)")
                return rate
        return 48000

    def setup_audio(self, input_device=None, output_device=None):
        """打开输入输出流。不传设备时沿用上次选定的。"""
        if input_device is not None:
            self._input_device = input_device
        if output_device is not None:
            self._output_device = output_device

        with self._stream_lock:
            self._close_streams_locked()
            self.input_stream = self.audio.open(
                format=self.FORMAT, channels=self.CHANNELS, rate=self.RATE,
                input=True, frames_per_buffer=self.CHUNK,
                input_device_index=self._input_device)
            self.output_stream = self.audio.open(
                format=self.FORMAT, channels=self.CHANNELS, rate=self.RATE,
                output=True, frames_per_buffer=self.CHUNK,
                output_device_index=self._output_device)

    def _close_streams(self):
        with self._stream_lock:
            self._close_streams_locked()

    def _close_streams_locked(self):
        for name in ("input_stream", "output_stream"):
            stream = getattr(self, name, None)
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception as e:
                    log.warning(f"closing the {name} stream raised: {e}")
                setattr(self, name, None)

    def set_mic_volume(self, percent):
        self.mic_volume = max(0, min(200, int(percent)))

    def set_speaker_volume(self, percent):
        self.speaker_volume = max(0, min(200, int(percent)))

    # ---------- 频道 ----------
    def _find_channel(self, name):
        try:
            return self.mumble.channels.find_by_name(name)
        except pymumble.errors.UnknownChannelError:
            return None

    def _channel_name(self, channel_id):
        """返回服务器当前给这个频道号的名称，拿不到时返回 None。

        Murmur 会回收 temporary channel 的数字 ID。只看 ``channel_id`` 会把
        一个已经被复用的旧号误认成目标频道，尤其是在判断自己是否已经入频率
        频道时。这里故意直接查当前频道表，不使用本地的频率映射缓存。
        """
        try:
            channel = self.mumble.channels[channel_id]
            if isinstance(channel, dict):
                return channel.get("name")
            return channel["name"]
        except (KeyError, TypeError, AttributeError, IndexError,
                pymumble.errors.UnknownChannelError):
            return None

    def _create_channel(self, name):
        """在根下建一个临时频道，**不要阻塞**。

        pymumble 的 channels.new_channel() 走 execute_command(blocking=True)，
        那个 acquire 没有任何超时——它自己的源码里就写着
        "TODO: manage a timeout for blocking commands"。命令一旦没被处理，这里
        就永远卡住，而 sync() 是整条电台栈同步链的入口，一起死掉：日志停在
        "建一个临时的"，之后既没有成功也没有任何错误，因为线程根本没从这一行
        返回。

        自己发命令、不等锁；频道有没有建出来由 _wait_for_channel 轮询判断，
        那本来就是更可靠的判据。
        """
        self.mumble.execute_command(messages.CreateChannel(0, name, True),
                                    blocking=False)

    def _wait_for_channel(self, name):
        """等服务器把新建的频道回报回来。

        建频道只是发一条消息，频道要等服务器回 ChannelState 才进本地表——这是
        一次网络往返。原来固定 sleep(0.2) 再找，连远程服务器时经常还没回来。

        **被拒就立刻回来，别等满。** 服务器用 PermissionDenied 明确说了不给建
        （少 MakeTempChannel 权限、或者重名），再干等 5 秒不会有别的结果——而这
        段等待是在 _sync_lock 里面的，会把后面排队的 sync 一起拖住。实测日志里
        一分钟内建了 34 次频道，就是这么攒出来的。atis/broadcast.py 一直是这么
        做的，管制端漏了这条。
        """
        deadline = time.time() + CHANNEL_TIMEOUT
        while time.time() < deadline and self.running:
            channel = self._find_channel(name)
            if channel is not None:
                return channel
            if self._denial:
                log.warning("the server refused to create channel %s: %s",
                            name, self._denial)
                return None
            time.sleep(0.1)
        return self._find_channel(name)

    def _resolve_channel(self, khz):
        """拿到频率对应的频道 id，没有就建一个临时频道。

        **每次都拿服务器的频道表核对一遍，绝不能直接吃缓存。** 频率频道都是
        temporary 的，最后一个人离开服务器当场就把它销毁——管制员把主频率从 A
        换到 B、A 上又没别人就足够了，根本用不着断线重连（重连时清缓存挡不住
        这条路径）。旧号留在表里的后果是两头都不报错：

        - `_join(旧号)` 的 MoveCmd 指向一个不存在的频道，服务器既不照做也不
          抱怨，日志里只剩一行接一行的"5 秒内没有生效"；
        - VoiceTarget 里编的还是那个旧号，话音被服务器直接丢掉。

        监听却落在别的活着的频道上，于是耳朵是好的——合起来正好是用户报的
        "收得到、发不动、不报错"。

        查一次频道表只是本地字典查找，不走网络，没有省下来的必要。
        """
        name = radiostack.channel_name(khz)
        channel = self._find_channel(name)

        if channel is None:
            self._forget_channel(khz, name)
            try:
                log.info("channel %s does not exist, creating a temporary one", name)
                # 清掉上一次的拒绝说明，否则 _wait_for_channel 会拿旧的当这一次的
                self._denial = None
                self._create_channel(name)
                channel = self._wait_for_channel(name)
            except Exception as e:
                log.warning(f"could not create channel {name}: {e}")
                return None
        if channel is None:
            log.warning("channel %s did not appear within %.0f s of being "
                        "created, skipping this round", name, CHANNEL_TIMEOUT)
            return None

        channel_id = channel["channel_id"]
        previous = self._channel_ids.get(khz)
        if previous is not None and previous != channel_id:
            # 同一个频率换了新号：反查表里的旧号要拆掉，否则收到的音频会被
            # 认成另一个频率
            if self._channel_to_khz.get(previous) == khz:
                self._channel_to_khz.pop(previous, None)
            log.info("channel %s changed id: %s -> %s", name, previous, channel_id)
        previous_khz = self._channel_to_khz.get(channel_id)
        if previous_khz is not None and previous_khz != khz:
            # 频道号被 Murmur 回收后分配给了另一个频率；旧频率的映射也必须
            # 拆掉，否则两个频率会同时指向同一个活频道。
            if self._channel_ids.get(previous_khz) == channel_id:
                self._channel_ids.pop(previous_khz, None)
            log.warning("channel id %s was reused: %s -> %s",
                        channel_id, radiostack.channel_name(previous_khz), name)
        log.debug("frequency %s maps to channel %s (id %s)",
                  radiostack.format_frequency(khz), name, channel_id)
        self._channel_ids[khz] = channel_id
        self._channel_to_khz[channel_id] = khz
        return channel_id

    def _forget_channel(self, khz, name):
        """频道已经不在服务器上了，把它的号从两张表里拆掉。"""
        stale = self._channel_ids.pop(khz, None)
        if stale is None:
            return
        if self._channel_to_khz.get(stale) == khz:
            self._channel_to_khz.pop(stale, None)
        self._listening.discard(stale)
        log.info("channel %s is gone (a temporary channel dies as soon as it "
                 "empties), discarding the stale id %s", name, stale)

    def sync(self, stack):
        """把电台栈的状态推到服务器：进主频道、监听其余 RX、设好 TX 目标。

        串行执行。界面每改一次电台栈就新起一个线程调这里，而这个函数里
        `_resolve_channel` 每个新频道最多要等 CHANNEL_TIMEOUT 秒，也就是说它会
        活好几秒；两个 sync 交错着写 `_join` / 监听列表 / `_tx_channels`，最后
        留下的组合可能跟任何一次栈状态都对不上。

        **门口最多站一个人等着，其余的直接返回。** 原来是全部排队——理由写的是
        "每一轮都重新读栈，后来的那次自然覆盖前面的"，但正因为每一轮都重读栈，
        排在中间的那些轮**做的是完全一样的事**，纯属白干；而每一轮都要几秒，
        队列于是变成一场风暴。实测日志里：`RadioStack(on_change=…)` 覆盖了加删、
        RX/TX/XC 开关、音量、静音、选主频率，加上数据源那条 60 秒定时器带来的
        set_transmit_allowed / set_locked / 自动加频率——一阵操作排出十几轮 sync，
        每轮重建两三个频道，一分钟内建了 34 次频道，服务器开始用 ChannelName
        （重名）回拒，而真正要紧的进频道请求一直没成。

        合并之后：正在跑的那一轮照跑，最多再排一轮，那一轮读的是**最新的**栈
        （所以用 self._stack 而不是参数），中间的调用全部丢掉——它们要推的状态
        已经被那一轮包含了。
        """
        # 记下来，重连之后和排队的那一轮都要照着它推
        self._stack = stack

        with self._sync_pending_lock:
            if self._sync_pending:
                return              # 已经有一轮排着了，它会读到最新的栈
            self._sync_pending = True

        with self._sync_lock:
            with self._sync_pending_lock:
                self._sync_pending = False
            # 读 self._stack：排队期间栈可能又变过，要推的是最新的那份
            self._sync(self._stack or stack)

    def _sync(self, stack):
        if not self.connected or not self.mumble:
            return

        self._volumes = {r.frequency_khz: r.effective_volume() for r in stack}

        # **一轮 sync 里每个频率只解析一次。**
        #
        # TX 集合是 RX 集合的子集（开 TX 会强制开 RX），XC 又是 TX 的子集，所以
        # 原来那三段各自调 _resolve_channel，同一个频率一轮里要解析两三遍。平时
        # 只是多查两次本地字典，不要紧；但只要第一遍的建频道在 CHANNEL_TIMEOUT
        # 内没回来（远程服务器上很常见），后面几遍就会看到"频道还是不存在"，
        # 于是**再各发一次 CreateChannel**——服务器随后用 ChannelName（重名）
        # 回拒，日志里同一秒里连着好几行"建一个临时的"就是这么来的。
        wanted_khz = list(dict.fromkeys(
            list(stack.rx_frequencies())
            + list(stack.tx_frequencies())
            + list(stack.xc_frequencies())))
        resolved = {}
        for khz in wanted_khz:
            channel_id = self._resolve_channel(khz)
            if channel_id is not None:
                resolved[khz] = channel_id

        rx_channels = {khz: resolved[khz] for khz in stack.rx_frequencies()
                       if khz in resolved}

        # 主频率：真正进入的那个频道。服务端不支持监听时，至少这个频率能听到
        primary = stack.selected_khz if stack.selected_khz in rx_channels else None
        if primary is None and rx_channels:
            primary = next(iter(rx_channels))
        joined = (primary is not None
                  and self._join_frequency(primary, rx_channels[primary]))

        # 其余频率用频道监听
        wanted = {cid for khz, cid in rx_channels.items() if khz != primary}
        if primary is not None and not joined:
            # 主频道没进去（临时频道死了、服务器没回话……）：至少把它挂上
            # 监听，别让管制员**唯一在用的那个频率**成了唯一听不到的
            wanted.add(rx_channels[primary])
            log.warning("could not join the primary channel, falling back to a "
                        "channel listener so RX still works")
        self._set_listening(wanted)

        # 发话目标。重连后的那一轮必须强制重发：断线前启动的一轮 sync 可能
        # 刚把 _sent_target 写回旧值，去重逻辑就会把重发吞掉——频道全对、
        # 听得见，PTT 发出去的帧却被服务器丢在地上。
        force_target = self._force_target
        self._force_target = False
        self._tx_channels = [resolved[khz] for khz in stack.tx_frequencies()
                             if khz in resolved]
        self._xc_channels = [resolved[khz] for khz in stack.xc_frequencies()
                             if khz in resolved]
        self._set_voice_target(self._tx_channels, force=force_target)
        self._program_cross_couple_targets()
        log.debug("syncing the radio stack: primary %s, RX %s, TX %s, XC %s",
                  radiostack.format_frequency(primary) if primary else "none",
                  [radiostack.format_frequency(k) for k in stack.rx_frequencies()],
                  [radiostack.format_frequency(k) for k in stack.tx_frequencies()],
                  [radiostack.format_frequency(k) for k in stack.xc_frequencies()])

    def _join_frequency(self, khz, channel_id):
        """进这个频率的频道，一次不成就按名字重新解析再试一次。

        频率频道是 temporary 的，最后一个人离开服务器当场销毁。所以从"解析出
        频道号"到"MoveCmd 被服务器处理"这中间，那个频道可能已经没了——服务器对
        指向不存在频道的 MoveCmd **既不照做也不报错**，我们只会等满
        CHANNEL_TIMEOUT 然后记一行"没有生效"。实测日志里这行出现了 40 次，
        管制员那一晚一直留在根频道：听不到、也发不出，而界面全是绿的。

        重解析一次的代价是一次本地字典查找加一次建频道；不重试的代价是这一轮
        彻底没进去，要等下一次栈变化才有机会——而那可能是几分钟以后。
        """
        name = radiostack.channel_name(khz)
        if self._join(channel_id, expected_name=name):
            return True

        again = self._resolve_channel(khz)
        if again is None or again == channel_id:
            return False
        log.info("channel for %s came back as id %s, joining again",
                 radiostack.format_frequency(khz), again)
        return self._join(again, expected_name=name)

    def _join(self, channel_id, expected_name=None):
        try:
            if (self.mumble.users.myself.get("channel_id") == channel_id
                    and (expected_name is None
                         or self._channel_name(channel_id) == expected_name)):
                return True
            if (expected_name is not None
                    and self.mumble.users.myself.get("channel_id") == channel_id):
                log.warning("channel id %s is not %s; forcing a re-resolve",
                            channel_id, expected_name)
                return False
            # move_in() 也走 execute_command(blocking=True)，和建频道一样会
            # 无限期卡住，同样自己发命令
            self.mumble.execute_command(
                messages.MoveCmd(self.mumble.users.myself_session, channel_id),
                blocking=False)
            # 命令是异步的，确认真的进去了再往下走——主频道没进去的话，服务端
            # 不支持频道监听时管制员一个频率都听不到
            if not self._wait_until_in(channel_id, expected_name=expected_name):
                log.warning("sent the request to join channel %s but it did not "
                            "take effect within %.0f s", channel_id,
                            CHANNEL_TIMEOUT)
                return False
            return True
        except Exception as e:
            log.warning(f"joining the channel failed: {e}")
            return False

    def _wait_until_in(self, channel_id, expected_name=None):
        """等服务器确认我们真的进了这个频道。"""
        deadline = time.time() + CHANNEL_TIMEOUT
        while time.time() < deadline and self.running:
            myself = self.mumble.users.myself
            if (myself is not None and myself.get("channel_id") == channel_id
                    and (expected_name is None
                         or self._channel_name(channel_id) == expected_name)):
                return True
            time.sleep(0.1)
        myself = self.mumble.users.myself
        return (myself is not None and myself.get("channel_id") == channel_id
                and (expected_name is None
                     or self._channel_name(channel_id) == expected_name))

    def _set_listening(self, channel_ids):
        """频道监听。pymumble 没封装，直接发 UserState。"""
        add = channel_ids - self._listening
        remove = self._listening - channel_ids
        if not add and not remove:
            return

        try:
            state = mumble_pb2.UserState()
            state.session = self.mumble.users.myself_session
            for channel_id in add:
                state.listening_channel_add.append(channel_id)
            for channel_id in remove:
                state.listening_channel_remove.append(channel_id)
            self.mumble.send_message(PYMUMBLE_MSG_TYPES_USERSTATE, state)
            log.debug("channel listeners +%s -%s, now listening to %s",
                      sorted(add), sorted(remove), sorted(channel_ids))
            self._listening = set(channel_ids)
        except Exception as e:
            log.warning(f"setting the channel listeners failed: {e}")

    def _program_target(self, target_id, channel_ids):
        """把一组频道编成某个编号的 VoiceTarget。"""
        if not self.mumble:
            return
        try:
            target = mumble_pb2.VoiceTarget()
            target.id = target_id
            for channel_id in channel_ids:
                entry = target.targets.add()
                entry.channel_id = channel_id
            self.mumble.send_message(PYMUMBLE_MSG_TYPES_VOICETARGET, target)
            log.debug("VoiceTarget %s set to channels %s", target_id, list(channel_ids))
        except Exception as e:
            log.warning(f"setting the voice target failed: {e}")

    def _program_cross_couple_targets(self):
        """给每个 XC 频率编一个"发给其它 XC 频率"的目标。

        每个来源一个编号，转发时只要切 target 编号就行，不用重编——否则会和
        PTT 抢同一个编号。
        """
        self._xc_targets = {}
        if len(self._xc_channels) < 2:
            return
        for index, source in enumerate(self._xc_channels):
            others = [c for c in self._xc_channels if c != source]
            target_id = XC_TARGET_BASE + index
            self._program_target(target_id, others)
            self._xc_targets[source] = target_id

    def _set_voice_target(self, channel_ids, force=False):
        """VoiceTarget：一次带上所有要发话的频道。

        sync() 在任何状态变化后都会被调用（包括拖动音量条），目标没变就别再发一遍。
        """
        if not self.mumble:
            return
        key = tuple(channel_ids)
        if not force and key == self._sent_target:
            return
        self._sent_target = key
        self._program_target(PTT_TARGET_ID, channel_ids)

    # ---------- 收 ----------
    def _on_sound(self, user, soundchunk):
        # 断线重连的空档里 myself 可能还没建立，早期版本在这里崩过
        if not self.mumble or not self.mumble.users.myself:
            return
        try:
            if user["name"] == self.mumble.users.myself["name"]:
                return
        except Exception:
            pass
        if not soundchunk or getattr(soundchunk, "pcm", None) is None:
            return

        channel_id = user.get("channel_id")
        khz = self._channel_to_khz.get(channel_id)
        if khz is None:
            return                      # 不是我们关心的频率
        # 号可能已经被服务器回收给**别的**频率了：临时频道空了就销毁，Murmur
        # 发号又是复用的。名字对不上就把这条旧映射拆掉——不校验的话，B 频率
        # 的话音会点亮 A 的灯、按 A 的音量播、还转发到 A 的交叉耦合上。
        try:
            channel = self.mumble.channels[channel_id]
            if channel.get("name") != radiostack.channel_name(khz):
                self._forget_channel(khz, radiostack.channel_name(khz))
                return
        except Exception:
            return

        if channel_id != self.mumble.users.myself.get("channel_id"):
            self.listeners_confirmed = True   # 收到了监听频道的话音，服务端确实支持

        now = time.time()
        first = khz not in self._last_rx or now - self._last_rx[khz] > RX_TIMEOUT
        self._last_rx[khz] = now
        if first and self.on_rx:
            try:
                self.on_rx(khz, True, user["name"])
            except Exception as e:
                log.warning(f"the RX callback raised: {e}")

        try:
            audio = np.frombuffer(soundchunk.pcm, dtype=np.int16)
            if not len(audio):
                return
            scale = (self.speaker_volume / 100.0) * (self._volumes.get(khz, 100) / 100.0)
            audio = np.clip(audio * scale, np.iinfo(np.int16).min,
                            np.iinfo(np.int16).max).astype(np.int16)
            with self._stream_lock:
                if self.output_stream:
                    self.output_stream.write(audio.tobytes())
        except Exception as e:
            log.warning(f"playing the received audio raised: {e}")

        self._forward_cross_couple(khz, soundchunk)

    def _forward_cross_couple(self, khz, soundchunk):
        """交叉耦合：在一个 XC 频率上收到的话音，转发到其它 XC 频率。"""
        # 管制员正在讲话时不转发：一条连接只有一个发送队列，两股音频挤进去
        # 只会互相打断，而且此刻管制员的话音才是要紧的
        if self.transmitting:
            return
        source = self._channel_ids.get(khz)
        target_id = self._xc_targets.get(source)
        if target_id is None:
            return

        # 目标编号是 sync 时就编好的，这里只切换，不重编——所以不会动到 PTT
        with self._audio_lock:
            if self.transmitting:
                return
            try:
                self.mumble.sound_output.target = target_id
                self.mumble.sound_output.add_sound(soundchunk.pcm)
            except Exception as e:
                log.warning(f"cross-couple forwarding failed: {e}")
            finally:
                self.mumble.sound_output.target = 0

    def _rx_monitor_loop(self):
        while self.running:
            now = time.time()
            for khz, last in list(self._last_rx.items()):
                # 用 pop 比对，而不是无条件 del：回调线程可能刚刚更新过这个
                # 时间戳，直接删会让指示灯灭一下又亮
                if now - last > RX_TIMEOUT:
                    if self._last_rx.get(khz) != last:
                        continue
                    self._last_rx.pop(khz, None)
                    if self.on_rx:
                        try:
                            self.on_rx(khz, False, "")
                        except Exception as e:
                            log.warning(f"the RX callback raised: {e}")
            time.sleep(0.1)

    # ---------- 发 ----------
    def start_transmit(self):
        if self.transmitting or not self.connected:
            return
        if not self._tx_channels:
            return                       # 没有任何频率开了 TX

        # 等上一条发话线程收完尾再起新的。连按 PTT 时旧线程可能还在退出途中，
        # 它退出时会把 target 清零——那会正好落在新线程开讲之后，话音就发到
        # 当前所在频道而不是 TX 集合去了。
        previous = self._tx_thread
        if previous and previous.is_alive() and previous is not threading.current_thread():
            previous.join(timeout=1)

        self.transmitting = True
        if self.on_tx:
            self.on_tx(True)
        self._tx_thread = threading.Thread(target=self._transmit_loop, daemon=True)
        self._tx_thread.start()

    def stop_transmit(self):
        if not self.transmitting:
            return
        self.transmitting = False
        if self.on_tx:
            self.on_tx(False)

    def _transmit_loop(self):
        try:
            with self._audio_lock:
                self.mumble.sound_output.target = PTT_TARGET_ID
        except Exception as e:
            log.warning(f"switching the voice target failed: {e}")

        while self.transmitting and self.running:
            try:
                with self._stream_lock:
                    stream = self.input_stream
                    if not stream:
                        break
                    data = stream.read(self.CHUNK, exception_on_overflow=False)
                if data:
                    audio = np.frombuffer(data, dtype=np.int16)
                    audio = np.clip(audio * (self.mic_volume / 100.0),
                                    np.iinfo(np.int16).min,
                                    np.iinfo(np.int16).max).astype(np.int16)
                    # 断线期间不要往外灌音频，否则会在缓冲里堆积
                    if not self.connected:
                        continue
                    with self._audio_lock:
                        # 交叉耦合可能刚把 target 切走，每次都重新确认
                        self.mumble.sound_output.target = PTT_TARGET_ID
                        self.mumble.sound_output.add_sound(audio.tobytes())
            except Exception as e:
                log.warning(f"recording raised: {e}")
                time.sleep(0.1)
            time.sleep(0.001)

        try:
            with self._audio_lock:
                self.mumble.sound_output.target = 0
        except Exception:
            pass
