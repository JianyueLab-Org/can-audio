# P3(Rust 客户端核心库)计划修订件

> **给执行者:** 这份文件与 `2026-09-12-p3-rust-client-core.md` **一并生效,且在冲突时以本文件为准**。
> 它裁定了执行前冲突扫描的全部 37 条发现。每条裁定给出:改什么、为什么、判错的代价。
> 原计划里凡与本文件抵触的文字,以本文件为准而不是照抄原文。

**裁定依据的事实基准:** `can-voice` @ `main` / `5b2c7ed`,P2 Task 1–11 已全部落地并通过评审。
P2 Task 12(流式回退)**未做**——P1 探针未部署,`docs/p1-connectivity-findings.md` 不存在。

---

## 零、先读这一段

原计划有六处会让执行**当场停住**:编译不过、测试与实现自相矛盾、或 clippy 门禁挡住计划自己的代码。
它们是 C3、C4、H5、M7、M15,加上 H1(端到端测试不可能通过)。
**Task 4 和 Task 6 在按本文件改写之前不要开工。**

---

## 一、Critical

### C1 — `SubAck` 缺 `rejected_xc`,交叉耦合被拒会静默丢失

**裁定:必改。** Task 1 的 `SubAck` 加:

```rust
#[serde(default)]
pub rejected_xc: Vec<[u32; 2]>,
```

Task 7 的 `on_ack` 必须存下它;Task 11 的 `pump` 必须为每一对发
`Event::XcDenied { a_khz, b_khz, reason }`。

**为什么:** 服务端这个字段是**专门**为了不静默丢弃而加的,P2 的裁定原文是
"一个设好了交叉耦合却不生效、又不知道为什么的管制员,正是整个重写要逃离的那类故障"。
serde 默认忽略未知键,所以不加这个字段的客户端会把服务端专门发来的拒绝理由丢掉——
**把这条裁定要防的故障原样复现一遍**。

**判错的代价:** 管制员把 DEL 和 GND 耦合起来,服务端拒了,界面显示已耦合,
一个频率上的通话在另一个频率上永远听不到,而且没有任何线索。

### C2 — 驱逐会变成无限登录循环

**裁定:必改,这是 P3 最重要的一条。** `LinkState` 加 `Evicted`,并且
**客户端必须读 QUIC 的应用层关闭码**。quinn 里这条路径大致是
`ConnectionError::ApplicationClosed(ApplicationClose { error_code, reason })`——
**这个类型名和字段名是凭记忆写的,动手前先对着实际依赖的 quinn 版本核一遍。**
要紧的是"必须读到应用层关闭码、并据此决定要不要重连",不是这一行的拼写。

映射表(与 `server/internal/transport/codes.go` 一一对应):

| 码 | 含义 | 客户端行为 |
|---|---|---|
| 0 `CloseNormal` | 服务端正常收工(进程退出/重启部署),或客户端自己断的 | **可以重连**,走 `ReconnectPolicy` |
| 1 `CloseHandshakeRefused` | 握手被拒 | **不要原样重连**;先看 reason(见 H3):`token_expired` → 去换新 token 再连,`token_invalid` / `refused` → 停 |
| 2 `CloseEvicted` | 同账号在别处登录,你被顶掉了 | **终态,绝不重连**,并告诉用户"账号在别处登录了" |
| 3 `CloseProtocolViolation` | 客户端把协议用坏了(今天只有一种:不读控制流,reason `control_write_stalled`) | **终态,去修客户端**,不要重连 |
| 其他 / 传输层错误 | 网络断了 | 走 `ReconnectPolicy`,三次 |

> **码 0 是"可以重连",不是终态——我第一版这张表把它写成了终态,那是错的。**
> 服务端重启部署走的正是这个码,把它当终态意味着**每次部署之后所有客户端永不回来**。
> 权威定义在 `server/internal/transport/codes.go` 的注释里,它明写了这四条的客户端契约。
>
> 注意码 3 和码 2 是同一类形状:`ReconnectPolicy` 的三次上限**两个都挡不住**,
> 因为重连本身是**成功**的,计数器一成功就清零,真正的失败发生在几秒之后。
> codes.go 自己把这一点写下来了——这也正是它不复用码 0 的理由。

**为什么:** `ReconnectPolicy` 只数**连续失败**。驱逐后的重连是**成功的**,
于是 `on_session_established()` 把 attempts 清零。两个客户端用同一个 CID 会
**永远互相驱逐**——三次上限根本拦不住,因为没有任何一次是失败。
`can-audio/CLAUDE.md` 已经记过这个故障的 Mumble 版本(僵尸重连循环把账号锁死),
这里更糟:循环发生在两个活着的客户端之间,而且每一轮都"成功"。

**判错的代价:** 两台机器登同一个账号,互相踢到天荒地老,两边都听不到任何声音,
两边的界面都显示"已连接"。服务端日志被登录风暴淹没。

### C3 — `read_msg` 在 `select!` 里不是取消安全的

**裁定:必改,改结构而不是打补丁。** 控制流的读取交给**一个独立的 task**,
它独占 `RecvStream`,把解析完整的 `Message` 通过 `tokio::sync::mpsc` 送出来;
`pump` 的 `select!` 只从 channel 上 `recv()`——**`mpsc::Receiver::recv()` 本身是取消安全的**。

**为什么:** `read_msg` 连着做两次 `read_exact`(4 字节长度前缀,然后包体)。
`select!` 里别的分支赢了的时候,这个 future 会在两次读之间被丢掉,**已经消费掉的字节回不来了**,
控制流从此错位。之后每一帧都解析失败,最可能的表现是被读成"控制流结束"→ 断开 → 重连,
而链路其实一直是好的。

**不要**试图用"加个缓冲区记住读了多少"来救——那是在手写一个状态机去模拟取消安全,
而独立 task 这个写法把整类问题消掉了。

**判错的代价:** 健康的链路上出现无法解释的周期性重连,而且只在控制面有并发活动时出现,
本地必现不了。

### C4 — Task 4 的实现通不过它自己的测试

**裁定:测试是对的,实现是错的。按测试重写实现,并且把 Task 4 改成真正的 TDD 循环。**

原计划写着"Expected: PASS(十七个测试)",而实测三个 FAIL:
`frames_come_out_in_sequence_order` 得到 `[0,1,2,3,4]` 而期望 `[0,1,2]`;
`out_of_order_frames_are_reordered` 得到 `[0,1,2,3]` 而期望 `[0,1,2]`;
`the_buffer_does_not_grow_without_bound` 得到 `depth()==24` 而断言 `<= 6`。
根因一致:**测试假定 `pop()` 会一直保持 `START_DEPTH` 帧的水位,实现却把缓冲区抽干**。

测试是对的,因为**抽干到空的抖动缓冲区不是抖动缓冲区,是个重排队列**——
它没有任何吸收抖动的能力,网络一抖就断音,而吸收抖动正是这个模块存在的唯一理由。

同时并入两条原本无人认领的规格要求:

- **M9** — 规格 §9.2 要求"连续丢超过 3 帧则结束这次发言"。原计划把它推给 Task 5,
  而 Task 5 是 `mix.rs`,不碰这件事;Task 6 也不碰。**归 Task 4**,连带测试。
- **M10** — 规格 §9.2 要求深度在 40–120 ms 之间自适应。原实现把 `self.depth` 赋成
  `START_DEPTH` 之后再没动过,`MIN_DEPTH` 全篇没人引用。**归 Task 4**,连带测试。

另修 **L2**(`depth()` 返回占用量而字段 `depth` 是起播水位,八行里一个词两个意思——
把字段改名 `target_depth`)和 **L3**(`seq` 用非回绕的 `<`/`>` 比较却用 `wrapping_add` 推进;
且封顶循环在起播之前就设了 `next`,绕过了 `START_DEPTH` 等待)。

**判错的代价:** 语音在任何抖动下都断续,而且因为"测试全绿"没人会去看这个模块。

---

## 二、High

### H1 — 端到端测试不可能通过(自签证书 vs 系统信任库)

**裁定:加一条"调用方提供根证书"的路径,而不是加一个跳过校验的开关。**

`conn::connect` 增加一个可选参数(或 builder 上的一个方法),接受一组额外的根证书 DER。
生产路径仍然是 `rustls_platform_verifier::tls_config()`;端到端测试把自签证书的 DER 传进去。

**为什么不用 `InsecureSkipVerify` 的等价物:** 那个开关一旦存在,就会有人在生产里打开它,
而这条链路上跑的是成员的网络密码。"多传一个根证书"和"不校验"在测试里一样方便,在生产里天差地别。

**判错的代价:** 要么端到端测试根本跑不起来(计划原样),要么发布版里带着一个能关掉 TLS 校验的开关。

### H2 — `rejected` 把 TX 限额拒和 RX 限额拒混成一件事

**裁定(经 P2 终审 B 修正——用差集,不要用 `rejected` 推断):**

```
被拒的 TX = 我声明的 tx − ack.tx
被拒的 RX = 我声明的 rx − ack.rx
```

**不要**去读 `rejected` 来判断方向。`ack.TX` 和 `ack.RX` 是**完整且权威**的:
服务端把授权后的全集放进去,长度受 `MaxTX`/`MaxRX` 约束(默认 32),不存在截断。
差集因此是精确的、完备的,而且**天然覆盖了 `maxRejected = 256` 截断的那部分**——
超过 256 个被拒频率时第 257 个以后根本不进 `rejected`,任何基于 `rejected` 的推断在那里都会失效。

`rejected` 只当作一个补充信号用(它带着"服务端确实看见并拒了这一条"的含义),
**绝不作为方向判断的依据**。

> 我最初的裁定是按 `f ∈ rejected ∧ f ∈ ack.rx` 这个交集规则分辨的。那条规则在没有截断时成立,
> 服务端 `router.go` 的注释也确实这么写——**但它在截断之后是错的**,而差集在任何情况下都对。
> 终审 B 同时要求服务端 README 补一节 SUBACK,把这个公式写下来,因为今天没有任何东西告诉客户端。

Task 11 的 `pump` 按差集分派 `Event::TxDenied` 和 `Event::RxDenied`,
**不要**把 `rejected` 里的每一项都当成 TxDenied。

**判错的代价(原计划):** 声明 40 个 RX 频率,客户端弹出 8 条"不能发射"的提示,
而那 8 个频率其实发射得好好的。

### H3 — BYE 的原因死在一行日志里

**裁定:必改。** 加 `Event::Refused { reason: RefusedReason }`,`RefusedReason` 是个枚举:

```rust
pub enum RefusedReason {
    TokenExpired,   // "token_expired" —— 去换一个新 token 再来,这一条是可恢复的
    TokenInvalid,   // "token_invalid" —— 停,别重试
    Refused,        // "refused"      —— 停,别重试
    Other(String),
}
```

**为什么:** 服务端这三个字符串是**专门为客户端造的**,而且有两个测试钉住它们稳定。
原计划把它们送进 `tracing::warn!` 就完事,Tauri 层永远拿不到,用户只看到一个没有理由的 Offline——
**协议里唯一一处专门为客户端设计的东西,客户端用不上。**

`TokenExpired` 是唯一可恢复的:上层应当去换 token 然后重连一次,而不是走 `ReconnectPolicy`。

### H4 — `audiopus = "0.3"` 不保证静态链接 libopus

**裁定:必改。** Task 6 Step 1 明确写出 vendored/static 的 feature,不要裸依赖。

**为什么:** `audiopus_sys` 先探 `pkg-config`,系统上装了 libopus 就链系统的,从源码编译只是**回退**。
Task 6 Step 6 却把"不依赖动态 libopus"列成硬性完成条件——于是在任何装了 libopus 的开发机上,
这个任务会卡在它自己的检查上。更要紧的是发布版:这正是 `can-audio/CLAUDE.md` 里
`opus.dll` 那个坑的重演——**文件没跟着打包走,程序照常启动,功能悄无声息地死了**。

### H5 — `NoiseGen::next` 撞 clippy 门禁

**裁定:改名 `sample()`。** `clippy::should_implement_trait` 是 warn-by-default,
而每个任务的门禁是 `cargo clippy --workspace -- -D warnings`,所以 Task 5 过不了自己的门。

---

## 三、Medium — 会当场炸的

| # | 裁定 |
|---|---|
| **M15** | Task 6 的 Files 列表加 `crates/*/src/rx/mod.rs`,步骤里加 `pub mod decode;`。否则 Task 6 Step 5 硬编译失败,Task 9 也跟着挂。 |
| **M2** | **每一个控制面消息的每一个 `Vec` 字段都加 `#[serde(default)]`**,两个方向都加。Go 把 nil slice 编码成 `null`,而 `rejected_xc` 按 P2 的明确指令**没有** `omitempty`,所以 `"rejected_xc": null` 是常态而不是边角。少一个 default,**每一个 SUBACK 都解不出来**,`pump` 会把它读成"控制流结束"→ 重连风暴。 |
| **M7** | 删掉 `use tokio::io::AsyncReadExt`——quinn 0.11 的 `RecvStream::read_exact` 是固有方法,把它盖住了,`-D warnings` 会因 `unused_imports` 失败。 |
| **M8** | tokio 的 feature 加 `net`。Task 11 的 `run` 调 `tokio::net::lookup_host`,现在只是**碰巧**靠 quinn 的 feature 合并编译过去——quinn 换个版本就塌。 |
| **M12** | rustls 被钉在 `features=["ring"]`,而 quinn 0.11 默认 `aws-lc-rs`,且没有任何地方调 `CryptoProvider::install_default()`。**二选一并写死**:要么统一到 aws-lc-rs,要么保留 ring 并在进程启动时显式装。否则第一次拨号 panic:"no process-level CryptoProvider available"。 |
| **M13** | P2 Task 11 已落地,`server/cmd/can-voice/` 存在,这条的前半已消解。后半仍然生效:**Rust 任务往 Go 仓库写 Go 文件,必须把两个 Go 文件列进 Files,并且只按显式 pathspec 提交,绝不 `git add -A`。** |
| **M3** | `read_msg` 不要自己重写一遍长度前缀和 `MAX_FRAME` 检查。把长度检查抽成一个函数给两边共用,或者干脆只留异步版本。否则**被测的那份实现从不上线,上线的那份从不被测**,两者一旦分叉就是协议错位且无测试可catch。(与 C3 的重构一起做。) |

## 四、Medium — 质量与完整性

| # | 裁定 |
|---|---|
| **M1** | **从 P3 里彻底删掉 `Hello.transport` 字段。** 它只存在于 P2 的 `task-12-brief`,而 Task 12 因 P1 未部署而**停在未做**。一个宣称了未构建行为的字段比没有这个字段更糟:真设成 `"stream"` 时服务端会忽略它,客户端却以为自己走了回退通道。等 Task 12 真做了再加。 |
| **M4** | 客户端在发 SUB 之前自己按 `maxXCPairs = 64` 夹一下,并把夹掉的部分报给上层。另外:**任何被 `MaxTX` 截掉的频率会让所有含它的耦合对失效**,这一点连同 C1 一起,是客户端必须向界面解释的。12 个交叉耦合的电台就是 66 对,尾巴会被拒。 |
| **M5** | `Link` 保留 `Ready.max_rx`,不要在边界上丢掉。客户端要能在声明**之前**夹住,而不是靠 `rejected` 事后发现。 |
| **M6** | `connect()` 改成真正等握手完成再返回,这样它的 `Result` 才可能是 `Err`,`Error::BadAddress` 才有构造点。现在它在 `tokio::spawn` 之后立刻 `Ok`,任何 I/O 都还没发生——错的主机名和坏 token 都只会变成一个没有理由的 `State(Offline)`(与 H3 叠加)。 |
| **M11** | **实现 PING/PONG,不要删掉 `Event::Health`。** `can-audio/CLAUDE.md` 的规矩是"**掉线必须自己解释**"——RTT、发送缓冲拥塞、收发帧数,正是区分"上行真的扛不住"和"抖了一下"的东西,这两者的处置完全不同。留着一个永远不发的公开事件变体,等于对上层撒谎。 |
| **M14** | `Event::RxEnd { frames, secs }` 的两个字段在 `pump` 里实算,不要硬编码 0。Task 10 专门写了测试论证这两个字段的意义("每次通话一行,自带时长和帧数"),而唯一的生产者填 0。 |
| **M9 / M10** | 已并入 C4。 |

## 五、Low — 批量处理,一次提交

L1(`serde_json` 重复声明且在 workspace 表外重钉版本)、L2/L3(并入 C4)、
L5(Task 8 的 Interfaces 块声称消费 `SubscriptionState`,实际消费者是 Task 11)、
L6(Task 3 的 Interfaces 块漏了 `set_gain`/`set_primary`)、
L7(Task 2 的 Interfaces 块写了 `Result<...>` 但 `wire.rs` 没有这个别名)、
L8(`let _ = SAMPLE_RATE;` 这行强制的空操作删掉;另核 audiopus 0.3 的
`decode` 签名是否真是 `Option<&[u8]>` + `&mut [i16]`——0.3 把它们包在
`Packet`/`MutSignals` newtype 里,**这条没有验证过,执行者要先核再写**)、
L9(`format!("{:?}")` 比字符串改成用已派生的 `PartialEq`)、
L10(两处 `/* Task 12 接入 */` 注释改成 P4,P3 只有 11 个任务)、
L11(`run` 每轮都重发 `Event::State`,改成只在变化时发)、
L12(`set_tx(f,false)` 顺带清 `xc` 是对的也是必要的,但没有测试钉住,补一个)。

**L4 要单独说:** `the_public_api_has_no_imperative_channel_verbs` 只 grep
`include_str!("client.rs")` 一个文件,七个文件里管一个,而且是在源码文本上匹配而不是在导出的 API 上。
`session.rs` 里加一个 `pub fn join_frequency` 它发现不了。**裁定:扫全部模块文件。**
要真做对得看导出符号,但那要 proc-macro 或 `cargo public-api`;扫七个文件是成本合适的近似,
而扫一个文件是**看起来在防守**。

---

## 六、命名陷阱(L13)——单独记一条

**P3 的 `Radio.primary` 和服务端的"主频率"是两个毫不相干的东西。**

- `Radio.primary` 是界面标记(电台列表里那个 `▸` 行),**不发给服务端**。
- 服务端的"主频率"是**发话人**发射时用的那个频率,它决定了当一个监听者同时订阅了
  一对耦合频率的两端时,包头的 `freq_khz` 填哪一个。

把 Task 3 接到 Task 11 的人一定会想把这两个连起来。**裁定:把 P3 的字段改名 `selected`**,
让这个诱惑消失。判错的代价很具体:恰好在"双订阅 + 交叉耦合"这个情况下,
音频会显示在错误的电台行上。

---

## 七、执行顺序的一处调整

Task 4 和 Task 6 按本文件改写之前不要开工(C4、H4、M15)。
Task 8 的 QUIC 部分(Step 6)在原计划里**明确未测**,而 C2、C3、M7、M8、M12 全在那里——
**裁定:Task 8 Step 6 必须带测试**,至少覆盖关闭码映射(C2)和控制流读取的取消安全(C3)。
这两条都是"本地跑起来像好的,上线之后无法解释"的形态,而这正是本项目已经栽过的那类。

---

## 八、P3 依赖的 P2 修复(来自 P2 终审 B)

P2 终审在协议面查出 12 条,其中四条**直接决定 P3 怎么写**。P3 开工前先确认这些已在服务端落地,
否则 P3 会照着一份说法不明的契约写代码。

1. **`seq` 的语义至今没有定义。** 是每次发话重新起算,还是每个会话单调?uint16 在 20 ms 一帧下
   约 21 分钟回绕一次——**Task 4 的抖动缓冲区正是照着这个字段写的**,而它现在用非回绕的
   `<`/`>` 比较却用 `wrapping_add` 推进(L3)。服务端把语义写进 wire 的文档和金文件之前,
   Task 4 的回绕处理没有依据。**这是 P3 对 P2 唯一的硬依赖。**
2. **下行 `qual` 恒为 1–255、永不为 0**,这一点哪里都没写,而金文件里唯一的 `qual=0` 样例是上行的。
   P3 的 `quality_gain`/`squelch_level` 要不要处理 0,取决于这条被写下来。
3. **`flags` 的第 2–7 位没有"保留,必须为零"的约定**,而服务端 `fanout.go` 是**原样转发**的。
   P3 解析时应当忽略未知位而不是拒绝,但这要等服务端把约定写下来才算有据。
4. **金文件第 4 个样例用了 `199998`** 当普通频率例子——那正是 can-audio 里"未设频率"的占位值。
   跨实现的金文件不该把一个有特殊含义的值当普通样例供着;P3 不要照抄它做测试夹具。

另有两条改善 P3 的体验,不阻塞:服务端 README 会补一节 SUBACK 写明差集公式(见 H2);
驱逐的 reason 会变成常量 `ReasonEvicted = "evicted"` 并被钉住(C2 读的是关闭**码**,不受影响,
但有个稳定字符串更好认)。

---

## 九、一条服务端查不出来的客户端义务(P2 第四轮带出)

**发射端必须为每一个 TX 频率各发一个数据报,同一个音频帧在每份副本上带同一个 `seq`。**

这不是新规矩,而是一直就成立的:包头只写得下**一个** `freq_khz`,而服务端的 `MayTransmit`
是按那个频率鉴权的。所以一个没有交叉耦合、但在两个频率上都开了 TX 的管制员,
本来就得发两个数据报。P2 第四轮只是让交叉耦合也守这条规矩,并把它写进了契约。

**为什么它必须写在 P3 这边:服务端检查不了。**
一个声明了 `xc` 却只发一个数据报的客户端,在另一个频率上**完全静默**,
而服务端日志一切正常——它收到了一个合法的数据报,鉴权通过,扇出成功。
没有任何一端会报错。这正是整套重写要逃离的那类故障,而这一次拦不住它的是协议本身。

配套的两条接收端规则(同样来自第四轮,写进 `wire.Header.Seq` 与 `server/README.md`):

- **服务端会扣掉"发话人自己也在发"的那个耦合副本。** 于是只订阅耦合对其中一边的听众
  每帧**恰好收到一份**,`seq` 在 `(speaker, freq_khz)` 这个键下是连号的。
- **同时订阅耦合对两边的听众,每个频率各收一份,`seq` 相同,两个独立缓冲区。**
  **这是有意的,不要跨频率去重**——RX 开在两个频率上的无线电台面本来就会在两行上
  都听到耦合通话。Rust 实现者看到"同一个 seq 出现两次"时第一反应会是去重,那是错的。

**另记:`FlagLast` 不是结束发话的机制,只是一个优化。** 它走不可靠数据报,而且听众飞出射程时
服务端**不打招呼就停发**——那正是 `FlagLast` 保证不会到达的情形。
**接收端必须另有静音超时**,否则 RX 指示灯会亮一整个会话。
这是 `can-audio` 那个老 bug 的升级版:那边是亮半秒,这边是永久。
