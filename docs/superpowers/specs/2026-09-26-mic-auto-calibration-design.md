# 麦克风音量自动校准 设计

日期：2026-09-26
状态：待评审
范围：`controller/`（audio-for-can）、`xpc/`、`msfs/`。`atis/` 不在范围内（发送 TTS，不用麦克风）。can-voice 不在范围内。

---

## 1. 目标

网络链路中所有麦克风客户端发出的语音响度一致。

- 每个输入设备通过一次朗读校准得到一个基准增益，作为该用户的 100% 音量。
- 现有麦克风音量滑块改为基准增益上的乘数，只在本次启动有效，每次改动写日志。
- 发送链路末端加限幅器，任何增益下都不削波回绕。

不做：持续 AGC、说话时自适应漂移、噪声抑制、VAD。

## 2. 发送链路

```
麦克风 int16 → float → × 基准增益 → × 会话乘数 → 峰值限幅器(−1 dBFS) → int16 → add_sound
```

- `controller/voice.py` `_transmit_loop`：用限幅器替换现有 `np.clip`。
- `xpc/voice.py` 发送循环（:1188 附近）：现在没有 clip，滑块超过 100% 时 int16 回绕。本设计修复。`msfs/voice.py` 同步。

## 3. 校准算法

目标：有效语音帧的 RMS 为 **−20 dBFS**。

流程（约 10 秒）：

1. 静音 1 秒，测底噪。
2. 用户以正常音量和距离朗读一段 ATC 用语。文本随界面语言。
3. 以 20 ms 为一帧计算 RMS。比底噪高 10 dB 以上的帧为语音帧。
4. 语音电平 = 语音帧的功率平均，单位 dBFS。
5. `gain_db = −20 − 语音电平`，限制在 **[−12, +20] dB**。

拒绝条件（给出原因，允许重试）：

| 条件 | 提示方向 |
| --- | --- |
| 语音帧总时长 < 2 秒 | 没有检测到足够的语音 |
| 原始信号削波帧 > 1% | 麦克风离远一点，或调低系统输入音量 |
| 语音电平 − 底噪 < 15 dB | 环境太吵，或麦克风太小声 |

校准使用与发送链路相同的采样率和帧长，只在未发射时进行，自己打开一个输入流。

## 4. 限幅器

- 峰值检测，瞬时起控，释放约 50 ms。
- 输出上限 −1 dBFS。
- 状态随发送线程保存，每次 PTT 按下不重置。

## 5. 会话乘数

- 滑块范围保持 0–200%，100% = 校准电平。
- 每次启动重置为 100%。
- `mic_volume` 不再读写设置文件。旧值忽略，保留在文件中不删除。
- 日志：滑块松开时写一行，logger `voice`，英文。例：
  `mic multiplier 100% -> 130% (baseline +8.5 dB, effective +10.8 dB)`

## 6. 存储

设置文件键 `mic_calibration`，按输入设备名索引（PyAudio 设备序号跨启动不稳定）：

```json
"mic_calibration": {
  "麦克风 (USB Audio Device)": {
    "gain_db": 8.5,
    "speech_dbfs": -28.5,
    "noise_dbfs": -62.0,
    "calibrated_at": "2026-09-26T10:00:00Z"
  }
}
```

- controller：`radio_settings.json`。
- xpc / msfs：`xpc_settings.json`。
- 无记录的设备：基准增益 0 dB（即现状）。

## 7. 日志

均为英文。

- 每次校准一行：设备、语音电平、底噪、增益、接受或拒绝及原因。
- 启动时一行：所选输入设备和生效的基准增益（或 `uncalibrated`）。

## 8. 界面

新增 `calibration.py`（每个组件各一份）：校准对话框。

- 显示朗读文本、实时输入电平条。
- 结束后显示结果（例：`基准 +8.5 dB`），按钮：重试 / 保存 / 取消。
- 拒绝时显示原因和重试。

触发：

- 启动后，所选输入设备没有校准记录时自动弹出。
- 切换到没有校准记录的输入设备时自动弹出。
- 可跳过。跳过后基准为 0 dB，下次启动再次提示。
- 设置对话框中麦克风音量滑块旁新增按钮「麦克风音量自动校准」，以及一行只读的当前基准增益。

所有界面文本经 `i18n.py`，zh 与 en 两份齐全。

## 9. 代码结构

新增 `micgain.py`：只依赖 numpy，不依赖 Qt 和音频设备。

- `frame_rms_dbfs(frame) -> float`
- `measure(frames, noise_frames) -> Measurement`（语音电平、底噪、语音时长、削波比例）
- `compute_gain(measurement) -> GainResult`（增益或拒绝原因）
- `class Limiter`：`process(samples: np.ndarray) -> np.ndarray`
- `db_to_linear(db) -> float`

组件间不共享代码（沿用仓库规则）：

- `controller/micgain.py`、`controller/calibration.py` 为独立副本。
- `xpc/micgain.py` 与 `msfs/micgain.py` 字节一致，`xpc/calibration.py` 与 `msfs/calibration.py` 字节一致。两者加入 `msfs/test_msfs.py` 的 `SharedCopyTest`。

## 10. 测试

无需音频设备和服务器，`python -m unittest discover -p "test_*.py"` 覆盖。

- `test_micgain.py`（每个组件）：
  - 已知电平的合成语音信号，增益误差 ≤ 0.5 dB。
  - 三个拒绝条件各自触发。
  - 增益上下限生效。
  - 输入超满量程 +20 dB 时，限幅器输出不超过 −1 dBFS。
- 发送链路：
  - controller：`test_voice.py` `TransmitThreadTest` 中断言输出 = 基准 × 乘数且无回绕。
  - xpc / msfs：`test_xpc.py` `VoiceRuntimeTest` 中同样断言。
- 设置：`mic_calibration` 保存后重新加载一致；乘数不写入文件。
- `test_i18n`：新增键 zh / en 齐全。
- `smoke_gui.py`：校准对话框可构造。
