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
**客户端必须读 QUIC 的应用层关闭码**。quinn 的路径是
`ConnectionError::ApplicationClosed(ApplicationClose { error_code, reason })`。
映射表(与 `server/internal/transport/codes.go` 一一对应):

| 码 | 含义 | 客户端行为 |
|---|---|---|
| 0 `CloseNormal` | 正常收工 | 终态,不重连 |
| 1 `CloseHandshakeRefused` | 握手被拒(看 reason 区分 `token_expired` / `token_invalid` / `refused`) | 终态;`token_expired` 例外,见 H3 |
| 2 `CloseEvicted` | 同账号在别处登录,你被踢了 | **终态,绝不重连** |
| 3 `CloseProtocolViolation` | 客户端违反协议 | 终态,且应当报 bug |
| 其他 / 传输层错误 | 网络断了 | 走 `ReconnectPolicy`,三次 |

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

**裁定:客户端**可以**且**必须**自己分辨,不改服务端协议。规则是:

```
f ∈ rejected ∧ f ∈ ack.rx   → TX 被限额拒了,但 RX 给了(可以收,不能发)
f ∈ rejected ∧ f ∉ ack.rx   → RX 被限额拒了(既不能收也不能发)
```

这条规则**已经写在服务端 `router.go` 的注释里**,并且是 `Subscribe` 的实际行为
(我核过源码:`ack.RX` 是从 `next.rx` 遍历出来的,TX 被拒的频率因为 TX⊆RX 传播不会进 `next.rx`,
但 RX 循环会把它按普通 RX 声明再收一次)。Task 11 的 `pump` 按这条规则分派
`Event::TxDenied` 和 `Event::RxDenied`,**不要**把 `rejected` 里的每一项都当成 TxDenied。

**一个边界要写进注释:** `ack.Rejected` 在服务端有 `maxRejected = 256` 的截断(排序后截断)。
超过 256 个被拒频率时,第 257 个以后既不在 `rx` 里也不在 `rejected` 里——
**客户端必须把"我声明了但两个列表里都没有"也当成被拒**,否则界面会显示一个根本没生效的频率。

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
