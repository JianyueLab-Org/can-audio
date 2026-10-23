"""C# 客户端推过来的电台同步计划，以及频率 ↔ Mumble 频道的命名约定。

RX / TX / XC 联动、值守闸门、主频率选择这些规则都在 C# 的 RadioStack 里算完，
这里只接结果。voice.VoiceClient.sync() 只用到下面这几样：

    rx_frequencies() / tx_frequencies() / xc_frequencies()   千赫整数列表
    selected_khz                                             主频率或 None
    locked_khz                                               本人值守的频率集合
    for radio in plan: radio.frequency_khz, radio.effective_volume()
"""


def format_frequency(khz):
    """118000 → "118.000"。"""
    return f"{khz / 1000:.3f}"


def channel_name(khz):
    """频率对应的 Mumble 频道名，全网统一的约定。"""
    return f"FREQ_{str(khz).zfill(6)}"


class PlannedRadio:
    def __init__(self, frequency_khz, volume=100):
        self.frequency_khz = frequency_khz
        self.volume = volume

    def effective_volume(self):
        return self.volume


class SyncPlan:
    def __init__(self, rx=(), tx=(), xc=(), selected=None, locked=(), volumes=None):
        self._rx = [int(khz) for khz in rx]
        self._tx = [int(khz) for khz in tx]
        self._xc = [int(khz) for khz in xc]
        self.selected_khz = int(selected) if selected is not None else None
        self.locked_khz = {int(khz) for khz in locked or ()}
        self._volumes = {int(khz): max(0, min(100, int(volume)))
                         for khz, volume in (volumes or {}).items()}

    @classmethod
    def from_dict(cls, data):
        return cls(data.get('rx') or (), data.get('tx') or (), data.get('xc') or (),
                   data.get('selected'), data.get('locked') or (), data.get('volumes'))

    def rx_frequencies(self):
        return list(self._rx)

    def tx_frequencies(self):
        return list(self._tx)

    def xc_frequencies(self):
        return list(self._xc)

    def __iter__(self):
        for khz in dict.fromkeys(self._rx + self._tx + self._xc):
            yield PlannedRadio(khz, self._volumes.get(khz, 100))
