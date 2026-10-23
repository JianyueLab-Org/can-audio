# Range Audio Protocol

本文档定义 C# 客户端向 `controller/bridge.py`（发行版为 `can-voice.exe`）发送范围通话策略的协议。

范围通话的目标是：当两个用户位于同一 Mumble 频率，但距离超过客户端计算出的允许范围时，接收方对该发话者静音；同一频率上的其他来源仍然正常播放。

## 1. 责任边界

### C# 客户端负责

- 获取本机和其他用户的位置；
- 根据产品规则计算距离和允许范围；
- 将结果整理为 `CID -> allow` 的完整快照；
- 定时发送快照；
- 在位置或范围判断发生变化时立即发送新快照。

### Python 语音进程负责

- 接收并原子替换最近一次策略快照；
- 识别 Mumble 音频来源的 CID；
- 在音频进入本地混音前执行 allow/deny；
- 对本地播放和交叉耦合使用同一个过滤结果。

Python 不负责访问位置接口，也不负责计算 Haversine 距离、位置过期或 `visual_range` 规则。

## 2. 身份字段

范围策略的键必须使用 **CID**，例如：

```json
{
  "1003": true,
  "10013": false
}
```

不要使用呼号作为策略键：

```json
{
  "CCA1001": true,
  "ZBAA_TWR": false
}
```

原因：

- CID 是账号级稳定身份；
- 呼号可能改变，也可能被重新使用；
- 呼号存在大小写和格式差异；
- Mumble 当前由 Python 使用 `cid` 作为登录用户名，所以收到音频时 `user.name` 对普通用户就是 CID。

呼号仍然可以用于 UI、日志和 `rx` 事件展示，但不能作为范围策略的主键。

> 注意：ATIS 用户名是特殊格式，例如 `900_atis127800_ZBAA_ATIS`。第一版范围策略只针对普通用户 CID。Python 对无法直接视为 CID 的来源不应用显式 CID deny，按默认 allow 处理。

## 3. 进程通信格式

Python 进程使用 UTF-8 JSON Lines：每行一个 JSON 对象。

客户端发送：

```json
{"id": 20, "command": "range_audio", "data": {"1003": true, "10013": false}}
```

Python 回复：

```json
{"id": 20, "result": {"entries": 2}}
```

命令失败时：

```json
{"id": 20, "error": "range_audio allow for CID 1003 must be boolean"}
```

`id` 由 C# 客户端生成，并用于匹配回复。`data` 必须是 JSON object，其每个属性名是 CID，每个属性值必须是 JSON boolean。

## 4. `range_audio` 命令

### 请求

```json
{
  "id": 20,
  "command": "range_audio",
  "data": {
    "1003": true,
    "10013": false
  }
}
```

### 字段定义

| 字段 | 类型 | 必需 | 含义 |
| --- | --- | --- | --- |
| `id` | integer/string | 是 | 请求和回复的关联 ID；沿用 bridge 的通用命令协议 |
| `command` | string | 是 | 固定为 `range_audio` |
| `data` | object | 是 | 完整的 CID 策略快照 |
| `data.<CID>` | boolean | 是 | `true` 允许听到该 CID，`false` 静音该 CID |

当前协议不使用呼号、不使用距离值、不使用位置坐标。距离计算结果应在 C# 端转换为 `true` 或 `false`。

### 完整快照语义

每次 `range_audio` 都会**替换整个旧快照**，不是增量合并：

```json
{"1001": false, "1002": true}
```

随后发送：

```json
{"1002": false}
```

新快照中 `1001` 已经不存在，因此 `1001` 会恢复为默认允许；`1002` 变为静音。

未出现在当前快照中的 CID 始终默认为 `allow`：

```text
CID 在快照中且值为 true  -> allow
CID 在快照中且值为 false -> deny
CID 不在快照中           -> allow
```

因此 C# 客户端每次发送时应发送完整策略，而不是只发送刚刚变化的 CID。

空对象也是合法快照：

```json
{"id": 21, "command": "range_audio", "data": {}}
```

它表示当前没有任何显式静音来源，所有来源默认允许。

## 5. 发送时机

范围策略通过独立 JSON 命令发送，不附加到 Mumble 音频包、PCM 帧或 `sync` 命令中。

推荐策略：

- Python 进程发送 `ready` 后，C# 发送一次；
- `connect` 成功后，C# 再发送一次；
- 位置数据刷新后，如果 allow/deny 结果发生变化，立即发送；
- 范围配置改变时，立即发送；
- 收到 `online` 或重连完成事件后，重新发送；
- 即使策略没有变化，也建议每 10 到 15 秒发送一次完整快照，作为恢复机制。

位置服务可以每 5 秒左右刷新，但不需要每次都发送策略。只有策略快照变化时才必须立即发送；定时重发用于覆盖管道重启、重连或丢失的情况。

## 6. C# 端实现建议

C# 可以维护一个单调递增的本地请求 ID，但 Python 当前只用 `id` 做回复关联，不需要额外的策略版本字段。

伪代码：

```csharp
private readonly Dictionary<string, bool> _rangePolicy = new();
private int _nextRequestId = 1;

private async Task SendRangePolicyAsync(
    IReadOnlyDictionary<string, bool> completePolicy)
{
    var request = new
    {
        id = _nextRequestId++,
        command = "range_audio",
        data = completePolicy
    };

    await voiceBridge.SendCommandAsync(request);
}
```

当位置服务返回数据后，C# 先计算完整结果：

```csharp
var policy = new Dictionary<string, bool>
{
    ["1003"] = true,
    ["10013"] = false
};

await SendRangePolicyAsync(policy);
```

不要发送呼号键：

```csharp
// 错误：Python 不会把呼号当作 CID 映射
var wrong = new Dictionary<string, bool>
{
    ["CCA1001"] = true
};
```

## 7. 连接和重连行为

范围策略保存在当前 Python `VoiceClient` 会话中。

- `connect` 前发送 `range_audio` 会返回错误，因为此时还没有 VoiceClient；C# 应在连接成功后发送；
- 普通 Mumble 重连不会主动改变 Python 中保存的范围策略，但 C# 仍应在收到 `online` 后重发；
- `disconnect` 会清空当前范围策略；下一次连接必须重新发送；
- Python 进程重启后策略为空，所有未列出的 CID 默认允许；C# 收到 `ready` 后必须重新发送。

建议 C# 将最近一次完整快照保存在内存中，在以下事件发生时重发：

```text
ready
online
reconnected
位置策略定时器触发
```

## 8. Python 的过滤时机

Python 在收到 Mumble 音频后按以下顺序处理：

```text
Mumble sound callback
    -> 取得 user.name 作为 CID
    -> 查 range_audio 快照
    -> 明确 deny：丢弃 PCM
    -> allow：缩放并放入接收混音队列
    -> allow：必要时执行交叉耦合
```

被拒绝的音频不会：

- 写入本地接收混音队列；
- 播放到扬声器；
- 通过交叉耦合转发到其他频率；
- 触发 `rx active` 事件。

### 交叉耦合的来源限制

上述 CID 过滤针对当前 Mumble 音频发送者。现有交叉耦合会把收到的 PCM
通过转发管制员自己的 Mumble 连接重新编码发送；接收方看到的是转发管制员的
CID，而不是原始飞行员的 CID。因此，转发端会先执行自己的距离策略，但接收端
无法再对这段转发话音应用原始飞行员的距离策略。若接收端对管制员不限距离，
则该转发话音也会被放行。

当前 `CID -> allow` 快照和标准 Mumble 转发链路不提供原始来源元数据，不能保证
跨交叉耦合后的端到端距离静音。实现这项保证需要可保留原始来源的服务端转发，
或发送端、接收端共同支持的新传输协议；不能仅在接收端将转发管制员静音，
否则会同时丢失该管制员本人的话音及其他合法转发来源。

同一频率的来源彼此独立。例如：

```text
CID 1001 -> true
CID 1002 -> false
```

在同一频率收到两人的音频时，只播放 CID 1001 的 PCM。

## 9. 错误处理

以下数据会被拒绝，并返回 `error`：

```json
{"1001": 1}
```

```json
{"": false}
```

```json
["1001", false]
```

正确做法是保留最近一次有效策略，不要因为 C# 生成了一次无效命令而清空旧策略。修正数据后重新发送完整快照。

如果 Python 返回 `error`，C# 应记录日志，并在下一次策略刷新或重试周期重新发送。

## 10. 与 `sync` 命令的关系

`sync` 仍然只描述电台栈：

```json
{
  "rx": [118000, 121700],
  "tx": [118000],
  "xc": [],
  "selected": 118000,
  "locked": [118000],
  "volumes": {
    "118000": 100,
    "121700": 80
  }
}
```

不要把范围策略放到 `sync` 中。两者生命周期不同：

- `sync` 由电台栈变化触发；
- `range_audio` 由位置和距离策略变化触发。

## 11. 第一版测试清单

C# 端至少测试：

- 距离在范围内时发送 `true`；
- 距离超出范围时发送 `false`；
- 一个频率内多个来源可以有不同结果；
- 发送完整快照后，之前被 deny 但现在未出现的 CID 恢复 allow；
- 位置服务短暂失败时，客户端仍能按约定生成或保留策略；
- `ready`、`online`、重连后会重新发送策略。

Python 端已经覆盖：

- 明确 `false` 的 CID 不进入混音；
- 明确 `false` 的 CID 不进入交叉耦合；
- 同频其他 `true` 来源仍然播放；
- 未出现的 CID 默认允许；
- 新快照替换旧快照；
- 断开连接后清空快照。
