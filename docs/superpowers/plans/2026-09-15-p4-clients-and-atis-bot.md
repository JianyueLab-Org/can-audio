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

### 0.2 硬前置一：can-api 还没有 `POST /api/v1/voice/token`

设计文档 §11.3 把它列为"**新版能否登录的前提，不是可选项**"。核过了：

```
can-api 的 /api/v1 路由：110 条
其中与语音 token 有关的：0 条
can-api 里的 ed25519：0 处
```

现在能连上 can-voice 的**只有端到端测试**，因为夹具用自己的密钥签票。
**在这个端点存在之前，四个客户端一个都登录不了**，P4 做到最后也是不能用的。

它要做的事很小，但有一个必须由人来定的决定：**Ed25519 私钥放在哪**。
can-voice 服务端只认 `CAN_VOICE_API_PUBKEY`（裸 32 字节的 base64），
而签名那一半在 can-api。相关约束：

- token 的有效期上限是 **10 分钟**（`auth.maxTokenLifetime`）。设计文档说签 60 秒。
  **短有效期是这套设计里唯一的吊销机制**——`auth` 包明令禁止任何网络调用，
  所以一张签出去的票在过期之前没有任何办法作废。
- claims 是 `{cid, rating, max_tx, exp}`。`rating < 1` 的成员不能用语音，
  和 `/api/v1/public/auth` 同一条规则——但**那条规则要在 can-api 这一侧判**，
  can-voice 只按 claims 里的 rating 拒。
- 鉴权复用现有的 `/api/v1/public/auth` 逻辑（CAN 号 + 网站密码）。

**这一条不在本计划的任务里**：它是 can-api 的改动，有自己的 CI 和部署。
列在这里是因为不先谈好，P4 做完那天没有人能登录。

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

### Task 2: PTT

**Files:**
- Create: `crates/can-voice-ptt/`（新 crate，四个桌面端共用）

**Interfaces:**
- Produces: `Binding`（键盘 / 鼠标侧键 / 手柄按钮）、`PttWatcher`、`PttCapture`

按下任意一个绑定即发话。**这一整块的规则是从 `can-audio/controller/ptt.py` 搬过来的，
逐条都是踩出来的，不要重新发明。**

- [ ] **Step 1: 建 crate 与 `Binding` 模型**

三种来源：键盘、鼠标侧键、手柄按钮。设置里存的是一个**列表**（`ptt_bindings`），
不是单个键——`ptt.py` 当年从 `ptt_key` + `joystick_ptt` 升级过来时专门做了迁移，
因为"升级时悄悄丢掉某人的 PTT 键"看起来和麦克风坏了一模一样。

- [ ] **Step 2: 鼠标只认侧键 X1/X2**

**绑左键意味着在任何窗口里点任何东西都会发话**，而 TX 指示灯还被挡在他点的东西后面。
`mouse_name()` 对左/右/中键返回空。按**按钮名字**匹配而不是编号：X11 把同一组物理键叫
`button8`/`button9`，而 Windows 和 macOS 叫 `x1`/`x2`。

- [ ] **Step 3: 绝不吞事件**

Python 版的规矩是"never `suppress=True`"。吞掉事件意味着那个键在**其它所有程序里**
都失灵，而它在模拟器里通常还有别的用途。Rust 侧用 `rdev` 的监听模式，不要用会拦截的接口。

- [ ] **Step 4: 只启动被绑定的来源**

macOS 上创建全局键盘监听会触发辅助功能授权弹窗。**用户只绑了手柄却被要求授权键盘监控，
读起来像恶意软件。**

- [ ] **Step 5: 手柄要轮询，而且要先核那条 SDL 线程陷阱还在不在**

Python 版踩的是这个：`SDL_Init(SDL_INIT_JOYSTICK)` 会为 DirectInput 建一个隐藏窗口，
而 **Win32 在线程退出时销毁该线程的窗口**；SDL 只建一次，于是第一个初始化 SDL 的线程
一旦退出，之后每次打开手柄都以 `E_HANDLE` 失败，直到进程重启。一条真实日志里是
01:28:11 成功捕获一次、01:28:13 之后每 3 秒失败一次、持续到结束。

**`gilrs` 不是 SDL，所以这条陷阱不一定成立——先核实，不要假设。**
无论结论如何都要写下来：成立的话照 Python 版的做法把初始化钉在 UI 线程上；
不成立的话写明为什么，省得下一个人照抄一条不存在的约束。

- [ ] **Step 6: 打不开的设备只报一次**

打不开的手柄会一直打不开。Python 版那条日志填满了整整一轮轮转。
每个设备首次失败记一条 WARN，之后降到 DEBUG，成功打开后清零。

- [ ] **Step 7: 捕获（"按一下你要的键"）必须先停掉监听**

两个线程同时泵同一个事件队列不是线程安全的，而且**正在录的那一下会被播出去**。

- [ ] **Step 8: 这个 crate 不产生任何界面文字**

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
- [ ] PBH 打包要是 can-fsd 解包的**精确逆运算** —— 并见 §六 关于符号的未决问题
- [ ] 机型匹配的分级顺序见 §五.3
- [ ] 观察员模式：**不开 FSD 连接**，频率可手输（空 = 跟随 COM1）

### Task 7: `msfs-for-can`（MSFS 飞行员端）

和 xpc 同一套，换 SimConnect。

- [ ] `snapshot()` 的字段要和 xpc 那边**逐字段一致**
- [ ] SimConnect 的三个陷阱见 §五.5
- [ ] AI 飞机注入用 `AICreateNonATCAircraft`，TCAS 因此是免费的（不必手填数组）

---

## 三、服务端 ATIS 机器人

### Task 8: 机器人

跑在服务器上，没有界面、没有声卡。

- [ ] 从 can-fsd 的 datafeed 取 `atis[]`，为每个 `_ATIS` 呼号起一路
- [ ] 音频经 `push_audio` 注入，`Config.audio_devices = false`
- [ ] **没有任何绕过账号的捷径。** Python 版为此专门写了一条测试
      （`test_there_is_no_shortcut_for_any_account`）防止它被加回来
- [ ] `frequency` 为 `199.998` 表示"没设频率"，**不要拿它去建频率**
- [ ] 一路给不起来不能拖垮其它路：机器人给三次机会就放弃会让全网 ATIS 悄悄下线，
      所以它这一侧**不设有界重连**（和四个桌面端相反，理由写进代码注释）

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
| R1 | **can-api 的 `/api/v1/voice/token` 不存在** | P4 做完也没人能登录。见 §0.2 |
| R2 | `@jianyuelab-org/can-ui` 的访问未验证 | 到第一个 `bun install` 才发现，那时骨架已搭一半 |
| R3 | **PBH 的符号仍未定** | `can-audio/CLAUDE.md` 明写这条没有结论：can-fsd 的 `normaliseSigned` 开头就是 `v = -v`，而 `test_xpc.py` 里那份"仲裁用"的参考实现恰好漏了这一行，于是两边在自己的约定里自洽而可能都是错的。**改错一侧会让所有人的飞机姿态倒过来。** 要对着 openfsd 或一次真实的 EuroScope 抓包定，不要对着这两份任何一份 |
| R4 | Tauri 2 的三平台打包与签名 | macOS 公证、Windows 签名都要证书；不签的话用户看到的是"这个程序不安全" |
| R5 | 44,800 行 Python 的领域知识 | §五 列的是**已知**的那些。`can-audio` 归档前应当再过一遍 `CLAUDE.md`，那是唯一的记录 |
| R6 | 四个客户端的封闭测试 | 设计文档 §11.1：大爆炸切换唯一能做的验证就是把它提前。要覆盖不同网络环境和三个平台 |
