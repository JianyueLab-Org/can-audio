# 麦克风音量自动校准与降噪 设计

日期：2026-09-26
状态：待评审
范围：`controller/`（audio-for-can）、`xpc/`、`msfs/`。`atis/` 不在范围内（发送 TTS，不用麦克风）。can-voice 不在范围内。

---

## 1. 目标

网络链路中所有麦克风客户端发出的语音响度一致，且背景噪声被压低。

- 每个输入设备通过一次朗读校准得到一个基准增益，作为该用户的 100% 音量。
- 现有麦克风音量滑块改为基准增益上的乘数，只在本次启动有效，每次改动写日志。
- 发送链路最前端加 RNNoise 降噪，可在设置中关闭。
- 发送链路末端加限幅器，任何增益下都不削波回绕。

不做：持续 AGC、说话时自适应漂移、VAD、噪声门。

## 2. 发送链路

```
麦克风 int16 → RNNoise 降噪（开/关） → × 基准增益 → × 会话乘数 → 峰值限幅器(−1 dBFS) → int16 → add_sound
```

- `controller/voice.py` `_transmit_loop`：用上述链路替换现有 `np.clip`。
- `xpc/voice.py` 发送循环（:1188 附近）：现在没有 clip，滑块超过 100% 时 int16 回绕。本设计修复。`msfs/voice.py` 同步。

## 3. 降噪

- 使用 xiph RNNoise v0.2（BSD-3），模型已编入库内。
- 通过 ctypes 调用原生库：`rnnoise_create(NULL)`、`rnnoise_process_frame(st, out, in)`、`rnnoise_destroy(st)`。每次处理 480 个 float（int16 刻度），一帧 960 采样调用两次，原地处理。
- 状态随发送线程保存，每次 PTT 按下不重置。
- 仅在 48 kHz 下工作（模型只训练了 48 kHz）。
- 以下情况直通不处理，写一条英文 WARNING：库不存在；设备回退到非 48 kHz 采样率。直通不影响发话。
- 设置项 `mic_denoise`，默认开，落盘。
- 实际生效状态 = `mic_denoise` 且库可用。下文"降噪状态"均指实际生效状态。

原生库来源：

- 不用 PyPI 包（唯一带 Windows 轮子的 `pyrnnoise` 导入时依赖 PyAV/FFmpeg、soundfile、soxr、matplotlib）。
- CI 从 xiph v0.2 发布包（`rnnoise-0.2.tar.gz`，固定 SHA-256）用 CMake + MSVC 在 `windows-latest` 上构建 `rnnoise.dll`。构建配方放在仓库 `native/rnnoise/`。
- 三个 `gui.spec` 打包 `rnnoise.dll`；发布流程的"验原生库"步骤检查 `_internal/rnnoise.dll` 存在。
- 本机开发：`brew install rnnoise`，或同一配方本地构建。没有库时降噪直通，相关测试跳过。

## 4. 校准算法

目标：有效语音帧的 RMS 为 **−20 dBFS**。

流程（约 10 秒）：

1. 静音 1 秒，测底噪。
2. 用户以正常音量和距离朗读一段 ATC 用语。文本随界面语言。
3. 以 20 ms 为一帧。降噪状态为开时，每帧先过 RNNoise。
4. 帧 RMS 比底噪高 10 dB 以上的为语音帧。
5. 语音电平 = 语音帧的功率平均，单位 dBFS。
6. `gain_db = −20 − 语音电平`，限制在 **[−12, +20] dB**。

削波检查使用原始信号；底噪、语音电平、信噪比使用降噪后的信号（与实际发出的一致）。

拒绝条件（给出原因，允许重试）：

| 条件 | 提示方向 |
| --- | --- |
| 原始信号削波帧 > 1% | 麦克风离远一点，或调低系统输入音量 |
| 语音帧总时长 < 2 秒 | 没有检测到足够的语音 |
| 语音电平 − 底噪 < 15 dB | 环境太吵，或麦克风太小声 |

按表中顺序判断。校准使用与发送链路相同的采样率和帧长，只在未发射时进行，自己打开一个输入流。

## 5. 限幅器

- 峰值检测，瞬时起控，释放约 50 ms。
- 输出上限 −1 dBFS。
- 状态随发送线程保存，每次 PTT 按下不重置。

## 6. 会话乘数

- 滑块范围保持 0–200%，100% = 校准电平。
- 每次启动重置为 100%。
- `mic_volume` 不再读写设置文件。旧值读取时忽略，下次保存时从文件中消失。
- 日志：设置保存且乘数变化时写一行，logger `gui`，英文。例：
  `mic multiplier 100% -> 130% (baseline +8.5 dB, effective +10.8 dB)`

## 7. 存储

设置文件键 `mic_calibration`，按输入设备名索引（PyAudio 设备序号跨启动不稳定）：

```json
"mic_calibration": {
  "麦克风 (USB Audio Device)": {
    "gain_db": 8.5,
    "speech_dbfs": -28.5,
    "noise_dbfs": -62.0,
    "denoise": true,
    "calibrated_at": "2026-09-26T10:00:00Z"
  }
},
"mic_denoise": true
```

- controller：`radio_settings.json`。
- xpc / msfs：`xpc_settings.json`。
- 无记录的设备：基准增益 0 dB（即现状）。
- 记录的 `denoise` 与当前降噪状态不一致时，该记录视为没有校准（降噪会改变测得的电平）。缺少 `denoise` 键视为 `false`。

## 8. 日志

均为英文。

- 每次校准一行，logger `calibration`：设备、降噪状态、语音电平、底噪、增益、接受或拒绝及原因。
- 启动时一行：所选输入设备和生效的基准增益（或 `uncalibrated`）。
- 降噪：库加载成功一行 INFO（路径）；库缺失或采样率不是 48 kHz 一行 WARNING，logger `denoise`。

## 9. 界面

新增 `calibration.py`（每个组件各一份）：校准对话框。

- 显示朗读文本、实时输入电平条。
- 结束后显示结果（例：`基准 +8.5 dB`），按钮：重试 / 保存 / 取消。
- 拒绝时显示原因和重试。

触发：

- 启动后，所选输入设备没有（与当前降噪状态一致的）校准记录时自动弹出。
- 切换输入设备或切换降噪开关并保存后，若没有一致的记录，自动弹出。
- 可跳过。跳过后基准为 0 dB，下次启动再次提示。

设置对话框：

- 麦克风音量滑块旁：按钮「麦克风音量自动校准」，以及一行只读的当前基准增益。
- 复选框「麦克风降噪」。库不可用时禁用，并显示「降噪不可用」。

所有界面文本经 `i18n.py`，zh 与 en 两份齐全。

## 10. 代码结构

`micgain.py`：只依赖 numpy。

- `frame_rms_dbfs(frame) -> float`
- `measure(frames, noise_frames, rate, clip_frames=None) -> Measurement`
- `compute_gain(measurement) -> GainResult`
- `calibration_entry(measurement, gain_db, denoise) -> dict`
- `baseline_db(calibrations, device_name, denoise) -> float | None`
- `class Limiter`：`process(samples) -> np.ndarray[int16]`
- `apply_gain(samples, baseline, percent, limiter) -> np.ndarray[int16]`

`denoise.py`：numpy + ctypes。

- `load()`：按 `sys._MEIPASS`、组件目录、`/opt/homebrew/lib`、`/usr/local/lib`、`ctypes.util.find_library("rnnoise")` 的顺序找库，只找一次。
- `available() -> bool`
- `effective(setting: bool) -> bool`
- `class Denoiser(rate, lib=None)`：`active`、`process(samples) -> np.ndarray[int16]`、`close()`

组件间不共享代码（沿用仓库规则）：

- `controller/` 持 `micgain.py`、`denoise.py`、`calibration.py` 的独立副本。
- `xpc/` 与 `msfs/` 的这三个文件字节一致，加入 `msfs/test_msfs.py` 的 `SharedCopyTest`。

## 11. 测试

无需音频设备和服务器，`python -m unittest discover -p "test_*.py"` 覆盖。

- `test_micgain.py`（每个组件）：
  - 已知电平的合成语音信号，增益误差 ≤ 0.5 dB。
  - 三个拒绝条件各自触发；削波判断使用 `clip_frames`。
  - 增益上下限生效。
  - 输入超满量程 +20 dB 时，限幅器输出不超过 −1 dBFS。
  - 记录的 `denoise` 与当前状态不一致时 `baseline_db` 返回 None。
- `test_denoise.py`（每个组件）：
  - 假库：帧切分、int16 与 float 转换、非 48 kHz 直通、帧长不是 480 整数倍时直通。
  - 真库（找不到时跳过）：−40 dBFS 白噪声经过 1 秒预热后衰减 ≥ 10 dB。
- 发送链路：
  - controller：`test_voice.py` 断言输出 = 基准 × 乘数、无回绕、降噪开启时经过降噪器。
  - xpc / msfs：`test_xpc.py` `VoiceRuntimeTest` 中同样断言。
- 设置：`mic_calibration` 与 `mic_denoise` 保存后重新加载一致；乘数不写入文件。
- `test_i18n`：新增键 zh / en 齐全；`calibration.py` 无硬编码中文。
- `smoke_gui.py`：校准对话框可构造；设置对话框在降噪不可用时可构造。
- CI：测试任务与打包任务都能拿到 `rnnoise.dll`；打包后验证 `_internal/rnnoise.dll` 存在。
