"""XPC for CAN —— X-Plane 他机渲染插件。

装法：把这个文件放进

    <X-Plane>/Resources/plugins/PythonPlugins/PI_XpcTraffic.py

需要先装 XPPython3（https://xppython3.readthedocs.io）。**装哪个版本取决于模拟器**：

    X-Plane 12        XPPython3 v4.x
    X-Plane 11.52     XPPython3 v3.1.5   —— v4 是用 SDK 420 编的，不兼容 XP11

TCAS 接管（override_TCAS + sim/cockpit2/tcas/targets/*）是 X-Plane 11.50 引入
的。11.50 以前只有旧的 19 个多人机位 dataref，这里不去支持——XPPython3 v3.1.5
本来也是对着 11.52 发的。所以能力是**探测**出来的，不是按版本号写死的：找不到
TCAS 的 dataref 就只画飞机、不送 TCAS，并在日志里说清楚。

反过来不用担心：X-Plane 在接管 TCAS 时会自动把最近 19 架镜像回旧的
sim/multiplayer/position/plane#_* dataref，所以还在读那套的老插件照样能看到。

分工：客户端收 FSD、匹配模型，把每架飞机**最新的样本**（上报的位置姿态、速度、
角速度、离地高、样本序号）发过来；插件每一帧按 xPilot 的运动模型积分
（`Motion`，逐条抄自客户端的 traffic.py，test_xpc.py 钉着两边一致），再贴地、
动舵面，然后：

    画       xp.createInstance + instanceSetPosition，模型是客户端指定的 .obj
    TCAS     acquirePlanes 之后写 sim/cockpit2/tcas/targets/*

积分放在这里而不是客户端，是因为只有这里知道每一帧什么时候画。客户端按定时器
推已经积分好的位置，飞机就按那个定时器的节奏一步一步跳。

这个文件不能 import 客户端的模块（它跑在 X-Plane 自己的 Python 里），所以
`Motion` 和四元数是抄过来的一份。纯计算的部分都不碰 `xp`，测试直接加载这个
文件来测。

两件容易踩的事：

1. **CSL 模型的动画 dataref 必须先注册再加载模型。** OBJ8 里写的是
   `libxplanemp/controls/gear_ratio` 这类名字，X-Plane 在加载 .obj 时就要能
   解析它们，晚了模型出得来但不会动。所以 XPluginStart 里先注册。

2. **XPLMInstance 画出来的飞机不进 TCAS。** 座舱的 TCAS/ND 看的是
   `sim/cockpit2/tcas/targets/*`，那是另一套，得单独填。填之前要
   acquirePlanes()，否则 override_TCAS 写不进去。
"""

import base64
import json
import math
import socket
import traceback
import zlib

try:
    import xp
except ImportError:      # 在 X-Plane 之外被导入（比如跑测试）时不炸
    xp = None

PLUGIN_PORT = 49900
# v3：每架飞机送的是样本（target/velocity/rotation/seq/received），不再是
# 积分好的位置。v2：分片按字节切、负载 base64。和 bridge.py 保持一致（有测试
# 钉着）。
PROTOCOL_VERSION = 3

# TCAS 目标数组是 64 个位置（sim/cockpit2/tcas/targets/*，float[64]）。
# 第 0 位是本机，所以他机最多 63 架。
MAX_TCAS_TARGETS = 63

# 客户端多久没有任何消息就清场（xPilot 的 30 s 心跳超时）。客户端没变化时
# 每秒也发一帧，所以这只在客户端死掉时才会触发。比这短不行：飞机现在每帧
# 自己积分，客户端不再需要一直推。
HEARTBEAT_TIMEOUT = 30.0

# CSL 的 OBJ8 用这些 dataref 驱动动画。名字是 libxplanemp 的约定，
# Bluebell / X-CSL 等包都按这个写，顺序就是 instanceSetPosition 里 data 的顺序。
ANIMATION_DATAREFS = [
    "libxplanemp/controls/gear_ratio",
    "libxplanemp/controls/flap_ratio",
    "libxplanemp/controls/spoiler_ratio",
    "libxplanemp/controls/speed_brake_ratio",
    "libxplanemp/controls/slat_ratio",
    "libxplanemp/controls/wing_sweep_ratio",
    "libxplanemp/controls/thrust_ratio",
    "libxplanemp/controls/yoke_pitch_ratio",
    "libxplanemp/controls/yoke_heading_ratio",
    "libxplanemp/controls/yoke_roll_ratio",
    "libxplanemp/controls/thrust_revers",
    "libxplanemp/controls/taxi_lites_on",
    "libxplanemp/controls/landing_lites_on",
    "libxplanemp/controls/beacon_lites_on",
    "libxplanemp/controls/strobe_lites_on",
    "libxplanemp/controls/nav_lites_on",
    # 前轮转角。名字叫 ratio，XPMP2 里其实是度（V_CONTROLS_NWS_RATIO）
    "libxplanemp/controls/nws_ratio",
]


# ---------- 运动模型 ----------
#
# 从客户端 traffic.py 逐条抄过来（那边又是抄 xPilot 的 network_aircraft.cpp 和
# abacus.hpp）。改一边必须改另一边：MotionAgreementTest 拿同一串输入喂两份，
# 比结果。

ERROR_TIME = 2.0
ROTATION_HOLD = 0.5
_EPSILON = 1e-12

FEET_PER_METRE = 3.280839895
METRES_PER_FOOT = 0.3048
NM_PER_DEGREE = 60.0
METRES_PER_DEGREE = 1852.0 * NM_PER_DEGREE


def _wrap_longitude(longitude):
    if longitude > 180.0:
        longitude -= 360.0
    elif longitude < -180.0:
        longitude += 360.0
    return longitude


_IDENTITY = (0.0, 0.0, 0.0, 1.0)


def quat_from_euler(yaw, pitch, roll):
    """Quaternion::CreateFromEuler，弧度。"""
    cy, sy = math.cos(yaw * 0.5), math.sin(yaw * 0.5)
    cp, sp = math.cos(pitch * 0.5), math.sin(pitch * 0.5)
    cr, sr = math.cos(roll * 0.5), math.sin(roll * 0.5)
    return (cy * cp * sr - sy * sp * cr,
            sy * cp * sr + cy * sp * cr,
            sy * cp * cr - cy * sp * sr,
            cy * cp * cr + sy * sp * sr)


def quat_to_euler(q):
    """Quaternion::ExtractEulerAngles，返回 (俯仰, 航向, 坡度)，弧度。"""
    x, y, z, w = q
    test = w * y - z * x
    if test > 0.4999999999999999:
        return math.pi / 2, 2.0 * math.atan2(x, w), 0.0
    if test < -0.4999999999999999:
        return -math.pi / 2, -2.0 * math.atan2(x, w), 0.0
    pitch = math.asin(2.0 * test)
    yaw = math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
    roll = math.atan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y))
    return pitch, yaw, roll


def quat_multiply(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (ax * bw + aw * bx + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


def quat_inverse(q):
    x, y, z, w = q
    n = x * x + y * y + z * z + w * w
    return (-x / n, -y / n, -z / n, w / n)


def _normalized(q):
    n = math.sqrt(sum(c * c for c in q))
    return tuple(c / n for c in q)


def quat_scale(q, t):
    """Quaternion::SlerpUnclamped(Identity, q, t)：同一根轴，转 t 倍的角度。"""
    x, y, z, w = q
    dot = w
    flip = dot < 0
    if flip:
        dot = -dot
    if dot > 0.999999:
        n2 = 1.0 - t
        n1 = -t if flip else t
    else:
        theta = math.acos(dot)
        inv = 1.0 / math.sin(theta)
        n2 = math.sin((1.0 - t) * theta) * inv
        n1 = math.sin(t * theta) * inv
        if flip:
            n1 = -n1
    return _normalized((n1 * x, n1 * y, n1 * z, n2 + n1 * w))


def _attitude_quat(pitch, bank, heading):
    """度 -> 四元数。"""
    return quat_from_euler(math.radians(heading), math.radians(pitch),
                           math.radians(bank))


class Motion:
    """一架飞机画出来的位置和姿态。traffic.Motion 的副本，行为必须一致。

        receive(...)   一个新样本
        refresh(...)   同一份快照又来了一遍：只换速度
        advance(dt)    往前积分 dt 秒
        state()        画出来的 {latitude, longitude, altitude, pitch, bank,
                       heading}

    位置是纬度/经度（度）和英尺；velocity 是 (东, 上, 北) 米每秒；rotation 是
    (俯仰, 航向, 坡度) 度每秒的机体角速度，抬头、右转、右坡为正。
    """

    __slots__ = ("ready", "latitude", "longitude", "altitude", "orientation",
                 "target", "target_orientation", "velocity", "rotation",
                 "error_velocity", "error_rotation", "error_remaining",
                 "since_update")

    def __init__(self):
        self.ready = False
        self.latitude = 0.0
        self.longitude = 0.0
        self.altitude = 0.0
        self.orientation = _IDENTITY
        self.target = None
        self.target_orientation = _IDENTITY
        self.velocity = (0.0, 0.0, 0.0)
        self.rotation = (0.0, 0.0, 0.0)
        self.error_velocity = (0.0, 0.0, 0.0)
        self.error_rotation = (0.0, 0.0, 0.0)
        self.error_remaining = 0.0
        self.since_update = 0.0

    def receive(self, latitude, longitude, altitude, pitch, bank, heading,
                velocity=(0.0, 0.0, 0.0), rotation=(0.0, 0.0, 0.0)):
        self.target = (latitude, longitude, altitude, pitch, bank, heading)
        self.target_orientation = _attitude_quat(pitch, bank, heading)
        self.velocity = tuple(float(v) for v in velocity)
        self.rotation = tuple(float(v) for v in rotation)
        if not self.ready:
            self.ready = True
            self.latitude, self.longitude, self.altitude = latitude, longitude, altitude
            self.orientation = self.target_orientation
            self.error_velocity = (0.0, 0.0, 0.0)
            self.error_rotation = (0.0, 0.0, 0.0)
            self.error_remaining = 0.0
            self.since_update = 0.0
            return
        if self.since_update >= ROTATION_HOLD:
            self._clear_rotation()
        self.since_update = 0.0
        self._update_errors()

    def refresh(self, velocity, rotation):
        self.velocity = tuple(float(v) for v in velocity)
        self.rotation = tuple(float(v) for v in rotation)
        if self.since_update >= ROTATION_HOLD:
            self._clear_rotation()
        self.since_update = 0.0

    def _update_errors(self):
        lat, lon, alt = self.target[0], self.target[1], self.target[2]
        north = (lat - self.latitude) * METRES_PER_DEGREE
        east = (_wrap_longitude(lon - self.longitude) * METRES_PER_DEGREE
                * math.cos(math.radians(lat)))
        up = (alt - self.altitude) * METRES_PER_FOOT
        self.error_velocity = (east / ERROR_TIME, up / ERROR_TIME, north / ERROR_TIME)
        if self.orientation == self.target_orientation:
            self.error_rotation = (0.0, 0.0, 0.0)
        else:
            delta = quat_multiply(quat_inverse(self.orientation),
                                  self.target_orientation)
            pitch, yaw, roll = quat_to_euler(delta)
            self.error_rotation = (pitch / ERROR_TIME, yaw / ERROR_TIME,
                                   roll / ERROR_TIME)
        self.error_remaining = ERROR_TIME

    def _clear_rotation(self):
        self.rotation = (0.0, 0.0, 0.0)
        self.error_rotation = (0.0, 0.0, 0.0)
        self.orientation = self.target_orientation

    def advance(self, dt):
        """往前积分 dt 秒，在误差速度和角速度到期的时刻切段，和帧率无关。"""
        if not self.ready or dt <= 0:
            return
        while dt > _EPSILON:
            step = dt
            if 0 < self.error_remaining < step:
                step = self.error_remaining
            live = self.since_update < ROTATION_HOLD
            if live and ROTATION_HOLD - self.since_update < step:
                step = ROTATION_HOLD - self.since_update
            self._integrate(step)
            dt -= step
            self.error_remaining = max(0.0, self.error_remaining - step)
            self.since_update += step
            if live and self.since_update >= ROTATION_HOLD - _EPSILON:
                self.since_update = max(self.since_update, ROTATION_HOLD)
                self._clear_rotation()

    def _integrate(self, step):
        east, up, north = self.velocity
        pitch_rate, heading_rate, bank_rate = (math.radians(v) for v in self.rotation)
        if self.error_remaining > 0:
            east += self.error_velocity[0]
            up += self.error_velocity[1]
            north += self.error_velocity[2]
            pitch_rate += self.error_rotation[0]
            heading_rate += self.error_rotation[1]
            bank_rate += self.error_rotation[2]

        scale = max(1e-6, math.cos(math.radians(self.latitude)))
        self.longitude = _wrap_longitude(
            self.longitude + east * step / (METRES_PER_DEGREE * scale))
        self.latitude = max(-90.0, min(90.0,
                                       self.latitude + north * step / METRES_PER_DEGREE))
        self.altitude += up * step * FEET_PER_METRE

        if pitch_rate or heading_rate or bank_rate:
            rotation = quat_from_euler(heading_rate, pitch_rate, bank_rate)
            self.orientation = _normalized(quat_multiply(
                self.orientation, quat_scale(rotation, step)))

    def attitude(self):
        pitch, yaw, roll = quat_to_euler(self.orientation)
        return (math.degrees(pitch), math.degrees(roll),
                math.degrees(yaw) % 360.0)

    def state(self):
        if not self.ready:
            return None
        pitch, bank, heading = self.attitude()
        return {"latitude": self.latitude, "longitude": self.longitude,
                "altitude": self.altitude, "pitch": pitch, "bank": bank,
                "heading": heading}


# ---------- 贴地 ----------
#
# xPilot 的 PerformGroundClamping / RecordTerrainElevationHistory
# （network_aircraft.cpp）。对方的模拟器和我们的地景高度不一样：同一条跑道，
# 对方那边 50 ft，我们这边可能 80 ft。按对方报的真高画，飞机就陷在地里或者
# 悬在半空。办法是算一个偏移 = 本地地形 −（对方真高 − 对方离地高），在地面附近
# 慢慢加上去，爬出去以后再慢慢撤掉。单位全是英尺。

# 这个高度以上不探地形（xPilot 同样的 18000 ft）
TERRAIN_PROBE_CEILING = 18000.0
# 离地高在这以下、持续这么久、地面坡度不超过这个，才信对方的离地高
MAX_USABLE_ALTITUDE_AGL = 100.0
TERRAIN_ELEVATION_DATA_USABLE_AGE = 2.0
TERRAIN_ELEVATION_MAX_SLOPE = 3.0
# 离地这么高以上、偏移目标归零时，算作爬升离场，用长窗口慢慢撤
MIN_AGL_FOR_CLIMBOUT = 50.0
TERRAIN_OFFSET_WINDOW_LANDING = 2.0
TERRAIN_OFFSET_WINDOW_CLIMBOUT = 10.0
MIN_TERRAIN_OFFSET_MAGNITUDE = 0.1


def _distance_ft(lat1, lon1, lat2, lon2):
    """两点地面距离（英尺），平面近似。只比两秒内的两个点，够用。"""
    mean_lat = math.radians((lat1 + lat2) / 2.0)
    dy = (lat2 - lat1) * METRES_PER_DEGREE
    dx = _wrap_longitude(lon2 - lon1) * METRES_PER_DEGREE * math.cos(mean_lat)
    return math.hypot(dx, dy) * FEET_PER_METRE


class TerrainClamp:
    """一架飞机的地形偏移。纯计算，地形高度由调用方探好了递进来。

        record(now, ...)   一个新样本到了（RecordTerrainElevationHistory）
        step(dt, ...)      每帧一次，返回该画的真高（PerformGroundClamping）

    和 xPilot 有三处不同，都是修它的毛病：

    - xPilot 只留最近 1.75 s 的历史（`- USABLE_AGE + 250`），却要求首尾跨度
      至少 2 s，所以 HasUsableTerrainElevationData 永远是 false，只有报了
      "在地上"才会加偏移。这里留 2.25 s，那条路径才真正能走到：五边最后
      100 ft 就开始对齐地面，而不是接地之后才补。
    - xPilot 的对方地形高度（RemoteValue）从来没赋值，坡度检查等于只看本地。
      这里按对方真高 − 离地高算。
    - 探不到地形时 xPilot 当 0 ft，这里当"不知道"，不贴地。
    """

    __slots__ = ("offset", "target", "magnitude", "history", "usable", "local")

    def __init__(self):
        self.offset = 0.0
        self.target = 0.0
        self.magnitude = 0.0
        self.history = []        # (时刻, 纬度, 经度, 本地地形, 对方地形)
        self.usable = False
        self.local = None        # 最近一帧探到的本地地形高度

    def record(self, now, latitude, longitude, altitude, agl):
        if self.local is None:
            return
        self.usable = False
        horizon = now - (TERRAIN_ELEVATION_DATA_USABLE_AGE + 0.25)
        self.history = [h for h in self.history if h[0] >= horizon]
        if agl is None or agl > MAX_USABLE_ALTITUDE_AGL:
            return
        self.history.append((now, latitude, longitude, self.local, altitude - agl))
        if len(self.history) < 2:
            return
        start, end = self.history[0], self.history[-1]
        if end[0] - start[0] < TERRAIN_ELEVATION_DATA_USABLE_AGE:
            return
        distance = _distance_ft(start[1], start[2], end[1], end[2])
        for index in (3, 4):
            delta = abs(start[index] - end[index])
            if math.degrees(math.atan2(delta, distance)) > TERRAIN_ELEVATION_MAX_SLOPE:
                return
        self.usable = True

    def step(self, dt, altitude, local, reported_altitude, agl, on_ground, first):
        """altitude 是积分出来的真高，local 是它脚下的本地地形（None = 没探到）。

        reported_altitude / agl / on_ground 来自对方最新的样本。
        """
        self.local = local
        if local is None:
            return altitude
        if agl is None:
            agl = altitude - local

        if (not self.usable and not on_ground
                and self.target == 0.0 and self.offset == 0.0):
            return max(altitude, local)

        if self.usable or on_ground:
            remote = reported_altitude - agl
            target = round(local - remote, 2)
            # 报了在地上但偏移之后还悬着：直接压到本地地面
            if on_ground and reported_altitude + target > local:
                target = local - reported_altitude
        else:
            target = 0.0

        if target != self.target:
            self.target = target
            self.magnitude = max(abs(self.target - self.offset),
                                 MIN_TERRAIN_OFFSET_MAGNITUDE)

        if self.offset != self.target:
            if first:
                self.offset = self.target
            else:
                climbing_out = (not on_ground and agl >= MIN_AGL_FOR_CLIMBOUT
                                and self.target == 0.0)
                window = (TERRAIN_OFFSET_WINDOW_CLIMBOUT if climbing_out
                          else TERRAIN_OFFSET_WINDOW_LANDING)
                step = self.magnitude * dt / window
                if step >= abs(self.target - self.offset):
                    self.offset = self.target
                else:
                    self.offset += step if self.target > self.offset else -step

        return max(altitude + self.offset, local)


# ---------- 舵面 ----------

# xPilot 默认机型的时长（network_aircraft.cpp 的 FlightModel）：起落架 10 s
# 走完全程，襟翼 5 s，扰流板跟襟翼一样。
GEAR_DURATION = 10.0
FLAPS_DURATION = 5.0
SPOILERS_DURATION = 5.0


def approach(current, target, dt, duration):
    """匀速走向 target，duration 秒走完 0→1 全程。到了就停，不越过去。"""
    if duration <= 0:
        return target
    step = dt / duration
    if abs(target - current) <= step:
        return target
    return current + step if target > current else current - step


def surface_targets(entry):
    """(起落架, 襟翼, 扰流板) 的目标位置，0..1。"""
    gear = entry.get("gear_down")
    if gear is None:
        # 对方没报配置就按状态猜：在地上或低速就放起落架
        gear = bool(entry.get("on_ground")) or entry.get("groundspeed", 0) < 150
    elif entry.get("on_ground"):
        # xPilot：IsGearDown || IsReportedOnGround
        gear = True
    return (1.0 if gear else 0.0,
            max(0.0, min(1.0, float(entry.get("flaps", 0.0) or 0.0))),
            1.0 if entry.get("spoilers") else 0.0)


def _as_triple(value):
    try:
        a, b, c = value
        return (float(a), float(b), float(c))
    except (TypeError, ValueError):
        return (0.0, 0.0, 0.0)


class NetworkAircraft:
    """一架他机在插件这边的全部状态，不碰 xp。xPilot 的 NetworkAircraft。

        accept(entry, now)     客户端发来的这架飞机的条目
        advance(dt)            积分
        draw(dt, probe)        贴地、动舵面，返回该画的状态

    样本靠两个序号识别：`received` 是客户端那边 Motion.receive 的次数，`seq`
    是 receive 加 refresh 的次数。UDP 会丢帧、客户端也不是每个样本都推，所以
    不能按"来一条算一条"：received 变了就 receive（用最新的 target），只有
    seq 变了就 refresh。
    """

    def __init__(self, callsign):
        self.callsign = callsign
        self.motion = Motion()
        self.terrain = TerrainClamp()
        self.entry = {}
        self.seq = None
        self.received = None
        self.first = True        # 还没画过第一帧（xPilot 的 IsFirstRenderPending）
        self.gear = 0.0
        self.flaps = 0.0
        self.spoilers = 0.0

    def accept(self, entry, now):
        self.entry = entry
        target = entry.get("target")
        if not isinstance(target, (list, tuple)) or len(target) != 6:
            return
        try:
            target = tuple(float(v) for v in target)
        except (TypeError, ValueError):
            return
        velocity = _as_triple(entry.get("velocity"))
        rotation = _as_triple(entry.get("rotation"))
        seq, received = entry.get("seq"), entry.get("received")
        if received != self.received or not self.motion.ready:
            self.motion.receive(*target, velocity=velocity, rotation=rotation)
            self.terrain.record(now, target[0], target[1], target[2],
                                entry.get("agl"))
        elif seq != self.seq:
            self.motion.refresh(velocity, rotation)
        self.seq, self.received = seq, received

    def advance(self, dt):
        self.motion.advance(dt)

    def draw(self, dt, probe=None):
        """这一帧该画的状态。probe(纬度, 经度) 返回本地地形英尺，或 None。"""
        state = self.motion.state()
        if state is None:
            return None
        entry = self.entry
        local = None
        if probe is not None and state["altitude"] < TERRAIN_PROBE_CEILING:
            local = probe(state["latitude"], state["longitude"])
        target = self.motion.target
        state["altitude"] = self.terrain.step(
            dt, state["altitude"], local, target[2], entry.get("agl"),
            bool(entry.get("on_ground")), self.first)

        gear, flaps, spoilers = surface_targets(entry)
        if self.first:
            self.gear, self.flaps, self.spoilers = gear, flaps, spoilers
        else:
            self.gear = approach(self.gear, gear, dt, GEAR_DURATION)
            self.flaps = approach(self.flaps, flaps, dt, FLAPS_DURATION)
            self.spoilers = approach(self.spoilers, spoilers, dt, SPOILERS_DURATION)
        self.first = False
        return state

    def surfaces(self):
        return {"gear": self.gear, "flaps": self.flaps, "spoilers": self.spoilers}


# ---------- 网络 ----------

class Reassembler:
    """把分片的 UDP 包拼回完整消息。和客户端 bridge.py 里那份对称。"""

    def __init__(self):
        self.sequence = None
        self.parts = {}
        self.total = 0

    def feed(self, packet):
        try:
            header = json.loads(packet.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if header.get("v") != PROTOCOL_VERSION:
            return None

        sequence = header.get("seq", 0)
        if sequence != self.sequence:
            # 16 位环回序号：只认往前走的新帧，迟到的旧分片直接扔
            if self.sequence is not None:
                delta = (sequence - self.sequence) & 0xFFFF
                if delta == 0 or delta > 0x8000:
                    return None
            self.sequence = sequence
            self.parts = {}
            self.total = max(1, int(header.get("total", 1) or 1))
        try:
            part = int(header["part"])
            if not 0 <= part < self.total:
                return None
            self.parts[part] = base64.b64decode(header["data"])
        except (KeyError, TypeError, ValueError):
            return None
        if len(self.parts) < self.total:
            return None

        body = b"".join(self.parts[i] for i in range(self.total))
        self.parts = {}
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None


class RenderedAircraft(NetworkAircraft):
    """一架正在画的飞机：NetworkAircraft 加上 X-Plane 里的对象和 instance。"""

    def __init__(self, callsign):
        super().__init__(callsign)
        self.object_path = ""
        self.object_ref = None
        self.instance = None
        self.loading = False
        # 每发起一次异步加载加一。回调带着发起时的号回来，对不上就说明这次
        # 加载已经被更新的一次顶掉了——直接卸掉，别覆盖 instance（否则旧
        # instance 没人销毁，一架冻住的重影永远挂在天上）。
        self.load_generation = 0

    def destroy(self):
        if self.instance is not None and xp:
            try:
                xp.destroyInstance(self.instance)
            except Exception:
                pass
        self.instance = None
        if self.object_ref is not None and xp:
            try:
                xp.unloadObject(self.object_ref)
            except Exception:
                pass
        self.object_ref = None


class PythonInterface:
    def XPluginStart(self):
        self.Name = "XPC for CAN Traffic"
        self.Sig = "org.can.xpc.traffic"
        self.Desc = "把 Cerulean 网络上的其他飞机画进 X-Plane，并送进 TCAS"

        self.socket = None
        self.reassembler = Reassembler()
        self.aircraft = {}          # 呼号 -> RenderedAircraft
        self.order = []             # 最近一条消息里的呼号顺序（按距离，TCAS 用）
        self.last_message = 0.0
        self.last_frame = None
        self.tcas_written = 0       # 上一帧写了多少个 TCAS 槽位
        self.have_planes = False
        self.accessors = []
        self.own_callsign = ""
        self.probe = None
        self.probe_failed = False

        # 动画 dataref 必须在任何 CSL 模型加载之前注册好，
        # 否则 X-Plane 解析 OBJ8 时找不到它们，模型能出来但不会动。
        self._register_animation_datarefs()

        self._find_tcas_datarefs()

        xp.registerFlightLoopCallback(self.flight_loop, -1, 0)
        xp.log(f"XPC traffic plugin started ({self._version_note()})")
        return self.Name, self.Sig, self.Desc

    @staticmethod
    def _version_note():
        """把模拟器和 SDK 版本记进日志——用户报问题时第一件要知道的事。"""
        try:
            sim, xplm, _ = xp.getVersions()
            return f"X-Plane {sim}, XPLM {xplm}"
        except Exception:
            return "版本未知"

    def _find_tcas_datarefs(self):
        """探测 TCAS 接管能力。

        override_TCAS 和 tcas/targets 是 X-Plane 11.50 才有的。找不到就只画飞
        机不送 TCAS——按版本号写死不如直接问 X-Plane 有没有这个 dataref。
        """
        self.override_tcas = xp.findDataRef("sim/operation/override/override_TCAS")
        self.tcas = {
            name: xp.findDataRef(f"sim/cockpit2/tcas/targets/{path}")
            for name, path in (
                ("x", "position/x"), ("y", "position/y"), ("z", "position/z"),
                ("psi", "position/psi"), ("the", "position/the"),
                ("phi", "position/phi"),
                ("vertical_speed", "position/vertical_speed"),
                ("weight_on_wheels", "position/weight_on_wheels"),
                ("modeC", "modeC_code"), ("modeS", "modeS_id"),
                ("flight_id", "flight_id"), ("icao_type", "icao_type"),
            )}

        missing = [name for name, ref in self.tcas.items() if ref is None]
        self.tcas_available = bool(self.override_tcas) and not missing
        if not self.tcas_available:
            xp.log("this X-Plane build has no TCAS override (11.50+ required); "
                   f"traffic will be drawn but will not reach TCAS. "
                   f"missing: {missing or 'override_TCAS'}")

    def _register_animation_datarefs(self):
        """注册 libxplanemp 那套动画 dataref。

        如果 LiveTraffic 之类的插件已经注册过同名 dataref，findDataRef 能找到
        它——那说明另一套 XPMP2 正在跑。同时开两套会抢 AI 机位，这里只记一条
        日志让用户知道，不去抢。
        """
        existing = xp.findDataRef(ANIMATION_DATAREFS[0])
        if existing is not None:
            xp.log("warning: another plugin already registered the libxplanemp "
                   "animation datarefs (LiveTraffic / XPMP2?); running two "
                   "traffic systems at once makes them fight each other")
            return

        for name in ANIMATION_DATAREFS:
            # 只读访问器就够：值是通过 instanceSetPosition 的 data 传的，
            # 不走 dataref 本身。这里注册只是让 OBJ8 能解析到名字。
            accessor = xp.registerDataAccessor(name, readFloat=lambda ref=None: 0.0)
            self.accessors.append(accessor)

    def XPluginEnable(self):
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.socket.bind(("127.0.0.1", PLUGIN_PORT))
            self.socket.setblocking(False)
        except OSError as e:
            xp.log(f"could not bind {PLUGIN_PORT}: {e}")
            return 0

        try:
            self.probe = xp.createProbe(xp.ProbeY)
        except Exception as e:
            self.probe = None
            xp.log(f"could not create a terrain probe, traffic will not be "
                   f"clamped to the ground: {e}")

        # 只有要送 TCAS 才需要抢 AI 机位。没这个能力就别抢——白占着会挡住
        # LiveTraffic 之类真正用得上的插件。
        if not self.tcas_available:
            return 1

        # acquirePlanes 是写 override_TCAS 的前提（X-Plane 的 dataref 文档里
        # 明写 "Only writeable by the plugin that has the AI planes acquired"）
        try:
            self.have_planes = bool(xp.acquirePlanes())
        except Exception as e:
            xp.log(f"acquirePlanes failed: {e}")
            self.have_planes = False

        if self.have_planes:
            xp.setDatai(self.override_tcas, 1)
            xp.log("acquired the AI planes, TCAS override is on")
        else:
            try:
                _, _, who = xp.countAircraft()
            except Exception:
                who = "?"
            xp.log(f"could not acquire the AI planes (plugin {who} holds them), "
                   f"traffic will not reach TCAS")
        return 1

    def XPluginDisable(self):
        self._clear_all()
        if self.have_planes:
            try:
                xp.setDatai(self.override_tcas, 0)
                xp.releasePlanes()
            except Exception:
                pass
            self.have_planes = False
        if self.probe is not None:
            try:
                xp.destroyProbe(self.probe)
            except Exception:
                pass
            self.probe = None
        if self.socket:
            try:
                self.socket.close()
            except OSError:
                pass
            self.socket = None

    def XPluginStop(self):
        xp.unregisterFlightLoopCallback(self.flight_loop, 0)
        for accessor in self.accessors:
            try:
                xp.unregisterDataAccessor(accessor)
            except Exception:
                pass
        self.accessors = []

    def XPluginReceiveMessage(self, who, message, param):
        pass

    # ---------- 主循环 ----------
    def flight_loop(self, lastCall, elapsedSim, counter, refcon):
        try:
            self._pump()
        except Exception:
            xp.log("traffic plugin error:\n" + traceback.format_exc())
        return -1        # 每帧都跑

    def _pump(self):
        now = xp.getElapsedTime()
        dt = 0.0 if self.last_frame is None else max(0.0, now - self.last_frame)
        self.last_frame = now

        # 先积分到此刻，再收样本：Motion 要求"先 advance 到样本到达那一刻再
        # receive"，否则新样本的误差是拿上一帧的位置算的。
        for aircraft in self.aircraft.values():
            aircraft.advance(dt)

        message = self._receive_latest()
        if message is not None:
            self.last_message = now
            self._apply(message, now)
        elif self.last_message and now - self.last_message > HEARTBEAT_TIMEOUT:
            # 客户端死了，把天上清空，别让一堆飞机沿着最后的速度一直飞下去
            xp.log(f"no word from the client for {HEARTBEAT_TIMEOUT:.0f} s, "
                   f"clearing {len(self.aircraft)} aircraft")
            self.last_message = 0.0
            self._clear_all()
            return

        if self.aircraft:
            self._draw_all(dt)

    def _receive_latest(self):
        """把收到的包都读干净，只保留最后一条完整消息。

        每条消息都带着每架飞机最新的样本和序号，所以只看最后一条不会漏掉
        receive：序号比较的是"变没变"，不是"来一条算一条"。
        """
        latest = None
        while True:
            try:
                packet, _ = self.socket.recvfrom(65535)
            except (BlockingIOError, OSError):
                break
            message = self.reassembler.feed(packet)
            if message is not None:
                latest = message
        return latest

    def _apply(self, message, now):
        if message.get("type") != "traffic":
            return
        entries = message.get("aircraft") or []

        order = []
        for entry in entries:
            callsign = entry.get("callsign")
            if not callsign or callsign in order:
                continue
            order.append(callsign)
            aircraft = self.aircraft.get(callsign)
            if aircraft is None:
                aircraft = RenderedAircraft(callsign)
                self.aircraft[callsign] = aircraft
            aircraft.accept(entry, now)
            self._ensure_model(aircraft, entry.get("object") or "")

        for callsign in [c for c in self.aircraft if c not in order]:
            self.aircraft.pop(callsign).destroy()
        self.order = order

    def _ensure_model(self, aircraft, wanted):
        if wanted and wanted != aircraft.object_path:
            # 客户端换了匹配结果（多半是机型问到了），换模型
            aircraft.destroy()
            aircraft.object_path = wanted
            aircraft.loading = True
            aircraft.load_generation += 1
            xp.loadObjectAsync(self._object_path(wanted), self._object_loaded,
                               (aircraft.callsign, aircraft.load_generation))

    def _terrain_elevation(self, latitude, longitude):
        """脚下本地地形的高度（英尺），探不到返回 None。xPilot 的 TerrainProbe。

        XPPython3 v4 的 probeTerrainXYZ 返回一个带 .result 的对象；出任何
        异常就关掉贴地并记一行，别让每一帧都刷日志。
        """
        if self.probe is None or self.probe_failed:
            return None
        try:
            x, y, z = xp.worldToLocal(latitude, longitude, 0.0)
            info = xp.probeTerrainXYZ(self.probe, x, y, z)
            if info.result != xp.ProbeHitTerrain:
                return None
            _, _, altitude = xp.localToWorld(info.locationX, info.locationY,
                                             info.locationZ)
            return altitude * FEET_PER_METRE
        except Exception as e:
            self.probe_failed = True
            xp.log(f"terrain probe failed, traffic will not be clamped to "
                   f"the ground: {e}")
            return None

    def _draw_all(self, dt):
        drawn = []
        for callsign in self.order:
            aircraft = self.aircraft.get(callsign)
            if aircraft is None:
                continue
            state = aircraft.draw(dt, self._terrain_elevation)
            if state is None:
                continue
            x, y, z = xp.worldToLocal(state["latitude"], state["longitude"],
                                      state["altitude"] * METRES_PER_FOOT)
            drawn.append((aircraft, state, (x, y, z)))
            if aircraft.instance is None:
                continue
            # CSL 模型的原点不在轮子底下：VERT_OFFSET（米）把它抬到起落架上
            # （XPMP2 的 GetVertOfs）。只抬画的那份，TCAS 用真高。
            try:
                lift = float(aircraft.entry.get("vert_offset") or 0.0)
            except (TypeError, ValueError):
                lift = 0.0
            # 注意顺序是 (x, y, z, pitch, heading, roll)——heading 在 roll 前面
            xp.instanceSetPosition(
                aircraft.instance,
                (x, y + lift, z, state["pitch"], state["heading"], state["bank"]),
                self._animation_values(aircraft.entry, aircraft.surfaces()))
        self._write_tcas(drawn[:MAX_TCAS_TARGETS])

    @staticmethod
    def _object_path(path):
        """把客户端给的路径整理成 XPLM 能吃的样子。

        客户端发来的是绝对路径（用户选的 CSL 目录），Windows 上还带反斜杠。
        XPLM 的对象加载习惯 X-System 根目录的相对路径（XPMP2 专门有个
        RemoveXPSysDir 干这个）——在 X-Plane 树里的就转成相对的，不在的原样
        交上去碰运气，失败会在 _object_loaded 里说清楚。
        """
        clean = (path or "").replace("\\", "/")
        try:
            system = xp.getSystemPath().replace("\\", "/")
            if system and clean.lower().startswith(system.lower()):
                clean = clean[len(system):].lstrip("/")
        except Exception:
            pass
        return clean

    def _object_loaded(self, object_ref, refcon):
        """loadObjectAsync 的回调。加载期间飞机可能已经走了，或又换过模型。"""
        callsign, generation = refcon
        aircraft = self.aircraft.get(callsign)
        if (aircraft is None or object_ref is None
                or generation != aircraft.load_generation):
            if object_ref is not None:
                xp.unloadObject(object_ref)
            if object_ref is None and aircraft is not None:
                # 不留这行日志的话，"TCAS 有目标、窗外没飞机"完全没法查
                xp.log(f"could not load the CSL object for {callsign}: "
                       f"{aircraft.object_path}")
                aircraft.loading = False
            return
        aircraft.loading = False
        aircraft.object_ref = object_ref
        aircraft.instance = xp.createInstance(object_ref, ANIMATION_DATAREFS)

    @staticmethod
    def _animation_values(entry, surfaces=None):
        """按 ANIMATION_DATAREFS 的顺序给值。顺序错了动画就会串。

        surfaces 是 NetworkAircraft 里慢慢走的舵面位置；不给就直接用目标值。
        """
        lights = entry.get("lights") or {}
        if surfaces is None:
            gear, flaps, spoilers = surface_targets(entry)
        else:
            gear, flaps, spoilers = (surfaces["gear"], surfaces["flaps"],
                                     surfaces["spoilers"])
        thrust = 0.0 if entry.get("on_ground") and entry.get("groundspeed", 0) < 1 else 0.7
        try:
            nose_wheel = float(entry.get("nose_wheel") or 0.0)
        except (TypeError, ValueError):
            nose_wheel = 0.0
        return [
            gear,
            flaps,
            spoilers,
            spoilers,                  # speed brake 跟扰流板（xPilot 同样）
            0.0,                       # slat
            0.0,                       # wing sweep
            thrust if entry.get("engines_on", True) else 0.0,
            0.0, 0.0, 0.0,             # yoke
            0.0,                       # reverser
            1.0 if lights.get("taxi_on") else 0.0,
            1.0 if lights.get("landing_on") else 0.0,
            1.0 if lights.get("beacon_on") else 0.0,
            1.0 if lights.get("strobe_on") else 0.0,
            1.0 if lights.get("nav_on") else 0.0,
            nose_wheel,
        ]

    # ---------- TCAS ----------
    def _write_tcas(self, drawn):
        """填 sim/cockpit2/tcas/targets/*。每帧一次，位置是这一帧画出来的。

        第 0 位是本机，他机从 1 开始。带上 flight_id 和 icao_type，ND 上显示
        的呼号和机型才是对的。X-Plane 会自动把最近 19 架镜像回旧的
        sim/multiplayer/position/plane#_*，所以老插件也能看到。
        """
        if not (self.tcas_available and self.have_planes):
            return

        count = len(drawn)
        # 数量变少时要把多出来的槽位清掉。X-Plane 靠 modeS_id 非零判断目标
        # 存在，不清的话走掉的飞机会以最后的位置永远留在 ND/TCAS 上。
        if count < self.tcas_written:
            stale = self.tcas_written - count
            try:
                xp.setDatavi(self.tcas["modeS"], [0] * stale, 1 + count, stale)
            except Exception:
                pass
        self.tcas_written = count
        if count == 0:
            return
        xs, ys, zs, psis, thes, phis, vss, wows = [], [], [], [], [], [], [], []
        modes, ids = [], []
        flight_ids, icao_types = bytearray(), bytearray()

        for aircraft, state, (x, y, z) in drawn:
            entry = aircraft.entry
            xs.append(x)
            ys.append(y)
            zs.append(z)
            psis.append(state["heading"])
            thes.append(state["pitch"])
            phis.append(state["bank"])
            vss.append(entry.get("vertical_speed", 0.0))
            wows.append(1 if entry.get("on_ground") else 0)
            modes.append(int(entry.get("squawk", 0)))
            # modeS_id 要求 1..0xFFFFFF 唯一，用呼号哈希凑一个稳定的。
            # 不能用 hash()：它按进程随机加盐，重启一次 X-Plane 同一架飞机
            # 就换了个 id。
            ids.append((zlib.crc32(aircraft.callsign.encode("utf-8"))
                        & 0xFFFFFF) or 1)
            flight_ids.extend(self._fixed_string(aircraft.callsign, 8))
            icao_types.extend(self._fixed_string(entry.get("equipment", ""), 8))

        # 从下标 1 开始写，0 号位留给本机
        xp.setDatavf(self.tcas["x"], xs, 1, count)
        xp.setDatavf(self.tcas["y"], ys, 1, count)
        xp.setDatavf(self.tcas["z"], zs, 1, count)
        xp.setDatavf(self.tcas["psi"], psis, 1, count)
        xp.setDatavf(self.tcas["the"], thes, 1, count)
        xp.setDatavf(self.tcas["phi"], phis, 1, count)
        xp.setDatavf(self.tcas["vertical_speed"], vss, 1, count)
        xp.setDatavi(self.tcas["weight_on_wheels"], wows, 1, count)
        xp.setDatavi(self.tcas["modeC"], modes, 1, count)
        xp.setDatavi(self.tcas["modeS"], ids, 1, count)
        xp.setDatab(self.tcas["flight_id"], bytes(flight_ids), 8, len(flight_ids))
        xp.setDatab(self.tcas["icao_type"], bytes(icao_types), 8, len(icao_types))

    @staticmethod
    def _fixed_string(text, width):
        """定长、以 0 结尾的字段。TCAS 的字符串数组是按固定跨度排的。"""
        raw = (text or "").encode("ascii", errors="replace")[:width - 1]
        return raw + b"\x00" * (width - len(raw))

    def _clear_all(self):
        for aircraft in self.aircraft.values():
            aircraft.destroy()
        self.aircraft.clear()
        self.order = []
        self.tcas_written = 0
        if self.tcas_available and self.have_planes:
            try:
                # 目标数清零，否则 ND 上会留下一圈不动的光点
                xp.setDatavi(self.tcas["modeS"], [0] * MAX_TCAS_TARGETS,
                             1, MAX_TCAS_TARGETS)
            except Exception:
                pass
