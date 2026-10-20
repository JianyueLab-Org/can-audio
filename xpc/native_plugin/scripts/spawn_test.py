"""在本机飞机正前方放一架测试他机，不用连 FSD 就能验证原生插件能不能画。

    python spawn_test.py                      # 前方 150 m 一架 CCA 的 A320
    python spawn_test.py --icao B738 --airline CSN --distance 300
    python spawn_test.py --csl "D:\\CSL\\X-CSL"

本机位置从 X-Plane 自带的 UDP 接口读（127.0.0.1:49000 订阅 RREF，不需要在
X-Plane 里开任何设置）；他机按插件协议 v3 每秒推一帧到 127.0.0.1:49900。
客户端（xpc-for-can.exe）要先关掉，两边会给插件推不同的他机表。

Ctrl+C 结束；插件 30 秒收不到帧会自己把飞机清掉。
"""

import argparse
import base64
import json
import math
import socket
import struct
import time

PLUGIN = ("127.0.0.1", 49900)
XPLANE = ("127.0.0.1", 49000)
PROTOCOL_VERSION = 3
DEFAULT_CSL = r"D:\SteamLibrary\steamapps\common\X-Plane 12\Resources\plugins\IVAO_CSL\CSL"

DATAREFS = [
    "sim/flightmodel/position/latitude",
    "sim/flightmodel/position/longitude",
    "sim/flightmodel/position/elevation",   # 米
    "sim/flightmodel/position/true_psi",
    "sim/flightmodel/position/y_agl",       # 米
]


def subscribe(sock, frequency):
    for index, name in enumerate(DATAREFS):
        sock.sendto(b"RREF\x00" + struct.pack("<ii400s", frequency, index, name.encode()), XPLANE)


def read_own(sock, timeout=5.0):
    """收一个 RREF 包，返回 {下标: 值}；X-Plane 没开就超时返回 None。"""
    sock.settimeout(timeout)
    values = {}
    deadline = time.time() + timeout
    while len(values) < len(DATAREFS) and time.time() < deadline:
        try:
            data, _ = sock.recvfrom(4096)
        except socket.timeout:
            return None
        if not data.startswith(b"RREF"):
            continue
        for offset in range(5, len(data) - 7, 8):
            index, value = struct.unpack_from("<if", data, offset)
            values[index] = value
    return values if len(values) == len(DATAREFS) else None


def ahead(lat, lon, heading_deg, metres):
    north = metres * math.cos(math.radians(heading_deg))
    east = metres * math.sin(math.radians(heading_deg))
    return (lat + north / 111_320.0,
            lon + east / (111_320.0 * max(1e-6, math.cos(math.radians(lat)))))


def encode(message, sequence):
    raw = json.dumps(message, separators=(",", ":")).encode("utf-8")
    header = {"v": PROTOCOL_VERSION, "seq": sequence, "part": 0, "total": 1,
              "data": base64.b64encode(raw).decode("ascii")}
    return json.dumps(header).encode("utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--callsign", default="XPCTEST")
    parser.add_argument("--icao", default="A320")
    parser.add_argument("--airline", default="CCA")
    parser.add_argument("--distance", type=float, default=150.0, help="前方多少米")
    parser.add_argument("--csl", default=DEFAULT_CSL, help="交给插件加载的 CSL 目录")
    args = parser.parse_args()

    xp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    xp.bind(("127.0.0.1", 0))
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    subscribe(xp, 2)
    sequence = 0
    anchor = None
    try:
        while True:
            own = read_own(xp)
            if own is None:
                print("没收到 X-Plane 的数据：X-Plane 开着吗？已经进到飞行里了吗？")
                subscribe(xp, 2)
                continue
            lat, lon, elev_m, psi, agl_m = (own[i] for i in range(len(DATAREFS)))
            if anchor is None:
                # 只在第一帧定位置：之后飞机停在原地，你可以绕着它看
                anchor = ahead(lat, lon, psi, args.distance) + (elev_m, psi, agl_m)
                print(f"本机 {lat:.5f} {lon:.5f} 高 {elev_m / 0.3048:.0f} ft，航向 {psi:.0f}°")
                print(f"{args.callsign} ({args.icao}/{args.airline}) 放在前方 {args.distance:.0f} m："
                      f"{anchor[0]:.5f} {anchor[1]:.5f}")
            t_lat, t_lon, t_elev, t_psi, t_agl = anchor
            on_ground = t_agl < 5.0
            pose = [t_lat, t_lon, t_elev / 0.3048, 0.0, 0.0, t_psi]
            entry = {
                "callsign": args.callsign, "equipment": args.icao, "airline": args.airline,
                "pose": pose, "target": pose, "velocity": [0, 0, 0], "rotation": [0, 0, 0],
                "on_ground": on_ground, "gear_down": True, "engines_on": True,
                "lights": {"beacon_on": True, "nav_on": True, "strobe_on": not on_ground,
                           "taxi_on": on_ground, "landing_on": False},
                "squawk": 2000, "mode": "N",
            }
            message = {"type": "traffic", "aircraft": [entry], "own": None, "labels": True}
            if args.csl:
                message["csl"] = args.csl
            sequence = (sequence + 1) & 0xFFFF
            out.sendto(encode(message, sequence), PLUGIN)
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        subscribe(xp, 0)
        sequence = (sequence + 1) & 0xFFFF
        out.sendto(encode({"type": "traffic", "aircraft": []}, sequence), PLUGIN)
        print("已清掉测试飞机")


if __name__ == "__main__":
    main()
