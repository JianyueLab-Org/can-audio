"""MSFS 数据链路。

对应 xpc/xplane.py，换成 SimConnect。作用完全一样：把机位、姿态、速度、无线电
和应答机取出来，`snapshot()` 返回的字典和 xpc 那边**逐字段一致**——FSD 位置包和
语音频道那两层因此可以原样复用。

和 X-Plane 的区别：

    X-Plane   自己发 UDP，订阅 dataref，靠 BECN 信标发现
    MSFS      SimConnect（本机命名管道/共享内存），一问一答取 SimVar

SimConnect 只能连本机的模拟器，没有发现过程；模拟器没开时 `SimConnect()` 直接
抛异常，所以重连逻辑是"隔几秒再试一次构造"，而不是 xpc 那种超时判定。

单位：SimVar 里角度是**弧度**，高度已经是英尺，速度有专门的 knots 变量。频率
用 `COM ACTIVE FREQUENCY:1`，单位 MHz，直接可用——不像 X-Plane 要分两个精度的
dataref 讨论。
"""

import logging
import math
import threading
import time

from i18n import t

log = logging.getLogger("sim")

# _poll 的三种结果。"没进飞行"和"连接断了"要分开——前者是常态，重开连接
# 只会在日志里刷一串 SIM OPEN。
OK = "ok"
NO_DATA = "no-data"
FAILED = "failed"

RETRY_INTERVAL = 3.0        # 连不上时隔多久再试
STALE_AFTER = 3.0           # 超过这么久取不到数据就认为断了
POLL_INTERVAL = 0.2         # 取数据的间隔，和位置包的 5 Hz 对齐

# snapshot 里 xpdr_mode 的两个取值。fsdpilot 拿 >= 2 判在线，见 xpdr_mode()。
XPDR_ONLINE = 2
XPDR_STANDBY = 1
# 判"停着"的地速门槛（节）。和 fsdpilot 降低位置包频率用的是同一条线。
PARKED_SPEED_KT = 1

# SimVar 名字 -> 我们内部的键。值都通过 AircraftRequests.get() 取。
# 名字必须和 SimConnect 的 SimVar 完全一致，写错了 get() 返回 None。
# 单位不是猜的：Python-SimConnect 的 RequestList.py 为每个 SimVar 写死了请求
# 单位。经纬度它按 Degrees 要，姿态和航向按 Radians 要——名字里带 DEGREES 的
# 那几个反而是弧度。照着它来，不要照 SimVar 的原生单位来。
SIMVARS = {
    "latitude": "PLANE_LATITUDE",                  # 度（已经是度，不要再转）
    "longitude": "PLANE_LONGITUDE",                # 度（同上）
    "altitude": "PLANE_ALTITUDE",                  # 英尺（真高，不是高度表读数）
    "agl": "PLANE_ALT_ABOVE_GROUND",               # 英尺
    # 只用来算位置包最后那个气压修正量，见 pressure_delta()。
    # 别改用 PRESSURE_ALTITUDE：RequestList.py 给它写的单位是 **Meters**，
    # 这一堆高度里就它一个不是英尺，直接当英尺用会差 3.28 倍。
    "indicated_altitude": "INDICATED_ALTITUDE",    # 英尺
    "baro_setting": "KOHLSMAN_SETTING_HG",         # inHg
    "groundspeed": "GROUND_VELOCITY",              # 节
    "pitch": "PLANE_PITCH_DEGREES",                # 弧度（名字骗人）
    "bank": "PLANE_BANK_DEGREES",                  # 弧度（名字同样骗人）
    "heading": "PLANE_HEADING_DEGREES_TRUE",       # 弧度
    "squawk": "TRANSPONDER_CODE:1",                # BCD
    # 0 关 1 待机 2 测试 3 开 4 高度(C) 5 地面。读不到就当在线（老行为）。
    "xpdr_state": "TRANSPONDER_STATE:1",
    "com1": "COM_ACTIVE_FREQUENCY:1",              # MHz
    "com2": "COM_ACTIVE_FREQUENCY:2",              # MHz
    "on_ground": "SIM_ON_GROUND",
    "engine_on": "GENERAL_ENG_COMBUSTION:1",
    "gear": "GEAR_HANDLE_POSITION",
    "flaps": "TRAILING_EDGE_FLAPS_LEFT_PERCENT",
    "spoilers": "SPOILERS_HANDLE_POSITION",
    "light_beacon": "LIGHT_BEACON",
    "light_landing": "LIGHT_LANDING",
    "light_taxi": "LIGHT_TAXI",
    "light_strobe": "LIGHT_STROBE",
    "light_nav": "LIGHT_NAV",
}

# 每一轮都读的那几个：位置包里会动的量。其余的（无线电、应答机、灯光、襟翼……）
# 轮流读，每轮读 SLOW_PER_POLL 个。
#
# 原因在 Python-SimConnect 的读法：AircraftRequests.get() 每个 SimVar 都是
# 一次独立的 RequestDataOnSimObjectType，然后 `time.sleep(.01)` 轮询到结果
# 回来（SimConnect.py 的 get_data）。Windows 上 sleep 的粒度是 15.6 ms，所以
# 一个 SimVar 至少一两个 tick；二十五个串着读，一轮要 0.4～0.8 秒，模拟器
# 一卡更久。位置包 5 Hz，于是连着好几个包是同一份快照。只把会动的九个放进
# 每一轮，一轮的读数从 25 个降到 9 + 4 个。
#
# indicated_altitude 也在这里：pressure_delta() 拿它和真高相减，两个不是同一
# 时刻读的话，爬升中每落后一秒就差出一截垂直速度。
FAST_SIMVARS = ("latitude", "longitude", "altitude", "indicated_altitude",
                "groundspeed", "pitch", "bank", "heading", "on_ground")
SLOW_SIMVARS = tuple(name for name in SIMVARS if name not in FAST_SIMVARS)
SLOW_PER_POLL = 4


# 标准大气压，高度表拨到这个值时"指示高度"就是气压高度。
STANDARD_PRESSURE_INHG = 29.92
# 高度表窗口能拨到的范围。超出这个范围的值一定是没读到（0）或者读错了，
# 拿它去算修正量会把飞机在雷达上挪三万英尺，所以宁可不修正。
BARO_RANGE = (25.0, 32.0)


def pressure_delta(indicated_ft, baro_inhg, true_altitude_ft):
    """位置包最后一个字段：气压高度减真高，英尺。

    和 xpc/xplane.py 里那份是同一套算法，只是数据来自 SimConnect。

    应答机报的是**气压高度**（高度表拨 29.92 时读到的数），而位置包第 7 个
    字段报的是**真高**——两者在巡航高度上能差一千英尺，这就是"座舱里 35000、
    雷达上 34000"的由来。FSD 协议把差值单独放在最后一个字段里，正是为了让
    画他机的客户端拿真高摆飞机、让管制端拿真高加修正量当高度显示。

    气压高度 = 指示高度 + (29.92 - 高度表拨的气压) * 1000。温度偏差带来的
    误差不需要另算：指示高度本身就带着它，真高不带，相减自然就有了。

    读不到就返回 0，也就是退回修正前的行为——宁可不修正，不能瞎修正。
    """
    if indicated_ft is None or baro_inhg is None:
        return 0
    if not BARO_RANGE[0] <= baro_inhg <= BARO_RANGE[1]:
        return 0
    pressure_altitude = (indicated_ft
                         + (STANDARD_PRESSURE_INHG - baro_inhg) * 1000.0)
    return int(round(pressure_altitude - true_altitude_ft))


def bcd_to_squawk(raw):
    """应答机码在 SimVar 里是 BCD 编码的。

    0x1200 表示 1200。直接当十进制读会得到 4608，塞进位置包就是个乱码。
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 2000
    digits = 0
    for shift in (12, 8, 4, 0):
        digit = (value >> shift) & 0xF
        if digit > 7:            # 应答机是八进制，出现 8/9 说明这不是 BCD
            # 当十进制收，但每一位也得是八进制的——4608 或 1290 这种
            # 根本不是应答机码，上了网管制端会看到一个不存在的号
            if 0 <= value <= 7777 and all(c in "01234567" for c in f"{value:04d}"):
                return value
            return 2000
        digits = digits * 10 + digit
    return digits


def xpdr_mode(state, on_ground, groundspeed_kt):
    """TRANSPONDER STATE -> 位置包要的应答机模式。

    SimVar 的取值是 0 关 1 待机 2 测试 3 开 4 高度(C) 5 地面，所以 >= 3 才是
    真的在线。读不到（老机模没有这个 SimVar）沿用旧行为当在线。

    待机和关只在飞机**确实停着**的时候才当真。冷舱的飞机不该在雷达上是个亮着
    的 C 模式目标，而冷舱恰恰就是"停在机坪上没动"这一种情况——按这条线判，那
    个意图一点没丢。反过来，一架已经在滑行或者已经离地的飞机还报待机，几乎都
    是机模压根没把应答机旋钮接到这个 SimVar 上、它就一直停在 1 造成的；很多
    默认机和非精细机都是这样，它们从头到尾都是待机。

    这个区别在管制端不是小事。待机在 FSD 位置包里是包头的 `@S`，EuroScope 收到
    就当成一个没有 C 模式的目标，标牌上的**高度和地速会一起空掉**——管制员看到
    的现象就是"有的飞机读不到速度"。宁可让一架真按了待机的飞机多报一个 C 模式，
    也不能让一架在飞的飞机从标牌上消失。
    """
    if state is None:
        return XPDR_ONLINE
    try:
        state = int(state)
    except (TypeError, ValueError):
        return XPDR_ONLINE
    if state >= 3:
        return XPDR_ONLINE
    if on_ground and groundspeed_kt < PARKED_SPEED_KT:
        return XPDR_STANDBY
    return XPDR_ONLINE


class SimLink:
    """和 MSFS 的连接。

    on_state(connected, message) 在后台线程调用。
    """

    def __init__(self, on_state=None):
        self.on_state = on_state
        self.values = {}
        self.last_update = 0.0
        self.running = False

        self._sim = None
        self._requests = None
        self._thread = None
        self._lock = threading.Lock()
        self._connected = False
        self._slow_cursor = 0       # 轮流读的那一组读到哪了

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
        if self._sim is not None:
            try:
                self._sim.exit()
            except Exception:
                pass
        self._sim = None
        self._requests = None

    @property
    def sim(self):
        """底层 SimConnect 句柄，他机注入那边要用。"""
        return self._sim

    def _open(self):
        """连模拟器。没开的话构造函数直接抛，这是正常情况不是错误。"""
        from SimConnect import AircraftRequests, SimConnect

        self._sim = SimConnect()
        # _time 是这个包的缓存窗口（毫秒）。位置一秒要读 5 次，缓存久了会发出
        # 过期的位置；设小一点让它每次都真的去问。
        self._requests = AircraftRequests(self._sim, _time=100)

    def _run(self):
        while self.running:
            if self._sim is None:
                try:
                    self._open()
                except Exception as e:
                    self._state(False, t("sim.not_running"))
                    log.debug("opening SimConnect failed: %s", e)
                    self._sleep(RETRY_INTERVAL)
                    continue
                log.info("connected to SimConnect")

            result = self._poll()
            if result is FAILED:
                # SimConnect 本身出错了，连接多半没了，重开
                self._state(False, t("sim.link_lost"))
                self._close()
                self._sleep(RETRY_INTERVAL)
                continue

            if result is NO_DATA:
                # 连接好好的，只是还没进飞行（在菜单里就是这样）。以前这里
                # 也走重开，日志里每隔几秒一条 "SIM OPEN"，白白反复建连接。
                self._report_no_data()
                self._sleep(POLL_INTERVAL)
                continue

            self._state(True, t("sim.link_up"))
            self._sleep(POLL_INTERVAL)

    def _report_no_data(self):
        """一轮没读到位置，不等于连接断了。

        `_poll()` 每 0.2 秒串行读二十几个 SimVar，模拟器一次帧卡顿、天气流送、
        自动存档，都够让经纬度那一次读超时。以前这里一轮读不到就翻状态，实飞
        日志里因此刷出 21 次「MSFS 没有数据（是否已进入飞行？）」——飞机在天上、
        也在网上（那段时间 FSD 一直在线），每次 0～2 秒就恢复，纯粹是虚惊，而
        文案还把人往「是不是没进飞行」上引。

        判据改成和 `connected` 属性同一条（`STALE_AFTER`）。这两处本来就该一致：
        位置包那边也是这么发的——`snapshot()` 返回 `self.values` 里上一轮的值，
        不看 `last_update`，所以短暂的空档对网上没有任何影响，没有理由让界面
        先慌。

        宽限只挡「已连上 → 断开」这一个方向：`last_update` 初值是 0，在主菜单里
        从没读到过位置时立刻就报，不会让人对着一个假的「已连接」等三秒。
        """
        idle = time.time() - self.last_update
        if idle < STALE_AFTER:
            log.debug("no position this round, %.1f s into the %.0f s grace",
                      idle, STALE_AFTER)
            return
        self._state(False, t("sim.no_data"))

    def _sleep(self, seconds):
        """可被 stop() 打断的等待。"""
        deadline = time.time() + seconds
        while self.running and time.time() < deadline:
            time.sleep(0.05)

    def _poll(self):
        """取一轮数据。

        返回 OK / NO_DATA / FAILED。这三者必须分开：在主菜单里读不到经纬度是
        完全正常的，不该把整条 SimConnect 连接推倒重来。
        """
        values = {}
        failures = 0
        names = self._names_this_round()
        for name in names:
            simvar = SIMVARS[name]
            # 逐个兜底：个别机模上某个 SimVar 会抛（包对不认识的名字也抛），
            # 让它把整轮拖垮的话，连接每 3 秒推倒重来一次，位置包一停一停
            try:
                value = self._requests.get(simvar)
            except Exception as e:
                failures += 1
                log.debug("reading %s raised: %s", simvar, e)
                continue
            if value is not None:
                values[name] = value
        if failures and failures >= len(names):
            # 全军覆没才是连接真的没了
            return FAILED

        # 经纬度是判断"有没有真的在飞"的最低要求
        if "latitude" not in values or "longitude" not in values:
            return NO_DATA

        with self._lock:
            # 合并而不是整体替换：偶尔一轮里某个值没读到（换机、加载卡顿）
            # 时沿用上一轮的，不然下一个位置包里航向/姿态全变成 0，别人屏幕
            # 上这架飞机猛拽一下。整体新旧由 last_update 把关。
            self.values.update(values)
            self.last_update = time.time()
        return OK

    def _names_this_round(self):
        """这一轮要读哪些 SimVar。

        还没有任何数据时（启动后第一次读到之前）整份读一遍，第一个快照就是完整的；
        之后每轮读 FAST_SIMVARS 加上轮到的 SLOW_PER_POLL 个慢变量。
        """
        with self._lock:
            have_values = bool(self.values)
        if not have_values:
            return tuple(SIMVARS)
        start = self._slow_cursor
        chunk = tuple(SLOW_SIMVARS[(start + i) % len(SLOW_SIMVARS)]
                      for i in range(min(SLOW_PER_POLL, len(SLOW_SIMVARS))))
        self._slow_cursor = (start + SLOW_PER_POLL) % len(SLOW_SIMVARS)
        return FAST_SIMVARS + chunk

    # ---------- 取值 ----------
    def snapshot(self):
        """当前这一份数据，字段和 xpc/xplane.py 的 snapshot() 完全一致。"""
        with self._lock:
            raw = dict(self.values)
        if not raw:
            return None

        altitude = int(round(raw.get("altitude", 0.0)))
        groundspeed = int(round(raw.get("groundspeed", 0.0)))
        on_ground = bool(raw.get("on_ground", 0))
        return {
            # 经纬度已经是度。以前这里又 math.degrees 了一次，31.14 变成
            # 1784.2，服务端每个位置包都回 "Invalid latitude/longitude"，
            # 90 秒后把连接掐掉。
            "latitude": raw.get("latitude", 0.0),
            "longitude": raw.get("longitude", 0.0),
            "altitude": altitude,
            "pressure_delta": pressure_delta(raw.get("indicated_altitude"),
                                             raw.get("baro_setting"), altitude),
            "agl": int(round(raw.get("agl", 0.0))),
            "groundspeed": groundspeed,
            "pitch": -math.degrees(raw.get("pitch", 0.0)),
            "bank": -math.degrees(raw.get("bank", 0.0)),
            "heading": math.degrees(raw.get("heading", 0.0)) % 360.0,
            "squawk": bcd_to_squawk(raw.get("squawk", 0x2000)),
            # 待机只在飞机确实停着的时候当真，理由见 xpdr_mode()。
            "xpdr_mode": xpdr_mode(raw.get("xpdr_state"), on_ground, groundspeed),
            "com1": self._frequency(raw.get("com1")),
            "com2": self._frequency(raw.get("com2")),
            "com1_power": True,
            "on_ground": on_ground,
            # 下面这些是报给别人做动画用的
            "gear_down": bool(raw.get("gear", 1)),
            "flaps": max(0.0, min(1.0, raw.get("flaps", 0.0) / 100.0)),
            "spoilers": bool(raw.get("spoilers", 0)),
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
    def _frequency(raw):
        """COM ACTIVE FREQUENCY 已经是 MHz，只要挡掉 0 和异常值。"""
        if not raw:
            return None
        value = round(float(raw), 3)
        # 民航 VHF 通信频段，超出范围的多半是没读到
        return value if 118.0 <= value <= 137.0 else None
