"""他机航迹表。

对应 xPilot 的 `src/aircrafts/`。FSD 的 `@` 位置包一秒才 5 个，直接照着画飞机
会一跳一跳，所以这里保留每架飞机的前后两个采样，由渲染端按时间插值。

这个模块**不碰 X-Plane、不碰网络**，只有数据和时间——所以它是这套东西里唯一
能完整测到的部分。插件和 FSD 各自把数据递进来：

    fsdpilot  --on_traffic-->  TrafficTable  --snapshot-->  插件（画 + TCAS）

位置从两种包来：`@`（所有客户端）和协议版本 101 的快速位置 `^` / `#SL` /
`#ST`（VATSIM Velocity 客户端，带速度矢量）。带速度的样本按速度外推，不靠
前后两点猜；有新鲜的带速度样本时，`@` 只更新应答机和地速，不再当位置样本，
免得两种精度的包交替进来让飞机来回抖。

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
# 插值最多外推这么久。再久就不如让飞机停住，免得跑出天际。
MAX_EXTRAPOLATE = 2.0
# 机型问不到就别一直问，隔这么久重试一次
PLANE_INFO_RETRY = 30.0
# 隔这么久重新要一次对方的配置（灯光/襟翼/起落架）。协议里没有"变了推一条"，
# 只能轮询；不问的话所有他机永远全程关灯、光杆落地。
CONFIG_REFRESH = 10.0
# 发送方会把同一份模拟器快照重复发几次（快照刷新比 5 Hz 慢）。和上一个样本
# 完全一样、又在这么久以内到的，不算新样本——算的话速度被拉成零，飞机一走
# 一停。超过这个间隔还一样，才是真的停住了。
DUPLICATE_WINDOW = 2.0
# 带速度的样本最多外推这么久。#SL 五秒一个，外推要能盖住这个间隔。
MAX_VELOCITY_EXTRAPOLATE = 6.0
# 这么久以内收到过带速度的样本，`@` 就不再当位置样本用。
VELOCITY_FRESH = 7.0

FEET_PER_METRE = 3.280839895
NM_PER_DEGREE = 60.0
METRES_PER_DEGREE = 1852.0 * NM_PER_DEGREE
KNOTS_PER_MPS = 1.943844492


def clock():
    """单调、高分辨率的秒数。这个模块里所有时刻都从这里取。"""
    return time.perf_counter()


class Sample:
    """一个时刻的位置和姿态。"""

    __slots__ = ("time", "latitude", "longitude", "altitude",
                 "pitch", "bank", "heading", "on_ground", "groundspeed",
                 "velocity")

    def __init__(self, time, latitude, longitude, altitude,
                 pitch, bank, heading, on_ground, groundspeed, velocity=None):
        self.time = time
        self.latitude = latitude
        self.longitude = longitude
        self.altitude = altitude          # 英尺
        self.pitch = pitch
        self.bank = bank
        self.heading = heading
        self.on_ground = on_ground
        self.groundspeed = groundspeed    # 节
        # (北, 东, 上)，米每秒。只有快速位置包带；`@` 包是 None。
        self.velocity = velocity

    def same_place(self, other):
        """位置和姿态完全一样——发送方重复发了同一份快照。"""
        return (self.latitude == other.latitude
                and self.longitude == other.longitude
                and self.altitude == other.altitude
                and self.pitch == other.pitch
                and self.bank == other.bank
                and self.heading == other.heading)


def _interpolate_angle(a, b, ratio):
    """按最短弧插值。359° 到 1° 应当往前走 2°，而不是倒着走 358°。"""
    difference = (b - a + 180.0) % 360.0 - 180.0
    return (a + difference * ratio) % 360.0


class Aircraft:
    """一架他机。"""

    def __init__(self, callsign):
        self.callsign = callsign
        self.squawk = 0
        self.transponder_mode = "S"
        self.previous = None
        self.latest = None
        # 建表时刻。机型先于位置到达的那些 latest 是 None，prune 按这个给
        # 它们留一段宽限，不然半秒后就被当成"太久没消息"清掉了。
        self.created = clock()
        # 最近一次收到它任何位置包的时刻。重复样本不进 latest，但对方显然还
        # 在线，prune 看的是这个。还没收到过位置时是 None。
        self.last_seen = None
        self.last_velocity = None    # 最近一次带速度样本的时刻

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

    def update(self, sample, squawk=None, mode=None):
        self.last_seen = (sample.time if self.last_seen is None
                          else max(self.last_seen, sample.time))
        latest = self.latest
        if sample.velocity is not None:
            self.last_velocity = sample.time
        elif (latest is not None and self.last_velocity is not None
                and sample.time - self.last_velocity < VELOCITY_FRESH):
            # 有新鲜的快速位置包时，`@` 只带来应答机和地速
            latest.groundspeed = sample.groundspeed
            sample = None

        if sample is None:
            pass
        elif latest is None:
            self.latest = sample
        elif sample.time <= latest.time:
            # 时间戳打平（同一次 recv 读出的两个包）：新的那个替换 latest，
            # previous 不动。丢掉新的就是丢掉更新的位置；拿它当新一段的话
            # 两点间隔是零，插值除零。
            sample.time = latest.time
            self.latest = sample
        elif (sample.velocity is None and sample.same_place(latest)
                and sample.time - latest.time < DUPLICATE_WINDOW):
            # 同一份快照又发了一遍。当新样本的话前后两点一样，速度被拉成
            # 零，飞机在两次真正的更新之间停下来。
            pass
        else:
            self.previous = latest
            self.latest = sample
        if squawk is not None:
            self.squawk = squawk
        if mode is not None:
            self.transponder_mode = mode

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
        """英尺每分钟。带速度的样本直接用；否则要两个采样才算得出来。"""
        if self.latest and self.latest.velocity is not None:
            return self.latest.velocity[2] * FEET_PER_METRE * 60.0
        if not (self.previous and self.latest):
            return 0.0
        span = self.latest.time - self.previous.time
        if span <= 0:
            return 0.0
        return (self.latest.altitude - self.previous.altitude) / span * 60.0

    def position_at(self, now):
        """插值出 now 时刻的位置。没有数据返回 None。

        两个采样之间走线性插值；超过最后一个采样就按最后的速度外推，但最多
        外推 MAX_EXTRAPOLATE 秒——对方掉线时飞机应当停在原地，不是一直飞下去。
        """
        latest = self.latest
        if not latest:
            return None
        previous = self.previous
        if not previous or latest.time <= previous.time:
            return self._dead_reckon(self._as_dict(latest), latest, now)

        span = latest.time - previous.time
        ratio = (now - previous.time) / span
        # 往前不外推（收到乱序包时会出现），往后最多外推固定秒数
        limit = 1.0 + min(MAX_EXTRAPOLATE, span * 2) / span
        ratio = max(0.0, min(limit, ratio))

        # 经度也要按最短弧走：179.98°E 到 -179.98° 是往前 0.04°，
        # 线性差值会让飞机横穿整个地球再回来
        lon_step = (latest.longitude - previous.longitude + 180.0) % 360.0 - 180.0
        longitude = previous.longitude + lon_step * ratio
        if longitude > 180.0:
            longitude -= 360.0
        elif longitude < -180.0:
            longitude += 360.0

        return self._dead_reckon({
            "latitude": previous.latitude + (latest.latitude - previous.latitude) * ratio,
            "longitude": longitude,
            "altitude": previous.altitude + (latest.altitude - previous.altitude) * ratio,
            "pitch": previous.pitch + (latest.pitch - previous.pitch) * ratio,
            "bank": previous.bank + (latest.bank - previous.bank) * ratio,
            "heading": _interpolate_angle(previous.heading, latest.heading, ratio),
            "on_ground": latest.on_ground,
            "groundspeed": latest.groundspeed,
        }, latest, now)

    @staticmethod
    def _dead_reckon(position, latest, now):
        """latest 带速度的话，位置按速度从 latest 往前推；姿态照旧。

        对方报的速度比两点连线准：两点之间还夹着发送方快照的刷新抖动。
        """
        if latest.velocity is None:
            return position
        north, east, up = latest.velocity
        elapsed = max(0.0, min(MAX_VELOCITY_EXTRAPOLATE, now - latest.time))
        latitude = latest.latitude + north * elapsed / METRES_PER_DEGREE
        cos_lat = max(1e-6, math.cos(math.radians(latest.latitude)))
        longitude = latest.longitude + east * elapsed / (METRES_PER_DEGREE * cos_lat)
        if longitude > 180.0:
            longitude -= 360.0
        elif longitude < -180.0:
            longitude += 360.0
        position["latitude"] = latitude
        position["longitude"] = longitude
        position["altitude"] = latest.altitude + up * elapsed * FEET_PER_METRE
        return position

    @staticmethod
    def _as_dict(sample):
        return {
            "latitude": sample.latitude, "longitude": sample.longitude,
            "altitude": sample.altitude, "pitch": sample.pitch,
            "bank": sample.bank, "heading": sample.heading,
            "on_ground": sample.on_ground, "groundspeed": sample.groundspeed,
        }


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
                        velocity=None):
        """收到一个位置包。

        `velocity` 是 (北, 东, 上) 米每秒，只有快速位置包（`^` / `#SL` /
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
                                   velocity=velocity),
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
        """给渲染端的一份数据，已经插值到 now。

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
                position = one.position_at(now)
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
