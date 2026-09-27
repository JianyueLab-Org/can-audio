"""他机航迹表。

对应 xPilot 的 `src/aircrafts/`（记账）加上插件里 `NetworkAircraft` 的运动模型
（`plugin/src/network_aircraft.cpp`）。

这个模块**不碰 X-Plane、不碰网络**，只有数据和时间——所以它是这套东西里唯一
能完整测到的部分。插件和 FSD 各自把数据递进来：

    fsdpilot  --update_position-->  TrafficTable  --snapshot-->  渲染端

运动模型照搬 xPilot：每架飞机有一个**画出来的**位置和姿态（`Motion`），按
上报的速度和角速度逐帧积分，而不是在样本之间插值。新样本到达时不改画出来的
位置，只算一个误差速度 =（上报 − 画出）/ 2 秒，在接下来 2 秒里叠加到速度上，
所以飞机从不跳。角速度 0.5 秒没有更新就清零，姿态落到最后上报的那个。

位置从两种包来：`@`（所有客户端）和协议版本 101 的快速位置 `^` / `#SL` /
`#ST`（带速度矢量）。一架飞机只要发过快速位置，`@` 就不再动它的位置，只更新
应答机、模式、地速和"还活着"。从没发过的（只发 `@` 的老客户端），用相邻两个
`@` 算出速度和角速度，走同一条误差速度的路。

`Motion` 是纯的：只有 `receive()` 和 `advance(dt)`，不读时钟。第二阶段要把它
原样搬进 X-Plane 插件，写成这样才能两边对着测。

时间一律用 `clock()`（perf_counter）。time.time() 在 Windows 上只有 15.6 ms
的分辨率，同一次 recv 里读出来的两个包常常拿到同一个时间戳。

机型从哪来：`@` 包里没有。要靠 `#SB … PIR` 问对方，对方回
`PI:GEN:EQUIPMENT=B738:AIRLINE=CCA`。在拿到之前先按通用模型画，拿到之后再换
——所以 `Aircraft.model_dirty` 存在，让渲染端知道该重新匹配了。
"""

import logging
import math
import threading
import time

log = logging.getLogger("traffic")

# 超过这么久没有新位置就认为对方掉线了。FSD 正常 5 Hz，给足余量。
STALE_AFTER = 15.0
# 机型问不到就别一直问，隔这么久重试一次
PLANE_INFO_RETRY = 30.0
# 隔这么久重新要一次对方的配置（灯光/襟翼/起落架）。协议里没有"变了推一条"，
# 只能轮询；不问的话所有他机永远全程关灯、光杆落地。
CONFIG_REFRESH = 10.0
# 发送方会把同一份模拟器快照重复发几次（快照刷新比 5 Hz 慢）。和上一个样本
# 完全一样、又在这么久以内到的，不算新的位置——算的话 `@` 推出来的速度被拉成
# 零，快速位置的误差速度把飞机往旧位置拽。超过这个间隔还一样，才是真的停住了。
DUPLICATE_WINDOW = 2.0
# 误差速度：新样本和画出来的位置之差，在这么多秒里走完（xPilot 的 2000 ms）。
ERROR_TIME = 2.0
# 角速度这么久没有更新就清零，姿态落到最后上报的那个（xPilot 的 500 ms）。
ROTATION_HOLD = 0.5
# 切段时比这更短的余数不再积分
_EPSILON = 1e-12

FEET_PER_METRE = 3.280839895
METRES_PER_FOOT = 0.3048
NM_PER_DEGREE = 60.0
METRES_PER_DEGREE = 1852.0 * NM_PER_DEGREE
KNOTS_PER_MPS = 1.943844492


def clock():
    """单调、高分辨率的秒数。这个模块里所有时刻都从这里取。"""
    return time.perf_counter()


def _wrap_longitude(longitude):
    if longitude > 180.0:
        longitude -= 360.0
    elif longitude < -180.0:
        longitude += 360.0
    return longitude


# ---------- 四元数 ----------
#
# 逐条对照 xPilot 的 plugin/include/abacus.hpp。元组是 (x, y, z, w)，对应它的
# (I, J, K, U)。欧拉角是航空的 ZYX：航向绕 z、俯仰绕 y、坡度绕 x，抬头、右坡、
# 右转为正。姿态用四元数存，航向过 0°/360° 不用单独处理。

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
    """Quaternion::SlerpUnclamped(Identity, q, t)：同一根轴，转 t 倍的角度。

    xPilot 用 Slerp（t 夹在 0..1）；这里不夹，因为 advance() 自己把时间切段，
    一段可以超过一秒。同一根轴上 scale(q, a) * scale(q, b) == scale(q, a + b)，
    所以积分结果和帧率无关。
    """
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
    """一架飞机画出来的位置和姿态。xPilot `NetworkAircraft` 的运动部分。

    纯状态机，不读时钟：

        receive(...)   一个位置样本到了（HandleFastPositionUpdate +
                       UpdateVelocityVectors）
        advance(dt)    往前积分 dt 秒（UpdatePosition 的一帧）
        state()        画出来的 {latitude, longitude, altitude, pitch, bank,
                       heading}

    调用方负责先 advance 到样本到达的那一刻再 receive。

    单位和方向：
        位置      纬度/经度（度），altitude 英尺
        velocity  (东, 上, 北) 米每秒
        rotation  (俯仰, 航向, 坡度) 度每秒，**机体**角速度，抬头、右转、
                  右坡为正（和 X-Plane 的 Q/R/P 同向）
    """

    __slots__ = ("ready", "latitude", "longitude", "altitude", "orientation",
                 "target", "target_orientation", "velocity", "rotation",
                 "error_velocity", "error_rotation", "error_remaining",
                 "since_update")

    def __init__(self):
        self.ready = False              # 收到第一个样本之前没有东西可画
        self.latitude = 0.0             # 画出来的位置
        self.longitude = 0.0
        self.altitude = 0.0
        self.orientation = _IDENTITY    # 画出来的姿态
        # 最后上报的 (纬度, 经度, 高度, 俯仰, 坡度, 航向)，和它的四元数
        self.target = None
        self.target_orientation = _IDENTITY
        self.velocity = (0.0, 0.0, 0.0)
        self.rotation = (0.0, 0.0, 0.0)
        # 误差速度：(东, 上, 北) 米每秒，和 (俯仰, 航向, 坡度) 弧度每秒
        self.error_velocity = (0.0, 0.0, 0.0)
        self.error_rotation = (0.0, 0.0, 0.0)
        self.error_remaining = 0.0      # 误差速度还要叠加多少秒
        self.since_update = 0.0         # 距上一个样本多少秒（清角速度用）

    def receive(self, latitude, longitude, altitude, pitch, bank, heading,
                velocity=(0.0, 0.0, 0.0), rotation=(0.0, 0.0, 0.0)):
        """一个位置样本。第一个直接画在那里；之后只改速度和误差速度。"""
        self.target = (latitude, longitude, altitude, pitch, bank, heading)
        self.target_orientation = _attitude_quat(pitch, bank, heading)
        self.velocity = tuple(float(v) for v in velocity)
        self.rotation = tuple(float(v) for v in rotation)
        if not self.ready:
            # IsFirstRenderPending：画在上报的位置，没有误差
            self.ready = True
            self.latitude, self.longitude, self.altitude = latitude, longitude, altitude
            self.orientation = self.target_orientation
            self.error_velocity = (0.0, 0.0, 0.0)
            self.error_rotation = (0.0, 0.0, 0.0)
            self.error_remaining = 0.0
            self.since_update = 0.0
            return
        if self.since_update >= ROTATION_HOLD:
            # 上一个样本太久以前了：这个样本带来的角速度一起清掉，姿态直接
            # 落到它上报的那个（xPilot 在 UpdateVelocityVectors 里也是这样）
            self._clear_rotation()
        self.since_update = 0.0
        self._update_errors()

    def refresh(self, velocity, rotation):
        """同一份快照又来了一遍：只换速度，不重算误差。

        拿一个旧位置重算误差，会把已经往前推的飞机往回拽。
        """
        self.velocity = tuple(float(v) for v in velocity)
        self.rotation = tuple(float(v) for v in rotation)
        if self.since_update >= ROTATION_HOLD:
            self._clear_rotation()
        self.since_update = 0.0

    def _update_errors(self):
        """UpdateErrorVectors：(上报 − 画出) / ERROR_TIME。"""
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
        """ClearRotationalVelocities。"""
        self.rotation = (0.0, 0.0, 0.0)
        self.error_rotation = (0.0, 0.0, 0.0)
        self.orientation = self.target_orientation

    def advance(self, dt):
        """往前积分 dt 秒。

        在误差速度到期、角速度到期这两个时刻切段，所以同样的总时长不管切成
        几帧，结果都一样——30 Hz 的注入循环、10 Hz 的推送和插件的每一帧对得上。
        """
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
        """ExtrapolatePosition：速度（加误差速度）乘时间。"""
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
        """画出来的 (俯仰, 坡度, 航向)，度。航向 0..360。"""
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


def derive_rates(older, newer):
    """两个 `@` 样本之间的速度和角速度，给只发 `@` 的客户端用。

    返回 ((东, 上, 北) 米每秒, (俯仰, 航向, 坡度) 度每秒)。角速度按机体轴算，
    和误差速度同一个办法（两个姿态之间的四元数差），所以航向过北不绕远路。
    """
    span = newer.time - older.time
    if span <= 0:
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
    north = (newer.latitude - older.latitude) * METRES_PER_DEGREE
    east = (_wrap_longitude(newer.longitude - older.longitude) * METRES_PER_DEGREE
            * math.cos(math.radians(newer.latitude)))
    up = (newer.altitude - older.altitude) * METRES_PER_FOOT
    delta = quat_multiply(
        quat_inverse(_attitude_quat(older.pitch, older.bank, older.heading)),
        _attitude_quat(newer.pitch, newer.bank, newer.heading))
    pitch, yaw, roll = quat_to_euler(delta)
    return ((east / span, up / span, north / span),
            (math.degrees(pitch) / span, math.degrees(yaw) / span,
             math.degrees(roll) / span))


class Sample:
    """一个时刻上报的位置和姿态。"""

    __slots__ = ("time", "latitude", "longitude", "altitude",
                 "pitch", "bank", "heading", "on_ground", "groundspeed",
                 "velocity", "rotation", "agl", "nose_wheel")

    def __init__(self, time, latitude, longitude, altitude,
                 pitch, bank, heading, on_ground, groundspeed, velocity=None,
                 rotation=None, agl=None, nose_wheel=None):
        self.time = time
        self.latitude = latitude
        self.longitude = longitude
        self.altitude = altitude          # 英尺，真高
        self.pitch = pitch
        self.bank = bank
        self.heading = heading
        self.on_ground = on_ground
        self.groundspeed = groundspeed    # 节
        # (东, 上, 北) 米每秒。只有快速位置包带；`@` 包是 None。
        self.velocity = velocity
        # (俯仰, 航向, 坡度) 度每秒，机体轴。只有快速位置包带。
        self.rotation = rotation
        self.agl = agl                    # 英尺，离地高
        self.nose_wheel = nose_wheel      # 度，前轮转角

    def same_place(self, other):
        """位置和姿态完全一样——发送方重复发了同一份快照。"""
        return (self.latitude == other.latitude
                and self.longitude == other.longitude
                and self.altitude == other.altitude
                and self.pitch == other.pitch
                and self.bank == other.bank
                and self.heading == other.heading)


class Aircraft:
    """一架他机：记账（机型、配置、应答机）加一个 Motion。

    `state_at(now)` 把 Motion 积分到 now 再返回画出来的状态。now 只能往前走：
    积分不能倒回去，更早的 now 拿到的就是当前状态。
    """

    def __init__(self, callsign):
        self.callsign = callsign
        self.squawk = 0
        self.transponder_mode = "S"
        self.motion = Motion()
        # Motion 积分到了哪个时刻。第一个位置样本之前是 None。
        self.time = None
        # 最近收下的位置样本。只发 `@` 的客户端靠 previous → latest 算速度。
        self.previous = None
        self.latest = None
        # 发过 ^ / #SL / #ST 之后，`@` 就只是心跳。不会再变回去。
        self.has_velocity = False
        # 建表时刻。机型先于位置到达的那些 latest 是 None，prune 按这个给
        # 它们留一段宽限，不然半秒后就被当成"太久没消息"清掉了。
        self.created = clock()
        # 最近一次收到它任何位置包的时刻。重复样本不进 latest，但对方显然还
        # 在线，prune 看的是这个。还没收到过位置时是 None。
        self.last_seen = None

        # 机型匹配信息，来自 #SB PI:GEN
        self.equipment = ""          # ICAO 机型码，如 B738
        self.airline = ""            # 航司码，如 CCA
        self.livery = ""
        self.csl = ""                # 对方直接指定的 CSL 名
        self.model_dirty = True      # 渲染端该（重新）匹配模型了
        # 上次发 PIR / 要 ACC 配置的时刻。起点是负无穷：单调钟的零点不固定，
        # 写 0.0 的话开机不到 30 秒时第一次询问会被跳过。
        self.info_requested = float("-inf")
        self.config_requested = float("-inf")

        # 来自 $CQ … ACC 的配置，用来驱动动画
        self.gear_down = None
        self.flaps = 0.0
        self.spoilers = False
        self.lights = {}
        self.engines_on = True

    @property
    def has_plane_info(self):
        return bool(self.equipment or self.csl)

    def _advance(self, now):
        if self.time is None or now <= self.time:
            return
        self.motion.advance(now - self.time)
        self.time = now

    def _receive(self, sample, velocity, rotation):
        """积分到样本那一刻，再把样本交给 Motion。"""
        if self.time is None:
            self.time = sample.time
        self._advance(sample.time)
        self.motion.receive(sample.latitude, sample.longitude, sample.altitude,
                            sample.pitch, sample.bank, sample.heading,
                            velocity=velocity, rotation=rotation)

    def update(self, sample, squawk=None, mode=None):
        self.last_seen = (sample.time if self.last_seen is None
                          else max(self.last_seen, sample.time))
        if sample.velocity is not None:
            self._update_fast(sample)
        elif self.has_velocity:
            # 发过快速位置的飞机：`@` 只带来应答机、模式和地速
            if self.latest is not None:
                self.latest.groundspeed = sample.groundspeed
        else:
            self._update_slow(sample)
        if squawk is not None:
            self.squawk = squawk
        if mode is not None:
            self.transponder_mode = mode

    def _update_fast(self, sample):
        """^ / #SL / #ST：上报的速度直接用。"""
        self.has_velocity = True
        rotation = sample.rotation or (0.0, 0.0, 0.0)
        latest = self.latest
        if latest is not None and sample.time < latest.time:
            sample.time = latest.time
        if (latest is not None and any(sample.velocity)
                and sample.same_place(latest)
                and sample.time - latest.time < DUPLICATE_WINDOW):
            # 同一份快照又发了一遍，飞机其实在动：只换速度，位置不算数
            self._advance(sample.time)
            self.motion.refresh(sample.velocity, rotation)
            latest.velocity, latest.rotation = sample.velocity, rotation
            latest.groundspeed = sample.groundspeed
            return
        self._receive(sample, sample.velocity, rotation)
        self.previous, self.latest = latest, sample

    def _update_slow(self, sample):
        """只发 `@` 的客户端：用相邻两个样本算速度。"""
        latest = self.latest
        if latest is None:
            self._receive(sample, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
            self.latest = sample
            return
        if sample.time <= latest.time:
            # 时间戳打平（同一次 recv 读出的两个包）：新的那个替换 latest，
            # previous 不动。丢掉新的就是丢掉更新的位置；拿它当新一段的话
            # 两点间隔是零，速度除零。
            sample.time = latest.time
            older = self.previous
            self.latest = sample
        elif (sample.same_place(latest)
                and sample.time - latest.time < DUPLICATE_WINDOW):
            # 同一份快照又发了一遍。当新样本的话前后两点一样，速度被拉成
            # 零，飞机在两次真正的更新之间停下来。
            return
        else:
            older = latest
            self.previous, self.latest = latest, sample
        if older is not None and older.time < sample.time:
            velocity, rotation = derive_rates(older, sample)
        else:
            velocity, rotation = self.motion.velocity, self.motion.rotation
        self._receive(sample, velocity, rotation)

    def set_plane_info(self, equipment=None, airline=None, livery=None, csl=None):
        """收到 PI:GEN。有变化才置脏，免得渲染端反复重新加载模型。"""
        changed = False
        for name, value in (("equipment", equipment), ("airline", airline),
                            ("livery", livery), ("csl", csl)):
            if value is None:
                continue
            value = value.strip().upper()
            if value and getattr(self, name) != value:
                setattr(self, name, value)
                changed = True
        if changed:
            self.model_dirty = True
        return changed

    @property
    def vertical_speed(self):
        """英尺每分钟，取当前用来积分的垂直速度。"""
        return self.motion.velocity[1] * FEET_PER_METRE * 60.0

    def state_at(self, now):
        """积分到 now，返回画出来的状态。没有数据返回 None。"""
        if not self.motion.ready:
            return None
        self._advance(now)
        state = self.motion.state()
        latest = self.latest
        state["on_ground"] = latest.on_ground
        state["groundspeed"] = latest.groundspeed
        state["agl"] = latest.agl
        state["nose_wheel"] = latest.nose_wheel or 0.0
        return state


class TrafficTable:
    """所有他机。FSD 线程写，渲染线程读，所以整体上锁。"""

    def __init__(self, on_request_info=None, on_request_config=None):
        # on_request_info(callsign) —— 需要向对方要机型时调用
        # on_request_config(callsign) —— 需要向对方要配置（灯光等）时调用
        self.on_request_info = on_request_info
        self.on_request_config = on_request_config
        self.aircraft = {}
        self._lock = threading.Lock()

    def __len__(self):
        with self._lock:
            return len(self.aircraft)

    def __contains__(self, callsign):
        with self._lock:
            return callsign in self.aircraft

    def get(self, callsign):
        with self._lock:
            return self.aircraft.get(callsign)

    def update_position(self, callsign, latitude, longitude, altitude,
                        pitch, bank, heading, on_ground=False,
                        groundspeed=0, squawk=None, mode=None, now=None,
                        velocity=None, rotation=None, agl=None, nose_wheel=None):
        """收到一个位置包。

        `velocity` 是 (东, 上, 北) 米每秒，`rotation` 是 (俯仰, 航向, 坡度)
        度每秒（机体轴，抬头/右转/右坡为正）。只有快速位置包（`^` / `#SL` /
        `#ST`）才有；`@` 包不传。
        """
        now = now if now is not None else clock()
        with self._lock:
            aircraft = self.aircraft.get(callsign)
            if aircraft is None:
                aircraft = Aircraft(callsign)
                self.aircraft[callsign] = aircraft
                # 降 DEBUG：紧跟着的模型匹配那行已经点了名。离线那条
                # 留在 INFO——飞机什么时候消失的，是查问题要看的
                log.debug("new aircraft %s", callsign)
            aircraft.update(Sample(now, latitude, longitude, altitude, pitch,
                                   bank, heading, on_ground, groundspeed,
                                   velocity=velocity, rotation=rotation,
                                   agl=agl, nose_wheel=nose_wheel),
                            squawk=squawk, mode=mode)
            needs_info = (not aircraft.has_plane_info
                          and now - aircraft.info_requested > PLANE_INFO_RETRY)
            if needs_info:
                aircraft.info_requested = now
            needs_config = now - aircraft.config_requested > CONFIG_REFRESH
            if needs_config:
                aircraft.config_requested = now

        # 回调放在锁外面：它会去发包，别把网络 IO 圈进锁里
        if needs_info and self.on_request_info:
            try:
                self.on_request_info(callsign)
            except Exception as e:
                log.warning("could not ask %s for its aircraft type: %s", callsign, e)
        if needs_config and self.on_request_config:
            try:
                self.on_request_config(callsign)
            except Exception as e:
                log.warning("could not ask %s for its configuration: %s", callsign, e)
        return aircraft

    def set_plane_info(self, callsign, **info):
        with self._lock:
            aircraft = self.aircraft.get(callsign)
            if aircraft is None:
                # 机型先于位置到达也要留住，别丢
                aircraft = Aircraft(callsign)
                self.aircraft[callsign] = aircraft
            changed = aircraft.set_plane_info(**info)
        if changed:
            # 降到 DEBUG：紧跟着的"最终匹配"那行本来就带机型和航司，
            # 而每架飞机都要来一次，真实日志里这类占了三成
            log.debug("%s is a %s/%s", callsign,
                     aircraft.equipment or "?", aircraft.airline or "?")
        return aircraft

    def set_config(self, callsign, config):
        """$CQ … ACC 的 config JSON，用来驱动动画。"""
        with self._lock:
            aircraft = self.aircraft.get(callsign)
            if aircraft is None:
                return None
            if "gear_down" in config:
                aircraft.gear_down = bool(config["gear_down"])
            if "flaps_pct" in config:
                try:
                    aircraft.flaps = max(0.0, min(1.0, float(config["flaps_pct"]) / 100.0))
                except (TypeError, ValueError):
                    pass
            if "spoilers_out" in config:
                aircraft.spoilers = bool(config["spoilers_out"])
            if isinstance(config.get("lights"), dict):
                aircraft.lights.update(config["lights"])
            engines = config.get("engines")
            if isinstance(engines, dict) and engines:
                aircraft.engines_on = any(
                    isinstance(e, dict) and e.get("on") for e in engines.values())
            return aircraft

    def remove(self, callsign):
        with self._lock:
            if self.aircraft.pop(callsign, None):
                log.info("%s went offline", callsign)
                return True
            return False

    def prune(self, now=None):
        """清掉太久没消息的。返回被清掉的呼号。

        看的是最近一次收到位置包的时刻（last_seen），不是 latest 的时刻：
        重复的快照不进 latest，但说明对方还在。

        还没收到过位置的是"机型先到、位置未到"的（set_plane_info 特意留住
        它们），按建表时刻给同样的宽限——立刻清掉的话，PI:GEN 白收了，等位置
        到达时机型又得重新问一轮。
        """
        now = now if now is not None else clock()
        with self._lock:
            gone = [callsign for callsign, aircraft in self.aircraft.items()
                    if now - (aircraft.created if aircraft.last_seen is None
                              else aircraft.last_seen) > STALE_AFTER]
            for callsign in gone:
                del self.aircraft[callsign]
        for callsign in gone:
            log.info("dropped %s, no updates for too long", callsign)
        return gone

    def snapshot(self, now=None, origin=None, limit=None, max_range_nm=None):
        """给渲染端的一份数据：每架飞机积分到 now 的状态。

        每次调用都会把飞机往前推到 now，所以 now 要只增不减（默认的 clock()
        就是）。两个线程各按各的节奏来取也没关系，积分按实际经过的时间算。

        origin 是本机 (纬度, 经度)。给了就按距离排序并可以截断——TCAS 只有 64
        个位置，飞机比这多的时候必须先扔远的，不能随便扔。
        """
        now = now if now is not None else clock()
        # 整个快照都在锁里做：全是字典和算术，没有 IO。原来只锁着取列表，
        # 后面读 lights 时 FSD 线程一条 set_config 更新进来就是
        # RuntimeError: dictionary changed size during iteration，丢一帧。
        entries = []
        with self._lock:
            aircraft = list(self.aircraft.values())
            for one in aircraft:
                position = one.state_at(now)
                if not position:
                    continue
                entry = {
                    "callsign": one.callsign,
                    "squawk": one.squawk,
                    "mode": one.transponder_mode,
                    "equipment": one.equipment,
                    "airline": one.airline,
                    "livery": one.livery,
                    "csl": one.csl,
                    "model_dirty": one.model_dirty,
                    "vertical_speed": one.vertical_speed,
                    "gear_down": one.gear_down,
                    "flaps": one.flaps,
                    "spoilers": one.spoilers,
                    "lights": dict(one.lights),
                    "engines_on": one.engines_on,
                }
                entry.update(position)
                if origin:
                    entry["range_nm"] = distance_nm(
                        origin[0], origin[1],
                        position["latitude"], position["longitude"])
                entries.append(entry)

        if origin:
            entries.sort(key=lambda e: e["range_nm"])
            if max_range_nm is not None:
                entries = [e for e in entries if e["range_nm"] <= max_range_nm]
        if limit is not None:
            entries = entries[:limit]
        return entries

    def mark_model_clean(self, callsign, equipment=None, airline=None):
        """渲染端匹配过模型之后回来清标记。

        带上匹配时用的机型/航司：如果 #SB 的回复恰好在快照和这里之间落地，
        无条件清标记会把那次更新吞掉——飞机从此停在通用模型上，再也不重新
        匹配。对不上就把标记留着，下一帧再匹配一次。
        """
        with self._lock:
            aircraft = self.aircraft.get(callsign)
            if aircraft is None:
                return
            if equipment is not None and aircraft.equipment != equipment:
                return
            if airline is not None and aircraft.airline != airline:
                return
            aircraft.model_dirty = False


def distance_nm(lat1, lon1, lat2, lon2):
    """两点距离（海里）。平面近似，几百海里内够用，比 haversine 便宜。"""
    mean_lat = math.radians((lat1 + lat2) / 2.0)
    dy = (lat2 - lat1) * NM_PER_DEGREE
    # 经度差按最短弧算，跨 180° 经线时才不会得出"绕地球一圈"的距离
    dlon = (lon2 - lon1 + 180.0) % 360.0 - 180.0
    dx = dlon * NM_PER_DEGREE * math.cos(mean_lat)
    return math.hypot(dx, dy)
