"""以飞行员身份连接 FSD 服务端（can-fsd）。

对应 xPilot 的 fsd 层。协议逐条对照 can-fsd 的解析代码
（internal/fsd/conn.go、handler.go、packet.go）和 docs/protocol.md：

    登录   $ID{呼号}:SERVER:{客户端ID}:{客户端名}:{主}:{次}:{CID}:{机器码}
           #AP{呼号}:SERVER:{CID}:{密码}:{等级}:{协议版本}:{模拟器}:{真实姓名}
    位置   @{应答机模式}:{呼号}:{squawk}:{等级}:{纬度}:{经度}:{高度}:{地速}:{PBH}:{气压差}
    快速   ^{呼号}:{纬度}:{经度}:{真高}:{离地高}:{PBH}:{东}:{上}:{北}:{俯仰率}:{航向率}
           :{坡度率}:{前轮角}      —— $SF 打开时 5 Hz。#SL 字段相同，5 秒一个；
           #ST 没有六个速度段，停着的时候发
    计划   $FP{呼号}:SERVER:{规则}:{机型}:{真空速}:{起飞地}:{预计起飞}:{实际起飞}
           :{巡航高度}:{目的地}:{航路小时}:{航路分钟}:{燃油小时}:{燃油分钟}
           :{备降场}:{备注}:{航路}          —— 一共 17 段，少一段整包被拒
    文字   #TM{呼号}:{收件人}:{正文}
    下线   #DP{呼号}:{CID}

$ID 的第 9 个字段（challenge）留空，服务端就不会发起 VATSIM 客户端质询——
那套算法只有官方客户端有密钥表，can-fsd 允许第三方客户端不参与
（internal/fsd/conn.go 的 authenticate）。

位置包里的姿态压在一个 32 位整数里（PBH），编码必须和服务端的解码严丝合缝，
错了会让别人看到飞机以奇怪的姿态飞行。
"""

import json
import logging
import math
import socket
import threading
import time

# _status() 的消息会直接进消息区，是界面文字，所以要过 i18n。日志那一半仍然
# 是英文——分界线在"这句话最后到哪儿去"，不在它写在哪个模块里。
from altitude import adjust_incoming_altitude
from i18n import t

log = logging.getLogger("fsd")

DEFAULT_PORT = 6809
# ProtoRevisionVelocity。can-fsd 只把 ^ / #SL / #ST 转给 101 的客户端
# （broadcast.go 的 broadcastRangedVelocity），报 100 的话 vPilot / xPilot
# 这类客户端的飞机在我们这里五秒才动一下。101 在服务端另外只多一件事：
# 5 海里内有别的 101 飞行员时发来 `$SF…:1` 叫我们发快速位置，6 海里外发
# `$SF…:0` 叫停（handler.go 的 updateSendFast）。
PROTO_REVISION = 101
RATING_OBSERVER = 1
POSITION_INTERVAL = 0.2       # 每秒 5 次，和 VATSIM 客户端一致
SLOW_POSITION_INTERVAL = 5.0  # 停在地面上没动时降频
# 快速位置的节奏，照 xPilot（networkmanager.cpp）：$SF 打开时每 200 ms 一个
# `^`（停着就是 `#ST`）；不管开没开，每 5 秒一个 `#SL`（在动的时候）。
# `@` 照旧按上面的节奏发：服务端只按 `@` 更新我们在邮局里的位置和 $SF 判定
# （handlePilotPosition），快速位置只刷新空闲计时（handleFastPilotPosition）。
FAST_POSITION_INTERVAL = 0.2
SLOW_FAST_INTERVAL = 5.0
# 自己的配置（灯光/起落架/襟翼）变了就向附近广播，最密每秒一次。放襟翼的
# 那几秒里 flaps_pct 一直在变，不限速的话一次放襟翼就是几十个包。
CONFIG_BROADCAST_INTERVAL = 1.0
# 广播给附近所有飞行员的收件人（can-fsd docs/protocol.md 的 @94836）
RANGED_ALL = "@94836"
# 速度和角速度都小于这个值就算停着（xPilot 的
# POSITIONAL_VELOCITY_ZERO_TOLERANCE，米每秒 / 弧度每秒）
STOPPED_TOLERANCE = 0.005
LOGIN_TIMEOUT = 10.0
# 掉线后重连按时间算，不按次数算。服务端要等旧连接死透才放出呼号
# （postoffice.go 的 register 回 `$ER … 1 … Callsign already in use`）：
# 读超时 90 秒（conn.go 的 readTimeout），空闲清理 60 秒、每 30 秒扫一次
# （reaper.go）。三次 × 3 秒的老预算在这之前就用完了，飞机直接下线。
RECONNECT_WINDOW = 150.0
# 每次重试前等多久，逐次拉长，最后一档一直用到窗口用完
RECONNECT_DELAYS = (3.0, 5.0, 10.0, 15.0, 20.0, 30.0)

# 服务端拒绝登录的错误码（can-fsd internal/fsd/errors.go）。只有这两个值得
# 再试：呼号被旧连接占着、服务器满了。其余（密码错、账号停用、等级太高、
# 被督导踢过……）重试只会得到同一个答案，而认证失败按 CID 限流。
ERR_CALLSIGN_IN_USE = "1"
ERR_SERVER_FULL = "12"

# 一次失败属于哪一类，决定 _run 要不要再试
FAILURE_FATAL = "fatal"            # 服务端说这个人/这次登录不行，停
FAILURE_IN_USE = "callsign-in-use"  # 呼号还被上一条连接占着，等它释放
FAILURE_TRANSIENT = "transient"    # 网络断了、超时、服务器满——登录过就再试

KNOTS_PER_MPS = 1.943844492
# can-fsd 的 IsValidCallsign 上限（packet.go 的 MaxCallsignLength）
MAX_CALLSIGN_LENGTH = 12

CLIENT_ID = "0001"
CLIENT_NAME = "MSFS for CAN"
CLIENT_MAJOR = 1
CLIENT_MINOR = 0

# 模拟器编号，取自 can-fsd 的 docs/enumerations.md。这份是从 xpc 复制来的，
# 连它报 X-Plane 的编号一起带了过来——MSFS 客户端不该说自己是 X-Plane。
# SimConnect 不好判断是 2020 还是 2024，按 2020 报。
SIMULATOR_MSFS_2020 = 10
SIMULATOR_MSFS_2024 = 11
SIMULATOR = SIMULATOR_MSFS_2020

# 应答机模式对应位置包的第一个字符
XPDR_STANDBY = "S"            # 待机 / 仅 mode A
XPDR_MODE_C = "N"             # 正常
XPDR_IDENT = "Y"              # 识别


def pack_pbh(pitch, bank, heading, on_ground=False):
    """把俯仰/坡度/航向压成 32 位整数。

    can-fsd 的 PitchBankHeading 是这样拆的（internal/fsd/packet.go）：

        pitch   = 位 22-31，乘 360/1024，再折到 -180..180
        bank    = 位 12-21，同上
        heading = 位 2-11，乘 360/1024
        位 1    = 是否在地面

    所以这里按同样的比例反着编。角度先折到 0..360 再量化，否则负角度会溢出。
    """
    ratio = 1024.0 / 360.0

    def quantise(value):
        return int(round((value % 360.0) * ratio)) & 0x3FF

    packed = (quantise(pitch) << 22) | (quantise(bank) << 12) | (quantise(heading) << 2)
    if on_ground:
        packed |= 0x2
    return packed & 0xFFFFFFFF


def unpack_pbh(packed):
    """pack_pbh 的逆运算，用来还原别人的姿态。

    位宽和比例必须和 pack_pbh 对称。test_xpc.py 里另有一份从 can-fsd 的 Go
    代码转写来的独立实现当参照物——那份才是判定标准，这里改了要能对上它。
    """
    ratio = 360.0 / 1024.0
    mask = 0x3FF

    def normalise(value):
        return value - 360.0 if value > 180.0 else value

    return {
        "pitch": normalise((packed >> 22 & mask) * ratio),
        "bank": normalise((packed >> 12 & mask) * ratio),
        "heading": (packed >> 2 & mask) * ratio,
        "on_ground": bool(packed & 0x2),
    }


def wire_rotation(snapshot):
    """快照里的机体角速度 -> 快速位置包的三个角速度字段（弧度每秒）。

    快照是抬头、右转、右坡为正（X-Plane 的 Q/R/P 方向）。包里的方向照 xPilot：
    它发 `-Qrad`、`Rrad`、`-Prad`（xplane_adapter.cpp），也就是 MSFS 的
    ROTATION VELOCITY BODY X/Y/Z 原样的方向——低头、右转、左坡为正。
    PBH 的符号约定和这个无关，那是 pack_pbh 的事。
    """
    return (-math.radians(snapshot.get("pitch_rate", 0.0)),
            math.radians(snapshot.get("heading_rate", 0.0)),
            -math.radians(snapshot.get("bank_rate", 0.0)))


def wire_altitude(snapshot):
    """位置包里报的高度：网络高度（真高 + 温度误差），见 altitude.py。"""
    return snapshot.get("network_altitude", snapshot["altitude"])


def is_stopped(snapshot):
    """xPilot 的 PositionalVelocityIsZero：速度和角速度都几乎为零。"""
    velocity = (snapshot.get("velocity_east", 0.0), snapshot.get("velocity_up", 0.0),
                snapshot.get("velocity_north", 0.0))
    return all(abs(v) < STOPPED_TOLERANCE
               for v in velocity + wire_rotation(snapshot))


def fast_position_packet(kind, callsign, snapshot):
    """拼一个 `^` / `#SL` / `#ST`。字段和精度照 xPilot 的 PDUFastPilotPosition。

        0 呼号  1 纬度  2 经度  3 网络高度（英尺）  4 离地高（英尺）  5 PBH
        6/7/8 速度 东/上/北（米每秒）  9/10/11 角速度（弧度每秒）  12 前轮角（度）

    `#ST` 没有 6-11 那六段，一共 7 段；另外两种 13 段——和 can-fsd 的
    minFields 一致，少一段服务端不转。
    """
    pbh = pack_pbh(snapshot["pitch"], snapshot["bank"], snapshot["heading"],
                   snapshot.get("on_ground", False))
    fields = [f"{kind}{callsign}",
              f"{snapshot['latitude']:.6f}", f"{snapshot['longitude']:.6f}",
              f"{float(wire_altitude(snapshot)):.2f}",
              f"{float(snapshot.get('agl', 0.0)):.2f}", str(pbh)]
    if kind != "#ST":
        fields += [f"{snapshot.get('velocity_east', 0.0):.4f}",
                   f"{snapshot.get('velocity_up', 0.0):.4f}",
                   f"{snapshot.get('velocity_north', 0.0):.4f}"]
        fields += [f"{value:.4f}" for value in wire_rotation(snapshot)]
    fields.append(f"{snapshot.get('nose_wheel', 0.0):.2f}")
    return ":".join(fields)


def callsign_problem(callsign):
    """呼号不合服务端规矩时返回说明。规则来自 can-fsd 的 IsValidCallsign。"""
    callsign = (callsign or "").strip().upper()
    if not 2 <= len(callsign) <= MAX_CALLSIGN_LENGTH:
        return t("callsign.length", callsign=callsign, count=len(callsign),
                 limit=MAX_CALLSIGN_LENGTH)
    if any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in callsign):
        return t("callsign.charset", callsign=callsign)
    return None


def sanitize(text):
    """包是冒号分隔的，正文里的冒号和换行会破坏分帧。"""
    return (text or "").replace(":", " ").replace("\r", " ").replace("\n", " ").strip()


# 督导频道。FSD 里 `*S` 是一个**收件人**而不是一条命令：客户端把 .wallop 翻成
# 发往这个地址的普通 #TM，服务端 handleTextMessage 认出它再转给所有在线督导。
# can-fsd 那条分支没有等级门槛（要求督导身份的是 `*` 全网广播那条），所以飞行
# 员发得出去。
WALLOP_RECIPIENT = "*S"

# 认识的点命令。EuroScope 和 CRC 是在**客户端**把 `.wallop` 翻成正确的包的，
# 这个客户端原来没有这一步：用户打的 `.wallop 求助` 被当成普通正文，跟着收件
# 人框（空的时候是 COM1 频率）发到频率上去了。服务端的 handleWallop 一次都不
# 会触发，督导收不到、Discord 中继也不会响，而界面还回一行"已发送"——所以它
# 看起来是好的。
DOT_COMMANDS = {".wallop": WALLOP_RECIPIENT}


def parse_dot_command(text):
    """把用户输入的一行翻成 `(收件人, 正文)`。

    不是已知点命令就返回 `(None, 原文)`，由调用方按原来的规矩挑收件人。

    **不认识的点命令原样发出去**，既不报错也不吞掉：猜不出用户是想打一条命令
    还是真要发一句以点开头的话，而吞掉一条本该发出去的消息，比把一句奇怪的话
    发到频率上更糟。

    命令名不分大小写（`.WALLOP` 也认）；正文一个字都不动——大小写、标点和内部
    空格都是用户写给督导的原话，转述时不该被改写。分帧要洗的冒号由 sanitize
    在发包那一步负责，不在这里。
    """
    stripped = (text or "").strip()
    if not stripped.startswith("."):
        return None, stripped

    # split(None, 1) 按任意空白切，所以 `.wallop\t求助` 也认得。
    parts = stripped.split(None, 1)
    recipient = DOT_COMMANDS.get(parts[0].lower())
    if recipient is None:
        return None, stripped

    return recipient, (parts[1].strip() if len(parts) > 1 else "")


class FSDPilot:
    """飞行员的 FSD 连接。

    回调都在后台线程触发：
        on_status(state, message)     connecting / online / reconnecting /
                                     error / offline / stopped
        on_text(sender, recipient, message)
        on_controllers(list)          附近的管制席位

    `reconnecting` 是暂时的，还在试；`offline` 是 RECONNECT_WINDOW 秒内都没
    连回来，这条链路彻底完了；`error` 是不重试的失败（首连连不上、密码错）。
    界面对后两者只收掉 FSD 这一条，语音不动。
    """

    def __init__(self, host, callsign, cid, password, real_name="",
                 port=DEFAULT_PORT, rating=RATING_OBSERVER, aircraft="",
                 on_status=None, on_text=None, on_controllers=None,
                 traffic=None, reconnect_window=RECONNECT_WINDOW):
        self.host = host
        self.port = int(port or DEFAULT_PORT)
        self.callsign = (callsign or "").strip().upper()
        self.cid = sanitize(str(cid))
        # 密码也要过 sanitize：带冒号的密码会让 #AP 后面的字段全部错位（服务器
        # 直接拒收），而 _redact() 按固定下标打码，错位后冒号后那一截密码会
        # **原样进日志**——用户贴日志求助时把自己的网站密码贴出去了。
        # 冒号在 FSD 协议里本来就带不动，这里换成空格不会让一个本来能登录的
        # 密码登不上。
        self.password = sanitize(password)
        self.real_name = sanitize(real_name) or self.cid
        self.rating = int(rating)
        self.aircraft = sanitize(aircraft).upper()

        self.on_status = on_status
        self.on_text = on_text
        self.on_controllers = on_controllers

        # 他机表。给了就解析别人的位置包并参与机型交换；不给就只当语音+上报用。
        self.traffic = traffic
        if traffic is not None and traffic.on_request_info is None:
            traffic.on_request_info = self.request_plane_info
        if traffic is not None and getattr(traffic, "on_request_config", None) is None:
            traffic.on_request_config = self.request_config
        # 航司码取呼号前三位字母，CCA1501 -> CCA。用于模型匹配的涂装选择。
        prefix = self.callsign[:3]
        self.airline = prefix if prefix.isalpha() else ""

        self.running = False
        self.stop_event = threading.Event()
        self.thread = None
        self._sock = None
        self._buffer = b""
        self._logged_in = False
        # 掉线后最多重试多少秒，用完就下线
        self.reconnect_window = float(reconnect_window)
        self.gave_up = False
        # 最近一次失败的类别（FAILURE_*），_run 据此决定要不要再试
        self._failure = FAILURE_TRANSIENT
        # 发送失败的那个异常。非 None 表示这条 socket 已经坏了、已经被关掉，
        # 收包那边应当当作掉线处理。
        self._broken = None
        # 登录成功过之后，失败就先当"可以重连"。这个标记让 _status 把中途的
        # error 翻成 reconnecting——否则界面收到一次 error 就把整条连接当没了，
        # 而我们其实马上就要再试。
        self._retryable = False

        self._lock = threading.Lock()
        self._position = None       # 最近一次从模拟器拿到的快照
        self._squawk = 2000
        self._xpdr_mode = XPDR_STANDBY
        self._ident_until = 0.0
        # 服务端的 $SF：附近有别的 101 飞行员，要 5 Hz 的快速位置
        self.send_fast = False
        self.controllers = {}       # 呼号 -> {frequency, ...}
        # 最近一次广播出去的配置。None 表示下一次要发全量。
        self._config_sent = None

    # ---------- 对外 ----------
    def start(self):
        if self.running:
            return
        self.running = True
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        # 先把"可以重连"撤掉，否则 _close() 的 stopped 会被翻成 reconnecting，
        # 用户明明自己点的断开，界面上却写着重连中。
        self._retryable = False
        self.stop_event.set()
        thread = self.thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)
        self.thread = None

    @property
    def connected(self):
        return bool(self._logged_in and self._sock)

    def update_position(self, snapshot):
        """模拟器那边有新数据了。只存下来，实际发包由连接线程按节奏做。"""
        with self._lock:
            self._position = snapshot
            if snapshot:
                self._squawk = snapshot.get("squawk", self._squawk)
                # simlink.xpdr_mode() 已经判过了，这里只有 2（在线）和 1（待机）
                # 两个值。取不到就当在线——拿不准的时候少报一个待机，总好过让
                # 管制端的标牌上高度和地速一起空掉。
                mode = snapshot.get("xpdr_mode", 2)
                self._xpdr_mode = XPDR_MODE_C if mode >= 2 else XPDR_STANDBY

    def ident(self, seconds=8.0):
        """按下 IDENT，位置包的模式字符临时变成 Y。"""
        self._ident_until = time.time() + seconds
        log.info("IDENT")

    def send_text(self, recipient, message):
        """给某个呼号发文字消息。recipient 用 @频率 可以发到频率上。"""
        message = sanitize(message)
        if not message:
            return False
        return self._send(f"#TM{self.callsign}:{sanitize(recipient)}:{message}")

    def file_flight_plan(self, plan):
        """提交飞行计划。plan 是 gui 那边攒好的字典。

        字段顺序和数量抄自 can-fsd 的 docs/protocol.md（Flight Plan `$FP`），
        **一共 17 段**，少一段服务端就回 "Too few fields for $FP"（真实日志里
        每次提交都是这个）。先前漏了燃油小时/分钟两段，而且把航路时间那两段
        当成了备降时间。

            $FP呼号:SERVER:规则:机型:真空速:起飞地:预计起飞:实际起飞:巡航高度
                   :目的地:航路小时:航路分钟:燃油小时:燃油分钟:备降场:备注:航路

        收件人按文档是 SERVER；`*A` 是服务端转发给管制时用的，不是填报用的。
        """
        fields = [
            sanitize(plan.get("rules", "I"))[:1] or "I",
            sanitize(plan.get("aircraft", self.aircraft)),
            sanitize(plan.get("cruise_speed", "")),
            sanitize(plan.get("departure", "")).upper(),
            sanitize(plan.get("departure_time", "")),
            sanitize(plan.get("actual_time", "")),
            sanitize(plan.get("cruise_altitude", "")),
            sanitize(plan.get("arrival", "")).upper(),
            sanitize(plan.get("enroute_hours", "0")) or "0",
            sanitize(plan.get("enroute_minutes", "0")) or "0",
            sanitize(plan.get("fuel_hours", "0")) or "0",
            sanitize(plan.get("fuel_minutes", "0")) or "0",
            sanitize(plan.get("alternate", "")).upper(),
            sanitize(plan.get("remarks", "")),
            sanitize(plan.get("route", "")).upper(),
        ]
        return self._send(f"$FP{self.callsign}:SERVER:" + ":".join(fields))

    def request_atis(self, callsign):
        """问某个管制席位要文字通播。"""
        return self._send(f"$CQ{self.callsign}:{sanitize(callsign).upper()}:ATIS")

    def request_metar(self, icao, timeout=20):
        """向服务端要 METAR（$AX）。拿不到返回 None。"""
        icao = (icao or "").strip().upper()
        if not self.connected:
            return None
        waiter = [threading.Event(), None]
        with self._lock:
            self._metar_waiter = waiter
        if not self._send(f"$AX{self.callsign}:SERVER:METAR:{icao}"):
            return None
        return waiter[1] if waiter[0].wait(timeout) else None

    # ---------- 内部 ----------
    def _status(self, state, message):
        # 重连期间的失败不是终态。这里翻一次比在每个报错点各判一次可靠：
        # _connect 有四条报错路径、_loop 还有一条，漏掉任何一条，界面就会在
        # 我们正准备重连的时候把这条连接当成彻底没了。
        if self._retryable and state in ('error', 'stopped'):
            state = 'reconnecting'
        log.info("%s %s: %s", self.callsign, state, message)
        if self.on_status:
            try:
                self.on_status(state, message)
            except Exception as e:
                log.warning("status callback raised: %s", e)

    @staticmethod
    def _redact(packet):
        """#AP 的第 4 段是密码，日志里换成星号。"""
        if not packet.startswith("#AP"):
            return packet
        fields = packet.split(":")
        if len(fields) > 3:
            fields[3] = "***"
        return ":".join(fields)

    def _send(self, packet):
        sock = self._sock
        if not sock:
            return False
        try:
            sock.sendall((packet + "\r\n").encode("utf-8", errors="replace"))
            log.debug("→ %s", self._redact(packet))
            return True
        except Exception as e:
            self._send_failed(sock, e)
            return False

    def _send_failed(self, sock, error):
        """发不出去说明连接已经坏了：把 socket 关掉，让收包那边走正常的掉线重连。

        以前这里只报一条 error（重连期间被翻成 reconnecting），socket 原样留着，
        什么都不会去重连——界面停在"重连中"，飞机在网上一动不动。shutdown 会
        叫醒正卡在 recv() 里的连接线程（它读到 EOF 或报错），不管 _send 是从
        哪条线程调的。
        """
        if self._broken is None:
            self._broken = error
            log.warning("FSD send failed (%s, errno %s): %s; closing the socket",
                        type(error).__name__, getattr(error, "errno", None), error)
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass

    def _run(self):
        """连接 → 收发 → 掉线重连，最多重试 reconnect_window 秒。

        失败按类别处理（_failure）：

        - **FAILURE_FATAL**：密码错、账号停用、被督导踢过……重试只会得到同一个
          答案，还会撞上服务端按 CID 的认证失败限流。立即停。
        - **FAILURE_IN_USE**：呼号还被上一条连接占着。服务端要等旧连接超时
          （最长约 90 秒）才放出来，所以**首连也等**——客户端崩了或断网后重开，
          头一次登录撞上的正是自己的旧连接。
        - **FAILURE_TRANSIENT**：连不上、超时、连接断了。首连就这样多半是地址
          填错，不重试；登录过之后才重试（服务器重启、网络抖动）。

        重试的间隔逐次拉长（RECONNECT_DELAYS），从第一次失败算起超过
        reconnect_window 秒还没连回来就报 offline 并结束。
        """
        established_once = False
        attempt = 0
        retry_since = None

        while self.running and not self.stop_event.is_set():
            self._failure = FAILURE_TRANSIENT
            try:
                if self._connect():
                    established_once = True
                    attempt = 0
                    retry_since = None
                    # 从这一刻起，掉线是可以重连的
                    self._retryable = True
                    self._loop()
            except Exception as e:
                self._status('error', t("fsd.exception", error=e))
            finally:
                self._close()

            if not self.running or self.stop_event.is_set():
                return                  # 用户自己断的
            failure = self._failure
            if failure == FAILURE_FATAL:
                return                  # 原因已经报过了，而且是终态
            if failure == FAILURE_TRANSIENT and not established_once:
                return                  # 首次就没连上，原因已经报过了

            now = time.monotonic()
            if retry_since is None:
                retry_since = now
            waited = now - retry_since
            if waited >= self.reconnect_window:
                self.gave_up = True
                self._retryable = False
                log.warning("could not get back onto FSD within %.0f s "
                            "(last failure: %s), giving up",
                            self.reconnect_window, failure)
                self._status('offline',
                             t("fsd.give_up", seconds=int(self.reconnect_window)))
                return

            delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
            delay = min(delay, self.reconnect_window - waited)
            attempt += 1
            log.info("FSD retry %d in %.0f s (%s, %.0f of %.0f s used)",
                     attempt, delay, failure, waited, self.reconnect_window)
            # 已经登录过的话 _retryable 本来就是真；首连撞上呼号被占时
            # $ER 那里已经置真。两种情况下这条都是 reconnecting。
            self._retryable = True
            if failure == FAILURE_IN_USE:
                message = t("fsd.callsign_busy", callsign=self.callsign,
                            delay=int(round(delay)))
            else:
                message = t("fsd.reconnecting", attempt=attempt,
                            delay=int(round(delay)))
            self._status('reconnecting', message)
            if self.stop_event.wait(delay):
                return

    def _connect(self):
        self._broken = None
        # $SF 是按连接给的，重连之后等服务端重新判
        self.send_fast = False
        problem = callsign_problem(self.callsign)
        if problem:
            self._failure = FAILURE_FATAL
            self._retryable = False
            self._status('error', problem)
            return False

        self._status('connecting',
                     t("fsd.connecting", callsign=self.callsign, server=self.host))
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=10)
            self._sock.settimeout(1.0)
        except Exception as e:
            self._status('error', t("fsd.connect_failed", host=self.host,
                                            port=self.port, error=e))
            return False

        greeting = self._read_packet(timeout=5)
        if greeting:
            log.info("server greeting: %s", greeting)

        machine_id = sum(ord(c) for c in self.callsign) * 7919
        self._send(f"$ID{self.callsign}:SERVER:{CLIENT_ID}:{CLIENT_NAME}:"
                   f"{CLIENT_MAJOR}:{CLIENT_MINOR}:{self.cid}:{machine_id}")
        self._send(f"#AP{self.callsign}:SERVER:{self.cid}:{self.password}:"
                   f"{self.rating}:{PROTO_REVISION}:{SIMULATOR}:{self.real_name}")
        self._send(f"$CQ{self.callsign}:SERVER:CAPS")

        deadline = time.time() + LOGIN_TIMEOUT
        while time.time() < deadline:
            if not self.running or self.stop_event.is_set():
                return False
            packet = self._read_packet(timeout=1)
            if packet is None:
                continue
            if packet == "":
                self._status('error', t("fsd.closed"))
                return False
            if self._handle_packet(packet) is False:
                return False
            if self._logged_in:
                self._status('online', t("fsd.online", callsign=self.callsign))
                # 这里曾经发过 $CQ…:SERVER:ATC 想要一份在线管制列表。那是误解：
                # can-fsd 的 handleQueryATC 是问"某个指定呼号是不是在线管制"，
                # 第 3 段必须带目标呼号，不带就回 "Missing callsign"（真实日志
                # 里每次登录都有一条）。本来也不需要——管制席位是靠 % 位置包
                # 主动广播过来的，_note_controller 已经在收了。
                return True

        self._status('error', t("fsd.login_timeout"))
        return False

    def _loop(self):
        # 登录后先报 `@`，再报一个快速位置（停着就是 #ST），和 xPilot 一样：
        # 服务端靠 `@` 知道我们在哪、该转给谁；别的 101 客户端从这一刻起就按
        # 快速位置画我们，不等第一个 $SF
        now = time.time()
        next_position = now + self._send_position()
        next_fast = 0.0
        next_slow = now + SLOW_FAST_INTERVAL
        next_config = now
        self._config_sent = None
        self._send_fast_position(slow=False)
        while self.running and not self.stop_event.is_set():
            # 收包的等待不能盖过下一次该发的时刻，否则 200 ms 的节奏会被拖成
            # 400 ms
            due = min(next_position, next_slow,
                      next_fast if self.send_fast else float("inf"))
            timeout = max(0.01, min(0.2, due - time.time()))
            packet = self._read_packet(timeout=timeout) if self._broken is None else ""
            if packet == "":
                broken = self._broken
                self._status('error', t("fsd.send_failed", error=broken)
                             if broken is not None else t("fsd.dropped"))
                return
            if packet and self._handle_packet(packet) is False:
                return

            now = time.time()
            if now >= next_position:
                interval = self._send_position()
                next_position = now + interval
            if self.send_fast and now >= next_fast:
                self._send_fast_position(slow=False)
                next_fast = now + FAST_POSITION_INTERVAL
            if now >= next_slow:
                self._send_fast_position(slow=True)
                next_slow = now + SLOW_FAST_INTERVAL
            if now >= next_config:
                self._broadcast_config()
                next_config = now + CONFIG_BROADCAST_INTERVAL

    def _send_position(self):
        """发一个位置包，返回下一次的间隔。"""
        with self._lock:
            snapshot = self._position
            squawk = self._squawk
            mode = self._xpdr_mode

        if not snapshot:
            return POSITION_INTERVAL

        if time.time() < self._ident_until:
            mode = XPDR_IDENT

        pbh = pack_pbh(snapshot["pitch"], snapshot["bank"], snapshot["heading"],
                       snapshot.get("on_ground", False))
        # 第 7 段是网络高度，最后一段是修正量（气压高度 − 网络高度），两者
        # 相加是应答机报的气压高度。见 altitude.py。
        self._send(
            f"@{mode}:{self.callsign}:{squawk:04d}:{self.rating}:"
            f"{snapshot['latitude']:.5f}:{snapshot['longitude']:.5f}:"
            f"{wire_altitude(snapshot)}:{snapshot['groundspeed']}:{pbh}:"
            f"{snapshot.get('pressure_delta', 0)}")

        # 停在地面上没动就降频，省得刷屏
        if snapshot.get("on_ground") and snapshot.get("groundspeed", 0) < 1:
            return SLOW_POSITION_INTERVAL
        return POSITION_INTERVAL

    def _send_fast_position(self, slow):
        """发一个快速位置包，返回发出的包头（`^` / `#SL` / `#ST`），没发返回 None。

        slow=False 是 200 ms 那一拍：在动发 `^`，停着发 `#ST`。
        slow=True 是 5 秒那一拍：在动才发 `#SL`，停着什么都不发（`@` 照发）。
        """
        with self._lock:
            snapshot = self._position
        if not snapshot:
            return None
        stopped = is_stopped(snapshot)
        if slow:
            if stopped:
                return None
            kind = "#SL"
        else:
            kind = "#ST" if stopped else "^"
        self._send(fast_position_packet(kind, self.callsign, snapshot))
        return kind

    def _set_send_fast(self, enabled):
        """服务端的 $SF。关掉时补一个包，让别人手里的速度是对的。

        xPilot 关掉时总是补一个 `#ST`；在动的飞机收到零速度会停住，等下一个
        `#SL` 再被误差速度拽走，所以在动的时候补 `#SL`。
        """
        if enabled == self.send_fast:
            return
        self.send_fast = enabled
        log.info("fast position updates %s", "on" if enabled else "off")
        if not enabled:
            self._send_fast_position(slow=True) or self._send_stopped()

    def _send_stopped(self):
        with self._lock:
            snapshot = self._position
        if snapshot:
            self._send(fast_position_packet("#ST", self.callsign, snapshot))

    def _read_packet(self, timeout=1):
        """读一个包。超时返回 None，连接关闭返回 ""，空行跳过。"""
        while True:
            while b"\n" not in self._buffer:
                try:
                    self._sock.settimeout(timeout)
                    chunk = self._sock.recv(4096)
                except socket.timeout:
                    return None
                except Exception as e:
                    # 类型和 errno 要进日志：EOF、对端重置、内核放弃重传是三种
                    # 完全不同的掉线，原来这里一声不吭，日志里只剩一句"断开了"。
                    log.info("FSD receive failed: %s (errno %s): %s",
                             type(e).__name__, getattr(e, "errno", None), e)
                    return ""
                if not chunk:
                    log.info("FSD receive: the server closed the connection (EOF)")
                    return ""
                self._buffer += chunk

            line, self._buffer = self._buffer.split(b"\n", 1)
            text = line.decode("utf-8", errors="replace").strip("\r").strip()
            if text:
                return text

    def _handle_packet(self, packet):
        """返回 False 表示应当结束连接。"""
        log.debug("← %s", packet)
        fields = packet.split(":")
        head = fields[0]

        if head.startswith("$ER"):
            code = fields[2] if len(fields) > 2 else "?"
            message = fields[4] if len(fields) > 4 else packet
            if not self._logged_in:
                if code == ERR_CALLSIGN_IN_USE:
                    # 首连也要等：撞上的多半是自己上一条还没死透的连接
                    self._failure = FAILURE_IN_USE
                    self._retryable = True
                elif code == ERR_SERVER_FULL:
                    self._failure = FAILURE_TRANSIENT
                else:
                    # 终态：不能被翻成 reconnecting，界面要知道这条连接没了
                    self._failure = FAILURE_FATAL
                    self._retryable = False
                log.info("login refused with code %s (%s)", code, self._failure)
                self._status('error', t("fsd.rejected", code=code, message=message))
                return False
            log.warning("the server returned an error (%s): %s", code, message)
            return True

        if head.startswith("$AR") and len(fields) >= 4 and fields[2] == "METAR":
            waiter = getattr(self, "_metar_waiter", None)
            if waiter:
                waiter[1] = ":".join(fields[3:]).strip()
                waiter[0].set()
            return True

        if head.startswith("#TM") and len(fields) >= 3:
            sender = head[3:]
            recipient = fields[1]
            body = ":".join(fields[2:])
            if self.on_text:
                try:
                    self.on_text(sender, recipient, body)
                except Exception as e:
                    log.warning("text-message callback raised: %s", e)
            return True

        if head.startswith("%") and len(fields) >= 3:
            # 管制席位的位置包：%呼号:频率:席位类型:可视范围:等级:纬度:经度:0
            self._note_controller(head[1:], fields)
            return True

        if head.startswith("@") and len(fields) >= 9:
            self._note_traffic(fields)
            return True

        # 快速位置。字段个数下限和 can-fsd 的 minFields 一致（^ / #SL 13 段，
        # #ST 7 段），它不够数的包服务端本来就不会转。
        if head.startswith("^") and len(fields) >= 13:
            self._note_fast_traffic(head[1:], fields, stopped=False)
            return True
        if head.startswith("#SL") and len(fields) >= 13:
            self._note_fast_traffic(head[3:], fields, stopped=False)
            return True
        if head.startswith("#ST") and len(fields) >= 7:
            self._note_fast_traffic(head[3:], fields, stopped=True)
            return True

        if head.startswith("$SF"):
            # $SFSERVER:{呼号}:{1|0}（handler.go 的 sendSendFast）
            if len(fields) >= 3 and fields[1].upper() == self.callsign:
                self._set_send_fast(fields[2].strip() == "1")
            return True

        if head.startswith("#SB") and len(fields) >= 3:
            self._handle_plane_info(head[3:], fields)
            return True

        if head.startswith("#DP"):
            # 注意是 is not None：TrafficTable 有 __len__，空表本身是假值
            if self.traffic is not None:
                self.traffic.remove(head[3:])
            return True

        if head.startswith("$CR") and len(fields) >= 3:
            if fields[1] == self.callsign and fields[2] == "CAPS":
                self._logged_in = True
            elif (fields[2] == "ACC" and len(fields) > 3
                    and self.traffic is not None):
                # 本客户端早先版本的回答：$CR 加不包 "config" 的 JSON。标准的
                # 回答走 $CQ，见 _handle_config_query。正文里有冒号，得把后面
                # 的段拼回去。
                try:
                    config = json.loads(":".join(fields[3:]))
                except ValueError:
                    config = None
                if isinstance(config, dict) and isinstance(config.get("config"), dict):
                    config = config["config"]
                if isinstance(config, dict):
                    self.traffic.set_config(head[3:], config)
            return True

        if head.startswith("$CQ") and len(fields) >= 3:
            sender, recipient, query = head[3:], fields[1], fields[2]
            if query == "ACC" and recipient in (self.callsign, RANGED_ALL):
                self._handle_config_query(sender, recipient, ":".join(fields[3:]))
            elif recipient == self.callsign:
                if query == "CAPS":
                    # ACCONFIG=1：xPilot/vPilot 只向报了它的客户端要 ACC
                    self._send(f"$CR{self.callsign}:{sender}:CAPS:"
                               "ATCINFO=0:MODELDESC=1:ACCONFIG=1")
                elif query == "RN":
                    self._send(f"$CR{self.callsign}:{sender}:RN:{self.real_name}::{self.rating}")
            return True

        if head.startswith("$PI") and len(fields) >= 3:
            self._send(f"$PO{self.callsign}:{head[3:]}:{':'.join(fields[2:])}")
            return True

        if head.startswith("#DA") or head.startswith("#DL"):
            if head.startswith("#DA"):
                self._forget_controller(head[3:])
            self._logged_in = True
            return True

        return True

    def _note_traffic(self, fields):
        """别人的位置包。字段顺序和我们自己发的那个一样。

        `@` 包里没有机型，所以第一次见到某架飞机时 TrafficTable 会回调
        request_plane_info() 去问对方要。
        """
        # 必须写 is None：TrafficTable 有 __len__，空表本身是假值，
        # 写成 `if not self.traffic` 会让第一架飞机永远进不来。
        if self.traffic is None:
            return
        callsign = fields[1]
        if callsign == self.callsign:
            return          # 服务器回显了我们自己的包
        try:
            attitude = unpack_pbh(int(fields[8]))
            self.traffic.update_position(
                callsign,
                latitude=float(fields[4]), longitude=float(fields[5]),
                # int(float(...))：有的客户端把高度/地速写成带小数点的，
                # 直接 int() 会抛 ValueError，整个包被丢，那架飞机就是隐形的
                altitude=int(self._adjust_altitude(float(fields[6]))),
                groundspeed=int(float(fields[7])),
                pitch=attitude["pitch"], bank=attitude["bank"],
                heading=attitude["heading"], on_ground=attitude["on_ground"],
                squawk=int(fields[2]), mode=fields[0][1:] or "S")
        except (IndexError, ValueError) as e:
            log.debug("could not parse the position packet %s: %s", fields[:2], e)

    def _note_fast_traffic(self, callsign, fields, stopped):
        """别人的快速位置包（`^` / `#SL` / `#ST`）。

        字段（can-fsd docs/protocol.md "Fast Pilot Position"）：

            0 呼号  1 纬度  2 经度  3 真高（英尺，带小数）  4 离地高（英尺）
            5 PBH  6/7/8 位置速度 X/Y/Z（米每秒）  9/10/11 角速度 X/Y/Z
            （弧度每秒）  12 前轮角（度）

        X 是向东、Y 是向上、Z 是向北——xPilot 发的是 local_vx、local_vy、
        -local_vz（X-Plane 的 +Z 朝南），vPilot 取 MSFS 的 VELOCITY WORLD
        X/Y/Z，同一个方向。角速度按 wire_rotation() 的方向换回抬头/右转/
        右坡为正、度每秒。`#ST` 是停着的飞机，没有六个速度段，速度就是零，
        前轮角在第 6 段。包里没有应答机和地速：地速由水平速度算，应答机沿用
        `@` 包带来的。
        """
        if self.traffic is None:
            return
        if callsign == self.callsign:
            return
        try:
            attitude = unpack_pbh(int(fields[5]) & 0xFFFFFFFF)
            agl = float(fields[4])
            if stopped:
                east = up = north = 0.0
                rotation = (0.0, 0.0, 0.0)
                nose_wheel = float(fields[6])
            else:
                east, up, north = float(fields[6]), float(fields[7]), float(fields[8])
                rotation = (-math.degrees(float(fields[9])),
                            math.degrees(float(fields[10])),
                            -math.degrees(float(fields[11])))
                nose_wheel = float(fields[12])
            self.traffic.update_position(
                callsign,
                latitude=float(fields[1]), longitude=float(fields[2]),
                altitude=self._adjust_altitude(float(fields[3])),
                groundspeed=int(round(math.hypot(north, east) * KNOTS_PER_MPS)),
                pitch=attitude["pitch"], bank=attitude["bank"],
                heading=attitude["heading"], on_ground=attitude["on_ground"],
                velocity=(east, up, north), rotation=rotation,
                agl=agl, nose_wheel=nose_wheel)
        except (IndexError, ValueError) as e:
            log.debug("could not parse the fast position packet %s: %s",
                      fields[:1], e)

    def _adjust_altitude(self, altitude):
        """他机高度按本机温度误差修正后再进运动模型。没有本机快照时不改。"""
        with self._lock:
            own = self._position
        if not own:
            return altitude
        return adjust_incoming_altitude(altitude, own.get("network_altitude"),
                                        own.get("temperature_error", 0))

    def request_plane_info(self, callsign):
        """问对方的机型，用于模型匹配。"""
        return self._send(f"#SB{self.callsign}:{callsign}:PIR")

    def request_config(self, callsign):
        """问对方的配置（灯光/襟翼/起落架），用于动画。TrafficTable 定期触发。

        格式照 can-fsd docs/protocol.md 的 ACC：请求体是 {"request":"full"}，
        xPilot/vPilot 不带它的请求不答。
        """
        return self._send(f'$CQ{self.callsign}:{callsign}:ACC:{{"request":"full"}}')

    def _handle_config_query(self, sender, recipient, body):
        """$CQ … ACC：别人要我们的配置，或者别人发来他的配置。

        标准格式里请求和回答都走 $CQ：请求体是 {"request":"full"}，回答和
        广播是 {"config":{...}}。本客户端早先的版本请求不带正文、回答走
        $CR 且不包 "config"——对它们照旧那样答，那边才解析得了。
        """
        try:
            payload = json.loads(body) if body.strip() else None
        except ValueError:
            log.debug("unreadable ACC payload from %s: %r", sender, body)
            return
        if isinstance(payload, dict) and isinstance(payload.get("config"), dict):
            if self.traffic is not None:
                self.traffic.set_config(sender, payload["config"])
            return
        if recipient != self.callsign:
            return
        if payload is None:
            self._send(f"$CR{self.callsign}:{sender}:ACC:"
                       f"{self._wire_json(self._config())}")
        elif isinstance(payload, dict) and payload.get("request") == "full":
            self._send(f"$CQ{self.callsign}:{sender}:ACC:"
                       f"{self._wire_json({'config': self._config(full=True)})}")

    def _config(self, full=False):
        """自己的配置，键名照 protocol.md 的 ACC，和 TrafficTable.set_config 对齐。

        没有快照（还没连上模拟器）返回 None。
        """
        with self._lock:
            snapshot = dict(self._position) if self._position else None
        if snapshot is None:
            return None
        lights = snapshot.get("lights") or {}
        config = {
            "lights": {key: bool(value) for key, value in lights.items()},
            "engines": {"1": {"on": bool(snapshot.get("engines_on", True)),
                              "is_reversing": False}},
            "gear_down": bool(snapshot.get("gear_down", True)),
            "flaps_pct": int(round(float(snapshot.get("flaps", 0.0)) * 100.0)),
            "spoilers_out": bool(snapshot.get("spoilers", False)),
            "on_ground": bool(snapshot.get("on_ground", False)),
        }
        if full:
            config = {"is_full_data": True, **config}
        return config

    @staticmethod
    def _wire_json(value):
        return json.dumps(value if value is not None else {}, separators=(",", ":"))

    def _broadcast_config(self):
        """配置变了就向附近广播（$CQ…:@94836:ACC），只带变了的键。

        xPilot 就是这样把开灯、放襟翼推给别人的；只靠对方每 10 秒来问一次的话，
        着陆灯要晚十秒才亮。连上后的第一次发全量。
        """
        config = self._config()
        if config is None:
            return
        last = self._config_sent
        if last is None:
            changed = {"is_full_data": True, **config}
        else:
            changed = {key: value for key, value in config.items()
                       if last.get(key) != value}
            if not changed:
                return
            changed = {"is_full_data": False, **changed}
        self._config_sent = config
        self._send(f"$CQ{self.callsign}:{RANGED_ALL}:ACC:"
                   f"{self._wire_json({'config': changed})}")

    def _handle_plane_info(self, sender, fields):
        """#SB。别人问我们机型要答，别人报机型要记下来。"""
        kind = fields[2] if len(fields) > 2 else ""

        if kind == "PIR":
            # 别人问我们。不答的话对方只能拿通用模型画我们。
            reply = f"#SB{self.callsign}:{sender}:PI:GEN:EQUIPMENT={self.aircraft or 'B738'}"
            if self.airline:
                reply += f":AIRLINE={self.airline}"
            self._send(reply)
            return

        if self.traffic is None:
            return

        if kind == "PI" and len(fields) > 3 and fields[3] == "GEN":
            # 键值对顺序不保证，出现与否也不保证（protocol.md 明说了）
            info = {}
            for field in fields[4:]:
                key, _, value = field.partition("=")
                name = {"EQUIPMENT": "equipment", "AIRLINE": "airline",
                        "LIVERY": "livery", "CSL": "csl"}.get(key.upper())
                if name and value:
                    info[name] = value
            if info:
                self.traffic.set_plane_info(sender, **info)
            return

        if kind == "PI" and len(fields) > 3 and fields[3] == "X":
            # 老式：#SB发方:收方:PI:X:0:发动机类型:CSL=名字（有的客户端写成 ~名字）
            for field in fields[4:]:
                if field.upper().startswith("CSL="):
                    self.traffic.set_plane_info(sender, csl=field[4:])
                elif field.startswith("~"):
                    self.traffic.set_plane_info(sender, csl=field[1:])

    def _note_controller(self, callsign, fields):
        """记下一个在线管制席位，界面用来列附近频率。"""
        try:
            frequency = fields[1]
            # 协议里频率是 5 位，开头的 1 和小数点是隐含的：28500 → 128.500
            display = f"1{frequency[:2]}.{frequency[2:]}" if len(frequency) == 5 else frequency
            entry = {"callsign": callsign, "frequency": display,
                     "facility": int(fields[2]) if len(fields) > 2 else 0,
                     "seen": time.time()}
        except (IndexError, ValueError):
            return
        self.controllers[callsign] = entry
        if self.on_controllers:
            try:
                self.on_controllers(list(self.controllers.values()))
            except Exception as e:
                log.warning("controller-list callback raised: %s", e)

    def _forget_controller(self, callsign):
        if self.controllers.pop(callsign, None) and self.on_controllers:
            try:
                self.on_controllers(list(self.controllers.values()))
            except Exception as e:
                log.warning("controller-list callback raised: %s", e)

    def _close(self):
        if self._sock:
            try:
                self._send(f"#DP{self.callsign}:{self.cid}")
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._logged_in = False
        self._status('stopped', t("fsd.stopped"))
