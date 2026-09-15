# P4：四个 Tauri 客户端与服务端 ATIS 机器人 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `can-voice-client` 变成成员真正装得上的东西 —— 四个桌面客户端与一个服务端机器人，
产品名沿用 `audio-for-can` / `atis-for-can` / `xpc-for-can` / `msfs-for-can`。

**Architecture:** 五个产品共用一个 Rust 核心（`can-voice-client`）。四个桌面端是 Tauri：
Rust 侧持 `VoiceClient`、把事件流桥到前端，Vue 前端只画界面。ATIS 机器人没有界面，
音频来自 TTS 经 `push_audio` 注入。

**Tech Stack:** Rust 2021（MSRV 1.80）、Tauri 2.11、Vue 3 + `@jianyuelab-org/can-ui`、
`rdev`/`gilrs`（PTT）、cpal（已在核心库里）

**Spec:** `docs/superpowers/specs/2026-09-12-can-voice-design.md`

**前置依赖:** P3 已完成（`can-voice` 的 `feat/p3-rust-client-core`，136+ 测试、4 条端到端）。
**另有两个硬前置在本仓库之外，见 §零。**

---

## 零、先读这一段

### 0.1 规模的实话

P4 不是一个任务，是**五个独立的产品**。被替换掉的那四个 Python 客户端加起来是：

| 客户端 | Python 文件 | 行数 |
|---|---|---|
| `controller` | 22 | 9,766 |
| `atis` | 28 | 11,306 |
| `xpc` | 23 | 12,908 |
| `msfs` | 21 | 10,775 |
| **合计** | **94** | **约 44,800** |

其中相当一部分是**不能简单重写的领域知识**：ATIS 的中文播报措辞是照着一段真实 ZBAA
广播逐元素定下来的，机型匹配的分级顺序是拿 24 个真实航班量出来的，PTT 的 SDL 线程陷阱
是一条日志里 40 次失败换来的。§五 把这些逐条列了出来——**那一节不是背景材料，是任务清单**。

按设计文档的建议顺序做：**controller → atis → xpc → msfs**。controller 的界面最简单
（一栈频率加状态栏），atis 次之（模板编辑器复杂但不接模拟器），xpc/msfs 最后，
因为要处理 SimConnect 和 X-Plane 桥接的 FFI。

### 0.2 硬前置一：`POST /api/v1/voice/token` —— **已做**

设计文档 §11.3 把它列为"**新版能否登录的前提，不是可选项**"，而它此前不存在：
can-api 的 110 条路由里相关的 0 条，整个仓库 0 处 ed25519。现在有了，
在 can-api 的 `feat/voice-token` 分支上。

- **非对称**（Ed25519）而不是像 `atcauth` 那样用 HMAC：can-voice 必须能验签而
  **不能签发**，共享密钥等于在每台语音服务器上放一个伪造者。
- **线格式是跨仓库契约，而且钉住了**：can-api 的 `voiceauth_test.go` 里那张黄金
  token 是**由 can-voice 自己的 `auth.Sign` 签出来的**。两个仓库之间没有共享代码，
  分岔的症状是"所有人都被 `token_invalid` 拒掉"，读起来像密钥不配对。
  另外做过一次真的跨仓库验证：can-api 签、can-voice 验，claims 一字不差。
- **私钥放 can-api**（`VOICE_TOKEN_KEY`，32 字节种子的 base64），公钥放 can-voice
  （`CAN_VOICE_API_PUBKEY`）。`go run ./cmd/voice-keygen` 一次生成两半并各自打上
  该去的变量名——放反了的症状是另一头的 `token signature does not verify`，
  那读起来像密钥不对而不是像拿反了。
- TTL 默认 60 秒、上限 10 分钟（对齐 `auth.maxTokenLifetime`）；`max_tx` 默认 8。

**还差一步，属于 Task 3：** 客户端要真的去调它。`can-voice-client` 的 `Config.token`
现在是调用方传进来的，四个应用要在连接前拿 CAN 号和密码换一张票，并在
`token_expired` 时换新票重连（`RefusedReason::TokenExpired` 是唯一可恢复的那一条，
而换票不是核心库能做的事——它拿不到凭据）。

### 0.3 硬前置二：`@jianyuelab-org/can-ui` 的访问

四个前端要装 GitHub Packages 上的私有包，需要一个有 `read:packages` 的 token。
**开工前先确认 `bun install` 装得下来**——这是那种"到第一个 `bun install` 才发现"的前置，
而那时骨架已经搭了一半。

装不下来时的退路是**不用 can-ui**，前端自带一套最小样式。那会让四个客户端和网站的
观感分家，是一个要明确做的决定，不是默默绕过去。

### 0.4 四个产品名是 can-api 的固定白名单

`audio-for-can` / `atis-for-can` / `xpc-for-can` / `msfs-for-can`。
**沿用，不要改名。** 它们是 can-api 的 `/api/v1/clients/download/<client>` 的固定白名单，
改名意味着下载中转要一起改，白白多一处可能出错的地方（设计文档 §11.3）。

注意 `audio-for-can` 指的是**管制端**，不是飞行员端——这个错位是历史遗留，
`can-audio/CLAUDE.md` 专门记了一条"产品名 ≠ 组件目录"。

---

## 一、共用的地基

### Task 1: 音频通路 —— **已完成**

**这一条在 P3 的计划里不存在，而它是 P4 每一件事的前提。** P3 交付的
`jitter.rs` / `decode.rs` / `mix.rs` / `tx::Encoder` 全都实现了、测过了，但**接在空气上**：
收到的 Opus 载荷直接丢掉，PTT 只打一行日志，`audio.rs` 里一个 cpal 流都没有。

已在 `can-voice` 的 `feat/p3-rust-client-core` 上补齐：

- `rx/mixer.rs` —— `feed` 由网络喂、`tick` 由 20 毫秒时钟拉，**每一拍出恰好一帧**
- `tx/capture.rs` —— 成帧、编码、`seq` 的三条契约
- `audio.rs` —— cpal 流活在一条自己的 OS 线程上（`cpal::Stream` 在 macOS 上是 `!Send`）
- `pump.rs` 的节拍从 200 ms 收到 **20 ms**，它现在就是音频时钟
- `Config.audio_devices` 与 `VoiceClient::push_audio`（给没有声卡的 ATIS 机器人）

验证：端到端测试 `audio_crosses_the_wire_from_one_client_to_another`，
两个客户端、两个账号，音频真的从一个穿到另一个。

- [x] 已完成，无需再做。留在这里是为了让下一个人知道它**曾经不存在**。

---

### Task 2: PTT —— **键盘与手柄已完成**

`crates/can-voice-ptt/` 已建，31 个测试。键盘、手柄、绑定模型、捕获与 PTT 的互斥、
失败设备只报一次都落地了；**鼠标那一半在 Windows/Linux 上可用，macOS 仍是缺口（R7）**。

**开工时核出一件会决定结构的事：`rdev::listen` 停不下来。** 没有 stop、没有
unsubscribe、没有 `CFRunLoopStop`，核过源码一处都没有——它跑到进程结束为止。
三条后果已经落进实现，也写在 `watcher.rs` 的模块文档里：

1. **一个进程最多起一次，而且懒起**——只有真的绑了键盘或鼠标才起（Step 4 的理由）。
2. **换绑定不重启监听**，只改共享状态。"取消了所有键盘绑定之后监听还在"是 rdev 的
   限制，不是疏忽。
3. **捕获共用这条监听**，不另起一条。那反倒把 Step 7 要防的两个问题结构性地消掉了。

**Files:**
- Create: `crates/can-voice-ptt/`（新 crate，四个桌面端共用）

**Interfaces:**
- Produces: `Binding`（键盘 / 鼠标侧键 / 手柄按钮）、`PttWatcher`、`PttCapture`

按下任意一个绑定即发话。**这一整块的规则是从 `can-audio/controller/ptt.py` 搬过来的，
逐条都是踩出来的，不要重新发明。**

- [x] **Step 1: 建 crate 与 `Binding` 模型**

三种来源：键盘、鼠标侧键、手柄按钮。设置里存的是一个**列表**（`ptt_bindings`），
不是单个键——`ptt.py` 当年从 `ptt_key` + `joystick_ptt` 升级过来时专门做了迁移，
因为"升级时悄悄丢掉某人的 PTT 键"看起来和麦克风坏了一模一样。

- [x] **Step 2: 鼠标只认侧键 X1/X2 —— 但 rdev 给不出名字，而且 macOS 根本给不出**（Windows/Linux 已落地；macOS 见 R7）

**绑左键意味着在任何窗口里点任何东西都会发话**，而 TX 指示灯还被挡在他点的东西后面。
所以左/右/中键一律不可绑，这一条无论如何都要有。

Python 版的规矩是"按**按钮名字**匹配而不是编号"（X11 叫 `button8`/`button9`，
Windows 和 macOS 叫 `x1`/`x2`）。**rdev 0.5.3 做不到这一条**，核过源码：

```rust
pub enum Button { Left, Right, Middle, Unknown(u8) }   // 没有任何侧键的名字
```

`Unknown(u8)` 里那个数是**平台相关**的，三个平台三个样：

| 平台 | 侧键怎么来 | 值 |
|---|---|---|
| Windows | `WM_XBUTTONDOWN` → `Unknown(HIWORD(mouseData))` | `Unknown(1)` = X1，`Unknown(2)` = X2 |
| Linux/X11 | `Unknown(X11 按钮号)`（4–7 是滚轮，已滤掉） | `Unknown(8)` = X1，`Unknown(9)` = X2 |
| **macOS** | **完全没有** | —— |

**macOS 那一行不是"待补"，是 rdev 压根没实现。** `macos/common.rs` 只处理
`LeftMouseDown/Up` 与 `RightMouseDown/Up`，`OtherMouseDown`/`OtherMouseUp` 一处都没有
——连中键都报不出来，`simulate.rs` 里还留着一句注释
`// ignored because we don't use OtherMouse EventType`。

三条后果，都要落实：

1. 存进设置的必须是**规范化后的 X1/X2**，不是 `Unknown(n)` 的原始值。否则同一份设置
   在 Windows 上是 X1、在 Linux 上什么都不是——而症状是"我的 PTT 突然不灵了"，
   没有任何报错。规范化表要有测试钉住，这样将来改一个数字是一行可见的 diff。
2. **macOS 上鼠标绑定要在界面上明说不可用**，而不是让它静默地永远不触发。
   一个绑好了、显示正常、却从来不响的 PTT，正是这个项目反复要躲开的那类故障。
3. 真要在 macOS 上支持，得自己下一层写 `CGEventTap` 收 `OtherMouseDown/Up`
   （或者给 rdev 提 PR）。**那是一件独立的事，不要塞进这个 task 里**——
   先把键盘和手柄做对，鼠标侧键在 macOS 上作为已知缺口列出来。

- [x] **Step 3: 绝不吞事件**

Python 版的规矩是"never `suppress=True`"。吞掉事件意味着那个键在**其它所有程序里**
都失灵，而它在模拟器里通常还有别的用途。Rust 侧用 `rdev` 的监听模式，不要用会拦截的接口。

- [x] **Step 4: 只启动被绑定的来源**

macOS 上创建全局键盘监听会触发辅助功能授权弹窗。**用户只绑了手柄却被要求授权键盘监控，
读起来像恶意软件。**

- [x] **Step 5: 手柄要轮询。那条 SDL 线程陷阱**不成立**，但要写明为什么**

Python 版踩的是这个：`SDL_Init(SDL_INIT_JOYSTICK)` 会为 DirectInput 建一个隐藏窗口，
而 **Win32 在线程退出时销毁该线程的窗口**；SDL 只建一次，于是第一个初始化 SDL 的线程
一旦退出，之后每次打开手柄都以 `E_HANDLE` 失败，直到进程重启。一条真实日志里是
01:28:11 成功捕获一次、01:28:13 之后每 3 秒失败一次、持续到结束。

**核过了：`gilrs 0.11.2` 不依赖 SDL**（Cargo.toml 里没有），所以那个隐藏窗口的机制
不存在，这条陷阱**不适用**。把结论写进代码注释——否则下一个人会照抄一条不存在的
约束，然后为了满足它把初始化硬钉在 UI 线程上，白白多一处耦合。

**但不要就此认为手柄没有线程问题**：gilrs 在 Windows 上走的是 XInput/DirectInput，
它自己怎么初始化尚未核实。真机上第一次跑手柄 PTT 时要专门看一眼"插拔之后还能不能打开"。

- [x] **Step 6: 打不开的设备只报一次**

打不开的手柄会一直打不开。Python 版那条日志填满了整整一轮轮转。
每个设备首次失败记一条 WARN，之后降到 DEBUG，成功打开后清零。

- [x] **Step 7: 捕获（"按一下你要的键"）——共用同一条监听，两者互斥**

两个线程同时泵同一个事件队列不是线程安全的，而且**正在录的那一下会被播出去**。

**落地时结构变了，而且更好：`rdev::listen` 根本停不掉，所以捕获只能共用那一条监听。**
`Router` 让捕获与 PTT 互斥——捕获期间事件只进捕获、不驱动 PTT——于是"两个线程同泵
一个队列"和"录的那一下被播出去"两个问题都不再有存在的余地，而不是靠纪律避开。

- [x] **Step 8: 这个 crate 不产生任何界面文字**

`Binding::token()` 返回 `"V"` / `"X1"` / `"3"`，措辞由上层的 i18n 决定。
共用文件里带中文会让同一句话出现在四个地方，而翻译时会漏掉两个。
`can-audio` 用一条 AST 扫描钉住这一点，Rust 侧照做（扫源码里的 CJK）。

---

### Task 3: Tauri 骨架与事件桥

**Files:**
- Create: `apps/_shared/`（Rust 侧的共用命令层）

四个客户端共用一层：Rust 侧持 `VoiceClient`，把 `Event` 流桥成 Tauri 事件，
把前端的操作桥成 `set_subscription` / `set_transmitting` / `set_frequency_volume`。

- [ ] **Step 1: 定命令与事件的形状**

前端能做的事就是核心库公开的那几件，**不多一件**：

```
命令：connect / disconnect / declare(sub) / set_ptt(bool) / set_volume(freq, gain)
       / list_devices / capture_ptt_binding
事件：state / rx_start / rx_end / tx_denied / rx_denied / xc_denied / refused
       / notice / health
```

**桥这一层不得引入 `join` / `leave` / `channel_id`。** 核心库有一条扫全部模块文件的
测试钉住这件事，桥这一层要有同一条——否则"声明式"这条设计在离用户最近的一层被绕开，
而那正是它要防的地方。

- [ ] **Step 2: 事件是广播，前端可能晚到**

`VoiceClient::events()` 是 `broadcast`，订阅之前发生的事收不到。桥这一层要把**状态**
（当前链路状态、当前订阅、每个频率的 RX 灯）单独持一份，前端一挂上就能查，
而不是靠"从第一条事件开始拼"。窗口重开、前端热重载都会打断事件流。

- [ ] **Step 3: 掉线的三种终态要分开呈现**

`Offline` / `Evicted` / 协议违规。**`Reconnecting` 与 `Offline` 是对立的**：前者意味着
链路还活着，界面不要把对象引用丢掉；后者意味着它没了。`Evicted` 要单独说
"账号在别处登录了"，而不是笼统的"连接断开"——`RefusedReason::ProtoUnsupported`
同理，那一条要说的是"请更新客户端"。

- [ ] **Step 4: 日志**

`applog.py` 的那套规矩搬过来：日志文本英文、界面文字中文；INFO 记"什么变了"而不是
"发生了什么"；**掉线必须自己解释**（`Event::Health` 的 RTT 与收发计数跟着掉线那一行打出来）。
`can-audio` 实测把一份真实日志压掉 29%，靠的是"每次通话一行而不是两行"。

- [ ] **Step 5: 更新检查**

四条规矩照搬：**走 can-api 不走 GitHub**（大陆连 GitHub 的 60 MB 资产常常卡死）；
版本比较两侧都是数值的（`2.0.10` 按字符串排在 `2.0.9` 前面）；失败要**安静**；
**绝不自动更新**；**不要打断正在工作的人**（管制员连着时、ATIS 在播时只在状态栏留一行）；
**记住被跳过的版本**。

---

## 二、四个客户端

四个都是 Tauri + Vue。**界面之外的行为规则在 `can-audio/CLAUDE.md` 里**，
那份文档是这四个产品十几个已修 bug 的唯一记录；下面只列每个客户端的骨架和
各自最容易出错的地方。

### Task 4: `audio-for-can`（管制语音客户端）

最简单的一个：一栈频率，每个带 RX/TX/XC 三个开关，加一个状态栏。

- [ ] 界面：`RadioStack` 一行一个频率 —— 频率、呼号、RX/TX/XC、音量、RX 指示灯
- [ ] 三条耦合规则**直接用核心库的 `stack.rs`**，前端不要再实现一遍
- [ ] 交叉耦合被拒（`XcDenied`）必须显示出来 —— 一个设好了耦合却不生效、
      又不知道为什么的管制员，正是整个重写要逃离的那类故障
- [ ] 频率超过 `max_tx` 时**在声明之前**就提示（`Limits` 已经从 READY 带上来了），
      而不是等 `TxDenied` 事后发现
- [ ] 台面（频率与开关）持久化，会话回来时还原

### Task 5: `atis-for-can`（情报通播客户端）

vATIS 的词汇：profile → station → preset → template。

- [ ] 天气：METAR 解析，每个要素都有 `text` 与 `voice` 两种形态
- [ ] 模板渲染两遍（一遍出文字、一遍出朗读），`:VOX` 是"把朗读形态塞进文字版"
- [ ] **中文播报是重新渲染，不是翻译**，而且措辞逐条照 §五.4
- [ ] vATIS profile 导入：`frequency` 是**赫兹的 uint**（`133800000` = 133.800），
      这是最容易读错、后果最重的一个字段
- [ ] 信息识别码按 station 的码段轮转，raw METAR 变了才进位
- [ ] 从 can-api 取网络配置（`/api/v1/atis/config`）——**只加不覆盖**，
      正在播的 station 一律不碰
- [ ] 音频经 `push_audio` 注入（TTS 合成），不开麦克风

### Task 6: `xpc-for-can`（X-Plane 飞行员端）

三条互不影响的链路：X-Plane UDP、FSD、语音。

- [ ] `PI_XpcTraffic.py` **原样保留**，连同 `bridge.py` 的 `PROTOCOL_VERSION` 一致性检查
      —— 它跑在 X-Plane 自己的 XPPython3 里，必须是 Python
- [ ] 订阅而不是轮询 dataref（位置报告一秒五次，一问一答扛不住）
- [ ] Windows 上的 `ConnectionResetError` 是**正常**的（UDP 打到没人听的端口），
      当成超时而不是致命错误
- [ ] PBH 打包沿用 `pack_pbh` **原样**——符号已经定了（R3），它和 `Vatsim.Network`
      的 pack 一致，改它才会出问题
- [ ] 机型匹配的分级顺序见 §五.3
- [ ] 观察员模式：**不开 FSD 连接**，频率可手输（空 = 跟随 COM1）

### Task 7: `msfs-for-can`（MSFS 飞行员端）

和 xpc 同一套，换 SimConnect。

- [ ] `snapshot()` 的字段要和 xpc 那边**逐字段一致**
- [ ] SimConnect 的三个陷阱见 §五.5
- [ ] AI 飞机注入用 `AICreateNonATCAircraft`，TCAS 因此是免费的（不必手填数组）

---

## 三、服务端 ATIS 机器人 —— **已完成**

### Task 8: 机器人 —— **已完成**

`crates/can-voice-atis/`，45 个测试。跑在服务器上，没有界面、没有声卡。

- [x] 从 can-fsd 的 datafeed 取 `atis[]`，为每个 `_ATIS` 呼号起一路
- [x] 音频经 `push_audio` 注入，`Config.audio_devices = false`
- [x] **没有任何绕过账号的捷径。** `TokenSource` 只有一个要凭据的构造函数，
      配置里也没有 token 字段；`no_shortcut_for_any_account` 扫源码钉住，
      变异验证过
- [x] `frequency` 为 `199.998` 表示"没设频率"，近似相等判（上游可能给 `199.9980`）
- [x] **不设有界重连**，退避到 60 秒上限但一直重来
- [x] **死掉的那一路要重新拉起**：判据是 `alive` 不是"在不在表里"
- [x] 取不到 datafeed 不停播；报文变了只换文本不重开；重开先停后起（顶号）
- [x] 别人在这个频率上开口就让出，而且让开之后这一轮不接着播

**落地时撞上一个计划里没有的约束，差一点静默地把报文吃掉：**
`TxPipeline` 只缓 4 帧、溢出丢最旧的。一段 30 秒的 ATIS 一次灌进 `push_audio`，
留下的是**最后那 80 毫秒**。所以播出按 20 毫秒一帧喂，最后一帧补静音
（短帧到编码器那里是 `WrongFrameSize`，整帧被丢掉，报文结尾被切掉）。

**搬 `process.py` 时露出一个它本来就有的毛病**，钉成了可见的记录而不是顺手改掉：
`/LEVEL 3600 M ADZ` 里的 `M` 是"米"，被念成 "Mike"。改对需要一张"哪些孤立大写
字母是单位"的表，而 `M` 在别的位置确实可能就是字母 M。

**另一个已知缺口**：中文那一半里的孤立字母仍念英文 NATO 词（Python 版的
`replace_letter` 不分语言）。它多半是错的——客户端侧 `chinese.py` 研究过，
结论是要念中文字母词（`J` → 朱丽叶）——但 `CLAUDE.md` 只给了 J 一个字母，
26 个凑不齐，猜出来的表比照搬更糟。补齐那张表之后再改。

外部依赖两个：TTS 命令（默认 `edge-tts`）和 `ffmpeg`。Python 版的部署本来就要求后者。

---

## 四、打包与发布

### Task 9: 三平台打包

- [ ] 四个产品名固定（§0.4）
- [ ] **libopus 静态链接**已经由核心库保证，验证方法在 `can-voice/README.md`
- [ ] macOS 要签名与公证；Windows 要签名——**不签的话 SmartScreen 会拦**，
      而用户看到的是"这个程序不安全"
- [ ] `PI_XpcTraffic.py` 在包里要有**两份**：一份给应用内安装器用，
      一份放在 exe 旁边给自动安装够不着的人（X-Plane 装在需要管理员权限的位置、
      便携版、模拟器在另一台机器上）
- [ ] release 资产的地址是 can-api 的下载中转指向的地方，改仓库要连带改（§0.2）

---

## 五、必须带过来的坑

设计文档 §11.4 明写：`can-audio` 归档之后，"音频设备回退、PTT 的 SDL 线程陷阱、
机型匹配的分级顺序、ATIS 中文播报的措辞"在新仓库里**仍然是有效知识，要在对应模块
落地时逐条带过去**。这一节是那份清单。**每一条都对应一个已经发生过的故障。**

### 5.1 音频设备

已由核心库处理，但要知道原因：Python 版**没有重采样**，注释写着"48 kHz 是理想路径，
回退采样率会产生变调音频"——也就是说设备不支持 48 kHz 时用户听到的是变调的声音，
而那看起来像"语音系统坏了"。核心库现在优先向设备要 48 kHz，拿不到才两端重采样。

### 5.2 PTT 的线程陷阱

见 Task 2 Step 5。**要先核实它在 `gilrs` 上成不成立。**

### 5.3 机型匹配的分级顺序

**类别层必须排在"按前缀猜"之前。** `GENERIC_BY_PREFIX` 用两个字符猜，而 `A3` / `B7`
横跨宽体窄体：`B77W` 猜成 `B738`、`A359` 猜成 `A320`。按前缀先猜的话，
**只要装了 737-800 或 A320 这两个最常见的机型，每一架宽体都会退化成窄体**，
而类别层根本轮不到执行——一架 777 在别人屏幕上是 737。

`cslmatch.py` 和 `aimatch.py` **两边都错过**。当时的测试没抓到，是因为它只装了
`A319` 和 `B78X`：没有 `B738` 在场，前缀猜不到东西，于是类别层照样执行、断言照样通过。
**新的测试必须装一个 `B738`。**

真实数据：一个 375 涂装、38 机型的装机量，24 个典型航班里 11 个机型正确、
11 个合理替身、2 个不相干、**0 个航司涂装正确**——因为 375 个涂装里只有 40 个带
`icao_airline`。那是用户侧的事实，不是 bug：现实的天花板是"机型对、涂装不对"。

### 5.4 ATIS 中文播报的措辞

**照一段真实 ZBAA 广播逐元素定下来的，不要"简化"回去。** 每一条都曾经是错的，
而且对中国机组听起来就是错的：

- 信息识别码念**中文语音字母**（`J` → 朱丽叶）—— 念拉丁字母会让 TTS 在中文句子中间
  蹦出一个英文字符
- 风要**两半都点名**（`风向 三洞洞 度 风速 拐 米每秒`）—— `风 三洞洞 度 拐 米每秒`
  听不出哪个数是哪个
- 温度与露点带单位、符号在标签之后（`气温 二十四 摄氏度`、`露点负 八 摄氏度`）
- 观测时间说 `世界协调时` —— 光说 `时` 会被听成当地时间
- 结尾请机组报告识别码，不是 `完毕`
- 云底高按 **100 ft = 30 m** 念（精确的 30.48 会给出"九百一十米"，
  而真实广播说"九百米"）
- 温度与高度**计数**（二十五），航向与 QNH **逐位**（洞九洞）
- 跑道那一格接受两种输入：光秃秃的跑道号（自动加"使用跑道"前缀），
  或者一整段已经以"跑道"开头的构型描述（不加前缀）

### 5.5 SimConnect 的三个陷阱

- **对象 ID 是异步回来的，而 Python-SimConnect 把它丢了**：它把结果塞进一个没有请求
  关联的全局环境变量。必须自己维护 `requestID → objectID` 的表。
- **换钩子要换 trampoline，不是换属性。** 库在构造时就把方法包成了 ctypes trampoline，
  接收循环调用的是 trampoline。只替换属性等于装了一个永远不会被调用的钩子——
  而**上游看起来一切正常**：一份真实日志里是 10 次创建请求、0 个对象 ID、0 次移除、
  0 条警告，症状是注入的飞机冻在出生点、离场后删不掉、`#SB` 回来时又被创建一次。
- **`TRANSPONDER CODE:1` 是 BCD**（`0x1200` 当十进制读是 4608），
  **`PLANE_PITCH_DEGREES` / `PLANE_BANK_DEGREES` 名字里写着度、实际是弧度**，
  而且符号约定与 FSD 相反。

### 5.6 高度是真高不是指示高

`PLANE_ALTITUDE` 与 X-Plane 的 `elevation` 都是几何真高，而座舱高度表是按
Kollsman 窗修正过的指示高。巡航在标准气压下，两者每英寸汞柱差约 1000 ft。
**这不是单位 bug，没有东西可"修"**——FSD 位置包的第十个字段就是那个气压修正量，
Python 版两边都硬编码成 0，所以网络看到的是未修正的真高。

### 5.7 i18n

- UI 文字走查表，**源码里不得有界面字符串**，用一条扫描测试钉住
- **`kind → 文案` 的字典在导入时求值**，会冻住当时的语言、之后再也不跟着切换；
  默认参数同理
- **日志文本英文，界面文字中文**，分界线是**这个字符串最终去哪**，不是它在哪个模块里

---

## 六、风险与未决

| # | 事 | 影响 |
|---|---|---|
| R1 | ~~can-api 的 `/api/v1/voice/token` 不存在~~ **已做**（can-api `feat/voice-token`） | 剩下的是客户端去调它，属于 Task 3。见 §0.2 |
| R2 | `@jianyuelab-org/can-ui` 的访问未验证 | 到第一个 `bun install` 才发现，那时骨架已搭一半 |
| R3 | ~~PBH 的符号仍未定~~ **已定：can-audio 是对的，can-fsd 在取负** | 三份独立实现都不取负：`Vatsim.Network` 的 `PDUBase.PackPitchBankHeading`（真实客户端往线上发的东西，是权威）、openfsd 的 `fsd/util.go`、以及 can-audio 自己的 `pack_pbh`。修在 can-fsd 的 `fix/pbh-sign`。xpc/msfs 的编码**不用改** |
| R4 | Tauri 2 的三平台打包与签名 | macOS 公证、Windows 签名都要证书；不签的话用户看到的是"这个程序不安全" |
| R5 | 44,800 行 Python 的领域知识 | §五 列的是**已知**的那些。`can-audio` 归档前应当再过一遍 `CLAUDE.md`，那是唯一的记录 |
| R7 | ~~macOS 上没有鼠标侧键 PTT~~ **已定：接受这个缺口** | 不写 `CGEventTap`。`mouse_supported()` 返回 false，界面要**明说**在本系统上不可用——一个绑好了、显示正常、却从来不响的 PTT 才是要躲开的那种故障。键盘与手柄在 macOS 上照常 |
| R6 | 四个客户端的封闭测试 | 设计文档 §11.1：大爆炸切换唯一能做的验证就是把它提前。要覆盖不同网络环境和三个平台 |
