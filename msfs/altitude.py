"""高度换算。xpc 和 msfs 逐字节共享（SharedCopyTest）。

照 xPilot（client/src/network/networkmanager.cpp）：

    网络高度   真高 + 高度表温度误差。`@` 第 7 段、`^`/`#SL`/`#ST` 第 4 段。
    气压高度   标准气压面（1013.25 hPa）上的高度。状态栏显示它。
    修正量     气压高度 − 网络高度。`@` 最后一段。
    他机高度   收到的高度 − 自己的温度误差 × 权重（adjust_incoming_altitude）。

snapshot 里的 `altitude` 仍是真高（几何高度），另有 `network_altitude`、
`pressure_altitude`、`temperature_error` 和 `pressure_delta`。

海平面气压和高度之间的换算用 ISA 气压公式（isa_altitude / pressure_altitude），
不用 xPilot 的 30 ft/hPa。线性近似在 FL380、海压 1030 hPa 时差 168 ft。

没有 import 任何东西。
"""

ISA_SEA_LEVEL_HPA = 1013.25
# 145366.45 × (1 − (p / 1013.25) ^ 0.190284)：NOAA 的气压高度公式，英尺。
ISA_SCALE_FT = 145366.45
ISA_EXPONENT = 0.190284
INHG_TO_HPA = 33.8639

# 超出这个范围的海平面气压是没读到或读错了，不参与换算。
SEA_LEVEL_RANGE_HPA = (870.0, 1090.0)
# 超出这个范围的温度误差是没读到或单位错了（PRESSURE ALTITUDE 按米读会差出
# 上万英尺），按 0 处理。
MAX_TEMPERATURE_ERROR_FT = 5000.0

# adjust_incoming_altitude 的权重：垂直距离 3000 ft 以内全额，6000 ft 以外不修。
ADJUST_FULL_FT = 3000.0
ADJUST_NONE_FT = 6000.0


def sea_level_valid(sea_level_hpa):
    """海平面气压是否可用。"""
    return (sea_level_hpa is not None
            and SEA_LEVEL_RANGE_HPA[0] <= sea_level_hpa <= SEA_LEVEL_RANGE_HPA[1])


def _ratio(sea_level_hpa):
    return (sea_level_hpa / ISA_SEA_LEVEL_HPA) ** ISA_EXPONENT


def standard_surface_height(sea_level_hpa):
    """1013.25 hPa 气压面在海平面之上的高度，英尺，ISA 温度。

    海压高于 1013.25 时为正。1030 hPa → 约 453 ft，990 hPa → 约 −644 ft。
    """
    return ISA_SCALE_FT * (1.0 - (ISA_SEA_LEVEL_HPA / sea_level_hpa) ** ISA_EXPONENT)


def pressure_altitude(altitude_ft, sea_level_hpa):
    """ISA 温度、海平面气压为 sea_level_hpa 的大气里，高度 altitude_ft 处的气压高度。"""
    return ISA_SCALE_FT * (1.0 - _ratio(sea_level_hpa)
                           * (1.0 - altitude_ft / ISA_SCALE_FT))


def isa_altitude(pressure_altitude_ft, sea_level_hpa):
    """pressure_altitude() 的反函数：高度表拨海平面气压时的读数，英尺。"""
    return ISA_SCALE_FT * (1.0 - (1.0 - pressure_altitude_ft / ISA_SCALE_FT)
                           / _ratio(sea_level_hpa))


def _temperature_error(value):
    if value is None or abs(value) > MAX_TEMPERATURE_ERROR_FT:
        return 0.0
    return value


def xplane_altitudes(elevation_ft, sea_level_hpa, pressure_altitude_ft=None,
                     temperature_error_ft=None):
    """X-Plane：返回 (网络高度, 气压高度, 温度误差)，英尺。

    X-Plane 12 推 `sim/flightmodel2/position/pressure_altitude` 和
    `sim/weather/aircraft/altimeter_temperature_error`：
        网络高度 = 真高 + 温度误差，气压高度 = dataref。
    X-Plane 11 没有这两个：
        网络高度 = 真高，气压高度 = pressure_altitude(真高, 海平面气压)。
    海平面气压也读不到时气压高度等于网络高度，修正量为 0。
    pressure_altitude 和网络高度差出 MAX_TEMPERATURE_ERROR_FT 以上（推来的是 0）
    按 X-Plane 11 处理。
    """
    if pressure_altitude_ft is not None:
        error = _temperature_error(temperature_error_ft)
        network = elevation_ft + error
        if abs(pressure_altitude_ft - network) <= MAX_TEMPERATURE_ERROR_FT:
            return network, pressure_altitude_ft, error
    if sea_level_valid(sea_level_hpa):
        return (elevation_ft, pressure_altitude(elevation_ft, sea_level_hpa), 0.0)
    return elevation_ft, elevation_ft, 0.0


def msfs_altitudes(altitude_ft, sea_level_hpa, pressure_altitude_ft=None):
    """MSFS：返回 (网络高度, 气压高度, 温度误差)，英尺。

    MSFS 没有温度误差 SimVar。网络高度取高度表拨海平面气压时的读数：
        网络高度 = isa_altitude(PRESSURE ALTITUDE, SEA LEVEL PRESSURE)
        温度误差 = 网络高度 − PLANE ALTITUDE
    和 X-Plane 12 的 elevation + altimeter_temperature_error 是同一个量。
    PRESSURE ALTITUDE 读不到，或和真高差出 MAX_TEMPERATURE_ERROR_FT 以上
    （按米读就是这样）：网络高度 = 气压高度 = 真高。
    海平面气压读不到：网络高度 = 真高，气压高度 = PRESSURE ALTITUDE。
    """
    if (pressure_altitude_ft is None
            or abs(pressure_altitude_ft - altitude_ft) > MAX_TEMPERATURE_ERROR_FT):
        return altitude_ft, altitude_ft, 0.0
    if not sea_level_valid(sea_level_hpa):
        return altitude_ft, pressure_altitude_ft, 0.0
    error = isa_altitude(pressure_altitude_ft, sea_level_hpa) - altitude_ft
    if abs(error) > MAX_TEMPERATURE_ERROR_FT:
        return altitude_ft, altitude_ft, 0.0
    return altitude_ft + error, pressure_altitude_ft, error


def adjust_incoming_altitude(altitude_ft, own_network_ft, own_temperature_error_ft):
    """他机的网络高度 -> 本机模拟器里画它的高度。xPilot 的 AdjustIncomingAltitude。

    自己的参考高度是自己的网络高度（真高 + 温度误差）。垂直距离 3000 ft 以内
    减去全部温度误差，3000–6000 ft 线性减到 0，6000 ft 以外不改。
    没有本机快照（own_network_ft 为 None）时不改。
    """
    if own_network_ft is None or not own_temperature_error_ft:
        return altitude_ft
    distance = abs(own_network_ft - altitude_ft)
    if distance > ADJUST_NONE_FT:
        return altitude_ft
    weight = 1.0
    if distance > ADJUST_FULL_FT:
        weight = 1.0 - (distance - ADJUST_FULL_FT) / (ADJUST_NONE_FT - ADJUST_FULL_FT)
    return altitude_ft - own_temperature_error_ft * weight
