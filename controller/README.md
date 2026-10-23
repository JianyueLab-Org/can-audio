# Python 语音引擎

C# 客户端（[can-audio-client](https://github.com/JianyueLab-Org/can-audio-client) 仓库的 `ui/src/ControllerClient`）负责界面、设置、PTT 输入（键盘 / 鼠标侧键 / 摇杆）、席位数据和电台栈规则；
本目录只负责**语音**：Mumble 连接、音频设备、收发、混音、降噪、麦克风增益。两边通过标准输入输出上的 JSON Lines 通信，
入口是 `bridge.py`（发行时用 PyInstaller 打成 `can-voice.exe`，放在 C# 发行包的 `controller/` 下）。

这个目录由 C# 客户端与 Python 开发共同维护：改 `bridge.py` 的命令或事件前先对齐下面的协议。

## 文件

| 文件 | 作用 |
| --- | --- |
| `bridge.py` | 协议入口：读 stdin 命令、写 stdout 回复和事件；日志只写 stderr（C# 写入 `logs/voice-*.log`） |
| `voice.py` | `VoiceClient`：Mumble 连接、重连上限、被踢判断、频道 / 监听 / VoiceTarget、收发线程 |
| `syncplan.py` | C# 推来的同步计划（`SyncPlan`）和频道命名约定 `FREQ_118000` |
| `connectionstatus.py` | 每秒上报的连接快照（传输方式、Ping RTT） |
| `rxmix.py` / `denoise.py` / `micgain.py` | 多路接收混音、RNNoise 降噪、增益与限幅 |
| `mumblecompat.py` | pymumble 兼容补丁（TLS、证书钉扎、发送重试） |
| `i18n.py` | `voice.*` / `reject.*` / `denied.*` 状态文字（中 / 英） |

## 协议

每行一个 JSON 对象，UTF-8。stdout 只能写协议，`print` 会被重定向到 stderr。

### 命令（C# → Python）

`{"id": 7, "command": "<名字>", "data": {...}}`，每条必须回 `{"id": 7, "result": ...}` 或 `{"id": 7, "error": "说明"}`。
C# 对每条命令等 90 秒。

| 命令 | data | result | 说明 |
| --- | --- | --- | --- |
| `devices` | 无 | `[{"index", "name", "input", "output"}]` | PortAudio 设备表；C# 用 `index` 回传设备 |
| `connect` | `host`, `port`, `cid`, `password`, `input`, `output`（设备 index 或 null） | `true` / `false` | 阻塞到连上或失败；失败原因走 `state` 事件 |
| `audio` | `mic`, `speaker`（0–200 %），`denoise`（bool），`input`, `output` | `null` | 未连接时报错 |
| `sync` | 见下 | `null` | 立即返回，频道同步在后台线程做 |
| `range_audio` | `{ "CID": true/false }` | `{"entries": n}` | 替换范围通话策略；未出现的 CID 默认允许 |
| `ptt` | `active`（bool） | `null` | 开始 / 停止发话；未连接时忽略。C# 已经过滤掉"未上席位"和"设置窗口打开"的情况 |
| `disconnect` | 无 | `null` | |
| `quit` | 无 | `null` | 收尾后退出进程；stdin 关闭也会退出 |

`sync` 的 data 是 C# `RadioStack` 已经算好的结果，Python 只照做：

```json
{"rx": [118000, 121700], "tx": [118000], "xc": [],
 "selected": 118000, "locked": [118000], "volumes": {"118000": 100, "121700": 80}}
```

- 频率都是千赫整数，按频率升序；`tx` ⊆ `rx`，`xc` ⊆ `tx`。
- `selected` 是主频率（真正进入的频道），可为 `null`；`locked` 是本人在数据源上值守的频率，进不去主频率时按它回退。
- `volumes` 的键是频率字符串，值 0–100（静音已折算为 0）。
- `tx` 为空时必须立刻停止发话（未上席位或全部关了 TX）。

`range_audio` 的完整约定见仓库根目录的
[`range_audio_protocol.md`](../range_audio_protocol.md)。C# 客户端应定时发送完整的
CID 策略快照，例如 `{"1003": true, "10013": false}`；Python 只在收到声音时按
Mumble 用户名（当前即 CID）查询该快照，明确为 `false` 的来源不会进入本地混音或
交叉耦合，未列出的来源放行。

### 事件（Python → C#）

| 事件 | 字段 | 说明 |
| --- | --- | --- |
| `ready` | | 进程启动完成 |
| `state` | `state`, `message` | `connecting` / `online` / `reconnecting` / `offline` / `denied` / `error` / `stopped`；`error`、`stopped` 会让界面回到未连接 |
| `connection` | `connected`, `running`, `transport`（`UDP`/`TCP`/null）, `latency_ms`（无 Ping 样本为 null） | 每秒一次 |
| `rx` | `khz`, `active`, `callsign`（CAN ID） | 某频率开始 / 停止收到话音 |
| `tx` | `active` | 真正开始 / 停止发话 |
| `fatal` | `message` | 引擎崩溃 |

改协议时要同时改 can-audio-client 里的 `Services/VoiceBridge.cs` 与 `ViewModels/MainWindowViewModel.Backend.cs`。

## 开发与测试

Python 3.12 x64（3.11 也能跑测试）：

```powershell
py -3.12 -m venv .venv
$env:PYTHONUTF8 = 1   # requirements 文件里有中文注释，pip 默认按 GBK 读会失败
.\.venv\Scripts\python.exe -m pip install -r requirements-voice.txt
.\.venv\Scripts\python.exe -m unittest discover -s controller -v
```

打包成单文件 `can-voice.exe`（在仓库根目录，先装 `requirements-build.txt`）：

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --name can-voice `
    --paths controller --collect-all pymumble_py3 --add-binary "controller/opus.dll;." controller/bridge.py
```

有 `controller/rnnoise.dll`（见 `native/rnnoise`）时再加 `--add-binary "controller/rnnoise.dll;."`。
产物 `dist/can-voice.exe` 放到 C# 发行包的 `controller/` 目录。

C# 客户端从源码运行时默认用它自己仓库里的 `.venv/Scripts/python.exe` 启动 `bridge.py`，
设置环境变量 `CAN_PYTHON` 可换解释器。
