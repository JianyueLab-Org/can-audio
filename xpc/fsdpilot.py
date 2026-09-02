"""以飞行员身份连接 FSD 服务端（can-fsd）。

对应 xPilot 的 fsd 层。协议逐条对照 can-fsd 的解析代码
（internal/fsd/conn.go、handler.go、packet.go）和 docs/protocol.md：

    登录   $ID{呼号}:SERVER:{客户端ID}:{客户端名}:{主}:{次}:{CID}:{机器码}
           #AP{呼号}:SERVER:{CID}:{密码}:{等级}:{协议版本}:{模拟器}:{真实姓名}
    位置   @{应答机模式}:{呼号}:{squawk}:{等级}:{纬度}:{经度}:{高度}:{地速}:{PBH}:{气压差}
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
import socket
import threading
import time

# _status() 的消息会直接进消息区，是界面文字，所以要过 i18n。日志那一半仍然
# 是英文——分界线在"这句话最后到哪儿去"，不在它写在哪个模块里。
from i18n import t

log = logging.getLogger("fsd")

DEFAULT_PORT = 6809
PROTO_REVISION = 100          # ProtoRevisionClassic
RATING_OBSERVER = 1
POSITION_INTERVAL = 0.2       # 每秒 5 次，和 VATSIM 客户端一致
SLOW_POSITION_INTERVAL = 5.0  # 停在地面上没动时降频
LOGIN_TIMEOUT = 10.0
# 已经登录过之后掉线，最多再试这么多次；都失败就整个下线。和语音那边同一条
# 策略（voice.RECONNECT_LIMIT），两条链路的行为要一致，不然"整个下线"就没有
# 统一的含义。
RECONNECT_LIMIT = 3
RECONNECT_DELAY = 3.0         # 每次重连之间等一下，别贴着服务器猛敲
# can-fsd 的 IsValidCallsign 上限（packet.go 的 MaxCallsignLength）
MAX_CALLSIGN_LENGTH = 12

CLIENT_ID = "0001"
CLIENT_NAME = "XPC for CAN"
CLIENT_MAJOR = 1
CLIENT_MINOR = 0

# 模拟器编号，取自 can-fsd 的 docs/enumerations.md。原来写的 8 是
# "Microsoft Flight Simulator 2004"——X-Plane 客户端一直在谎报模拟器。
# 我们没有从链路上判断 X-Plane 大版本，按 12 报（11 是 15）。
SIMULATOR_XPLANE_11 = 15
SIMULATOR_XPLANE_12 = 16
SIMULATOR = SIMULATOR_XPLANE_12

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

    掉线相关的两条状态和语音那边一个意思：`reconnecting` 是暂时的，还在试；
    `offline` 是 RECONNECT_LIMIT 次都失败、这条链路彻底完了，界面应当整个下线。
    """

    def __init__(self, host, callsign, cid, password, real_name="",
                 port=DEFAULT_PORT, rating=RATING_OBSERVER, aircraft="",
                 on_status=None, on_text=None, on_controllers=None,
                 traffic=None, reconnect_limit=RECONNECT_LIMIT):
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
        # 掉线后最多重连几次，用尽就整个下线
        self.reconnect_limit = int(reconnect_limit)
        self.gave_up = False
        # 登录成功过之后，失败就先当"可以重连"。这个标记让 _status 把中途的
        # error 翻成 reconnecting——否则界面收到一次 error 就把整条连接当没了，
        # 而我们其实马上就要再试。
        self._retryable = False

        self._lock = threading.Lock()
        self._position = None       # 最近一次从模拟器拿到的快照
        self._squawk = 2000
        self._xpdr_mode = XPDR_STANDBY
        self._ident_until = 0.0
        self.controllers = {}       # 呼号 -> {frequency, ...}

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
                # xplane.xpdr_mode() 已经判过了，这里只有 2（在线）和 1（待机）
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
        if not self._sock:
            return False
        try:
            self._sock.sendall((packet + "\r\n").encode("utf-8", errors="replace"))
            log.debug("→ %s", self._redact(packet))
            return True
        except Exception as e:
            self._status('error', t("fsd.send_failed", error=e))
            return False

    def _run(self):
        """连接 → 收发 → 掉线重连，最多 reconnect_limit 次。

        **首次连不上不重试。** 那多半是呼号被占、密码不对或者地址填错——重试三
        次只会把同一条错误刷三遍，还可能触发服务端对认证失败的限流。只有"登录
        成功过之后掉的线"才重连：那种是服务器重启或者网络抖动，重连是对的。

        次数用尽后报 offline 并结束，界面据此整个下线，而不是留一条谁也说不清
        状态的连接。
        """
        attempts = 0
        established_once = False

        while self.running and not self.stop_event.is_set():
            connected = False
            try:
                connected = self._connect()
                if connected:
                    attempts = 0
                    established_once = True
                    # 从这一刻起，掉线是可以重连的
                    self._retryable = True
                    self._loop()
            except Exception as e:
                self._status('error', t("fsd.exception", error=e))
            finally:
                self._close()

            if not established_once:
                return                  # 首次就没连上，原因已经报过了
            if not self.running or self.stop_event.is_set():
                return                  # 用户自己断的

            attempts += 1
            if attempts > self.reconnect_limit:
                self.gave_up = True
                self._retryable = False
                self._status('offline',
                             t("fsd.give_up", limit=self.reconnect_limit))
                return

            self._status('reconnecting',
                         t("fsd.reconnecting", attempt=attempts,
                           limit=self.reconnect_limit))
            if self.stop_event.wait(RECONNECT_DELAY):
                return

    def _connect(self):
        problem = callsign_problem(self.callsign)
        if problem:
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
        next_position = 0.0
        while self.running and not self.stop_event.is_set():
            packet = self._read_packet(timeout=0.2)
            if packet == "":
                self._status('error', t("fsd.dropped"))
                return
            if packet and self._handle_packet(packet) is False:
                return

            now = time.time()
            if now >= next_position:
                interval = self._send_position()
                next_position = now + interval

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
        # 最后一个字段是气压修正量：高度字段报的是真高，加上它才是应答机报的
        # 气压高度。老代码这里写死 0，于是管制端看到的高度就是真高，和座舱
        # 高度表能差一千英尺。取不到就还是 0，退回原来的行为。
        self._send(
            f"@{mode}:{self.callsign}:{squawk:04d}:{self.rating}:"
            f"{snapshot['latitude']:.5f}:{snapshot['longitude']:.5f}:"
            f"{snapshot['altitude']}:{snapshot['groundspeed']}:{pbh}:"
            f"{snapshot.get('pressure_delta', 0)}")

        # 停在地面上没动就降频，省得刷屏
        if snapshot.get("on_ground") and snapshot.get("groundspeed", 0) < 1:
            return SLOW_POSITION_INTERVAL
        return POSITION_INTERVAL

    def _read_packet(self, timeout=1):
        """读一个包。超时返回 None，连接关闭返回 ""，空行跳过。"""
        while True:
            while b"\n" not in self._buffer:
                try:
                    self._sock.settimeout(timeout)
                    chunk = self._sock.recv(4096)
                except socket.timeout:
                    return None
                except Exception:
                    return ""
                if not chunk:
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
                # 对方回的配置 JSON。正文里有冒号，得把后面的段拼回去。
                try:
                    config = json.loads(":".join(fields[3:]))
                except ValueError:
                    config = None
                if isinstance(config, dict):
                    self.traffic.set_config(head[3:], config)
            return True

        if head.startswith("$CQ") and len(fields) >= 3:
            sender, recipient, query = head[3:], fields[1], fields[2]
            if recipient == self.callsign:
                if query == "CAPS":
                    self._send(f"$CR{self.callsign}:{sender}:CAPS:ATCINFO=0:MODELDESC=1")
                elif query == "RN":
                    self._send(f"$CR{self.callsign}:{sender}:RN:{self.real_name}::{self.rating}")
                elif query == "ACC":
                    # 回配置 JSON（灯光/襟翼/起落架），对面拿去驱动动画。
                    # 以前这里回的是机型码——和请求方期望的负载完全对不上，
                    # 结果就是所有他机永远全程关灯。
                    self._send(f"$CR{self.callsign}:{sender}:ACC:{self._config_json()}")
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
                altitude=int(float(fields[6])), groundspeed=int(float(fields[7])),
                pitch=attitude["pitch"], bank=attitude["bank"],
                heading=attitude["heading"], on_ground=attitude["on_ground"],
                squawk=int(fields[2]), mode=fields[0][1:] or "S")
        except (IndexError, ValueError) as e:
            log.debug("could not parse the position packet %s: %s", fields[:2], e)

    def request_plane_info(self, callsign):
        """问对方的机型，用于模型匹配。"""
        return self._send(f"#SB{self.callsign}:{callsign}:PIR")

    def request_config(self, callsign):
        """问对方的配置（灯光/襟翼/起落架），用于动画。TrafficTable 定期触发。"""
        return self._send(f"$CQ{self.callsign}:{callsign}:ACC")

    def _config_json(self):
        """把自己的快照攒成 ACC 回复的 JSON，键名和 TrafficTable.set_config 对齐。"""
        with self._lock:
            snapshot = dict(self._position) if self._position else {}
        lights = snapshot.get("lights") or {}
        return json.dumps({
            "gear_down": bool(snapshot.get("gear_down", True)),
            "flaps_pct": round(float(snapshot.get("flaps", 0.0)) * 100.0, 1),
            "spoilers_out": bool(snapshot.get("spoilers", False)),
            "lights": {key: bool(value) for key, value in lights.items()},
            "engines": {"1": {"on": bool(snapshot.get("engines_on", True))}},
        }, separators=(",", ":"))

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
