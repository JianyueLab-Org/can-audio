"""devices 命令的设备表：只列默认 host API 下的设备。

Windows 上 PortAudio 把同一个麦克风按 MME、DirectSound、WASAPI、WDM-KS 各列一遍，
名字大多相同。C# 端按名字回查 index，同名的只能留一个；全列出来就可能留下 WDM-KS
那一个，打开时报 -9999 Unanticipated host error，而"系统默认"能用——因为 None
走的正是默认 host API。所以只列默认 host API 的设备，选具体设备和系统默认走同一条路。
"""


def list_devices(audio):
    """返回 [{"index", "name", "host_api", "input", "output"}]，只含默认 host API 的设备。"""
    host = audio.get_default_host_api_info()
    devices = []
    for index in range(audio.get_device_count()):
        info = audio.get_device_info_by_index(index)
        if info['hostApi'] != host['index']:
            continue
        devices.append(dict(index=index, name=info['name'], host_api=host['name'],
                            input=bool(info['maxInputChannels']),
                            output=bool(info['maxOutputChannels'])))
    return devices
