"""X-Plane 数据链路。

对应 xPilot 的 simulator 层：把机位、姿态、速度、无线电和应答机从 X-Plane 取
出来，供 FSD 位置包和语音频率使用。

和 xplane_client 里那个一问一答的读法不同，这里是**订阅**：一次 RREF 请求把
所有需要的 dataref 按频率订上，之后 X-Plane 会持续推过来，本地只保留最新值。
飞行员客户端每秒要发好几次位置包，一次一问一答的往返扛不住。

    发现   监听多播 239.255.1.1:49707 的 BECN 信标，拿到 X-Plane 的地址和端口
    订阅   RREF 请求，freq 指定每秒推送次数，index 是我们自己编的号
    接收   RREF 回包里是 (index, float32) 对，按 index 对回 dataref
"""

import logging
import socket
import struct
import threading
import time

import altitude as altitude_model
from i18n import t

log = logging.getLogger("sim")

MCAST_GROUP = "239.255.1.1"
MCAST_PORT = 49707
DISCOVER_TIMEOUT = 10
DEFAULT_PORT = 49000
UPDATE_RATE = 5                 # 每个 dataref 每秒推送次数
STALE_AFTER = 3.0               # 超过这么久没有新数据就认为断了
REDISCOVER_AFTER = 15.0         # 还是没有的话，重新去找一次 X-Plane
BEACON_GATHER = 1.0             # 收到第一份信标后再等这么久，收齐其他网卡的

# 这些网段几乎都是虚拟网卡：VPN、WSL、Hyper-V、Docker。信标会从它们身上也回
# 来一份，但往那边发 RREF 收不到任何数据。排序时排在最后。
VIRTUAL_PREFIXES = ("198.18.", "198.19.", "172.17.", "172.18.", "172.19.",
                    "172.20.", "169.254.", "10.211.", "10.37.")


def _address_rank(ip):
    """给发现到的地址排个优先级，小的优先。

    本机最优（同机跑 X-Plane 是最常见的情形，而且必然通）；其次是普通局域网
    地址；已知的虚拟网卡段排最后——实测里 198.18.0.1 就是这么混进来的，选中
    它之后一个 dataref 都收不到。
    """
    if ip.startswith("127."):
        return 0
    if any(ip.startswith(prefix) for prefix in VIRTUAL_PREFIXES):
        return 2
    return 1

# 我们订阅的 dataref。索引是发给 X-Plane 的编号，回包按它对应回来。
DATAREFS = {
    "latitude":     "sim/flightmodel/position/latitude",
    "longitude":    "sim/flightmodel/position/longitude",
    # 真高（米），FSD 要英尺
    "elevation":    "sim/flightmodel/position/elevation",
    "agl":          "sim/flightmodel/position/y_agl",
    # 海平面气压（inHg）。X-Plane 11 用它算气压高度，见 altitude.xplane_altitudes()。
    "sea_level_baro": "sim/weather/barometer_sealevel_inhg",
    # 这两个只有 X-Plane 12 有：气压高度（英尺）和高度表温度误差（英尺）。
    # 不存在的 dataref X-Plane 不推送，推了就是 12，不按版本分支。
    "pressure_altitude": "sim/flightmodel2/position/pressure_altitude",
    "temperature_error": "sim/weather/aircraft/altimeter_temperature_error",
    "groundspeed":  "sim/flightmodel/position/groundspeed",
    "pitch":        "sim/flightmodel/position/theta",
    "bank":         "sim/flightmodel/position/phi",
    "heading_true": "sim/flightmodel/position/psi",
    "squawk":       "sim/cockpit/radios/transponder_code",
    "xpdr_mode":    "sim/cockpit/radios/transponder_mode",
    # 0.001 MHz 精度，支持 8.33 kHz 间隔。X-Plane 11.30 起才有。
    "com1":         "sim/cockpit2/radios/actuators/com1_frequency_hz_833",
    "com2":         "sim/cockpit2/radios/actuators/com2_frequency_hz_833",
    # 老的 dataref，0.01 MHz 精度。两个一起订，谁回就用谁——不存在的 dataref
    # X-Plane 只是不推送，不会报错，所以不需要按版本分支。
    "com1_legacy":  "sim/cockpit/radios/com1_freq_hz",
    "com2_legacy":  "sim/cockpit/radios/com2_freq_hz",
    "com1_power":   "sim/cockpit2/radios/actuators/com1_power",
    "on_ground":    "sim/flightmodel/failures/onground_any",
    # 快速位置包（`^` / `#SL`）要的速度，米每秒。OpenGL 局部坐标：+x 东、
    # +y 上、+z **南**，所以向北是 -local_vz（xPilot 也是这么发的）。
    "local_vx":     "sim/flightmodel/position/local_vx",
    "local_vy":     "sim/flightmodel/position/local_vy",
    "local_vz":     "sim/flightmodel/position/local_vz",
    # 机体角速度，度每秒：Q 俯仰（抬头为正）、R 偏航（右转为正）、P 滚转
    # （右坡为正）
    "pitch_rate":   "sim/flightmodel/position/Q",
    "heading_rate": "sim/flightmodel/position/R",
    "bank_rate":    "sim/flightmodel/position/P",
    # 前轮转角，度（xPilot 用的同一个）
    "nose_wheel":   "sim/flightmodel2/gear/tire_steer_actual_deg[0]",
    # 下面这些是报给别人做动画用的（ACC），dataref 和 xPilot 的 XplaneAdapter
    # 用的同一套
    "light_beacon":  "sim/cockpit/electrical/beacon_lights_on",
    "light_landing": "sim/cockpit/electrical/landing_lights_on",
    "light_taxi":    "sim/cockpit/electrical/taxi_light_on",
    "light_strobe":  "sim/cockpit/electrical/strobe_lights_on",
    "light_nav":     "sim/cockpit/electrical/nav_lights_on",
    "gear":          "sim/cockpit/switches/gear_handle_status",
    "flaps":         "sim/flightmodel/controls/flaprat",
    "speedbrake":    "sim/flightmodel2/controls/speedbrake_ratio",
    "engine_on":     "sim/flightmodel/engine/ENGN_running[0]",
}
# 扰流板比例超过这个值才算"放出"
SPOILERS_OUT_RATIO = 0.1

INDEX_TO_NAME = {index: name for index, name in enumerate(DATAREFS)}
NAME_TO_INDEX = {name: index for index, name in INDEX_TO_NAME.items()}

METRES_PER_FOOT = 0.3048
KNOTS_PER_MPS = 1.9438444924406

# snapshot 里 xpdr_mode 的两个取值。fsdpilot 拿 >= 2 判在线，见 xpdr_mode()。
XPDR_ONLINE = 2
XPDR_STANDBY = 1
# 判"停着"的地速门槛（节）。和 fsdpilot 降低位置包频率用的是同一条线。
PARKED_SPEED_KT = 1

def xpdr_mode(raw_mode, on_ground, groundspeed_kt):
    """transponder_mode dataref -> 位置包要的应答机模式。

    dataref 的取值是 0 关 1 待机 2 开 3 测试/C，所以 >= 2 算在线。读不到（这一
    轮 RREF 还没推过来）当在线：默认值原来写的是 0，也就是"关"，等于在拿不准的
    时候主动把自己从管制端的标牌上抹掉，方向反了。

    待机和关只在飞机**确实停着**的时候才当真。冷舱的飞机不该在雷达上是个亮着
    的 C 模式目标，而冷舱恰恰就是"停在机坪上没动"这一种情况——按这条线判，那
    个意图一点没丢。一架已经在滑行或者已经离地的飞机还报待机，对管制没有任何
    好处：待机在 FSD 位置包里是包头的 `@S`，EuroScope 收到就当成一个没有 C 模式
    的目标，标牌上的**高度和地速会一起空掉**。

    和 msfs/simlink.py 的 xpdr_mode() 是同一条规则的两份实现——两个客户端的
    snapshot() 逐字段一致是刻意的，这一项也不例外。
    """
    if raw_mode is None:
        return XPDR_ONLINE
    try:
        raw_mode = int(raw_mode)
    except (TypeError, ValueError):
        return XPDR_ONLINE
    if raw_mode >= 2:
        return XPDR_ONLINE
    if on_ground and groundspeed_kt < PARKED_SPEED_KT:
        return XPDR_STANDBY
    return XPDR_ONLINE


class XPlaneLink:
    """和 X-Plane 的一条 UDP 链路。

    on_state(connected, message) 在后台线程调用。
    """

    def __init__(self, on_state=None):
        self.on_state = on_state
        self.address = None
        self._known_good = None     # 真的回过数据的地址，最可信
        self._last_discovered = None    # 最近一次信标发现到的地址
        self.values = {}            # dataref 名 -> 最新值
        self.last_update = 0.0
        self.running = False

        self._socket = None
        self._thread = None
        self._lock = threading.Lock()
        self._connected = False

    # ---------- 状态 ----------
    def _state(self, connected, message):
        if connected != self._connected:
            self._connected = connected
            log.info("%s: %s", "connected" if connected else "disconnected", message)
            if self.on_state:
                try:
                    self.on_state(connected, message)
                except Exception as e:
                    log.warning("status callback raised: %s", e)

    @property
    def connected(self):
        return self._connected and (time.time() - self.last_update) < STALE_AFTER

    # ---------- 生命周期 ----------
    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)
        self._thread = None
        self._close()

    def _close(self):
        if self._socket:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None

    # ---------- 发现 ----------
    def discover(self, timeout=DISCOVER_TIMEOUT):
        """等 X-Plane 的多播信标。返回 (ip, port) 或 None。

        一台机器上装了 VPN 或虚拟网卡时，同一个信标会从多个网卡各收到一份，
        源地址各不相同（实测见过 198.18.0.1 这种 CGNAT 段的虚拟网卡地址）。
        只取先到的那份等于抽签，抽中虚拟网卡就永远收不到数据。所以在整个超时
        窗口里把能收到的都收下来，再按"哪个地址更可能真的通"来挑。
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        candidates = []
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", MCAST_PORT))
            mreq = struct.pack("4sl", socket.inet_aton(MCAST_GROUP), socket.INADDR_ANY)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

            deadline = time.time() + timeout
            # 收到第一份之后再多等一会儿，好把其他网卡上的同一个信标也收齐
            while time.time() < deadline:
                sock.settimeout(max(0.1, min(BEACON_GATHER, deadline - time.time())))
                try:
                    data, addr = sock.recvfrom(1500)
                except socket.timeout:
                    if candidates:
                        break
                    continue
                except OSError:
                    break
                found = self._parse_beacon(data, addr)
                if found and found not in candidates:
                    candidates.append(found)
                    deadline = min(deadline, time.time() + BEACON_GATHER)
        except OSError:
            return None
        finally:
            sock.close()

        if not candidates:
            return None
        best = min(candidates, key=lambda a: _address_rank(a[0]))
        if len(candidates) > 1:
            log.info("beacon arrived on %d interfaces %s, using %s",
                     len(candidates), [a[0] for a in candidates], best[0])
        else:
            log.info("found X-Plane at %s:%s", best[0], best[1])
        return best

    @staticmethod
    def _parse_beacon(data, addr):
        if data[:5] != b"BECN\x00":
            log.debug("unknown beacon %r", data[:5])
            return None
        try:
            # 这两个字节是**信标协议**的版本，不是 X-Plane 的版本号——早先按
            # "X-Plane v1.2" 打进日志是错的，会让人以为装了个远古版本。
            _, _, _, _, _, _, port = struct.unpack_from("=5sBBiiIH", data)
        except struct.error:
            return None
        return (addr[0], port)

    # ---------- 订阅 ----------
    def _subscribe(self, sock, address, rate=UPDATE_RATE):
        """把所有 dataref 订上。rate=0 表示退订。"""
        for name, dataref in DATAREFS.items():
            packet = struct.pack("=5sii", b"RREF\x00", rate, NAME_TO_INDEX[name])
            packet += dataref.encode() + b"\x00"
            packet = packet.ljust(413, b"\x00")
            try:
                sock.sendto(packet, address)
            except OSError as e:
                log.warning("could not subscribe to %s: %s", dataref, e)

    def _run(self):
        while self.running:
            address = self.address or self.discover(timeout=5)
            if not address:
                # 这一轮没收到信标。以前无脑退回本机，结果是：明明发现过
                # 192.168.31.231，等 15 秒没数据（X-Plane 还在读盘）就把它扔了，
                # 下一轮退回 127.0.0.1，再等 15 秒，来回折腾了 8 分钟。
                # 收过数据的地址最可信，其次是上一次发现到的。
                address = (self._known_good or self._last_discovered
                           or ("127.0.0.1", DEFAULT_PORT))

            self._close()
            try:
                self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self._socket.bind(("0.0.0.0", 0))
                self._socket.settimeout(1.0)
            except OSError as e:
                self._state(False, t("sim.port_error", error=e))
                time.sleep(2)
                continue

            self._subscribe(self._socket, address)
            self.address = address
            if address != self._known_good:
                self._last_discovered = address
            log.info("subscribed to %d datarefs at %s:%s", len(DATAREFS), address[0],
                     address[1])

            got_data = False
            silent_since = time.time()
            while self.running:
                try:
                    data, _ = self._socket.recvfrom(2048)
                except socket.timeout:
                    if not self._still_waiting(silent_since):
                        break
                    continue
                except ConnectionResetError:
                    # Windows 上向没人监听的端口发 UDP 会回一个 ICMP 不可达，
                    # 下一次 recvfrom 就抛这个。X-Plane 还没起来时就是这种情况，
                    # 当成超时接着等——按 OSError 处理会一秒重订一次。
                    if not self._still_waiting(silent_since):
                        break
                    time.sleep(0.2)
                    continue
                except OSError:
                    break

                if self._handle(data):
                    silent_since = time.time()
                    if not got_data:
                        got_data = True
                        # 这个地址真的有数据回来，记住它——以后信标收不到时
                        # 优先用它，不要退回本机瞎试
                        self._known_good = address
                        self._state(True, t("sim.link_up", address=address[0]))

            self._close()
            if self.running:
                time.sleep(1)

    def _still_waiting(self, silent_since):
        """一直没数据的时候该继续等还是换个地址重来。"""
        silent = time.time() - silent_since
        if silent > STALE_AFTER:
            self._state(False, t("sim.no_data"))
        if silent > REDISCOVER_AFTER:
            self.address = None          # 重新发现一次
            return False
        return True

    def _handle(self, data):
        """解析一个 RREF 回包，返回是否有有效数据。"""
        if len(data) < 13 or data[:4] != b"RREF":
            return False
        body = len(data) - 5
        if body % 8:
            return False

        updates = {}
        for i in range(body // 8):
            index, value = struct.unpack_from("=if", data, 5 + i * 8)
            name = INDEX_TO_NAME.get(index)
            if name:
                updates[name] = value
        if not updates:
            return False

        with self._lock:
            self.values.update(updates)
            self.last_update = time.time()
        return True

    # ---------- 取值 ----------
    def snapshot(self):
        """当前这一份数据，已经换算成 FSD 要的单位。"""
        with self._lock:
            raw = dict(self.values)
            last_update = self.last_update
        if not raw:
            return None
        # Do not let the FSD client keep broadcasting the last frame after
        # X-Plane has stopped producing samples.  A zero timestamp is kept as
        # a fresh value for callers that preload a snapshot in tests/tools.
        if last_update and time.time() - last_update >= STALE_AFTER:
            return None

        elevation = raw.get("elevation", 0.0) / METRES_PER_FOOT
        altitude = int(round(elevation))
        sea_level = raw.get("sea_level_baro")
        network, pressure, temperature_error = altitude_model.xplane_altitudes(
            elevation,
            sea_level * altitude_model.INHG_TO_HPA if sea_level else None,
            raw.get("pressure_altitude"), raw.get("temperature_error"))
        network = int(round(network))
        pressure = int(round(pressure))
        groundspeed = int(round(raw.get("groundspeed", 0.0) * KNOTS_PER_MPS))
        on_ground = bool(raw.get("on_ground", 0))
        return {
            "latitude": raw.get("latitude", 0.0),
            "longitude": raw.get("longitude", 0.0),
            # 真高。网络高度、气压高度和修正量的算法见 altitude.py。
            "altitude": altitude,
            "network_altitude": network,
            "pressure_altitude": pressure,
            "temperature_error": int(round(temperature_error)),
            "pressure_delta": pressure - network,
            "agl": int(round(raw.get("agl", 0.0) / METRES_PER_FOOT)),
            "groundspeed": groundspeed,
            "pitch": raw.get("pitch", 0.0),
            "bank": raw.get("bank", 0.0),
            "heading": raw.get("heading_true", 0.0) % 360.0,
            "squawk": int(raw.get("squawk", 2000)),
            # 待机只在飞机确实停着的时候当真，理由见 xpdr_mode()。
            "xpdr_mode": xpdr_mode(raw.get("xpdr_mode"), on_ground, groundspeed),
            "com1": self._frequency(raw.get("com1"), raw.get("com1_legacy")),
            "com2": self._frequency(raw.get("com2"), raw.get("com2_legacy")),
            "com1_power": bool(raw.get("com1_power", 1)),
            "on_ground": on_ground,
            # 快速位置包用：世界速度（东/上/北）米每秒，机体角速度度每秒
            "velocity_east": raw.get("local_vx", 0.0),
            "velocity_up": raw.get("local_vy", 0.0),
            "velocity_north": -raw.get("local_vz", 0.0),
            "pitch_rate": raw.get("pitch_rate", 0.0),
            "heading_rate": raw.get("heading_rate", 0.0),
            "bank_rate": raw.get("bank_rate", 0.0),
            "nose_wheel": raw.get("nose_wheel", 0.0),
            # 下面这些是报给别人做动画用的，键名和 msfs/simlink.py 一致
            "gear_down": bool(raw.get("gear", 1)),
            "flaps": max(0.0, min(1.0, raw.get("flaps", 0.0))),
            "spoilers": raw.get("speedbrake", 0.0) > SPOILERS_OUT_RATIO,
            "engines_on": bool(raw.get("engine_on", 1)),
            "lights": {
                "beacon_on": bool(raw.get("light_beacon", 0)),
                "landing_on": bool(raw.get("light_landing", 0)),
                "taxi_on": bool(raw.get("light_taxi", 0)),
                "strobe_on": bool(raw.get("light_strobe", 0)),
                "nav_on": bool(raw.get("light_nav", 0)),
            },
        }

    @staticmethod
    def _frequency(precise, legacy=None):
        """COM 频率（MHz）。优先 8.33 那个，没有就用老的。

        _833 的单位是 kHz，除以 1000 得兆赫，能表示 8.33 间隔（132.005）。
        老的那个单位是 10 kHz，除以 100 得兆赫，只有 0.01 MHz 精度——X-Plane
        11.30 以前只有它，8.33 的频道会被舍到最近的 25 kHz。
        """
        if precise and precise > 0:
            return round(float(precise) / 1000.0, 3)
        if legacy and legacy > 0:
            return round(float(legacy) / 100.0, 3)
        return None
