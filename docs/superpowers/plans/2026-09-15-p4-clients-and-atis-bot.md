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

## 状态（2026-09-16 回写）

> **2026-09-17 补记。** 下面列的 8 条未做项和 #52 都已合入 main（PR #53–#66）。
> #50 只做了版本号那一半（PR #67：版本号和预发布标记都从 tag 来），签名定为暂不做，
> 见 Task 9 与 R4。R5 的服务端审计做完了，开出 #68–#80，其中 **#68 会挡住切换**。
> 这一段余下的内容仍是 9-16 的快照。

**事实基准：can-voice 的 `main` @ `373004e`**——PR #51 合并之后的那个提交。

这一段写的时候基准还是 `fix/client-defects` @ `6a12cb3`，而那条分支边写边动：
先落了 `#34 席位兜底半径改成配置`，又落了 `#47 通播端六条运行期退化`，随后整条
分支合进了 main（#19 的两个提交本就在它里面，于是一并进去、自动变成 MERGED）。
下面每一条都以 `373004e` 为准。

计划里 Task 3–7 的复选框当时一个都没勾，**它们其实在 2026-09-15 一天之内全部落地了**：
PR #2（管制端 + 通播端 + 共用层）与 PR #8（xpc + msfs + 共用层）。Task 9 跟着
PR #12–#16 走到了"能装的测试版"：打了 v27.0.1 / v27.0.2 / v27.0.3 三个 tag，
其中后两个有 release（都是预发布），v27.0.1 只有 tag 没有发布物。

**然后 9-16 做了一次对着 can-audio Python 原版的逐项审计，开出 31 条 issue（#20–#50）。**
那次审计才是这份计划真正的验收：它证明 §五"必须带过来的坑"不是背景材料——
其中好几条确实没带过来。**31 条里 23 条随 PR #51 合进了 main，8 条仍未做**：
#29（i18n）、#36（观察员模式）、#38（跨实现黄金文件）、#40（提示音）、#42（msfs 机库扫描）、
#44（atis 四件缺失）、#45（设置对话框）、#50（签名与版本号）。

审计之外另有一条本次回写新发现的缺陷：**#52**（msfs 注入他机的姿态少取一次负号），
见 §六 R8。所以此刻 can-voice 上开着的是 9 条。

| Task | 状态 | 缺口 |
|---|---|---|
| 1 音频通路 | 已完成 | —— |
| 2 PTT | 已完成 | macOS 鼠标侧键是已接受的缺口（R7），界面已明说 |
| 3 Tauri 骨架与事件桥 | 已落地，**形状与计划不同** | 没有事件桥，改成轮询快照；掉线日志不带 RTT |
| 4 管制端 | 已落地 | `max_tx` 事前提示完全没有 |
| 5 通播端 | 已落地，**音频那一半被推翻** | vATIS 导入、`/api/v1/atis/config` 两件零代码 |
| 6 xpc | 已落地 | 观察员模式没做（#36）；Windows 的 `ConnectionResetError` 没分类 |
| 7 msfs | 已落地 | 注入他机的姿态**少取一次负号**（本次新发现，已开 #52） |
| 8 服务端通播机器人 | 已完成 | —— |
| 9 打包与发布 | **部分** | 第三个平台是 Linux 不是 macOS；两个平台都没签名；版本号不注入 |

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

**那一步已经做完了（2026-09-16 回写）：** `crates/can-voice-token/`，见 Task 3 末尾。
原文留在这里，因为它说明了这件事为什么不能放进核心库——`can-voice-client` 的 `Config.token`
现在是调用方传进来的，四个应用要在连接前拿 CAN 号和密码换一张票，并在
`token_expired` 时换新票重连（`RefusedReason::TokenExpired` 是唯一可恢复的那一条，
而换票不是核心库能做的事——它拿不到凭据）。

### 0.3 硬前置二：`@jianyuelab-org/can-ui` 的访问 —— **已验证，能装**

实测过，不是看文档：

```
+ @jianyuelab-org/can-ui@0.3.5
203 packages installed [4.52s]
```

做法和树里六个 Astro 站点（can-dev / can-radar / can-exam / can-efb /
can-controller / can-database）完全一样，各自都有一份 `.npmrc`：

```
@jianyuelab-org:registry=https://npm.pkg.github.com
//npm.pkg.github.com/:_authToken=${GITHUB_TOKEN}
```

**这个文件要提交**：它写的是 registry 地址，不是凭据。GitHub 的 npm registry
**即使是公开包也要求带令牌**，那是 GitHub 的规矩。本地用一个带 `read:packages`
的个人令牌，CI 里用工作流自带的 `secrets.GITHUB_TOKEN`。

**它在 Tauri 里能用，而且不必引 Astro。** 包里 35 个组件**有 34 个是 `.vue`**，
只有一个 `.astro`——Vue 应用不 import 它就是了。`astro` 虽然列在
`peerDependencies` 里，但留空不影响，真正要的两个 peer 是 `tailwindcss` 和 `vue`。
导出面：`.`、`./styles`、`./motion`、`./composables`、`./icons`、`./i18n`、
`./nav`、`./sites`、`./components/*`、`./assets/*`。

所以"不用 can-ui、前端自带一套最小样式"那条退路**不需要了**。

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

### Task 3: Tauri 骨架与事件桥 —— **已落地，但形状和下面写的不一样**

`apps/_shared/` **从未存在**（`git log --all -- 'apps/_shared*'` 是空的）。共用层是
workspace crate `crates/can-voice-app/`，只有 `bridge.rs` 与 `snapshot.rs` 两个文件，
落在 `f90eaae`；四个 Tauri 壳子各自后到。两条结构性差异值得记下来：

- **`apps/` 被故意排除在 workspace 之外**（`Cargo.toml:24-26`），所以
  `cargo test --workspace` 永远不编 wry/webkit——桥这一层几秒钟测完，
  四个壳子根本不是 workspace 成员。
- **通播端不用这座桥**（`apps/atis/src-tauri/` 里 0 处 `can_voice_app`）。
  "四端共用一层"实际是三端，理由见 Task 5：那一支不出声，也就没有 `VoiceClient`。

**Files:**
- ~~Create: `apps/_shared/`~~ → `crates/can-voice-app/`；`update.rs` 后来又分出去成了
  `can-voice-update`，同期分出的还有 `can-voice-log` / `can-voice-settings` /
  `can-voice-token`

- [x] **Step 1: 定命令与事件的形状** —— 命令落地了，**事件这一半整个没有**

计划里那九个事件（`state` / `rx_start` / …）**一个都不作为 Tauri 事件存在**：
`apps/*/src-tauri/` 里 0 处 `.emit(`，四个前端 0 处 `listen()`。它们变成了
**一个轮询的快照结构**——`apps/controller/src/App.vue:90` 每 200 毫秒
`invoke<Snapshot>("snapshot")`。核心库的 `Event` 只被 `bridge.rs:213` 的
`supervise()` 消费，折进 `Snapshot::apply`，前端一条事件也看不到。

**这不是漏做，是换了一种结构，而且它和 Step 2 是同一件事的两面：** 既然桥无论如何
都要为晚到的前端持一份状态，那么让前端只读那一份、不再另外收事件，就少了一条
会和快照打架的路径。代价要写明：延迟下限变成一个轮询周期，`RxStart`/`RxEnd`
这类瞬时事件只能靠快照里的集合体现，而且**窗口不可见时仍在轮询**。

命令名也改了。这张对照表是给将来读 `generate_handler!` 的人对表用的：

| 计划 | 实际 |
|---|---|
| `connect` | `connect(cid, password)` —— 收的是**凭据**不是票，见本节末尾 |
| `declare(sub)` | 没有。拆成 `add_frequency` / `remove_frequency` / `set_switch`，三者都经 `Bridge::with_stack` 重推一份**完整声明** |
| `set_ptt(bool)` | `set_transmitting(on)` |
| `list_devices` | `audio_devices()` |
| `capture_ptt_binding` | 拆成 `begin_ptt_capture()` + `take_captured_binding()` |
| `set_volume(freq, gain)` | 原样 |

计划之外还长出十几个：`snapshot` / `radios` / `feed` / `settings` /
`set_audio_devices` / `check_update` / `skip_update` / `log_file` / `send_log`，
PTT 绑定那一组，以及 xpc 独有的 `xplane_installs` / `install_plugin` /
`set_traffic_range` / `set_csl_dir`。两支飞行员端把 `snapshot` 换成复合的 `view()`，
把桥的快照嵌成其中的 `voice` 字段，并且**没有 `set_volume`**——频率跟着 COM1。

- [x] **Step 2: 事件是广播，前端可能晚到** —— 做了，但**订阅不在快照里**

`Bridge::snapshot()`（`bridge.rs:70`）返回 `Mutex<Snapshot>` 的克隆，
`attaching_late_still_sees_the_current_state`（`snapshot.rs:239`）钉住。
RX 灯是**说话人的集合而不是布尔**（`snapshot.rs:55`）,所以两个人同时说话、
其中一个停下时灯不会灭——这条计划里没写，是落地时才显出来的。

和计划不同的一点：**当前订阅不在快照里**，它在 `Inner::stack`，要另外调 `radios()`。
前端挂上时因此是两次调用而不是一次（`App.vue:80-81`）。

那条"桥不得引入 `join` / `leave` / `channel_id`"的扫描测试**有**：
`the_bridge_has_no_imperative_channel_verbs`（`bridge.rs:283`），和核心库那条
（`client.rs:282`）配对。**但它比核心库那条弱一级**：核心库递归走目录，桥这条用的是
平铺的 `read_dir`（`bridge.rs:286`）。`can-voice-app/src/` 今天是平的所以过得去，
将来多一个 `src/foo/mod.rs` 就对它不可见——正是核心库那条注释说自己修订过、
要躲开的"看起来在设防"的形状。

- [x] **Step 3: 掉线的三种终态要分开呈现** —— 桥和管制端做了，**两支飞行员端没有**

桥这一层是齐的：`Ended::{Offline, Evicted, Refused}`（`snapshot.rs:19`），
`Reconnecting` 不写 `ended`（`snapshot.rs:162`，`reconnecting_is_not_an_ending` 钉住），
`Offline` 用 `get_or_insert` 以免盖掉更具体的原因。管制端四句话都说得出来
（`App.vue:106,109,121,122`）。

**xpc 和 msfs 没跟上**：它们的 `voiceText()` 只看 `link`，`ended` 的类型是 `unknown`
（`apps/xpc/src/types.ts:191,196-210`，msfs 同），于是**每一种拒绝——`ProtoUnsupported`
也在内——都落进默认的那句"语音已断开"**。那正是 `conn.rs:64-67` 的注释专门要防的话。

**还有一种终态在更下游就被抹平了**：协议违规（`conn.rs:93` 的 `ProtocolViolation`）
被 pump 压成 `LinkState::Offline`（`pump.rs:154`），桥和界面无从分辨，只剩日志里
那一行还留着。计划说的"三种终态"，实际能分辨的是两种。

- [x] **Step 4: 日志** —— crate 有了，**掉线那一行仍然不解释自己**

`crates/can-voice-log/`：4×1 MiB 轮转、进程级 panic 钩子、按平台的日志路径、
带 CAN 号和密码回传 can-api，四个应用都接了（#28）。"日志英文、界面中文"在实践上成立，
**但没有任何测试钉它**，而且 `lib.rs:382,483` 两处注释还写着"这份日志里全是中文"
（那是给 UTF-8 安全的 `tail()` 找的理由，文字本身是陈旧的）。

**计划里最要紧的那一条没做到：** `Event::Health` 的 RTT 与收发计数**没有**跟着掉线
那一行打出来。四条掉线日志（`pump.rs:107,148,154,162`）一个数字都不带；快照倒是
特意留着 health，测试名就叫 `health_is_kept_for_the_drop_line`（`snapshot.rs:514`），
但没有任何地方去打印它。那些数字只在管制端界面上实时显示（`App.vue:357-362`），
**窗口一关就没了——而寄回来的正是日志文件**。

- [x] **Step 5: 更新检查** —— `crates/can-voice-update/`，四端都接了（#27）

四条规矩照搬：**走 can-api 不走 GitHub**（大陆连 GitHub 的 60 MB 资产常常卡死）；
版本比较两侧都是数值的（`2.0.10` 按字符串排在 `2.0.9` 前面）；失败要**安静**；
**绝不自动更新**；**不要打断正在工作的人**（管制员连着时、ATIS 在播时只在状态栏留一行）；
**记住被跳过的版本**。

**但它比的那个"自报版本"本身不可信，见 Task 9：** CI 不把 tag 注入 `tauri.conf.json`，
两者可以静默不一致（#50）。更新检查做得再对，比的也是一个可能撒谎的数。

- [x] **硬前置一的客户端那一半：`crates/can-voice-token/`**

§0.2 留下的"客户端要真的去调 `/api/v1/voice/token`"做了，而且做成了一个只认凭据的 crate：

- `TokenSource::fetch()` 在**解 JSON 之前**先看状态码，401/403 → `Error::Credentials`，
  其余 → `Error::Rejected(status)`。打错密码的人不会被送去查网络（#48）。
- 换票**只换一次**：`should_renew` 只对 `TokenExpired` 为真（唯一可恢复的那一条）。
  第二次过期是时钟问题，循环下去会撞上和 FSD 登录共用的那只按 CAN 号计的限流桶。
- **没有 `from_token` 构造函数**，`there_is_no_way_in_but_credentials` 扫源码钉住，
  通播机队那边还有一份下游副本——和 Task 8 的 `no_shortcut_for_any_account` 是同一条规矩。
- 会话掉了之后的换票在桥这一层（`bridge.rs:226,243`），
  `MIN_SESSION_BEFORE_RENEWAL = 30s`（票寿命的一半）挡住"连上就掉、掉了就连"的循环；
  `disconnect()` 先清 `session` 再关客户端，免得主动下线被当成掉线。

---

## 二、四个客户端

四个都是 Tauri + Vue。**界面之外的行为规则在 `can-audio/CLAUDE.md` 里**，
那份文档是这四个产品十几个已修 bug 的唯一记录；下面只列每个客户端的骨架和
各自最容易出错的地方。

### Task 4: `audio-for-can`（管制语音客户端）—— **已落地**

最简单的一个：一栈频率，每个带 RX/TX/XC 三个开关，加一个状态栏。

- [x] 界面：一行一个频率 —— 频率、呼号、RX/TX/XC、音量、RX 指示灯

**没有 `RadioStack.vue`**：那一栈是 `App.vue:320-333` 里的 `v-for`，行是
`components/RadioRow.vue`。呼号、单频静音、最后一次通话、本行发射时的高亮这四样
**一开始被削光了**（#46），9-16 才补回来（`6a12cb3`）——在那之前 RX 只是一个绿点，
管制员分不清是谁在叫他。

- [x] 三条耦合规则**直接用核心库的 `stack.rs`**，前端不要再实现一遍

落实了，而且两边都写了注释互指：行只发意图（`RadioRow.vue:24` 的
`switch: [name, on]`），Tauri 原样转发（`lib.rs:423-432`），规则只在
`crates/can-voice-client/src/stack.rs:200-249`。还原台面时按 RX→TX→XC 的次序
重放同一套规则（`lib.rs:145-156`），而不是绕过它们直接写状态。

- [x] 交叉耦合被拒（`XcDenied`）必须显示出来

`App.vue:273-277` 的顶部横幅"这些交叉耦合没有生效：…"。**是横幅不是逐行**——
`RadioRow.vue:130-131` 只有 TX/RX 两个被拒徽标，没有 XC 的。

- [ ] 频率超过 `max_tx` 时**在声明之前**就提示 —— **完全没有，这一条是空的**

`Limits { max_tx }` 从 READY 解出来了（`conn.rs:410`、`pump.rs:126`、`session.rs:18`），
但 **`limits()` 在 `session.rs` 自己的测试之外零调用方**：`Bridge` 没有访问器，
`Snapshot` 没有这个字段，没有任何 `Event` 带着它，前端的 `Snapshot` 接口里也没有，
`apps/controller` 里找不到任何一句相关提示文字。

于是用户只能从 `TxDenied` 事后知道——**正是这一条要躲开的那个次序**。
要补的是一条从 `Limits` 到快照的路，不是一句文案。

- [x] 台面（频率与开关）持久化，会话回来时还原（#26）

`Settings.radios`（`lib.rs:42`）在每次用户动作后重新从桥拉一份写盘，连接之前还原
（`lib.rs:84,145`），`a_saved_stack_is_restored_switch_for_switch`（`lib.rs:832`）钉住。

**这一支还缺的（都在 issue 里）：** #45 没有设置对话框，语音服务器地址、FSD 主机、
调试级别全退化成环境变量——装了 msi 的人没有地方改；深色主题、窗口置顶、精简模式
一并缺着，后两样是把窗口压在雷达屏上用的，不是装饰。

---

### Task 5: `atis-for-can`（情报通播客户端）—— **已落地，但音频那一半被推翻了**

**先读这一条，否则下面的复选框会读错：这一支不出声。**
`crates/can-voice-atis/src/lib.rs:11-38` 记着已经拍板的分工——**声音归服务端机队**
（机队不睡觉，桌面端会关机），桌面端只做稿子。`apps/atis/src-tauri/Cargo.toml`
因此连 `can-voice-client` 都不依赖，一处音频代码都没有。

**代价是写下来的，不是藏着的**：机队那边只有 `text_atis`，念的是原始码组，
所以 **`:VOX`、`voicefix` 和整份中文稿都照样算、照样存、照样在界面上给操作员看，
但从来没有被播出去过**。要让它们出声得先在 can-fsd 那边加一个字段，那不是这个仓库
一家能决定的事。渲染保留在本地，就是为了哪天改成本地出声不必重写
（`apps/atis/src-tauri/src/lib.rs:8-10`）。

- [x] 天气：METAR 解析，每个要素都有 `text` 与 `voice` 两种形态

`metar.rs:157-176`，`Element { text, voice }`，voice 为空时回退到 text；
数字在这里就拼好，不丢给 TTS 去猜。

- [x] 模板渲染两遍（一遍出文字、一遍出朗读），`:VOX` 是"把朗读形态塞进文字版"

`template.rs:24` 的 `\[([A-Z_]+)(:VOX)?\]`，`render` 返回 `(String, String)`
（`template.rs:240`），`:298-301` 有一条专门钉住这个后缀唯一存在理由的测试。

- [x] **中文播报是重新渲染，不是翻译**，而且措辞逐条照 §五.4

八条一条不少，且都有测试：中文语音字母（`chinese.rs:80`）、风向风速两半都点名
（`:178-189`）、温度露点带单位且符号在标签之后（`:290-309`）、世界协调时（`:328`）、
结尾请机组报告识别码（`:410`）、云底高 100 ft = 30 m（`:26`，测试还 `assert_ne!`
了 30.48 算出来的九百一十四米）、计数与逐位的分工（`:307` 对 `:180,315`）、
跑道那一格两种输入都收（`:370-378`）。

- [ ] vATIS profile 导入 —— **没有导入这条路，而且字段形状已经定死在另一边**

没有任何 `import` / `from_vatis` 函数，没有对应的 Tauri 命令，界面上没有按钮。
更要紧的是：**`frequency` 落成了十进制 MHz 的字符串**（`profile.rs:149`，
`"127.850"`），还有一条测试叫 `the_frequency_is_a_number_of_kilohertz_and_nothing_else`
把它钉住。vATIS 那个 `133800000` 拿过来会被当 MHz 解析成废数。

所以这一条不再是"补一个解析器"，是"补一个转换器"——计划原文说 `frequency` 是
最容易读错、后果最重的字段，这句话仍然成立，只是踩点从"读错"变成了"两边根本不是
同一个单位"。

- [x] 信息识别码按 station 的码段轮转，raw METAR 变了才进位

码段 `code_range`、Y→B 回绕、越界字母拒绝，都在 `profile.rs:154-284`；
进位判据是比较原始报文串（`apps/atis/src-tauri/src/lib.rs:496-503`），
四条测试覆盖首播、不变、变了、重连（`:660-692`）；取不到报文时保留旧稿（`:515-520`）。

- [ ] 从 can-api 取网络配置（`/api/v1/atis/config`）—— **全仓库 0 命中**

`atis/config` 和 `api/v1/atis` 在整个 can-voice 里搜不到任何一处。三段合并规则、
"正在播的 station 一律不碰"都无从谈起。can-api 那一侧的契约仍然有效（那是四个桌面
客户端读的四件东西之一，键是 snake_case，改名等于每台机器上的静默数据丢失）。

- [x] 音频经 `push_audio` 注入（TTS 合成），不开麦克风 —— **在机队里，不在这一支里**

`crates/can-voice-atis/src/station.rs:205` 按帧喂。桌面端这一条按上面的分工
**作废而不是未做**。

**这一支还缺的（都在 issue 里）：** #44 的另外三件（vATIS 导入、在线席位、
**METAR 的 HTTP 兜底**）——最后那件影响最大，没有它，没连上 FSD 就完全取不到真实
报文，"先起客户端写稿子"这件事本身就不成立。

**#47 的六条运行期退化已经修掉了**（上线后不能切跑道构型、不能手动推进字母、刷新周期
写死 300 秒、登录 rating 写死观察员、模板拼错不再提示、TTS 每轮重合成没有缓存）——
它是 PR #51 合并前最后落的一个提交，比这份回写的第一版还晚。

---

### Task 6: `xpc-for-can`（X-Plane 飞行员端）—— **已落地**

三条互不影响的链路：X-Plane UDP、FSD、语音。

- [x] `PI_XpcTraffic.py` **原样保留**，连同 `PROTOCOL_VERSION` 一致性检查

**对侧不是 `bridge.py`——那个文件不存在了，那一半是 Rust**
（`crates/can-voice-sim/src/bridge.rs:39`）。两边都是 `PROTOCOL_VERSION = 2`，
两边都丢不匹配的帧（`PI_XpcTraffic.py:80`、`bridge.rs:137`）。插件里那句
"和 bridge.py 保持一致"（`:35`）是陈旧文字，不是陈旧代码。

比计划多做了四层，值得记：插件在 UDP 49901 上回报自己的版本（#41）、安装时解析并
标出不匹配、一条编译期测试把打包进二进制的 Python 和 Rust 常量钉在一起
（`install.rs:278`）、一条跨语言黄金测试进了 CI（`ci.yml:129`）。

- [x] 订阅而不是轮询 dataref

RREF 订阅，5 Hz（`xplane.rs:18,125,137`），413 字节的包长有测试钉住（`:300`）。

- [ ] Windows 上的 `ConnectionResetError` 当成超时 —— **Rust 侧没做；Python 插件侧做了**

`ConnectionReset` / `10054` / `WSAECONNRESET` / `SIO_UDP_CONNRESET` 在 `crates/` 和
`apps/*/src*` 里 0 命中。`xplane.rs:671` 是 `Ok(Err(_)) => return`：**任何** socket
错误都把订阅拆掉（不永久致命，`run()` 会重新发现，但要多走一轮）；
`apps/xpc/src-tauri/src/lib.rs:575` 的插件状态监听器更硬，任何 recv 错误直接永久终止。
插件那一侧是对的：`PI_XpcTraffic.py:334` 的 `except (BlockingIOError, OSError): break`。

- [x] PBH 打包沿用 `pack_pbh` **原样**

`crates/can-voice-fsd/src/pilot.rs:77-86`，不取负；R3 定下来的结论写进了它的文档注释，
另有一份独立实现的交叉校验（`:405-446`）。xpc 把俯仰坡度原值送进去，一处符号都没动。

- [x] 机型匹配的分级顺序（§五.3）

类别层在前（`csl.rs:287-312`），按前缀猜在后（`:314-327`），失效模式写在注释里。
**回归测试真的装了一架 `B738`**（`csl.rs:514-526`：`B77W` 在 `{B738, A333}` 里
匹配到 `A333` 的宽体类别），另有一条装 `A320` 的姊妹测试。这个 bug 不会再静默回来。

- [ ] 观察员模式：**不开 FSD 连接**，频率可手输 —— **没做，而且看起来是故意放弃的**

`follow` 在两支飞行员端都恒为空串（`apps/xpc/src-tauri/src/lib.rs:341`），没有开关，
没有手输频率框。库那一侧是齐的（`conn.rs:345`、`control.rs:78`），服务端也是齐的，
**只有客户端这一截没接**。

**这里有一处需要拍板的矛盾**：界面和模块文档都在主动反驳它——
`apps/xpc/src/App.vue:179` 写着"频率跟着 COM1 走，界面上没有第二个频率框"，
`lib.rs:13-17` 同样的说法；而 issue #36 把它记成**回归**，理由是
`can-audio/xpc/observer.py` 那一整块能力没了。两边不能都对：要么把 #36 关成
"不做"并把理由写进设计文档，要么承认 App.vue 那句注释是在给一个缺口找说法。

---

### Task 7: `msfs-for-can`（MSFS 飞行员端）—— **已落地，带一条新发现的缺陷**

和 xpc 同一套，换 SimConnect。

- [x] `snapshot()` 的字段要和 xpc 那边**逐字段一致** —— 做法比计划更强

**不再是两个结构体要对齐，而是同一个类型**：`crates/can-voice-sim/src/lib.rs:47`
的 `Snapshot`，`xplane::snapshot` 和 `msfs::snapshot` 各自填。一致性成了类型而不是
纪律。两边的 TS 镜像也逐字段相同。

- [x] SimConnect 的三个陷阱（§五.5）—— **两个仍适用并已落地，一个不适用了**

**(a) 异步对象 ID 仍适用，做了，而且多做了一层**：`by_request` 映射
（`msfs.rs:834`）之外还有 `by_send`（`:836`），因为创建**失败**是以
`RECV_ID_EXCEPTION` 带着 `send_id` 回来的，和请求号对不上；另有 10 秒超时清扫，
管"什么都不回来"的那种情况。

**(b) ctypes trampoline 那一条不适用了**：换成 Rust 之后没有 Python-SimConnect、
没有派发回调、没有钩子，是 `SimConnect_GetNextDispatch` 轮询。没有属性可换，
这个失效模式从结构上就不存在了——**这是"这条坑不适用"的第二例，和 Task 2 Step 5
的 SDL 是同一类判断，要写明理由，否则下一个人会照抄一条不存在的约束。**

**(c) BCD 与"名字写着度实际是弧度"仍适用，读路径做了**：squawk 走 BCD 转换并加了
八进制合法性检查（任何一位 >7 就作废成 2000）；俯仰坡度声明成 `"Radians"`
（`msfs.rs:48-50`），读的时候转换**并取负**（`:101-102`），都有测试。

- [ ] **本次审计新发现，已开 #52：注入他机的姿态少取了一次负号**

取负只在**读**路径上。注入的**写**路径把 FSD 约定的俯仰坡度直接交给 SimConnect，
单位写的是 `"degrees"`，没有再取一次负——`msfs.rs:478-479`（`TRAFFIC_VARS`）、
`:867-868`（创建）、`:906-907`（更新）。那些值来自 `unpack_pbh`
（`pilot_client.rs:379` → `feed.rs:30-31`），就是 FSD 约定。

**预期症状：注入进 MSFS 的他机俯仰和坡度是反的**，而自机上网的姿态是对的——
所以它在单机自测里看不出来，要两个人对飞才看得见。没有任何测试覆盖注入侧的符号。
这一条和 §五.5 的 (c) 是同一个陷阱的另一半，计划当时只想到了读。

**判定它不需要先知道哪一边的绝对约定为真**：同一个仓库对同一个接口做了相反的假设，
必有一条错，而读那一侧另有三份独立实现背书（见 `pack_pbh` 的文档注释）。

**xpc 不受影响，而且原因值得记**：X-Plane 那一侧读 `theta`/`phi` 不取负
（`xplane.rs:39-40` → `:209-210`），插件注入时也原样送（`PI_XpcTraffic.py:383-384`）
——两头都是恒等映射，所以自洽。**出错的不是"取负"这件事本身，是一个接口的两端
各自做了不同的假设。**

- [x] AI 飞机注入用 `AICreateNonATCAircraft`，TCAS 因此是免费的

`msfs.rs:554,645,878`，更新走 `SetDataOnSimObject`，移除走 `AIRemoveObject`；
MSFS 这一侧一处手填 TCAS 数组的代码都没有。

**这一支还缺的：** #42 本机机库扫描没有——旧版扫 `UserCfg.opt` → `aircraft.cfg`
实测认出 375 个涂装，新版只剩 18 条内置 Asobo 标题加手写 `titles.json`，
装了 FSLTL / AIG 的人一个机模都不会被发现。

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

### Task 9: 三平台打包 —— **部分。说好的三平台里没有 macOS**

发出去的是 v27.0.2 与 v27.0.3，都是预发布（v27.0.1 只有 tag，没有发布物）。
v27.0.2 是 8 个 Windows 文件，
v27.0.3 是 20 个（Windows 加 deb / rpm / AppImage）。

- [x] 四个产品名固定（§0.4）

`productName` 就是那四个名字（四份 `tauri.conf.json:3`），Tauri 按它派生文件名，
真实资产名验过：`audio-for-can_27.0.3_x64-setup.exe`、`atis-for-can_27.0.3_amd64.deb`、
`xpc-for-can-27.0.3-1.x86_64.rpm`、`msfs-for-can_27.0.3_amd64.AppImage`。
产品名是每个资产名的首段，can-api 的白名单对得上。

- [ ] **"三平台"的第三个平台是 Linux，不是 macOS**

`release.yml:54` 是 `os: [windows-latest, ubuntu-latest]` × 四个应用 = 8 个构建。
整个 `.github/workflows/` 里 `macos|darwin|apple` **0 命中**。理由写在 `README.md:39-42`
和 `release.yml:7-13`：没有 Developer ID 证书，公证做不了；`ci.yml:246-249` 还特意说
macOS 的 Rust 作业推迟到"和签名一起做才有意义"。

**这一条要改计划的措辞而不是只勾一半**：macOS 上装不上任何一个客户端，而 macOS 是
这个项目自己的开发机平台，也是 Task 2 专门为它保留了"鼠标侧键不可用"提示的那个平台。

- [x] **libopus 静态链接**已经由核心库保证

`crates/can-voice-client/Cargo.toml:9` 的 `audiopus_sys` 带 `features = ["static"]`，
`README.md:111-118` 给了 `otool -L` / `ldd` 的验证方法，并写明它消灭的是 Python 版
"`opus.dll` 没跟着打包，程序照常启动，语音静默失效"那一整类故障。

- [ ] macOS 要签名与公证；Windows 要签名 —— **两边都没有，而 Windows 已经在发了**

**2026-09-17 定下：暂不签名。** 这不是遗漏，而是一个决定，写进了 can-voice 的 `README.md`（"平台"一段）
和 `release.yml` 的文件头。SmartScreen 的提示从 release 正文里挪到了
`.github/release-notes/common.md`，正式版和测试版都会带上。以后接上签名时，那一段要跟着删掉。

任何工作流和任何 `tauri.conf.json` 里都没有 `signCommand` /
`certificateThumbprint` / `signingIdentity` / 公证密钥。`release.yml:15-20` 直说
"没有证书"，`:152-155` 干脆把 SmartScreen 的蓝色警告写进了 release 正文交给用户。
这是 #50 的后一半，也是 R4。

- [x] **版本号注入** —— #50 的前一半，can-voice PR #67

**2026-09-17 修复**：新增一个 `version` job，在八个构建开始之前校验一次 tag；每个构建再用
`scripts/release-version.ts` 把 tag 写进四份 `Cargo.toml` / `tauri.conf.json` /
`package.json`。同一个 tag 也决定预发布标记：`YY = 0` 发 prerelease，否则发正式版。
因为 can-api 取的是 `/releases/latest`，会跳过预发布版，所以这一条是切换的前提。
在分支上 dry run 过 `v27.0.4`，Windows 和 Linux 打出来的包都是 `27.0.4`。
`can-voice-log` 日志头里报的是它自己的 0.1.0，这一处也一并改了。

下面是修复前的情况：

版本硬写在仓库里（四份 `tauri.conf.json:4` 与 `Cargo.toml:3` 都是 `27.0.3`），
工作流的 `version` 输入**只用来打 git tag**（`release.yml:143`），`bun run tauri build`
之前没有任何一步去改那两个文件。tag 打成 v27.0.4 而配置还停在 27.0.3 时，
release 页面和客户端自报版本静默不一致——**而更新检查比的正是自报版本**。

- [ ] `PI_XpcTraffic.py` 在包里要有**两份** —— 第二份不在 exe 旁边

第一份在二进制里（`install.rs:37` 的 `include_str!`），给应用内安装器用。
第二份**不是**放在可执行文件旁边——四份 `tauri.conf.json` 都没有
`bundle.resources`——而是做成了单独的 release 资产
`xpc-for-can-xplane-plugin.zip`（`release.yml:135-140`）。

后果两条：**"X-Plane 装在要管理员权限的位置、文件本来就该在手边"那种情况没被覆盖**，
那个人得回下载页去找；而且**那一步比 v27.0.3 还新**（`79bd592` 不是 `v27.0.3` 的祖先），
所以**已经发出去的 v27.0.3 里根本没有这个 zip**，它的说明还指向私有仓库的路径。

- [x] release 资产的地址是 can-api 的下载中转指向的地方

---

## 五、必须带过来的坑

设计文档 §11.4 明写：`can-audio` 归档之后，"音频设备回退、PTT 的 SDL 线程陷阱、
机型匹配的分级顺序、ATIS 中文播报的措辞"在新仓库里**仍然是有效知识，要在对应模块
落地时逐条带过去**。这一节是那份清单。**每一条都对应一个已经发生过的故障。**

**9-16 的审计是这一节的验收，结果是七条里三条出了问题**：5.5 的 (c) 只做了读的一半
（见 Task 7），5.6 的结论已经过时（下面改了），5.7 整块是回归（#29）。
另外两条被证伪——5.2 的 SDL 陷阱在 gilrs 上不成立，5.5 的 trampoline 在 Rust 上
不存在——**证伪也是结论，照样要写明理由**，否则下一个人会为了满足一条不存在的
约束去加耦合。

### 5.1 音频设备

已由核心库处理，但要知道原因：Python 版**没有重采样**，注释写着"48 kHz 是理想路径，
回退采样率会产生变调音频"——也就是说设备不支持 48 kHz 时用户听到的是变调的声音，
而那看起来像"语音系统坏了"。核心库现在优先向设备要 48 kHz，拿不到才两端重采样。

**带过来了。** 另外 #24 补上了旧版也没有的一半：声卡拔了会自己重开，而且设备可选、
这件事在界面上说得出来。

### 5.2 PTT 的线程陷阱

见 Task 2 Step 5。**要先核实它在 `gilrs` 上成不成立。**

**核过了，不成立**：`gilrs 0.11.2` 不依赖 SDL，那个隐藏窗口的机制根本不存在，
结论写进了代码注释。仍未核实的是 gilrs 在 Windows 上走 XInput/DirectInput 时
自己怎么初始化——真机上第一次跑手柄 PTT 时要专门看"插拔之后还能不能打开"。

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

**带过来了，而且那个测试缺陷也补上了。** 类别层在前（`csl.rs:287-312`），
回归测试真的装了一架 `B738`（`csl.rs:514-526`）。

**但 MSFS 那一侧塌了另一半**：本机机库扫描没有跟过来（#42），旧版实测认出的 375 个
涂装现在只剩 18 条内置 Asobo 标题。分级顺序对了，可分级表本身是空的。

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

**八条一条不少地带过来了，都有测试**（`chinese.rs`，逐条位置见 Task 5）。

**然而它至今没有被任何人听到过。** 出声的是服务端机队，机队只有 `text_atis`，
念的是原始码组——中文稿算了、存了、在界面上给操作员看，**就是不播**。
这一节保住的是知识，不是产品行为；要变成产品行为，得先在 can-fsd 那边加一个字段。

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

**(a) 做了，还多做了一层**（创建失败是以 `RECV_ID_EXCEPTION` 带 `send_id` 回来的，
和请求号对不上，要第二张映射表）。**(b) 不适用了**——Rust 侧是 `GetNextDispatch`
轮询，没有钩子可换。**(c) 只做了一半**：读路径取负了，注入他机的写路径没有，
见 Task 7 那条新发现。

### 5.6 高度是真高不是指示高

`PLANE_ALTITUDE` 与 X-Plane 的 `elevation` 都是几何真高，而座舱高度表是按
Kollsman 窗修正过的指示高。巡航在标准气压下，两者每英寸汞柱差约 1000 ft。
**这不是单位 bug，没有东西可"修"**——FSD 位置包的第十个字段就是那个气压修正量，
Python 版两边都硬编码成 0，所以网络看到的是未修正的真高。

**这一段的前提仍然对，结论已经过时：第十个字段不再是 0 了。**
`crates/can-voice-sim/src/lib.rs:118-131` 真的算了：
`indicated_ft + (29.92 − baro_inHg)·1000 − true_altitude_ft`，X-Plane 从
`altitude_ft_pilot` / `barometer_setting_in_hg_pilot` 取，MSFS 从
`INDICATED ALTITUDE` / `KOHLSMAN SETTING HG` 取。

还加了一条计划里没有的护栏：**气压落在 25.0–32.0 inHg 之外就退回 0**——模拟器
初始化期间会瞬时读出 0 或 1013，照单全收会把飞机在雷达上挪走约三万英尺。

高度字段本身仍然是几何真高，所以"这不是单位 bug"那句话照旧。有一处不对称值得知道：
**接收侧把这个字段丢掉了**（`pilot_client.rs:372-380` 只解 1–8 号字段），
画图不受影响，但客户端无法还原他机的指示高度。

### 5.7 i18n

- UI 文字走查表，**源码里不得有界面字符串**，用一条扫描测试钉住
- **`kind → 文案` 的字典在导入时求值**，会冻住当时的语言、之后再也不跟着切换；
  默认参数同理
- **日志文本英文，界面文字中文**，分界线是**这个字符串最终去哪**，不是它在哪个模块里

**这一整条是回归（#29），也是 §五 里唯一整块没带过来的。**

四个前端**没有任何 i18n 机制**：没有 i18n 依赖，没有任何语言文件，界面文字全是
写死在 SFC 里的中文（`apps/controller/src/App.vue` 一个文件 856 个汉字）。
`crates/can-voice-ptt/src/binding.rs:3-7` 还专门写着"措辞由上层 i18n 决定"——
而上层没有这一层。

三条具体的账：

1. **扫描测试只护住了一个 crate。** `no_ui_strings`（`can-voice-ptt/src/state.rs:421`）
   只扫 ptt 自己的 `src`，而且是平铺的 `read_dir`，加一层子目录就看不见了。
   其余十一个 crate、四个 `src-tauri` 后端、四个前端都没有对应物。
2. **"日志英文、界面中文"在库 crate 里已经破了。** 按"字符串最终去哪"这条分界线，
   下面这些都该在上层：`can-voice-update/src/lib.rs:146,148,154`、
   `can-voice-log/src/lib.rs:297,298,359`、`can-voice-atis/src/profile.rs:289`
   的 `"默认"`、以及 `apps/msfs/src-tauri/src/lib.rs:500,504`。
   没有东西拦得住，因为扫描测试没覆盖到它们。
3. **"导入时求值的 `kind → 文案` 字典"这个陷阱目前不存在，但不是因为防住了**——
   是因为根本没有语言可切。现有文案都在 `computed()` 和普通函数里逐次求值，
   所以将来接 i18n 时这些调用点是安全的，可以直接改。

---

## 六、风险与未决

| # | 事 | 影响 |
|---|---|---|
| R1 | ~~can-api 的 `/api/v1/voice/token` 不存在~~ **全部做完** | 客户端那一半也做了：`crates/can-voice-token/`，只认凭据、只换一次票。见 Task 3 末尾 |
| R2 | ~~`@jianyuelab-org/can-ui` 的访问未验证~~ **已验证，能装** | 0.3.5，203 个包；35 个组件里 34 个是 `.vue`，Tauri 里不必引 Astro。见 §0.3 |
| R3 | ~~PBH 的符号仍未定~~ **已定：can-audio 是对的，can-fsd 在取负** | 修在 can-fsd 的 `fix/pbh-sign`。xpc/msfs 的编码没改，落地时验过 |
| R4 | **签名。2026-09-17 定下：暂不签名** | 定下的是"先不去弄"，不是"不需要"。Windows 照常发无签名的包，SmartScreen 的提示写在 release 正文里；macOS 没有证书就没有构建，那个平台上仍然装不上任何一个客户端。见 Task 9 |
| R5 | 44,800 行 Python 的领域知识 | **两半都审完了。** 9-16 审四个客户端，开出 #20–#50，现在只剩 #50 的签名一半。9-17 审服务端那一半（`server/`、compose、镜像 CI），开出 **#68–#80**。**#68 会挡住切换**：TLS 证书只在启动时读一次，Let's Encrypt 续期后，旧证书一到期全网就连不上，而那天大约在切换后两个月，两周退路早就没了。其余是机队的行为回退（#69–#71、#74–#77、#79）、运维（#72、#78）、限流（#73）和一处潜在的密码进日志（#80）。有两处去掉的东西没有写下来：`whereami.py` 的诊断职能没有替代品；容器日志从 `TZ=Asia/Shanghai` 改成了 UTC 的 slog JSON |
| R6 | 四个客户端的封闭测试 | **还没有做。** 已经发了两个预发布版，但没有组织过覆盖不同网络环境和三个平台的封闭测试；而且现在只有两个平台可测 |
| R7 | ~~macOS 上没有鼠标侧键 PTT~~ **已定：接受这个缺口，而且界面真的说了** | `mouse_supported()` 在 macOS 返回 false，三个带 PTT 的应用都在设置里明说不可用；Wayland 那条同样处理 |
| R8 | **注入进 MSFS 的他机姿态少取一次负号**（本次新发现，已开 #52） | 读路径取负、写路径没取（`msfs.rs:478-479,867-868,906-907`），注入的他机俯仰坡度应当是反的。**单机自测看不出来，要两个人对飞才看得见**，而且没有任何测试覆盖注入侧的符号。xpc 两头都是恒等映射所以自洽，不受影响。见 Task 7 |
| R9 | **观察员（follow）模式的去留没有拍板** | 库和服务端都是齐的，客户端一截没接。界面注释在主动反驳它（"没有第二个频率框"），issue #36 把它记成回归。两边不能都对——要么关成"不做"并把理由写进设计文档，要么承认那句注释是在给缺口找说法 |
| R10 | **事件桥换成了轮询快照** | 不是缺陷，是换了结构（见 Task 3 Step 1），但两条代价要记住：延迟下限是一个轮询周期（200 ms），以及窗口不可见时仍在轮询。将来若要加"按键即亮"这类要求更紧的指示，得先把这条路径改回事件 |
