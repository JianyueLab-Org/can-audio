"""客户端和 X-Plane 插件之间的本地通道。

插件为什么要单独一个进程：X-Plane 的绘图 API 只有插件够得着，而插件跑在
X-Plane 自己的 Python 里（XPPython3），装的包和我们客户端那套不是一回事——
把 PyQt6、pymumble、pyaudio 塞进 X-Plane 进程里既装不动也不该装。

所以分工是：

    客户端  收 FSD、算插值、匹配模型      ← 复杂的部分，在这里能测
    插件    照着给的数据画 + 写 TCAS      ← 薄，改动少，不用反复重启 X-Plane

协议是**每行一个 JSON**，UDP 发到本机。选 UDP 不选 TCP 是因为这是纯位置流：
丢一帧下一帧就补上了，为了可靠性去排队反而会积压出延迟。插件那边永远只认最后
收到的一帧。

每架飞机送的是它**最新的样本**，不是积分好的位置：上报的位置姿态（`target`）、
速度、角速度，加两个序号。插件自己按 xPilot 的运动模型逐帧积分——只有它知道
每一帧什么时候画。序号的意思见 `SampleTable`。

一帧超过一个 UDP 包时按 seq/part/total 分片——64 架飞机的 JSON 能到十几 KB，
本机回环 MTU 通常够，但不能赌。
"""

import base64
import json
import logging
import socket

import traffic

log = logging.getLogger("bridge")

# 客户端 → 插件。49900 往上是 X-Plane 自己不用的区间。
PLUGIN_PORT = 49900
# 插件 → 客户端（握手和状态回报）
CLIENT_PORT = 49901
HOST = "127.0.0.1"

# 一个 UDP 包里放多少字节的负载。本机回环能扛更大，但 8 KB 是安全线。
MAX_PAYLOAD = 8000

# v3：每架飞机送样本（target/velocity/rotation/seq/received），插件每帧自己
# 积分。v2 送的是客户端积分好的位置，飞机按推送定时器的节奏一步一步跳。
# v2：分片改按**字节**切并用 base64 装进 JSON。v1 是切完字节再按 UTF-8 解回
# 字符串，切口落在多字节字符中间（呼号、CSL 路径里的中文）时 json.dumps 直接
# UnicodeEncodeError——从那一帧起插件再也收不到任何数据，整个天空清空。
PROTOCOL_VERSION = 3

# 内容没变时客户端也至少隔这么久推一帧，插件靠它判断客户端还活着（插件那边
# 30 s 收不到就清场）。
PUSH_HEARTBEAT = 1.0

# 一帧最多送多少架飞机。TCAS 数组是 64 个位置，0 号给本机，所以他机 63 架。
# 客户端按距离排序后截断——超了必须先扔远的，不能随便扔。
MAX_TRAFFIC = 63


def encode(message, sequence=0, max_payload=MAX_PAYLOAD):
    """把一条消息切成若干个待发的 UDP 包（bytes 列表）。

    每个包本身是一行 JSON，带 seq/part/total，插件那边靠这三个字段重组。

    切片按**原始字节**来，负载走 base64——切口可以落在任何位置，不用管字符
    边界。按字符串切再拼是修不好的：UTF-8 字节流从中间断开后两半各自都不是
    合法字符串，surrogateescape 能把它们塞进 JSON，拼回来的却不是原文。
    """
    raw = json.dumps(message, ensure_ascii=False,
                     separators=(",", ":")).encode("utf-8")
    # base64 会膨胀 4/3，切片尺寸要按编码后不超过 max_payload 算
    step = max(1, max_payload * 3 // 4)
    chunks = [raw[i:i + step] for i in range(0, len(raw), step)] or [b""]

    packets = []
    for index, chunk in enumerate(chunks):
        header = {"v": PROTOCOL_VERSION, "seq": sequence,
                  "part": index, "total": len(chunks),
                  "data": base64.b64encode(chunk).decode("ascii")}
        packets.append(json.dumps(header).encode("utf-8"))
    return packets


class Reassembler:
    """插件侧：把分片拼回完整消息。

    只保留最新的那一帧。旧帧收不齐就直接扔——位置流里迟到的数据没有价值，
    留着反而会让飞机往回跳。
    """

    def __init__(self):
        self.sequence = None
        self.parts = {}
        self.total = 0

    def feed(self, packet):
        """喂一个 UDP 包。拼齐了返回消息，否则返回 None。"""
        try:
            header = json.loads(packet.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if header.get("v") != PROTOCOL_VERSION:
            return None

        sequence = header.get("seq", 0)
        if sequence != self.sequence:
            # 序号是 16 位环回的。只有**往前走**才算新帧；乱序迟到的旧帧
            # 必须扔掉——原来"不等于就算新"会让一组迟到的旧分片顶掉刚拼好的
            # 新帧，飞机往回跳，正是这套设计要防的事。
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
        except (ValueError, UnicodeDecodeError) as e:
            log.debug("still could not parse after reassembly: %s", e)
            return None


class BridgeSender:
    """客户端侧：把他机数据推给插件。"""

    def __init__(self, host=HOST, port=PLUGIN_PORT):
        self.address = (host, port)
        self.sequence = 0
        self._socket = None

    def _ensure(self):
        if self._socket is None:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._socket.setblocking(False)
        return self._socket

    def send(self, message):
        """发一帧。失败返回 False——插件没开是常态，不该当错误刷屏。"""
        self.sequence = (self.sequence + 1) & 0xFFFF
        try:
            sock = self._ensure()
            for packet in encode(message, self.sequence):
                sock.sendto(packet, self.address)
            return True
        except OSError as e:
            log.debug("sending to the plugin failed (it may not be running): %s", e)
            return False

    def send_traffic(self, entries, own=None):
        return self.send({"type": "traffic", "aircraft": entries, "own": own})

    def close(self):
        if self._socket:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None


# 样本里送给插件的字段。积分出来的位置（latitude … heading）不送：插件自己算，
# 送了也只会被误用。
DRAWN_KEYS = ("latitude", "longitude", "altitude", "pitch", "bank", "heading")


class RecordingMotion(traffic.Motion):
    """traffic.Motion 加上"最近喂进来的是什么"。

    插件要的是**喂给 Motion 的原始输入**：积分过程中 Motion 会在 0.5 秒后把
    角速度清零，拿 motion.rotation 当样本发出去，插件那边就收到一个"新的零角速度"。
    只发 `@` 的飞机，速度是 derive_rates 推出来的，也只有这里看得到。

        received   receive() 的次数
        seq        receive() 加 refresh() 的次数
    """

    __slots__ = ("received", "seq", "sample_velocity", "sample_rotation")

    def __init__(self):
        super().__init__()
        self.received = 0
        self.seq = 0
        self.sample_velocity = (0.0, 0.0, 0.0)
        self.sample_rotation = (0.0, 0.0, 0.0)

    def receive(self, latitude, longitude, altitude, pitch, bank, heading,
                velocity=(0.0, 0.0, 0.0), rotation=(0.0, 0.0, 0.0)):
        super().receive(latitude, longitude, altitude, pitch, bank, heading,
                        velocity=velocity, rotation=rotation)
        self.received += 1
        self.seq += 1
        self.sample_velocity = tuple(float(v) for v in velocity)
        self.sample_rotation = tuple(float(v) for v in rotation)

    def refresh(self, velocity, rotation):
        super().refresh(velocity, rotation)
        self.seq += 1
        self.sample_velocity = tuple(float(v) for v in velocity)
        self.sample_rotation = tuple(float(v) for v in rotation)


class SampleTable(traffic.TrafficTable):
    """xpc 用的他机表：snapshot() 的条目里多带每架飞机的最新样本。

    traffic.py 是 xpc 和 msfs 共用的一份，这里不去改它：新建的每架飞机换上
    RecordingMotion，snapshot() 之后再在锁里把样本字段补进去。

    插件按序号认样本：`received` 变了就 receive（位置姿态用最新的 target），
    只有 `seq` 变了就 refresh。UDP 会丢帧、推送也不是每个样本一次，所以插件
    不能"来一条算一条"。
    """

    def _adopt(self, callsign):
        """在基类建表之前先把飞机建好，换上 RecordingMotion。"""
        with self._lock:
            if callsign not in self.aircraft:
                aircraft = traffic.Aircraft(callsign)
                aircraft.motion = RecordingMotion()
                self.aircraft[callsign] = aircraft

    def update_position(self, callsign, *args, **kwargs):
        self._adopt(callsign)
        return super().update_position(callsign, *args, **kwargs)

    def set_plane_info(self, callsign, **info):
        self._adopt(callsign)
        return super().set_plane_info(callsign, **info)

    def snapshot(self, now=None, **kwargs):
        entries = super().snapshot(now=now, **kwargs)
        with self._lock:
            for entry in entries:
                aircraft = self.aircraft.get(entry["callsign"])
                motion = aircraft.motion if aircraft is not None else None
                if motion is None or motion.target is None:
                    continue
                entry["target"] = list(motion.target)
                if isinstance(motion, RecordingMotion):
                    entry["velocity"] = list(motion.sample_velocity)
                    entry["rotation"] = list(motion.sample_rotation)
                    entry["seq"] = motion.seq
                    entry["received"] = motion.received
                else:
                    entry["velocity"] = list(motion.velocity)
                    entry["rotation"] = list(motion.rotation)
        return entries


def plugin_entry(entry):
    """snapshot() 的一条 → 发给插件的一条：去掉积分出来的位置。"""
    return {key: value for key, value in entry.items() if key not in DRAWN_KEYS}
