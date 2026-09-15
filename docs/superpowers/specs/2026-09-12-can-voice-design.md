# can-voice 设计：替换 Mumble，重写语音层

日期：2026-09-12
状态：已评审，待实施
范围：P0（架构决策）。P1–P5 各自另有实施计划。

---

## 1. 为什么

三个痛点，按优先级：

**pymumble 填不完的坑。** `CLAUDE.md` 里为它写了整整一节：`ssl.wrap_socket` 在 Python 3.12
被删而它的回退分支调用的是同一个被删的函数；发送缓冲满被当成连接断开；`Mumble.__init__`
记住构造它的线程，那个线程一退出整个主循环就死但连接状态仍然显示 `CONNECTED`；
`new_channel()` / `move_in()` 的阻塞命令永不超时；临时频道的 id 会失效而缓存不知道。每一条
都是线上事故，每一条都需要一个专门的测试盯着它不要复发。

**多频率模型是硬撑出来的。** 一个 Mumble 用户只能待在一个频道里。现在的做法是：主频率靠
`move_in` 真的进去，其余频率靠 Mumble 1.4 的 channel listener（服务端从 1.2/1.3 升级上来的
话根 ACL 里没有 `Listen` 位，症状是除了主频率以外什么都听不见），发送靠手搓 `VoiceTarget`
protobuf（因为 pymumble 的 `set_whisper()` 在 `id == 1` 时只复制 `targets[0]`），交叉耦合靠
客户端把 A 频率收到的音频重新发到 B 频率去。一个 13 小时的真实日志里有 202 次建频道、
其中 93 次和上一次在同一秒、40 次移动请求石沉大海，而用户整晚坐在 root 频道里什么都听不到，
界面全程是绿的。

**运维太重，延迟不行。** Murmur + Ice 认证器 + ACL + SuperUser 口令 + 一个丢了就全网瘫痪且
无法远程修复的 sqlite 卷 + 客户端侧固定的证书指纹。延迟问题的根源则是：**pymumble 根本没有
UDP socket**，音频被包进 `UDPTUNNEL` 走的是和控制通道同一条 TCP 连接（`soundoutput.py`，
注释写着 `# encapsulate in tcp tunnel`），按下 PTT 就是往那条 TCP 上灌 50 包/秒，队头阻塞
直接变成延迟抖动。

注意第三条：**这不是 Mumble 协议的问题**，Mumble 自己有 UDP 语音，是 pymumble 没实现。

## 2. 决策摘要

| 决策 | 结论 | 理由 |
|---|---|---|
| 兼容边界 | 硬切，不保留 Mumble 协议兼容 | 只有自家四个客户端；CRC 在本仓库无任何语音代码 |
| 传输 | QUIC，控制面走 stream，音频走 unreliable datagram | 拿到 UDP 的延迟特性，同时免掉自写 DTLS 握手与密钥协商 |
| 服务端语言 | Go | 与 can-fsd 同栈同部署；服务端要做的事正好是 Go 最舒服的形状 |
| 服务端是否混音 | 否，只转发不解码 | 保住每频率音量和每频率 RX 指示灯（现在就是客户端混音）；服务端因此无 Opus 依赖、无音频线程 |
| 同频多人讲话 | 全部扇出，客户端叠加干扰音 | 真实感最好，服务端保持无讲话权状态 |
| 射程 | 服务端硬过滤 + 客户端平滑衰减 | 见 §7，单靠客户端解决不了带宽 |
| 位置来源 | 订阅 can-fsd 的 `GET /v1/events` SSE | 权威，客户端无法撒谎；关联键 `cid` 免费 |
| 客户端栈 | Rust 核心 + Tauri（Vue 前端） | 三平台、单二进制、音频与网络库一流；前端复用 can-ui 设计系统 |
| 代码去向 | **新建仓库 `can-voice`**，`can-audio` 切换后归档 | 工程结构不被现有 Python 布局牵扯；顺带消掉"目录 `can-audio` / 远端 `airwaysn_audio`"这个历史错位 |
| 上线方式 | **全部做完，择日一次性切换** | 见 §11。已知代价：中间很长一段没有任何东西上线，切换当天无退路 |

## 3. 范围与分解

这份文档是 **P0**：定协议、定栈、定切换路径。不写实现代码。

```
P0  本文档
     │
P1  连通性探针（丢弃型）          ← 唯一可能推翻传输选型的实验，排在最前
     │
     ├─────────────────┐
     ▼                 ▼
P2  语音服务端           P3  客户端核心库 can-voice-client
    （Go）                   （Rust）
     └────────┬────────┘
              ▼
P4  四个客户端 UI（Tauri）+ 服务端 ATIS 机器人
              ▼
P5  切换日：旧版停用，can-audio 归档
```

P2 和 P3 可并行。P4 内部四个客户端无依赖关系，但建议顺序 controller → atis → xpc → msfs：
controller 的 UI 最简单（无线电栈 + 状态栏），atis 次之（模板编辑器复杂但不接模拟器），
xpc/msfs 最后，因为要处理 SimConnect 和 X-Plane 桥接的 FFI。

**P1 排在最前，且不受"一次性切换"影响** —— 它不是发布，是测量。`can-voice` 的整个传输选型
压在"QUIC 在大陆够不够通"这一个假设上（§12），而这个假设只能靠真实网络回答。一个丢弃型
命令行探针几天就能出结论，比四个客户端做完之后才发现要便宜两个数量级。

**一块换不掉的东西**：`PI_XpcTraffic.py` 跑在 X-Plane 自己的 XPPython3 里，必须是 Python。
新仓库里它原样保留在 `apps/xpc/plugin/` 下，连同 `bridge.py` 的 UDP 桥接协议和那个
`PROTOCOL_VERSION` 一致性检查（两边常量不一致时插件会**静默丢弃每一帧**，症状是"完全没有
交通"而两边日志都干净，所以那个检查必须跟着过来）。

### 3.1 新仓库结构

```
can-voice/
  crates/
    can-voice-proto/    线协议：消息类型、编解码
    can-voice-client/   QUIC 连接、订阅状态机、抖动缓冲、Opus、混音、无线电栈
  server/               Go：can-voice 服务端
  apps/
    controller/         Tauri
    atis/               Tauri
    xpc/                Tauri + plugin/PI_XpcTraffic.py（Python，XPPython3）
    msfs/               Tauri
  bots/
    atis-bot/           Rust：服务端 ATIS 机器人
  docs/
```

**服务端 ATIS 机器人重写成 Rust，而不是保留 Python。** 现在的 `server/ATIS/mumble.py` 本质上
就是一个跑在服务器上的语音客户端，让它直接用 `can-voice-client` 就不需要任何绑定层，也不会
出现"同一个协议两份实现"。它的文本处理（`process.py` 的 `幺两拐洞` 读法、NATO 字母展开）是
纯函数，逐条可翻译，翻译时要连同现有测试一起带过来。TTS 仍然外调 edge-tts。

## 4. 架构

仓库布局见 §3.1。这里只说组件之间的关系。

`can-voice-proto` 是 Rust 侧协议的唯一真相，四个客户端和 ATIS 机器人都经 `can-voice-client`
间接依赖它。**服务端不与客户端共享协议代码**：它不解码音频，两边真正共享的只有包头布局和
控制面消息形状，而那个由跨实现黄金文件来钉（§10）。

```
                        ┌─── can-api ────┐
   ① POST /voice/token  │  签 60 秒 token │  复用现有 /api/v1/public/auth 的鉴权逻辑
     （现有 CAN 凭据）   └────────┬───────┘
                                 │
   ┌─────────┐   ② QUIC 握手（TLS 1.3，Let's Encrypt）
   │ 客户端  │ ──────────────────────────────────► ┌──────────────┐
   │         │   ③ stream:   HELLO / SUB / NOTICE  │  can-voice   │
   │         │   ④ datagram: 音频                  │   （Go）      │
   └─────────┘ ◄───────────────────────────────────└──────┬───────┘
                                                          │ ⑤ SSE
                                                   ┌──────┴───────┐
                                                   │   can-fsd    │
                                                   │ /v1/events   │
                                                   └──────────────┘
```

## 5. 线协议

ALPN：`can-voice/1`。

### 5.1 控制面（QUIC bidirectional stream，JSON，长度前缀）

```
→ HELLO  {token, client:"can-controller/3.0.0", proto:1, follow:null}
← READY  {session, server:"can-voice/1.0.0", max_tx:8, max_rx:32}
→ SUB    {rx:[118000,121800,124550], tx:[121800], xc:[[121800,124550]]}
← SUBACK {rx:[...], tx:[...], rejected:[]}
← NOTICE {kind:"tx_denied"|"range_unavailable"|..., freq:121800, reason:"..."}
→ PING   {t}                    ← 仅用于测 RTT；保活由 QUIC 自己做
← PONG   {t, server_t}
← BYE    {reason}
```

**`SUB` 是全量声明，不是增量。** 客户端在任何时候把完整的收发意图发一次，服务端整个替换掉
旧的。这条规则是本设计里最重要的一条：它是幂等的，没有"这次是加还是减"的状态推导，重连后
重发一次即恢复。现在那一整类"客户端以为自己在某个频道里、实际不在"的 bug 因此没有藏身之处。

控制面用 JSON 而非 protobuf 是刻意的：消息频率极低（登录、订阅变更、偶发通知），可读性和
排障便利压过字节效率 —— 出问题时要能直接在日志里读懂发生了什么。

`follow` 只有观察员模式用，见 §7.3。

### 5.2 数据面（QUIC unreliable datagram，13 字节头）

```
 0        1        2        3        5               9              13
 +--------+--------+--------+--------+---------------+--------------+
 |  ver   | flags  |  qual  | seq(2) |  freq_khz(4)  | speaker(4)   | opus…
 +--------+--------+--------+--------+---------------+--------------+
```

| 字段 | 宽度 | 上行（客户端→服务端） | 下行（服务端→客户端） |
|---|---|---|---|
| `ver` | u8 | 协议版本，恒为 1 | 同 |
| `flags` | u8 | bit0 = 本次发言首帧，bit1 = 尾帧 | 同 |
| `qual` | u8 | 填 0 | 服务端算出的信号质量 0–255 |
| `seq` | u16 | 本次发言的帧序号 | 原样转发 |
| `freq_khz` | u32 | 我发到哪个频率 | 这一包来自哪个频率 |
| `speaker` | u32 | 填 0 | 服务端填入的发言者 session id |

频率就是 kHz 整数（121800 = 121.800 MHz）。**`FREQ_<六位>` 字符串频道名这个约定整个消失。**

`flags`：首帧用于立刻点亮 RX 指示灯并重置抖动缓冲；尾帧用于立刻熄灭 RX 指示灯 —— 现在是靠
一个 0.5 秒的超时循环清的，所以松开 PTT 之后灯还要亮半秒。

`speaker` 是"同频干扰音"所必需的：没有它，客户端无法分辨"同一频率上现在有两个人在讲"和
"一个人的包乱序了"。它同时修好一个现存的隐蔽问题 —— 现在音频按 `user["channel_id"]` 路由，
所以同频两人讲话时 RX 灯只能亮一个。

音频：Opus 48 kHz 单声道 20 ms 帧，VBR 约 24 kbps ≈ 60 字节，加头 73 字节。QUIC datagram
上限约 1200 字节，余量充足。管制员在 8 个频率上收，最坏情况约 400 包/秒 ≈ 230 kbps 下行。

**不做"同一订阅者多频率合包"的优化。** 省下来的是包头，而包头只占 18%，不值得换来一个
需要拆包的接收路径。

## 6. 鉴权

```
客户端 ──POST /api/v1/voice/token（现有 CAN 凭据）──► can-api
                                  ◄── {token, expires_in:60}
客户端 ──HELLO{token}──► can-voice    本地验签（持有 can-api 公钥）
```

token 由 can-api 用 Ed25519 签发，载荷含 `cid`、`rating`、`max_tx`、`exp`。can-voice 持公钥
本地验签，**不回调 can-api**。

`max_tx` 的权威值是 **token 里的那个**；`READY` 里的同名字段只是回显，方便客户端在界面上
提前灰掉多余的 TX 开关。服务端判定以 token 为准。

三个后果，都是现状的改进：

- **can-voice 在鉴权路径上对 can-api 无出站依赖。** can-api 挂掉不影响已连接的用户，也不
  影响持有未过期 token 的重连。（can-voice 确实有一条到 can-fsd 的出站依赖，但那是射程功能
  的输入，见 §7.3，且它的降级是安全的 —— 不影响任何人能否连上和能否通话。）现在是认证器一挂，
  Murmur 退回自己的空账号库，每次登录都被拒，客户端显示"密码错误"，而语音端口照常应答、
  容器看起来完全健康。
- **重连不消耗登录限流配额。** 现在 `server/login.py` 按 CAN ID 限流登录失败，一个僵尸重连
  循环能把账号锁出语音，改对密码也没用，必须重启应用。改用 token 之后重连不碰密码。
- **ATIS 机器人的用户名编码 hack 消失。** `{cid}_atis{freq6}` 这套编码、以及它"`idToName`
  天生不自洽（`118000` 既是 CAN 118000 又是任何在 118.000 上播的 ATIS）"的问题，随 Murmur
  用户 id 一起没有了。ATIS 会话就是普通会话，带一个 `station` 标记。

随之删除的服务端组件：`server/login.py`、`fix_acl.py`、`whereami.py`、`serverconf.py`、
六份 `mumblecompat.py`、`Dockerfile`、`start.sh`、以及客户端侧固定证书指纹的那一整套
（`PINNED_FINGERPRINTS`、`CAN_MUMBLE_FINGERPRINTS`、`_PinnedSSLSocket`、`CertificatePinError`）
—— QUIC 用正常的 Let's Encrypt 证书链，不再需要指纹固定，也就不再有"证书卷丢了则全网客户端
拒绝连接且无远程修复手段"的风险。

## 7. 射程与衰减

### 7.1 为什么必须在服务端做

如果只在客户端衰减、服务端无脑扇出：全球 200 人同时在 121.500 时，每个客户端要收下 200 路
流才能丢掉其中 199 路 —— 200 × 50 包/秒 × 73 字节 ≈ **5.8 Mbps 下行**。客户端衰减解决了
"吵"，解决不了"爆炸"。

所以拆成两件事：

| | 在哪 | 做什么 | 为了什么 |
|---|---|---|---|
| 射程过滤 | 服务端 | 超出射程直接不扇出 | 带宽；顺带防作弊 |
| 平滑衰减 | 客户端 | 边缘区降音量、混静噪 | 真实感；硬截断听起来像 bug |

### 7.2 射程怎么算

**飞行员**用 VHF 视距公式，双方高度决定：

```
range_nm ≈ 1.23 × (√h₁ + √h₂)       h 单位英尺
```

FL350 对 100 英尺的地面台约 240 nm；两架 FL350 之间约 460 nm；地面两架之间约 20 nm。公式
本身就产生了正确的行为，不需要额外规则。

**管制员和 ATIS 用 datafeed 里已有的 `visual_range`**（单位 nm），那是管制员在 `#AA` 里
声明、EuroScope 报上来的权威值 —— `ZSHA_CTR` 是 600，`LAX_25_CTR` 是 600。

`visual_range` 为 0 时（golden 文件里 `ZSSS_ATIS` 正是 0）退回一张按席位后缀的兜底表：

| 后缀 | 半径 nm |
|---|---|
| `_DEL` / `_GND` | 15 |
| `_TWR` | 30 |
| `_APP` / `_DEP` | 80 |
| `_CTR` | 250 |
| `_FSS` | 600 |
| `_ATIS` | 60 |

这张表是**服务端配置**，不是编译进去的常量：中国的 FIR 尺寸和 VATSIM 的默认值不一样，调它
不该需要发版。

服务端的过滤边界比算出的射程放宽 10%，边缘那一段交给客户端平滑处理 —— 否则飞机停在射程线
上时音频会忽有忽无。

### 7.3 位置从哪来

can-voice 订阅 can-fsd 的 `GET /v1/events`（SSE：连接时一个 `snapshot`，之后每 tick 一个
只含变化条目的 `update`）。关联键是 **`cid`**，它在 `pilots`、`controllers`、`atis` 三段里
都有，而 token 本来就带 `cid` —— 所以正常情况下协议里**一个字段都不用加**。

三件必须写下来的事：

- **类型不对称。** 飞行员的 `latitude`/`longitude` 是 JSON **数字**（`25.10232`），管制员和
  ATIS 的是 JSON **字符串**（`"31.20466"`）。这是 can-fsd 刻意的、由
  `testdata/datafeed_golden.json` 钉住的契约。Go 侧的解析必须两种都吃，否则会静默地把所有
  管制员的位置解析成 0,0（几内亚湾），表现为管制员谁都听不见。
- **观察员模式没有 FSD 连接。** XPC 的副驾只有语音，can-fsd 不知道他在哪。他在 `HELLO` 里
  用 `follow:"CCA1501"` 声明跟随主驾的呼号，服务端用那架飞机的位置。
- **降级是安全的，不是拒绝服务。** SSE 断开时退回"不做射程过滤"（等于现状，全球互通），并
  在日志里说明。语音比射程真实感重要得多。

### 7.4 信号质量放在包头里

服务端算好 0–255 的信号质量填进 `qual`，客户端直接拿它做增益和静噪强度。`qual` 是距离与
射程之比 `d / range` 的函数，和 §7.2 那个放宽 10% 的过滤边界是同一条曲线的两端：

| `d / range` | `qual` | 客户端行为 |
|---|---|---|
| ≤ 0.8 | 255 | 全音量，无噪 |
| 0.8 → 1.1 | 255 → 0 线性 | 按比例降增益并混入静噪 |
| > 1.1 | — | **服务端不扇出**（降级模式下扇出并填 255） |

也就是说：客户端只会收到 `qual` 大于 0 的包，`qual` 越低说明越接近射程边缘。0.8–1.1 这段
重叠区的存在，是为了让飞机停在射程线上时音频逐渐变差而不是忽有忽无。

**客户端因此完全不知道别人在哪。** 隐私和防作弊是免费拿到的，客户端那侧只剩一条增益曲线
和一个噪声混合，逻辑简单到可以纯函数单测。

顺带解决一件事：现在 ATIS 是全球可听的，有了射程它自然只在机场附近听得到。

## 8. 服务端设计

全部状态：

```go
type Server struct {
    sessions map[SessionID]*Session            // QUIC 连接 + cid + 订阅
    rx       map[FreqKHz]map[SessionID]struct{} // 谁在听这个频率
    xc       map[SessionID][][2]FreqKHz        // 交叉耦合规则
    pos      map[CID]Position                  // 来自 can-fsd SSE
}
```

没有数据库、没有持久化、没有 ACL、没有 SuperUser、没有证书卷。进程重启 = 所有人重连并重发
`SUB`。**"丢了 `/var/lib/mumble-server` 卷则全网瘫痪且无法远程修复"这个风险直接不存在。**

收到一个上行 datagram 的处理路径：

1. 校验发送方确实声明了在 `freq_khz` 上发送（否则丢弃并记一条 `NOTICE`）
2. 取 `rx[freq_khz]` 的订阅者集合
3. 对每个订阅者算 `qual`；低于阈值的跳过
4. 填入 `speaker` 和 `qual`，原样转发（**不解码**）
5. 应用该发送方的 `xc` 规则，对耦合频率重复 2–4

**交叉耦合从客户端移到服务端。** 现在是客户端把 A 频率收到的音频重新发到 B 频率，靠临时
改 voice target 实现，还必须小心"自己讲话时不要转发"、"每个耦合频率要有自己的 target id
否则会悄悄改掉下一次 PTT 的去向"。服务端知道全部路由，一条规则就够，客户端那段逻辑整个删掉。

部署：一个 Go 二进制，一个 UDP 端口，无状态，可水平扩展（多实例时需要实例间转发，**不在
本期范围**，单实例足够当前规模）。

## 9. 客户端核心库

```
crates/can-voice-client/
  conn.rs       QUIC 连接生命周期、有界重连
  session.rs    订阅状态机 —— SUB 的唯一真相源
  rx/jitter.rs  抖动缓冲，per (speaker, freq)
  rx/decode.rs  Opus 解码 + PLC
  rx/mix.rs     混音、同频干扰音、射程衰减
  tx/           采集、Opus 编码
  stack.rs      无线电栈模型（RX/TX/XC 耦合规则）
  audio.rs      设备枚举与选择（cpal）
```

### 9.1 公开 API 是声明式的，不是命令式的

```rust
client.set_subscription(sub);              // 全量意图，对应 SUB
client.set_transmitting(true);             // PTT
client.set_frequency_volume(khz, gain);
client.events()                            // Connected / RxStart / RxEnd / TxDenied / Health
```

**没有 `join_channel()`，没有 `leave_channel()`，没有 channel id。** 这是从现有代码里提炼
出的最重要一条教训 —— `CLAUDE.md` 写了两遍："*Joining a channel is not a fact you can
remember*" 和 "*A channel id is not a fact you can remember either*"。声明式 API 里根本
没有"记住"这个动作：客户端只声明意图，库负责让服务端收敛过去，重连后自动重发。

`stack.rs` 放在共享库而不是 controller 里：xpc/msfs 用的是它的退化版（单频率）。现有的
`controller/test_radiostack.py` 逐条可翻译。

### 9.2 抖动缓冲

按 `(speaker, freq)` 分开 —— 同频可能有多个发言者，各自网络路径不同。

- 自适应深度 40–120 ms（2–6 帧），起始 60 ms
- 丢包用 Opus 自带 PLC（`decode(None)`）；连续丢超过 3 帧则静音并结束本次发言
- 乱序按 `seq` 插入；迟到超过缓冲深度的丢弃
- 收到尾帧（`flags` bit1）即排空缓冲并立刻发 `RxEnd`

### 9.3 混音、干扰音与衰减

```
对每个订阅频率 f：
    sources = f 上当前活跃的发言者解码流
    每路先按自己的 qual 做增益与静噪
    若 sources.len() == 1  →  out_f = sources[0]
    若 sources.len() >= 2  →  out_f = interfere(sources)
    out_f *= 用户设置的该频率音量
final = Σ out_f，软限幅
```

`interfere()`：两路以上同频信号相加后注入随机相位的拍频啸叫（真实 AM 无线电上两个载波差频
产生的音，约 1–2 kHz）并加轻微削波失真。

这一整块是**纯 DSP，无 I/O**，和 `radiostack.py` 一样是全库最值得单测的部分：给定 N 路输入
断言输出频谱含拍频分量。

## 10. 测试策略

现有约 9000 行测试中的大部分是为 pymumble 各种坑搭的假服务器，会随之作废。新的重心：

| 层 | 方法 |
|---|---|
| 协议编解码 | **Rust/Go 跨实现黄金文件** —— 防两边漂移的唯一手段；can-fsd 的 `datafeed_golden.json` 已证明这招有效 |
| 服务端扇出 | Go 表驱动：给定订阅集合 + 一个上行包，断言扇出目标集合 |
| 射程 | Go 表驱动：位置/高度/席位 → 期望 `qual`，含 `visual_range=0` 的兜底路径和 SSE 断开的降级路径 |
| SSE 解析 | 直接吃 can-fsd 的 `datafeed_golden.json`，断言数字与字符串两种经纬度都解析正确 |
| 订阅状态机 | Rust 纯逻辑，无 I/O；重点是重连后重发 |
| 抖动缓冲 | 喂乱序 / 丢包 / 重复 / 迟到序列，断言输出 |
| 干扰音与衰减 | 纯函数，断言输出频谱与增益曲线 |
| 无线电栈耦合 | 直接翻译现有 `test_radiostack.py` |
| 端到端 | CI 里真跑 Rust 客户端 + Go 服务端：连接、订阅、发音频、收到、断连重连后订阅自动恢复 |

## 11. 切换路径

**新版在 `can-voice` 里独立做完，择日一次性切换，旧版随即停用。** 不做 PyO3 过渡桥，不做
客户端逐个迁移 —— 这是一个明确的决定，代价写在 §12。

期间 `can-audio` 的 Mumble 服务和 2.2.x 客户端**照常运行**，不做任何功能改动，只接受安全
修复。网络的语音不能停几个月等新版。

### 11.1 切换日之前

- **封闭测试**：新版全部就绪后、公开切换之前，找若干人（覆盖不同网络环境和三个平台）实际
  用一段时间。这不是"分阶段上线"，是测试 —— 大爆炸切换唯一能做的验证就是把它提前。
- **跨仓库联动必须先谈好**（见 §11.3），否则切换当天用户拿不到新版。

### 11.2 切换日

| 步骤 | 动作 |
|---|---|
| 1 | can-api 的 `/api/v1/clients/latest` 与 `/api/v1/clients/download/<client>` 指向 `can-voice` 仓库的 release 资产 |
| 2 | can-voice 服务端上线 |
| 3 | 公告 |
| 4 | Murmur 容器**停止但不删除**，卷保留 |

**旧客户端会自己提示升级，这就是切换机制。** 四个客户端启动时都会调 `/api/v1/clients/latest`，
而版本比较两侧都是数值的（`update.is_newer` 与 can-api 的 `compareVersions`），所以
`3.0.0 > 2.2.x` 成立 —— 不需要任何额外的通知渠道。

**保留的退路（唯一一条）**：Murmur 容器停而不删、卷保留，旧客户端安装包保持可下载，保留
至少两周。这不影响"一次性切换"，但意味着新版出现无法当场修复的问题时，把容器起回来加上
回滚 can-api 那两个地址，就能在十分钟内回到旧世界。**两周后删除，届时这条退路消失。**

### 11.3 跨仓库连锁反应

换仓库会打断发行链，这三处必须在切换日前改好：

| 在哪 | 什么 |
|---|---|
| can-api | `/api/v1/clients/latest` 的版本来源、`/api/v1/clients/download/<client>` 的中转地址 —— 都锁在旧仓库的 release 资产 URL 上 |
| can-api | 新增 `POST /api/v1/voice/token`（§6）。**这是新版能否登录的前提，不是可选项** |
| 监管仓 | `.gitmodules` 去掉 `can-audio`、加入 `can-voice` |

四个产品名 `audio-for-can` / `atis-for-can` / `xpc-for-can` / `msfs-for-can` 是 can-api 里的
固定白名单。**沿用这四个名字**，否则下载中转要一起改，白白多一处可能出错的地方。

### 11.4 切换之后

`can-audio` 归档（archive，不删除）—— 它是十几个已修 bug 的唯一记录，`CLAUDE.md` 里那份
踩坑清单尤其。本设计的 §13 已经把其中被结构性消除的部分摘出来了，但没被摘出来的那些（音频
设备回退、PTT 的 SDL 线程陷阱、机型匹配的分级顺序、ATIS 中文播报的措辞）在新仓库里仍然是
有效知识，要在对应模块落地时逐条带过去。

## 12. 风险与未决问题

**QUIC 是纯 UDP，没有 TCP 回退。** Mumble 有（UDP 优先，不通则退回 TCP 隧道 —— 讽刺的是
pymumble 只实现了退化的那一半，这正是当前延迟问题的来源）。用户在大陆，部分运营商和企业
网络对长时间 UDP 流不友好，QUIC 尤甚。不做回退的话，这些用户会从"音质差"变成"完全连不上"。

**处置：这就是 P1，排在所有实现之前。** 服务端一个 QUIC echo，客户端一个丢弃型命令行工具，
找若干不同网络环境的用户跑一周，拿真实连通率再定要不要做 stream 回退通道。音频帧格式
**现在就**设计成既能走 datagram 也能走 stream（两者携带的是同一个 13 字节头加同一个 Opus
帧），这样日后要加回退不必改协议。

一次性切换放大了这条风险：如果连通率问题要到切换日才暴露，那时四个客户端都已经做完了。
花几天先量出来，比之后返工便宜两个数量级。

**一次性切换本身是一个已知风险，且已被明确接受。** 后果有三条，都无法消除、只能减轻：
协议和 UI 第一次面对真实用户是同一天，出问题时难以定位是哪一层；中间很长一段时间没有任何
东西上线，无法从真实使用中获得反馈；切换当天除了 §11.2 那条"两周内起回 Murmur"之外没有
退路。减轻手段只有两个 —— **P1 的连通性探针**，和 **§11.1 的封闭测试**。两者都不是可选项。

**没有解决 / 明确不做的：**

- 多实例水平扩展需要实例间转发，本期单实例。
- 语音录音与回放（现在也没有）。
- pbh 符号问题（`CLAUDE.md` 末尾那条）与本设计无关，仍然悬而未决，仍然只能靠 openfsd 或
  真实 EuroScope 抓包来定。
- 席位半径兜底表的具体数值需要按中国 FIR 的实际尺寸校准，初值是估的。

## 13. 本设计消除的现存问题

对照 `CLAUDE.md` 里逐条记录的坑，看哪些是被**结构性**消除的（不是"修好了"，是"没有发生的
地方了"）：

| 现存问题 | 为什么消失 |
|---|---|
| `wrap_socket` 在 3.12 被删、`patch_send()` 把满缓冲当断线、`parent_thread` 杀死主循环、阻塞命令永不超时 | 不再有 pymumble |
| 频道 id 失效、临时频道被销毁、建频道是个 round trip | 不再有频道，频率是 u32 |
| `sync()` 风暴（202 次建频道 / 93 次同秒 / 40 次移动无效） | `SUB` 全量声明，幂等 |
| 重连后人在 root 但客户端以为还在原频道 | 声明式 API，重连即重发 |
| `Listen` 权限缺失导致除主频率外全部静音 | 不再有 ACL |
| ATIS `MakeTempChannel` 被拒显示成"频道不存在" | 不再有频道 |
| `listenersperuser` / `listenersperchannel` 上限 | 不再有 listener |
| 认证器挂掉表现为"密码错误"而容器健康 | can-voice 自己验签，无此耦合 |
| 僵尸重连锁死账号 | 重连用 token，不碰登录限流 |
| `idToName` 天生不自洽 | 不再有 Murmur 用户 id |
| 丢失 sqlite 卷导致全网瘫痪 | 服务端无持久化 |
| 丢失证书导致全网客户端拒连且无远程修复 | 正常 CA 证书链，不再指纹固定 |
| 同频两人讲话只有一个 RX 灯亮 | 包头带 `speaker` |
| 松开 PTT 后 RX 灯还亮半秒 | 包头带尾帧标志 |
| 音频走 TCP 隧道导致延迟抖动 | QUIC unreliable datagram |
| 交叉耦合会悄悄改掉下一次 PTT 的去向 | 交叉耦合移到服务端 |
| 全球同频互相干扰 | 服务端射程过滤 |
