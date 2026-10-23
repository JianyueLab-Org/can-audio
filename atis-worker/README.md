# ATIS 语音发送进程

C# 桌面通播客户端 [can-atis](https://github.com/JianyueLab-Org/can-atis)（ATIS for CAN）负责界面、模板、
METAR、FSD 和语音合成；它把合成好的 PCM 交给本目录的 `can_mumble_worker.py`，由这个进程
连上 Mumble、进入频率频道并循环播报。发行时用 PyInstaller 打成 `can-voice-worker.exe`，
作为嵌入资源打进 can-atis 的单文件程序。

这个目录由 C# 客户端与 Python 开发共同维护：改命令行参数、stdin 格式或 stdout 状态前，
先对齐下面的协议，并同时改 can-atis 里的 `Voice/Network/VoiceServerConnection.cs`。

## 文件

| 文件 | 作用 |
| --- | --- |
| `can_mumble_worker.py` | 进程入口：连接、建 / 进 `FREQ_xxxxxx` 频道、循环发送 20 ms 帧、每秒上报连接状态 |
| `mumblecompat.py` | pymumble 兼容补丁（TLS、证书指纹钉扎、发送重试）；和 `controller/`、`server/ATIS/` 那两份逐字节相同 |
| `opus.dll` | pymumble 经 ctypes 加载的 Opus 库，和 `controller/opus.dll` 是同一个文件 |
| `test_worker.py` | 单元测试，不需要服务器、音频设备或网络 |

## 协议

每次播报起一个进程；内容变了，C# 先结束旧进程再起新的。

### 输入

```text
can_mumble_worker.py --host audio.ceruleanavi.net --port 64738 \
    --cid <CAN ID> --callsign <席位呼号> --frequency <频率>
```

- 网络密码走环境变量 `CAN_VOICE_PASSWORD`，不放在命令行上；进程读完就从环境里删掉。
- `--frequency` 接受 Hz 或 MHz：`127800000` 和 `127.8` 都进频道 `FREQ_127800`。can-atis 传的是 Hz
  （`AtisStation.Frequency`）。1000–999999 之间的值分不清是 kHz 还是写错了，直接拒绝。
- Mumble 用户名是 `<cid>_atis<千赫六位>_<呼号>`，如 `1234_atis127800_ZBAA_ATIS`。
- stdin：整段播报音频，48 kHz、单声道、16 位小端 PCM。C# 写完就关掉 stdin；读到空内容时以退出码 2 结束。

### 输出

stdout 每行一个 JSON 对象：`{"state", "transport", "latency", "message"}`。

| `state` | 说明 |
| --- | --- |
| `ready` | 只在 `--self-test` 时出现 |
| `connecting` | 开始连接 |
| `connected` | 每秒一次；`transport` 是 `UDP` / `TCP`，`latency` 是 Ping RTT 毫秒数，没有样本时为 `null` |
| `error` | 连接、建频道或进频道失败，`message` 是中文说明 |
| `disconnected` | 连接断开，进程随后退出 |

stderr 不属于协议，C# 原样记进自己的日志（密码替换成 `[redacted]`）。

| 退出码 | 含义 |
| --- | --- |
| 0 | `--self-test` 通过 |
| 2 | 参数不对（包括频率无法识别），或 stdin 为空 |
| 3 | 15 秒内没连上 |
| 4 | 5 秒内建不出频道 |
| 5 | 5 秒内进不了频道 |
| 6 | 播报中连接断开 |

## 开发与测试

依赖只有 pymumble-udp（opuslib、protobuf 由它自己钉版本），装仓库根目录的
`requirements-voice.txt` 即可：

```powershell
py -3.12 -m venv .venv
$env:PYTHONUTF8 = 1
.\.venv\Scripts\python.exe -m pip install -r requirements-voice.txt
.\.venv\Scripts\python.exe -m unittest discover -s atis-worker -v
```

打包成单文件（在仓库根目录，先装 `requirements-build.txt`）：

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --name can-voice-worker `
    --paths atis-worker --collect-all pymumble_py3 --collect-all opuslib --collect-all google.protobuf `
    --hidden-import mumblecompat --add-binary "atis-worker/opus.dll;." atis-worker/can_mumble_worker.py
.\dist\can-voice-worker.exe --self-test
```

can-atis 的 `build-single-file.ps1` 用同样的参数打包，再通过 `-p:VoiceWorkerExe=...` 把 exe
嵌进 `atis-for-can.exe`。
