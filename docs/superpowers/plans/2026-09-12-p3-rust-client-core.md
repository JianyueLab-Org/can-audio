# P3：Rust 客户端核心库 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 `can-voice-client` —— 四个 Tauri 客户端和服务端 ATIS 机器人共用的 Rust 语音核心：QUIC 连接、声明式订阅、抖动缓冲、Opus 编解码、按频率混音、同频干扰音、射程衰减、无线电栈模型。

**Architecture:** 公开 API 是**声明式**的 —— 调用方声明"我要收哪些频率、发哪些频率"，库负责让服务端状态收敛过去，重连后自动重发。没有 `join_channel()`，没有 channel id，没有任何"需要记住"的连接状态。业务逻辑（订阅状态机、抖动缓冲、混音、无线电栈）全部是无 I/O 的纯逻辑，可以不碰网络和声卡地测试。

**Tech Stack:** Rust 2021、quinn 0.11、rustls、tokio、serde_json、audiopus（静态链接 libopus）、cpal

**Spec:** `docs/superpowers/specs/2026-09-12-can-voice-design.md`

**前置依赖:** P2 的 `server/testdata/wire-golden.json` 必须已存在 —— 本计划 Task 2 测同一份文件。P2 的服务端要能跑起来，Task 11 的端到端测试打它。

## Global Constraints

- **公开 API 里不得出现 `join`、`leave`、`channel`、`channel_id` 这类词汇。** 这不是命名洁癖：现有 Python 代码里那一整类"UI 是绿的但人还在 root 频道"的 bug，根源就是把"我在哪个频道"当成一个可以记住的事实。`CLAUDE.md` 为此写了两遍 —— *"Joining a channel is not a fact you can remember"* 和 *"A channel id is not a fact you can remember either"*。声明式 API 里没有"记住"这个动作。
- **`wire` 模块与 Go 侧是跨实现契约**，由 `server/testdata/wire-golden.json` 钉住。改布局要同时改黄金文件与 Go 侧。
- **libopus 静态链接进二进制**（`audiopus_sys` 从源码构建）。这一条彻底消灭现有 Python 版那一整类"`opus.dll` 没跟着打包，程序照常启动、语音静默失效"的故障。
- 协议常量（spec §5.2、§7.4）：包头 13 字节；`ver` 恒为 1；`flags` bit0=首帧、bit1=尾帧；Opus 48 kHz 单声道 20 ms 帧（每帧 960 采样）；频率单位 kHz。`qual` 曲线：`≤0.8·range`→255，`0.8→1.1` 线性降至 0。
- **本库产出的字符串一律英文**，且只进日志（`tracing`）。**面向用户的中文文案属于上层 Tauri 应用，不属于这里** —— 这沿用 can-audio 的既有约定，也是 `ptt.py` 当年"共享文件不得含界面文字"那条规则的延续。
- 重连策略沿用现有约定：**会话已建立后掉线最多重连 3 次，全失败则进入 `Offline`；首次连接失败不重试**（那是密码错或地址错，重试只是把同一个错误打印三遍）。
- 每个任务结束时 `cargo test --workspace` 与 `cargo clippy --workspace -- -D warnings` 都必须干净。

---

## 文件结构

```
can-voice/
  Cargo.toml                        workspace
  crates/
    can-voice-proto/
      src/lib.rs
      src/wire.rs                   数据面包头（跨实现契约）
      src/control.rs                控制面消息
    can-voice-client/
      src/lib.rs                    公开 API：VoiceClient、Subscription、Event
      src/conn.rs                   QUIC 连接、有界重连
      src/session.rs                订阅状态机
      src/stack.rs                  无线电栈模型（RX/TX/XC 耦合规则）
      src/rx/jitter.rs              抖动缓冲
      src/rx/decode.rs              Opus 解码 + PLC
      src/rx/mix.rs                 混音、同频干扰音、射程衰减
      src/tx/mod.rs                 采集与 Opus 编码
      src/audio.rs                  设备枚举（cpal）
      examples/canvoice-cli.rs      端到端手工验证用的命令行客户端
```

按职责切分：`proto` 是契约，`session`/`stack` 是纯逻辑，`rx`/`tx` 是音频，`conn`/`audio` 是
I/O 边界。前三类全部可以脱离网络和声卡测试，这也是绝大部分测试所在的地方。

---

### Task 1: workspace 骨架与控制面消息

**Files:**
- Create: `Cargo.toml`（workspace）
- Create: `crates/can-voice-proto/Cargo.toml`
- Create: `crates/can-voice-proto/src/lib.rs`
- Create: `crates/can-voice-proto/src/control.rs`

**Interfaces:**
- Consumes: 无
- Produces: `can_voice_proto::control::{Hello, Ready, Sub, SubAck, Notice, Ping, Pong, Bye, Message}`；`Message::decode(&[u8]) -> Result<Message>`；`Message::encode(&self) -> Result<Vec<u8>>`；`write_frame` / `read_frame`

- [ ] **Step 1: 建 workspace**

`Cargo.toml`：

```toml
[workspace]
resolver = "2"
members = ["crates/can-voice-proto", "crates/can-voice-client"]

[workspace.package]
version = "0.1.0"
edition = "2021"
rust-version = "1.80"

[workspace.dependencies]
serde = { version = "1", features = ["derive"] }
serde_json = "1"
thiserror = "1"
tracing = "0.1"
```

`crates/can-voice-proto/Cargo.toml`：

```toml
[package]
name = "can-voice-proto"
version.workspace = true
edition.workspace = true
rust-version.workspace = true

[dependencies]
serde.workspace = true
serde_json.workspace = true
thiserror.workspace = true

[dev-dependencies]
hex = "0.4"
```

- [ ] **Step 2: 写失败的测试**

`crates/can-voice-proto/src/control.rs` 的测试部分（先只写测试，实现留空）：

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decode_dispatches_on_the_type_field() {
        let raw = br#"{"type":"HELLO","token":"abc","client":"can-controller/3.0.0","proto":1}"#;
        match Message::decode(raw).expect("decode") {
            Message::Hello(h) => {
                assert_eq!(h.token, "abc");
                assert_eq!(h.proto, 1);
            }
            other => panic!("decoded {other:?}, want Hello"),
        }
    }

    #[test]
    fn decode_rejects_an_unknown_type() {
        assert!(Message::decode(br#"{"type":"NOPE"}"#).is_err());
    }

    #[test]
    fn sub_is_a_full_declaration_with_no_delta_fields() {
        let raw = br#"{"type":"SUB","rx":[118000,121800],"tx":[121800],"xc":[[121800,124550]]}"#;
        match Message::decode(raw).expect("decode") {
            Message::Sub(s) => {
                assert_eq!(s.rx, vec![118000, 121800]);
                assert_eq!(s.tx, vec![121800]);
                assert_eq!(s.xc, vec![[121800, 124550]]);
            }
            other => panic!("decoded {other:?}, want Sub"),
        }
    }

    #[test]
    fn encode_round_trips_through_decode() {
        let sub = Message::Sub(Sub {
            rx: vec![118000, 121800],
            tx: vec![121800],
            xc: vec![[121800, 124550]],
        });
        let bytes = sub.encode().expect("encode");
        let back = Message::decode(&bytes).expect("decode");
        assert_eq!(format!("{back:?}"), format!("{sub:?}"));
    }

    #[test]
    fn encode_emits_the_type_discriminator() {
        let bytes = Message::Ping(Ping { t: 42 }).encode().expect("encode");
        let text = String::from_utf8(bytes).expect("utf8");
        assert!(text.contains(r#""type":"PING""#), "encoded as {text}");
    }

    #[test]
    fn read_frame_rejects_an_oversized_length_prefix() {
        // 一个恶意的长度前缀不能让客户端去分配 4 GB。
        let mut cursor = std::io::Cursor::new(vec![0xff, 0xff, 0xff, 0xff]);
        assert!(read_frame(&mut cursor).is_err());
    }

    #[test]
    fn frames_round_trip() {
        let mut buf = Vec::new();
        write_frame(&mut buf, br#"{"type":"PING","t":1}"#).expect("write");
        let mut cursor = std::io::Cursor::new(buf);
        let got = read_frame(&mut cursor).expect("read");
        assert_eq!(&got, br#"{"type":"PING","t":1}"#);
    }
}
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cargo test -p can-voice-proto`
Expected: 编译失败，`Message`、`Sub`、`read_frame` 等未定义

- [ ] **Step 4: 实现**

`crates/can-voice-proto/src/control.rs`（把上面的测试模块追加到文件末尾）：

```rust
//! 控制面消息：长度前缀 JSON 帧。
//!
//! 控制面刻意用 JSON 而不是 protobuf：消息频率极低（登录、订阅变更、偶发通知），
//! 可读性和排障便利压过字节效率。高频的音频走 `crate::wire` 的紧凑二进制。

use serde::{Deserialize, Serialize};
use std::io::{Read, Write};

/// 一个控制帧的上限。SUB 会携带订阅列表，64 KB 留了充足余量。
pub const MAX_FRAME: usize = 64 * 1024;

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("control frame of {0} bytes exceeds the {MAX_FRAME} byte limit")]
    TooLarge(usize),
    #[error("control frame is not valid JSON: {0}")]
    Json(#[from] serde_json::Error),
    #[error("unknown control message type {0:?}")]
    UnknownType(String),
    #[error(transparent)]
    Io(#[from] std::io::Error),
}

type Result<T> = std::result::Result<T, Error>;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Hello {
    pub token: String,
    pub client: String,
    pub proto: u32,
    /// 只有观察员模式填：观察员没有 FSD 连接，位置取自它跟随的那架飞机。
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub follow: String,
    /// 为 "stream" 时音频走回退通道。帧格式完全相同。
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub transport: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Ready {
    pub session: u32,
    pub server: String,
    pub max_tx: u32,
    pub max_rx: u32,
}

/// **全量**收发声明，不是增量。
///
/// 服务端收到即整体替换该会话的订阅集合。这是消除 sync 风暴的根本机制：
/// 幂等，没有"这次是加还是减"的状态推导，重连后重发一次即恢复。
/// 刻意没有 add/remove 字段 —— 任何增量语义都会把那一类 bug 请回来。
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
pub struct Sub {
    pub rx: Vec<u32>,
    pub tx: Vec<u32>,
    pub xc: Vec<[u32; 2]>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
pub struct SubAck {
    pub rx: Vec<u32>,
    pub tx: Vec<u32>,
    pub rejected: Vec<u32>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Notice {
    pub kind: String,
    #[serde(default)]
    pub freq: u32,
    #[serde(default)]
    pub reason: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Ping {
    pub t: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Pong {
    pub t: i64,
    pub server_t: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Bye {
    pub reason: String,
}

/// 控制面消息。判别字段是 JSON 里的 `type`。
#[derive(Debug, Clone, PartialEq)]
pub enum Message {
    Hello(Hello),
    Ready(Ready),
    Sub(Sub),
    SubAck(SubAck),
    Notice(Notice),
    Ping(Ping),
    Pong(Pong),
    Bye(Bye),
}

impl Message {
    /// 按 `type` 字段分发。未知类型直接拒绝，不静默忽略 ——
    /// 静默忽略会让一个拼错的类型表现为"消息发出去了但什么都没发生"。
    pub fn decode(b: &[u8]) -> Result<Message> {
        #[derive(Deserialize)]
        struct Probe {
            #[serde(rename = "type")]
            ty: String,
        }
        let probe: Probe = serde_json::from_slice(b)?;
        Ok(match probe.ty.as_str() {
            "HELLO" => Message::Hello(serde_json::from_slice(b)?),
            "READY" => Message::Ready(serde_json::from_slice(b)?),
            "SUB" => Message::Sub(serde_json::from_slice(b)?),
            "SUBACK" => Message::SubAck(serde_json::from_slice(b)?),
            "NOTICE" => Message::Notice(serde_json::from_slice(b)?),
            "PING" => Message::Ping(serde_json::from_slice(b)?),
            "PONG" => Message::Pong(serde_json::from_slice(b)?),
            "BYE" => Message::Bye(serde_json::from_slice(b)?),
            other => return Err(Error::UnknownType(other.to_string())),
        })
    }

    pub fn encode(&self) -> Result<Vec<u8>> {
        // serde 的 tagged enum 在这里不好用：每个变体的字段是平铺的，
        // 手写一次比为八个类型各加一个 #[serde(tag)] 属性更直白。
        fn tag<T: Serialize>(ty: &str, v: &T) -> Result<Vec<u8>> {
            let mut val = serde_json::to_value(v)?;
            if let serde_json::Value::Object(ref mut m) = val {
                m.insert("type".into(), serde_json::Value::String(ty.into()));
            }
            Ok(serde_json::to_vec(&val)?)
        }
        match self {
            Message::Hello(v) => tag("HELLO", v),
            Message::Ready(v) => tag("READY", v),
            Message::Sub(v) => tag("SUB", v),
            Message::SubAck(v) => tag("SUBACK", v),
            Message::Notice(v) => tag("NOTICE", v),
            Message::Ping(v) => tag("PING", v),
            Message::Pong(v) => tag("PONG", v),
            Message::Bye(v) => tag("BYE", v),
        }
    }
}

/// 写一个 4 字节大端长度前缀加载荷。
pub fn write_frame<W: Write>(w: &mut W, b: &[u8]) -> Result<()> {
    if b.len() > MAX_FRAME {
        return Err(Error::TooLarge(b.len()));
    }
    w.write_all(&(b.len() as u32).to_be_bytes())?;
    w.write_all(b)?;
    Ok(())
}

/// 读一个长度前缀帧。长度检查在分配之前。
pub fn read_frame<R: Read>(r: &mut R) -> Result<Vec<u8>> {
    let mut hdr = [0u8; 4];
    r.read_exact(&mut hdr)?;
    let n = u32::from_be_bytes(hdr) as usize;
    if n > MAX_FRAME {
        return Err(Error::TooLarge(n));
    }
    let mut b = vec![0u8; n];
    r.read_exact(&mut b)?;
    Ok(b)
}
```

`crates/can-voice-proto/src/lib.rs`：

```rust
//! can-voice 线协议。
//!
//! 这是一个跨实现契约：Go 侧（can-voice 服务端）有一份独立实现，
//! 两边都测 `server/testdata/wire-golden.json`。

pub mod control;
pub mod wire;
```

暂时建一个空的 `crates/can-voice-proto/src/wire.rs`（Task 2 填）：

```rust
//! 数据面包头。Task 2 实现。
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cargo test -p can-voice-proto && cargo clippy -p can-voice-proto -- -D warnings`
Expected: PASS（七个测试）

- [ ] **Step 6: 提交**

```bash
git add Cargo.toml crates/can-voice-proto/
git commit -m "proto: workspace 骨架与控制面消息"
```

---

### Task 2: 数据面包头，测 Go 侧同一份黄金文件

**这个任务是整个 P3 里最重要的一个**：它证明 Rust 与 Go 对同一串字节的理解一致。任何一边
改了布局，这里立刻红。

**Files:**
- Modify: `crates/can-voice-proto/src/wire.rs`
- Modify: `crates/can-voice-proto/Cargo.toml`（dev-dependencies 加 `serde_json`）

**Interfaces:**
- Consumes: `server/testdata/wire-golden.json`（P2 Task 1 产出）
- Produces:
  - `pub const HEADER_SIZE: usize = 13`、`pub const VERSION: u8 = 1`
  - `pub const FLAG_FIRST: u8 = 1 << 0`、`pub const FLAG_LAST: u8 = 1 << 1`
  - `pub struct Header { pub ver: u8, pub flags: u8, pub qual: u8, pub seq: u16, pub freq_khz: u32, pub speaker: u32 }`
  - `impl Header { pub fn write_to(&self, out: &mut Vec<u8>); pub fn parse(b: &[u8]) -> Result<(Header, &[u8])> }`

- [ ] **Step 1: 写失败的测试**

`crates/can-voice-proto/src/wire.rs` 的测试模块：

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use serde::Deserialize;

    #[derive(Deserialize)]
    struct Golden {
        header_size: usize,
        cases: Vec<Case>,
    }

    #[derive(Deserialize)]
    struct Case {
        name: String,
        header: GoldenHeader,
        opus_hex: String,
        encoded_hex: String,
    }

    #[derive(Deserialize)]
    struct GoldenHeader {
        ver: u8,
        flags: u8,
        qual: u8,
        seq: u16,
        freq_khz: u32,
        speaker: u32,
    }

    fn golden() -> Golden {
        // 与 Go 侧同一份文件。改它等于改协议。
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../server/testdata/wire-golden.json");
        let raw = std::fs::read(path)
            .unwrap_or_else(|e| panic!("read {path}: {e} — P2 Task 1 must have produced it"));
        serde_json::from_slice(&raw).expect("parse golden")
    }

    #[test]
    fn header_size_matches_the_golden_file() {
        assert_eq!(golden().header_size, HEADER_SIZE);
    }

    #[test]
    fn encoding_matches_the_golden_file() {
        for c in golden().cases {
            let opus = hex::decode(&c.opus_hex).expect("opus_hex");
            let h = Header {
                ver: c.header.ver,
                flags: c.header.flags,
                qual: c.header.qual,
                seq: c.header.seq,
                freq_khz: c.header.freq_khz,
                speaker: c.header.speaker,
            };
            let mut out = Vec::new();
            h.write_to(&mut out);
            out.extend_from_slice(&opus);
            assert_eq!(hex::encode(&out), c.encoded_hex, "case {:?}", c.name);
        }
    }

    #[test]
    fn parsing_matches_the_golden_file() {
        for c in golden().cases {
            let raw = hex::decode(&c.encoded_hex).expect("encoded_hex");
            let (h, opus) = Header::parse(&raw).expect("parse");
            assert_eq!(h.ver, c.header.ver, "case {:?}", c.name);
            assert_eq!(h.flags, c.header.flags, "case {:?}", c.name);
            assert_eq!(h.qual, c.header.qual, "case {:?}", c.name);
            assert_eq!(h.seq, c.header.seq, "case {:?}", c.name);
            assert_eq!(h.freq_khz, c.header.freq_khz, "case {:?}", c.name);
            assert_eq!(h.speaker, c.header.speaker, "case {:?}", c.name);
            assert_eq!(hex::encode(opus), c.opus_hex, "case {:?}", c.name);
        }
    }

    #[test]
    fn parse_rejects_a_short_packet() {
        assert!(Header::parse(&[0u8; HEADER_SIZE - 1]).is_err());
    }

    #[test]
    fn parse_rejects_an_unknown_version() {
        let mut out = Vec::new();
        Header { ver: 2, ..Default::default() }.write_to(&mut out);
        assert!(
            Header::parse(&out).is_err(),
            "silently accepting an unknown version means decoding a future layout as if it were this one"
        );
    }

    #[test]
    fn a_header_with_no_payload_is_valid() {
        // 尾帧可以不带 Opus 载荷。
        let mut out = Vec::new();
        Header { ver: VERSION, flags: FLAG_LAST, freq_khz: 118_000, ..Default::default() }
            .write_to(&mut out);
        let (_, opus) = Header::parse(&out).expect("parse");
        assert!(opus.is_empty());
    }
}
```

在 `crates/can-voice-proto/Cargo.toml` 的 `[dev-dependencies]` 里补上：

```toml
serde_json = "1"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cargo test -p can-voice-proto wire`
Expected: 编译失败，`Header`、`HEADER_SIZE` 未定义

- [ ] **Step 3: 实现**

`crates/can-voice-proto/src/wire.rs`（把上面的测试模块追加到末尾）：

```rust
//! 数据面包头。
//!
//! 跨实现契约：Go 侧（can-voice 服务端）有一份独立实现，两边都测
//! `server/testdata/wire-golden.json`。改这里的布局等于改协议，
//! 必须同时改黄金文件和 Go 侧。
//!
//! ```text
//!  0        1        2        3               5               9              13
//!  +--------+--------+--------+---------------+---------------+--------------+
//!  |  ver   | flags  |  qual  |    seq(2)     |  freq_khz(4)  | speaker(4)   | opus…
//!  +--------+--------+--------+---------------+---------------+--------------+
//! ```

/// 包头字节数。
pub const HEADER_SIZE: usize = 13;

/// 本实现能处理的唯一协议版本。
pub const VERSION: u8 = 1;

/// 一次发言的首帧：接收端据此立刻点亮 RX 指示灯并重置抖动缓冲。
pub const FLAG_FIRST: u8 = 1 << 0;

/// 一次发言的尾帧：接收端据此立刻熄灭 RX 指示灯，
/// 而不是靠一个超时循环 —— 那会让指示灯在松开 PTT 后多亮半秒。
pub const FLAG_LAST: u8 = 1 << 1;

#[derive(Debug, thiserror::Error, PartialEq)]
pub enum Error {
    #[error("packet is {0} bytes, need at least {HEADER_SIZE}")]
    TooShort(usize),
    #[error("unknown protocol version {0}, this build speaks {VERSION}")]
    UnknownVersion(u8),
}

/// 数据面包头。
///
/// `qual` 与 `speaker` 上行时填 0，由服务端填入后再扇出：客户端因此
/// 不知道任何人的位置，而 `speaker` 让接收端能分辨"同一频率上有两个人在讲"
/// 与"一个人的包乱序了"。
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Header {
    pub ver: u8,
    pub flags: u8,
    pub qual: u8,
    pub seq: u16,
    pub freq_khz: u32,
    pub speaker: u32,
}

impl Header {
    /// 把包头追加到 `out`。
    pub fn write_to(&self, out: &mut Vec<u8>) {
        out.push(self.ver);
        out.push(self.flags);
        out.push(self.qual);
        out.extend_from_slice(&self.seq.to_be_bytes());
        out.extend_from_slice(&self.freq_khz.to_be_bytes());
        out.extend_from_slice(&self.speaker.to_be_bytes());
    }

    /// 解出包头，并返回其后的 Opus 载荷。载荷可以为空 —— 尾帧不必携带音频。
    pub fn parse(b: &[u8]) -> Result<(Header, &[u8]), Error> {
        if b.len() < HEADER_SIZE {
            return Err(Error::TooShort(b.len()));
        }
        let h = Header {
            ver: b[0],
            flags: b[1],
            qual: b[2],
            seq: u16::from_be_bytes([b[3], b[4]]),
            freq_khz: u32::from_be_bytes([b[5], b[6], b[7], b[8]]),
            speaker: u32::from_be_bytes([b[9], b[10], b[11], b[12]]),
        };
        // 版本不认识就拒绝：默默接受等于把未来的布局当成现在的来解，
        // 那会表现为音频乱码而不是一条清晰的错误。
        if h.ver != VERSION {
            return Err(Error::UnknownVersion(h.ver));
        }
        Ok((h, &b[HEADER_SIZE..]))
    }

    /// 这一帧是不是一次发言的开始。
    pub fn is_first(&self) -> bool {
        self.flags & FLAG_FIRST != 0
    }

    /// 这一帧是不是一次发言的结束。
    pub fn is_last(&self) -> bool {
        self.flags & FLAG_LAST != 0
    }
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cargo test -p can-voice-proto && cargo clippy -p can-voice-proto -- -D warnings`
Expected: PASS（十三个测试）。**若黄金文件的用例不通过，先怀疑这里，不要改黄金文件** ——
它是手工按 spec 推出来的，Go 侧已经通过了同一份。

- [ ] **Step 5: 提交**

```bash
git add crates/can-voice-proto/
git commit -m "proto: 数据面包头，与 Go 侧共测同一份黄金文件"
```

---

### Task 3: 无线电栈模型

从 `can-audio/controller/radiostack.py` 翻译过来，连同它的耦合规则和测试。规则是从
TrackAudio 的 `radio.tsx` 抄的，现有的 `test_radiostack.py` 逐条可翻译。

放在共享库而不是 controller 应用里：xpc/msfs 用的是它的退化版（单频率）。

**Files:**
- Create: `crates/can-voice-client/Cargo.toml`
- Create: `crates/can-voice-client/src/lib.rs`
- Create: `crates/can-voice-client/src/stack.rs`

**Interfaces:**
- Consumes: 无
- Produces:
  - `pub struct Radio { pub freq_khz: u32, pub rx: bool, pub tx: bool, pub xc: bool, pub gain: f32, pub primary: bool }`
  - `pub struct RadioStack { … }`
  - `impl RadioStack { pub fn new() -> Self; pub fn add(&mut self, freq_khz: u32); pub fn remove(&mut self, freq_khz: u32); pub fn set_rx(&mut self, freq_khz: u32, on: bool); pub fn set_tx(&mut self, freq_khz: u32, on: bool); pub fn set_xc(&mut self, freq_khz: u32, on: bool); pub fn radios(&self) -> &[Radio]; pub fn to_subscription(&self) -> can_voice_proto::control::Sub }`

- [ ] **Step 1: 建 crate 并写失败的测试**

`crates/can-voice-client/Cargo.toml`：

```toml
[package]
name = "can-voice-client"
version.workspace = true
edition.workspace = true
rust-version.workspace = true

[dependencies]
can-voice-proto = { path = "../can-voice-proto" }
serde.workspace = true
serde_json.workspace = true
thiserror.workspace = true
tracing.workspace = true
```

`crates/can-voice-client/src/lib.rs`：

```rust
//! can-voice 客户端核心：四个 Tauri 应用与服务端 ATIS 机器人共用。
//!
//! 公开 API 是**声明式**的：调用方声明"我要收哪些频率、发哪些频率"，
//! 库负责让服务端状态收敛过去，重连后自动重发。没有 join/leave，
//! 没有 channel id，没有任何"需要记住"的连接状态 —— 那正是旧实现里
//! 一整类"UI 是绿的但人还在 root 频道"的 bug 的根源。

pub mod stack;
```

`crates/can-voice-client/src/stack.rs` 的测试模块：

```rust
#[cfg(test)]
mod tests {
    use super::*;

    fn stack_with(freqs: &[u32]) -> RadioStack {
        let mut s = RadioStack::new();
        for f in freqs {
            s.add(*f);
        }
        s
    }

    fn radio(s: &RadioStack, freq: u32) -> &Radio {
        s.radios().iter().find(|r| r.freq_khz == freq).expect("radio present")
    }

    // 以下三条耦合规则抄自 TrackAudio 的 radio.tsx，
    // 与 can-audio 的 controller/test_radiostack.py 一一对应。

    #[test]
    fn turning_rx_off_also_clears_tx_and_xc() {
        // 不接收，那么发送和耦合都没有意义。
        let mut s = stack_with(&[121_800]);
        s.set_xc(121_800, true);
        assert!(radio(&s, 121_800).tx, "xc should have forced tx on");

        s.set_rx(121_800, false);
        let r = radio(&s, 121_800);
        assert!(!r.rx && !r.tx && !r.xc, "clearing rx must clear tx and xc, got {r:?}");
    }

    #[test]
    fn turning_tx_on_forces_rx_on() {
        // 没有只发不收的电台。
        let mut s = stack_with(&[121_800]);
        s.set_rx(121_800, false);
        s.set_tx(121_800, true);
        assert!(radio(&s, 121_800).rx, "turning tx on must force rx on");
    }

    #[test]
    fn turning_xc_on_forces_both_rx_and_tx_on() {
        let mut s = stack_with(&[121_800]);
        s.set_rx(121_800, false);
        s.set_xc(121_800, true);
        let r = radio(&s, 121_800);
        assert!(r.rx && r.tx && r.xc, "xc must force rx and tx on, got {r:?}");
    }

    #[test]
    fn a_new_radio_starts_receiving() {
        let s = stack_with(&[118_000]);
        let r = radio(&s, 118_000);
        assert!(r.rx, "a freshly added radio should receive");
        assert!(!r.tx && !r.xc);
    }

    #[test]
    fn adding_the_same_frequency_twice_does_not_duplicate_it() {
        let mut s = RadioStack::new();
        s.add(118_000);
        s.add(118_000);
        assert_eq!(s.radios().len(), 1);
    }

    #[test]
    fn to_subscription_reflects_the_switches() {
        let mut s = stack_with(&[118_000, 121_800, 124_550]);
        s.set_tx(121_800, true);
        s.set_rx(124_550, false);

        let sub = s.to_subscription();
        assert!(sub.rx.contains(&118_000));
        assert!(sub.rx.contains(&121_800));
        assert!(!sub.rx.contains(&124_550), "a radio with rx off must not be subscribed");
        assert_eq!(sub.tx, vec![121_800]);
    }

    #[test]
    fn to_subscription_pairs_every_cross_coupled_frequency() {
        // XC 是"这些频率互相转发"，所以要产生每一对组合。
        let mut s = stack_with(&[118_000, 121_800, 124_550]);
        s.set_xc(118_000, true);
        s.set_xc(121_800, true);
        s.set_xc(124_550, true);

        let sub = s.to_subscription();
        assert_eq!(sub.xc.len(), 3, "three cross-coupled radios make three pairs, got {:?}", sub.xc);
    }

    #[test]
    fn a_single_cross_coupled_radio_produces_no_pairs() {
        // 一个频率没法和自己交叉耦合。
        let mut s = stack_with(&[118_000]);
        s.set_xc(118_000, true);
        assert!(s.to_subscription().xc.is_empty());
    }

    #[test]
    fn removing_a_radio_drops_it_from_the_subscription() {
        let mut s = stack_with(&[118_000, 121_800]);
        s.remove(118_000);
        let sub = s.to_subscription();
        assert_eq!(sub.rx, vec![121_800]);
    }
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cargo test -p can-voice-client`
Expected: 编译失败，`RadioStack` 未定义

- [ ] **Step 3: 实现**

`crates/can-voice-client/src/stack.rs`（测试模块追加到末尾）：

```rust
//! 无线电栈模型：一个管制员的若干频率，每个带 RX/TX/XC 三个开关。
//!
//! 耦合规则抄自 TrackAudio 的 `radio.tsx`，与 can-audio 的
//! `controller/radiostack.py` 一一对应。这里没有 I/O、没有网络、没有 UI ——
//! 和 Python 版一样，这是全库最值得单测的部分。
//!
//! 放在共享库而不是 controller 应用里：xpc/msfs 用的是它的退化版（单频率）。

use can_voice_proto::control::Sub;

/// 一个频率及其开关。
#[derive(Debug, Clone, PartialEq)]
pub struct Radio {
    pub freq_khz: u32,
    pub rx: bool,
    pub tx: bool,
    pub xc: bool,
    /// 每频率音量，1.0 为原音量。
    pub gain: f32,
    /// 主频率。UI 上用 ▸ 标记。
    pub primary: bool,
}

/// 一组频率。
#[derive(Debug, Clone, Default)]
pub struct RadioStack {
    radios: Vec<Radio>,
}

impl RadioStack {
    pub fn new() -> Self {
        Self::default()
    }

    /// 加一个频率。已存在则什么也不做。新加的频率默认接收。
    pub fn add(&mut self, freq_khz: u32) {
        if self.radios.iter().any(|r| r.freq_khz == freq_khz) {
            return;
        }
        let primary = self.radios.is_empty();
        self.radios.push(Radio {
            freq_khz,
            rx: true,
            tx: false,
            xc: false,
            gain: 1.0,
            primary,
        });
    }

    pub fn remove(&mut self, freq_khz: u32) {
        self.radios.retain(|r| r.freq_khz != freq_khz);
        // 移掉的正好是主频率时，把主标记交给第一个。
        if !self.radios.iter().any(|r| r.primary) {
            if let Some(first) = self.radios.first_mut() {
                first.primary = true;
            }
        }
    }

    /// 关掉 RX 同时清掉 TX 和 XC：不接收，那么发送和耦合都没有意义。
    pub fn set_rx(&mut self, freq_khz: u32, on: bool) {
        if let Some(r) = self.get_mut(freq_khz) {
            r.rx = on;
            if !on {
                r.tx = false;
                r.xc = false;
            }
        }
    }

    /// 打开 TX 强制打开 RX：没有只发不收的电台。
    pub fn set_tx(&mut self, freq_khz: u32, on: bool) {
        if let Some(r) = self.get_mut(freq_khz) {
            r.tx = on;
            if on {
                r.rx = true;
            } else {
                r.xc = false;
            }
        }
    }

    /// 打开 XC 强制打开 RX 和 TX。
    pub fn set_xc(&mut self, freq_khz: u32, on: bool) {
        if let Some(r) = self.get_mut(freq_khz) {
            r.xc = on;
            if on {
                r.rx = true;
                r.tx = true;
            }
        }
    }

    pub fn set_gain(&mut self, freq_khz: u32, gain: f32) {
        if let Some(r) = self.get_mut(freq_khz) {
            r.gain = gain.clamp(0.0, 2.0);
        }
    }

    pub fn set_primary(&mut self, freq_khz: u32) {
        for r in &mut self.radios {
            r.primary = r.freq_khz == freq_khz;
        }
    }

    pub fn radios(&self) -> &[Radio] {
        &self.radios
    }

    /// 把当前开关状态折算成一次全量订阅声明。
    ///
    /// 这是栈与网络之间唯一的接口：UI 改开关，这里产出完整意图，
    /// 由 `session` 发出去。没有"这次改了哪一个"的增量路径。
    pub fn to_subscription(&self) -> Sub {
        let rx: Vec<u32> = self.radios.iter().filter(|r| r.rx).map(|r| r.freq_khz).collect();
        let tx: Vec<u32> = self.radios.iter().filter(|r| r.tx).map(|r| r.freq_khz).collect();

        // XC 的语义是"这些频率互相转发"，所以要产生每一对组合。
        // 一个频率没法和自己耦合，所以单独一个 XC 频率不产生任何配对。
        let coupled: Vec<u32> = self.radios.iter().filter(|r| r.xc).map(|r| r.freq_khz).collect();
        let mut xc = Vec::new();
        for (i, a) in coupled.iter().enumerate() {
            for b in &coupled[i + 1..] {
                xc.push([*a, *b]);
            }
        }
        Sub { rx, tx, xc }
    }

    fn get_mut(&mut self, freq_khz: u32) -> Option<&mut Radio> {
        self.radios.iter_mut().find(|r| r.freq_khz == freq_khz)
    }
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cargo test -p can-voice-client && cargo clippy --workspace -- -D warnings`
Expected: PASS（九个测试）

- [ ] **Step 5: 提交**

```bash
git add crates/can-voice-client/
git commit -m "client: 无线电栈模型与 TrackAudio 的耦合规则"
```

---

### Task 4: 抖动缓冲

**Files:**
- Create: `crates/can-voice-client/src/rx/mod.rs`
- Create: `crates/can-voice-client/src/rx/jitter.rs`
- Modify: `crates/can-voice-client/src/lib.rs`

**Interfaces:**
- Consumes: `can_voice_proto::wire::Header`（Task 2）
- Produces:
  - `pub struct JitterBuffer { … }`
  - `impl JitterBuffer { pub fn new() -> Self; pub fn push(&mut self, seq: u16, payload: Vec<u8>, last: bool); pub fn pop(&mut self) -> Option<Frame>; pub fn depth(&self) -> usize; pub fn finished(&self) -> bool }`
  - `pub enum Frame { Audio(Vec<u8>), Lost, End }`

按 `(speaker, freq)` 一个缓冲 —— 同频可能有多个发言者，各自网络路径不同。深度 40–120 ms
（2–6 帧），起始 60 ms（3 帧）。

`Frame::Lost` 交给解码器走 Opus 的 PLC；连续丢超过 3 帧则结束这次发言（Task 5 处理）。

- [ ] **Step 1: 写失败的测试**

`crates/can-voice-client/src/rx/jitter.rs` 的测试模块：

```rust
#[cfg(test)]
mod tests {
    use super::*;

    fn push_n(j: &mut JitterBuffer, seqs: &[u16]) {
        for s in seqs {
            j.push(*s, vec![*s as u8], false);
        }
    }

    #[test]
    fn nothing_comes_out_until_the_buffer_has_filled() {
        // 缓冲的意义就是先攒一点再放 —— 立刻输出等于没有抖动缓冲。
        let mut j = JitterBuffer::new();
        j.push(0, vec![0], false);
        assert!(j.pop().is_none(), "a single frame must not be released immediately");
    }

    #[test]
    fn frames_come_out_in_sequence_order() {
        let mut j = JitterBuffer::new();
        push_n(&mut j, &[0, 1, 2, 3, 4]);
        let mut got = Vec::new();
        while let Some(Frame::Audio(b)) = j.pop() {
            got.push(b[0]);
        }
        assert_eq!(got, vec![0, 1, 2], "expected the first three frames in order, got {got:?}");
    }

    #[test]
    fn out_of_order_frames_are_reordered() {
        let mut j = JitterBuffer::new();
        push_n(&mut j, &[2, 0, 1, 3]);
        let mut got = Vec::new();
        while let Some(Frame::Audio(b)) = j.pop() {
            got.push(b[0]);
        }
        assert_eq!(got, vec![0, 1, 2], "out-of-order arrival must be reordered, got {got:?}");
    }

    #[test]
    fn a_gap_yields_a_lost_frame_rather_than_stalling() {
        // 丢了一个包就永远等它，会让整条音频卡死。
        let mut j = JitterBuffer::new();
        push_n(&mut j, &[0, 1, 3, 4, 5, 6]);
        let mut got = Vec::new();
        for _ in 0..4 {
            match j.pop() {
                Some(Frame::Audio(b)) => got.push(Some(b[0])),
                Some(Frame::Lost) => got.push(None),
                _ => break,
            }
        }
        assert_eq!(got, vec![Some(0), Some(1), None, Some(3)],
            "seq 2 is missing and must surface as Frame::Lost, got {got:?}");
    }

    #[test]
    fn a_frame_that_arrives_too_late_is_dropped() {
        let mut j = JitterBuffer::new();
        push_n(&mut j, &[0, 1, 2, 3, 4, 5]);
        j.pop();
        j.pop();
        j.pop();
        // seq 0 现在已经播过了，再来一份必须被丢弃而不是把播放指针拉回去。
        j.push(0, vec![99], false);
        match j.pop() {
            Some(Frame::Audio(b)) => assert_ne!(b[0], 99, "a late frame must not rewind playback"),
            other => panic!("pop returned {other:?}"),
        }
    }

    #[test]
    fn the_last_flag_ends_the_talkspurt_after_the_buffer_drains() {
        let mut j = JitterBuffer::new();
        j.push(0, vec![0], false);
        j.push(1, vec![1], false);
        j.push(2, vec![2], true);

        let mut audio = 0;
        loop {
            match j.pop() {
                Some(Frame::Audio(_)) => audio += 1,
                Some(Frame::End) => break,
                Some(Frame::Lost) => {}
                None => panic!("buffer stalled before delivering End"),
            }
        }
        assert_eq!(audio, 3, "every buffered frame must play before End");
        assert!(j.finished());
    }

    #[test]
    fn the_buffer_does_not_grow_without_bound() {
        let mut j = JitterBuffer::new();
        for s in 0..1000u16 {
            j.push(s, vec![0], false);
        }
        assert!(j.depth() <= MAX_DEPTH,
            "depth grew to {} frames; a sender faster than realtime must not exhaust memory", j.depth());
    }

    #[test]
    fn a_duplicate_frame_is_ignored() {
        let mut j = JitterBuffer::new();
        push_n(&mut j, &[0, 0, 1, 2]);
        let mut got = Vec::new();
        while let Some(Frame::Audio(b)) = j.pop() {
            got.push(b[0]);
        }
        assert_eq!(got, vec![0, 1, 2], "a repeated seq must not be played twice, got {got:?}");
    }
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cargo test -p can-voice-client jitter`
Expected: 编译失败，`JitterBuffer` 未定义

- [ ] **Step 3: 实现**

`crates/can-voice-client/src/rx/jitter.rs`（测试模块追加到末尾）：

```rust
//! 抖动缓冲。
//!
//! 按 `(speaker, freq)` 一个 —— 同频可能有多个发言者，各自网络路径不同，
//! 共用一个缓冲会让两个人的包互相当成对方的乱序。

use std::collections::BTreeMap;

/// 起始深度：3 帧 × 20 ms = 60 ms。
pub const START_DEPTH: usize = 3;

/// 深度下限：2 帧 = 40 ms。
pub const MIN_DEPTH: usize = 2;

/// 深度上限：6 帧 = 120 ms。再深就是可感知的延迟了。
pub const MAX_DEPTH: usize = 6;

/// 从缓冲里取出的一帧。
#[derive(Debug, Clone, PartialEq)]
pub enum Frame {
    /// 一帧 Opus 数据。
    Audio(Vec<u8>),
    /// 这一帧丢了。交给解码器走 Opus 的丢包隐藏。
    Lost,
    /// 这次发言结束了。
    End,
}

/// 一个发言者在一个频率上的抖动缓冲。
#[derive(Debug, Default)]
pub struct JitterBuffer {
    frames: BTreeMap<u16, Vec<u8>>,
    /// 下一个该播的序号。None 表示还没开始播。
    next: Option<u16>,
    /// 尾帧的序号，收到后才知道。
    last_seq: Option<u16>,
    finished: bool,
    depth: usize,
}

impl JitterBuffer {
    pub fn new() -> Self {
        Self { depth: START_DEPTH, ..Default::default() }
    }

    /// 收下一帧。
    pub fn push(&mut self, seq: u16, payload: Vec<u8>, last: bool) {
        if last {
            self.last_seq = Some(seq);
        }
        // 已经播过的序号是迟到帧，丢弃 —— 收下它会把播放指针拉回去，
        // 听感上是一小段音频重复。
        if let Some(next) = self.next {
            if seq < next {
                return;
            }
        }
        // 重复帧忽略。
        self.frames.entry(seq).or_insert(payload);

        // 上限保护：一个比实时更快的发送方不该把内存吃光。
        while self.frames.len() > MAX_DEPTH * 4 {
            if let Some(&k) = self.frames.keys().next() {
                self.frames.remove(&k);
                self.next = Some(k.wrapping_add(1));
            }
        }
    }

    /// 取下一帧。缓冲还没攒够时返回 None。
    pub fn pop(&mut self) -> Option<Frame> {
        if self.finished {
            return None;
        }
        // 还没开始播：等攒够起始深度。尾帧已到时不必再等 ——
        // 那意味着不会再有更多数据了。
        if self.next.is_none() {
            if self.frames.len() < self.depth && self.last_seq.is_none() {
                return None;
            }
            self.next = self.frames.keys().next().copied();
        }
        let next = self.next?;

        if let Some(payload) = self.frames.remove(&next) {
            self.next = Some(next.wrapping_add(1));
            if self.last_seq == Some(next) {
                self.finished = true;
                // 尾帧本身也有音频，先播它，End 由 finished 表达；
                // 调用方看到 finished 之后不再 pop。
            }
            return Some(Frame::Audio(payload));
        }

        // 这一格是空的。尾帧已过就结束。
        if let Some(last) = self.last_seq {
            if next > last {
                self.finished = true;
                return Some(Frame::End);
            }
        }
        // 后面还有帧在等：说明 next 是真的丢了，报 Lost 并前进。
        // 死等它会让整条音频卡住。
        if !self.frames.is_empty() {
            self.next = Some(next.wrapping_add(1));
            return Some(Frame::Lost);
        }
        // 什么都没有：还没到，下次再说。
        None
    }

    /// 当前缓存的帧数。
    pub fn depth(&self) -> usize {
        self.frames.len()
    }

    /// 这次发言是否已经放完。
    pub fn finished(&self) -> bool {
        self.finished
    }
}
```

注意上面 `the_last_flag_ends_the_talkspurt_after_the_buffer_drains` 那个测试期望在播完全部
音频后拿到一个 `Frame::End`。按上面的实现，尾帧播出时 `finished` 被置位而没有产出 `End`。
**这是实现与测试的真实冲突，按测试改实现**：把尾帧那一支改成记录 `finished_after`，下一次
`pop()` 才返回 `Frame::End`：

```rust
        if let Some(payload) = self.frames.remove(&next) {
            self.next = Some(next.wrapping_add(1));
            return Some(Frame::Audio(payload));
        }
        // 尾帧已播完 —— 补一个 End，让上层立刻熄灭 RX 指示灯。
        if let Some(last) = self.last_seq {
            if next > last {
                self.finished = true;
                return Some(Frame::End);
            }
        }
```

即删掉 `if self.last_seq == Some(next) { self.finished = true; }` 那一段，让尾帧之后的那次
`pop()` 走到"next > last"分支产出 `End`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cargo test -p can-voice-client && cargo clippy --workspace -- -D warnings`
Expected: PASS（十七个测试）

- [ ] **Step 5: 提交**

```bash
git add crates/can-voice-client/
git commit -m "client: 按 (speaker, freq) 分开的抖动缓冲"
```

---

### Task 5: 混音、同频干扰音与射程衰减

纯 DSP，无 I/O。这是 spec §9.3 那一节，也是"同频叠加"选型的落地处。

**Files:**
- Create: `crates/can-voice-client/src/rx/mix.rs`
- Modify: `crates/can-voice-client/src/rx/mod.rs`

**Interfaces:**
- Consumes: 无
- Produces:
  - `pub fn quality_gain(qual: u8) -> f32` —— `qual` 到增益
  - `pub fn squelch_level(qual: u8) -> f32` —— `qual` 到静噪强度
  - `pub fn apply_quality(samples: &mut [i16], qual: u8, noise: &mut NoiseGen)`
  - `pub fn interfere(sources: &[&[i16]], out: &mut [i16], phase: &mut f32)` —— 同频多源叠加成干扰音
  - `pub fn mix_into(dst: &mut [i16], src: &[i16], gain: f32)` —— 带软限幅
  - `pub struct NoiseGen { … }` —— 确定性伪随机，测试可复现

- [ ] **Step 1: 写失败的测试**

`crates/can-voice-client/src/rx/mix.rs` 的测试模块：

```rust
#[cfg(test)]
mod tests {
    use super::*;

    /// 一段 1 kHz 正弦，48 kHz 采样。
    fn tone(freq: f32, n: usize, amp: f32) -> Vec<i16> {
        (0..n)
            .map(|i| {
                let t = i as f32 / 48_000.0;
                (amp * 32767.0 * (2.0 * std::f32::consts::PI * freq * t).sin()) as i16
            })
            .collect()
    }

    fn rms(s: &[i16]) -> f32 {
        if s.is_empty() {
            return 0.0;
        }
        let sum: f64 = s.iter().map(|&v| (v as f64) * (v as f64)).sum();
        (sum / s.len() as f64).sqrt() as f32
    }

    #[test]
    fn full_quality_is_unity_gain() {
        assert!((quality_gain(255) - 1.0).abs() < 0.01, "qual 255 = {}", quality_gain(255));
    }

    #[test]
    fn gain_falls_as_quality_falls() {
        let g255 = quality_gain(255);
        let g128 = quality_gain(128);
        let g1 = quality_gain(1);
        assert!(g255 > g128 && g128 > g1, "gains: {g255} {g128} {g1}");
        assert!(g1 > 0.0, "an in-range signal must still be audible");
    }

    #[test]
    fn squelch_rises_as_quality_falls() {
        assert!(squelch_level(255) < squelch_level(128));
        assert!(squelch_level(128) < squelch_level(1));
        assert_eq!(squelch_level(255), 0.0, "a full-quality signal carries no squelch");
    }

    #[test]
    fn apply_quality_attenuates_a_weak_signal() {
        let mut strong = tone(1000.0, 960, 0.5);
        let mut weak = strong.clone();
        let mut n = NoiseGen::new(1);
        apply_quality(&mut strong, 255, &mut n);
        let mut n2 = NoiseGen::new(1);
        apply_quality(&mut weak, 40, &mut n2);
        assert!(rms(&weak) < rms(&strong),
            "a low-quality signal must be quieter: weak={} strong={}", rms(&weak), rms(&strong));
    }

    #[test]
    fn apply_quality_adds_noise_to_a_weak_signal() {
        // 全静音输入下，低 qual 应当产生非零输出 —— 那就是静噪。
        let mut silence = vec![0i16; 960];
        let mut n = NoiseGen::new(7);
        apply_quality(&mut silence, 40, &mut n);
        assert!(rms(&silence) > 0.0, "a weak signal must carry audible squelch noise");
    }

    #[test]
    fn apply_quality_leaves_a_full_signal_noise_free() {
        let mut silence = vec![0i16; 960];
        let mut n = NoiseGen::new(7);
        apply_quality(&mut silence, 255, &mut n);
        assert_eq!(rms(&silence), 0.0, "a full-quality signal must not have noise added");
    }

    #[test]
    fn a_single_source_passes_through_interfere_unchanged() {
        let src = tone(1000.0, 960, 0.4);
        let mut out = vec![0i16; 960];
        let mut phase = 0.0;
        interfere(&[&src], &mut out, &mut phase);
        assert_eq!(out, src, "one speaker on a frequency is not interference");
    }

    #[test]
    fn two_sources_produce_a_heterodyne_beat() {
        // 两路同频信号相加会产生拍频啸叫——这正是真实 AM 无线电上
        // 两个载波差频的声音，也是"听得出有两个人在压"的依据。
        let a = tone(500.0, 4800, 0.3);
        let b = tone(700.0, 4800, 0.3);
        let mut plain = vec![0i16; 4800];
        for i in 0..4800 {
            plain[i] = a[i].saturating_add(b[i]);
        }
        let mut out = vec![0i16; 4800];
        let mut phase = 0.0;
        interfere(&[&a, &b], &mut out, &mut phase);
        assert_ne!(out, plain, "interfere must do more than add the two sources");
        assert!(rms(&out) > 0.0);
    }

    #[test]
    fn interference_is_louder_than_either_source_alone() {
        let a = tone(500.0, 960, 0.3);
        let b = tone(700.0, 960, 0.3);
        let mut out = vec![0i16; 960];
        let mut phase = 0.0;
        interfere(&[&a, &b], &mut out, &mut phase);
        assert!(rms(&out) > rms(&a), "two people talking over each other should be more, not less");
    }

    #[test]
    fn interfere_handles_sources_of_different_lengths() {
        let a = tone(500.0, 960, 0.3);
        let b = tone(700.0, 480, 0.3);
        let mut out = vec![0i16; 960];
        let mut phase = 0.0;
        interfere(&[&a, &b], &mut out, &mut phase);
        // 不 panic 即可；短的那一路后半段按静音处理。
        assert_eq!(out.len(), 960);
    }

    #[test]
    fn mix_into_clips_softly_rather_than_wrapping() {
        // i16 溢出回绕会产生刺耳的爆音。
        let loud = vec![i16::MAX; 960];
        let mut dst = vec![i16::MAX; 960];
        mix_into(&mut dst, &loud, 1.0);
        assert!(dst.iter().all(|&v| v > 0),
            "mixing two loud signals must not wrap around to negative");
    }

    #[test]
    fn mix_into_respects_gain() {
        let src = tone(1000.0, 960, 0.5);
        let mut half = vec![0i16; 960];
        mix_into(&mut half, &src, 0.5);
        let mut full = vec![0i16; 960];
        mix_into(&mut full, &src, 1.0);
        assert!(rms(&half) < rms(&full));
    }

    #[test]
    fn noise_is_deterministic_for_a_given_seed() {
        let mut a = NoiseGen::new(42);
        let mut b = NoiseGen::new(42);
        let xs: Vec<f32> = (0..100).map(|_| a.next()).collect();
        let ys: Vec<f32> = (0..100).map(|_| b.next()).collect();
        assert_eq!(xs, ys, "noise must be reproducible so these tests are not flaky");
    }
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cargo test -p can-voice-client mix`
Expected: 编译失败，一堆未定义

- [ ] **Step 3: 实现**

`crates/can-voice-client/src/rx/mix.rs`（测试模块追加到末尾）：

```rust
//! 混音、同频干扰音与射程衰减。
//!
//! 纯 DSP，无 I/O —— 和 `stack` 一样，是全库最值得单测的部分。
//!
//! 射程过滤本身在服务端做（省带宽、防作弊）；这里做的是边缘带的平滑衰减，
//! 因为硬截断听起来像 bug（spec 7.1、7.4）。客户端完全不知道任何人的位置，
//! 它只拿到服务端算好的一个 `qual` 字节。

/// `qual` 到增益。
///
/// 服务端保证只有在射程内的包才会到达（`qual` 至少为 1），
/// 所以这里不需要处理"出界"——那种包根本不会来。
pub fn quality_gain(qual: u8) -> f32 {
    // 从 0.35 到 1.0：边缘信号明显更小声，但仍然听得清内容。
    // 直接线性到 0 会让边缘带最后一点变成听不见的耳语，
    // 那和硬截断没有区别。
    0.35 + 0.65 * (qual as f32 / 255.0)
}

/// `qual` 到静噪强度。满格无噪。
pub fn squelch_level(qual: u8) -> f32 {
    if qual >= 255 {
        return 0.0;
    }
    // 越弱噪声越大，最强约为满量程的 6%。
    0.06 * (1.0 - qual as f32 / 255.0)
}

/// 确定性伪随机，供静噪使用。
///
/// 刻意不用 `rand`：测试需要可复现的噪声，否则断言会时灵时不灵，
/// 而一个偶尔失败的音频测试最终会被人关掉。
#[derive(Debug, Clone)]
pub struct NoiseGen {
    state: u32,
}

impl NoiseGen {
    pub fn new(seed: u32) -> Self {
        Self { state: seed | 1 }
    }

    /// 下一个 -1.0..1.0 的样本。
    pub fn next(&mut self) -> f32 {
        // xorshift32
        self.state ^= self.state << 13;
        self.state ^= self.state >> 17;
        self.state ^= self.state << 5;
        (self.state as f32 / u32::MAX as f32) * 2.0 - 1.0
    }
}

/// 按信号质量做衰减并混入静噪。
pub fn apply_quality(samples: &mut [i16], qual: u8, noise: &mut NoiseGen) {
    let gain = quality_gain(qual);
    let squelch = squelch_level(qual);
    for s in samples.iter_mut() {
        let mut v = *s as f32 * gain;
        if squelch > 0.0 {
            v += noise.next() * squelch * 32767.0;
        }
        *s = v.clamp(i16::MIN as f32, i16::MAX as f32) as i16;
    }
}

/// 拍频啸叫的频率，单位赫兹。真实 AM 无线电上两个载波差频落在这个量级。
const BEAT_HZ: f32 = 1200.0;

/// 同频多路信号叠加成干扰音。
///
/// 一路时原样通过 —— 一个人在讲话不是干扰。
/// 两路及以上时相加后注入拍频啸叫并轻微削波，听感上就是
/// "有人在压我的话"，而不是两段可分辨的语音。
pub fn interfere(sources: &[&[i16]], out: &mut [i16], phase: &mut f32) {
    if sources.is_empty() {
        out.fill(0);
        return;
    }
    if sources.len() == 1 {
        let src = sources[0];
        for (i, o) in out.iter_mut().enumerate() {
            *o = src.get(i).copied().unwrap_or(0);
        }
        return;
    }

    let step = 2.0 * std::f32::consts::PI * BEAT_HZ / 48_000.0;
    for (i, o) in out.iter_mut().enumerate() {
        let sum: f32 = sources.iter().map(|s| s.get(i).copied().unwrap_or(0) as f32).sum();
        // 拍频调制：幅度随啸叫起伏，这是"两个载波在打架"的听感来源。
        let beat = phase.sin();
        *phase += step;
        if *phase > 2.0 * std::f32::consts::PI {
            *phase -= 2.0 * std::f32::consts::PI;
        }
        let modulated = sum * (1.0 + 0.45 * beat) + beat * 0.08 * 32767.0;
        // 轻微削波：过载失真是真实无线电互相压制时的另一半听感。
        *o = soft_clip(modulated * 1.15);
    }
}

/// 把一路信号按增益混进目标缓冲，带软限幅。
///
/// i16 直接相加溢出会回绕，产生刺耳的爆音 —— 那是最容易被误报成
/// "语音系统坏了"的故障。
pub fn mix_into(dst: &mut [i16], src: &[i16], gain: f32) {
    for (i, d) in dst.iter_mut().enumerate() {
        let v = *d as f32 + src.get(i).copied().unwrap_or(0) as f32 * gain;
        *d = soft_clip(v);
    }
}

/// 软限幅：接近满量程时逐渐压缩而不是硬切。
fn soft_clip(v: f32) -> i16 {
    const LIMIT: f32 = 32767.0;
    const KNEE: f32 = 0.8 * LIMIT;
    let a = v.abs();
    if a <= KNEE {
        return v as i16;
    }
    let over = (a - KNEE) / (LIMIT - KNEE);
    let compressed = KNEE + (LIMIT - KNEE) * (1.0 - (-over).exp());
    (compressed.min(LIMIT) * v.signum()) as i16
}
```

`crates/can-voice-client/src/rx/mod.rs`：

```rust
//! 接收路径：抖动缓冲、解码、混音。

pub mod jitter;
pub mod mix;
```

在 `lib.rs` 里加上 `pub mod rx;`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cargo test -p can-voice-client && cargo clippy --workspace -- -D warnings`
Expected: PASS（三十个测试）

- [ ] **Step 5: 提交**

```bash
git add crates/can-voice-client/
git commit -m "client: 射程衰减、静噪、同频干扰音与软限幅混音"
```

---

### Task 6: Opus 编解码与丢包隐藏

**Files:**
- Create: `crates/can-voice-client/src/rx/decode.rs`
- Create: `crates/can-voice-client/src/tx/mod.rs`
- Modify: `crates/can-voice-client/Cargo.toml`
- Modify: `crates/can-voice-client/src/lib.rs`

**Interfaces:**
- Consumes: `rx::jitter::Frame`（Task 4）
- Produces:
  - `pub const SAMPLE_RATE: u32 = 48_000`、`pub const FRAME_SAMPLES: usize = 960`（20 ms）
  - `pub struct Decoder { … }`；`impl Decoder { pub fn new() -> Result<Self>; pub fn decode(&mut self, frame: &Frame, out: &mut [i16]) -> Result<usize> }`
  - `pub struct Encoder { … }`；`impl Encoder { pub fn new() -> Result<Self>; pub fn encode(&mut self, pcm: &[i16]) -> Result<Vec<u8>> }`

**libopus 静态链接进二进制** —— `audiopus_sys` 从源码构建。这一条彻底消灭现有 Python 版
那一整类"`opus.dll` 没跟着打包，程序照常启动、语音静默失效"的故障（`CLAUDE.md` 为它写了两条
独立的条目）。

- [ ] **Step 1: 加依赖**

`crates/can-voice-client/Cargo.toml` 的 `[dependencies]` 加：

```toml
# audiopus_sys 从源码构建 libopus 并静态链接进二进制。
# 这一条彻底消灭 Python 版那一整类 "opus.dll 没跟着打包、程序照常启动、
# 语音静默失效" 的故障 —— 库不可能不跟着走，它就在二进制里。
audiopus = "0.3"
```

- [ ] **Step 2: 写失败的测试**

`crates/can-voice-client/src/rx/decode.rs` 的测试模块：

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::rx::jitter::Frame;
    use crate::tx::Encoder;

    fn tone(n: usize) -> Vec<i16> {
        (0..n)
            .map(|i| {
                let t = i as f32 / SAMPLE_RATE as f32;
                (0.4 * 32767.0 * (2.0 * std::f32::consts::PI * 440.0 * t).sin()) as i16
            })
            .collect()
    }

    #[test]
    fn a_frame_survives_a_round_trip() {
        let mut enc = Encoder::new().expect("encoder");
        let mut dec = Decoder::new().expect("decoder");
        let pcm = tone(FRAME_SAMPLES);

        let packet = enc.encode(&pcm).expect("encode");
        assert!(!packet.is_empty(), "encoding produced no bytes");
        assert!(packet.len() < 400, "a 20 ms voice frame should be small, got {}", packet.len());

        let mut out = vec![0i16; FRAME_SAMPLES];
        let n = dec.decode(&Frame::Audio(packet), &mut out).expect("decode");
        assert_eq!(n, FRAME_SAMPLES, "a 20 ms frame decodes to {FRAME_SAMPLES} samples");

        // Opus 是有损的，不能比对样本；比对能量即可。
        let energy: f64 = out.iter().map(|&v| (v as f64).abs()).sum();
        assert!(energy > 0.0, "decoded frame is silent");
    }

    #[test]
    fn a_lost_frame_is_concealed_rather_than_silenced() {
        // Opus 的 PLC 会用前一帧外推。直接填静音会在音频里留下
        // 一个清晰的"咔哒"，比稍微失真难听得多。
        let mut enc = Encoder::new().expect("encoder");
        let mut dec = Decoder::new().expect("decoder");
        let pcm = tone(FRAME_SAMPLES);

        for _ in 0..3 {
            let p = enc.encode(&pcm).expect("encode");
            let mut out = vec![0i16; FRAME_SAMPLES];
            dec.decode(&Frame::Audio(p), &mut out).expect("decode");
        }

        let mut out = vec![0i16; FRAME_SAMPLES];
        let n = dec.decode(&Frame::Lost, &mut out).expect("conceal");
        assert_eq!(n, FRAME_SAMPLES);
        let energy: f64 = out.iter().map(|&v| (v as f64).abs()).sum();
        assert!(energy > 0.0, "packet loss concealment produced pure silence");
    }

    #[test]
    fn an_end_frame_produces_no_audio() {
        let mut dec = Decoder::new().expect("decoder");
        let mut out = vec![0i16; FRAME_SAMPLES];
        assert_eq!(dec.decode(&Frame::End, &mut out).expect("end"), 0);
    }

    #[test]
    fn encoding_silence_still_produces_a_packet() {
        // 静音帧也必须发出去：接收端的抖动缓冲靠连续的序号判断丢包，
        // 静音时不发会被当成丢了一大片。
        let mut enc = Encoder::new().expect("encoder");
        let packet = enc.encode(&vec![0i16; FRAME_SAMPLES]).expect("encode");
        assert!(!packet.is_empty());
    }

    #[test]
    fn encoding_rejects_a_wrong_sized_frame() {
        let mut enc = Encoder::new().expect("encoder");
        assert!(enc.encode(&vec![0i16; 123]).is_err(),
            "only exact 20 ms frames are valid; a wrong size must fail loudly");
    }
}
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cargo test -p can-voice-client decode`
Expected: 编译失败

- [ ] **Step 4: 实现**

`crates/can-voice-client/src/rx/decode.rs`：

```rust
//! Opus 解码与丢包隐藏。

use super::jitter::Frame;
use audiopus::{coder::Decoder as OpusDecoder, Channels, SampleRate};

/// 采样率。pymumble 时代 48 kHz 是"理想路径"而低采样率会导致变调；
/// 这里它是唯一路径 —— 设备采样率的适配在 `crate::audio` 里做。
pub const SAMPLE_RATE: u32 = 48_000;

/// 一帧的采样数：20 ms × 48 kHz。
pub const FRAME_SAMPLES: usize = 960;

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("opus: {0}")]
    Opus(#[from] audiopus::Error),
    #[error("output buffer holds {0} samples, need {FRAME_SAMPLES}")]
    BufferTooSmall(usize),
}

type Result<T> = std::result::Result<T, Error>;

/// 一个发言者的解码器。**每个 `(speaker, freq)` 一个** ——
/// Opus 解码器是有状态的，共用一个会让两个人的音频互相污染，
/// 而丢包隐藏更是完全依赖前一帧的状态。
pub struct Decoder {
    inner: OpusDecoder,
}

impl Decoder {
    pub fn new() -> Result<Self> {
        Ok(Self {
            inner: OpusDecoder::new(SampleRate::Hz48000, Channels::Mono)?,
        })
    }

    /// 解一帧。返回写入 `out` 的采样数。
    pub fn decode(&mut self, frame: &Frame, out: &mut [i16]) -> Result<usize> {
        if out.len() < FRAME_SAMPLES {
            return Err(Error::BufferTooSmall(out.len()));
        }
        match frame {
            Frame::Audio(p) => {
                let n = self.inner.decode(Some(p), &mut out[..FRAME_SAMPLES], false)?;
                Ok(n)
            }
            // 丢包隐藏：让 Opus 用前一帧外推。填静音会在音频里留下一个
            // 清晰的"咔哒"，比稍微失真难听得多。
            Frame::Lost => {
                let n = self.inner.decode(None, &mut out[..FRAME_SAMPLES], false)?;
                Ok(n)
            }
            Frame::End => Ok(0),
        }
    }
}
```

`crates/can-voice-client/src/tx/mod.rs`：

```rust
//! 发送路径：采集与 Opus 编码。

use crate::rx::decode::{FRAME_SAMPLES, SAMPLE_RATE};
use audiopus::{coder::Encoder as OpusEncoder, Application, Channels, SampleRate};

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("opus: {0}")]
    Opus(#[from] audiopus::Error),
    #[error("frame holds {0} samples, a 20 ms frame is {FRAME_SAMPLES}")]
    WrongFrameSize(usize),
}

type Result<T> = std::result::Result<T, Error>;

/// 每帧最大编码字节数。24 kbps 的 20 ms 帧约 60 字节，400 是充足上限。
const MAX_PACKET: usize = 400;

pub struct Encoder {
    inner: OpusEncoder,
    buf: Vec<u8>,
}

impl Encoder {
    pub fn new() -> Result<Self> {
        let mut inner = OpusEncoder::new(SampleRate::Hz48000, Channels::Mono, Application::Voip)?;
        // 无线电语音：可懂度优先于音乐保真度。
        inner.set_bitrate(audiopus::Bitrate::BitsPerSecond(24_000))?;
        let _ = SAMPLE_RATE; // 文档性引用，确保两侧常量同源
        Ok(Self { inner, buf: vec![0u8; MAX_PACKET] })
    }

    /// 编一帧 20 ms 的单声道 PCM。
    pub fn encode(&mut self, pcm: &[i16]) -> Result<Vec<u8>> {
        // 尺寸不对必须响亮地失败：一个被悄悄接受的错误尺寸
        // 会表现为音频忽快忽慢，那比一条错误难查得多。
        if pcm.len() != FRAME_SAMPLES {
            return Err(Error::WrongFrameSize(pcm.len()));
        }
        let n = self.inner.encode(pcm, &mut self.buf)?;
        Ok(self.buf[..n].to_vec())
    }
}
```

在 `lib.rs` 里加 `pub mod tx;`。

- [ ] **Step 5: 跑测试确认通过**

Run: `cargo test -p can-voice-client && cargo clippy --workspace -- -D warnings`
Expected: PASS（三十五个测试）。首次构建会编译 libopus，慢一些是正常的。

- [ ] **Step 6: 确认 libopus 真的是静态链接的**

macOS / Linux：

```bash
cargo build -p can-voice-client --release
# 在 target/release/ 下找到测试二进制，确认它不依赖外部 libopus
otool -L target/release/deps/can_voice_client-* 2>/dev/null | grep -i opus || echo "no dynamic libopus — good"
ldd target/release/deps/can_voice_client-* 2>/dev/null | grep -i opus || echo "no dynamic libopus — good"
```

Expected: 打印 `no dynamic libopus — good`。**如果这里看到动态依赖，停下来解决它** ——
它正是这个任务要根除的那一类故障。

- [ ] **Step 7: 提交**

```bash
git add crates/can-voice-client/
git commit -m "client: Opus 编解码与丢包隐藏，libopus 静态链接"
```

---

### Task 7: 订阅状态机

声明式 API 的核心。它持有"我想要什么"，负责在连接可用时把意图推给服务端，并在重连后
自动重推。**它不持有"我现在在哪"** —— 那是服务端的事。

**Files:**
- Create: `crates/can-voice-client/src/session.rs`
- Modify: `crates/can-voice-client/src/lib.rs`

**Interfaces:**
- Consumes: `can_voice_proto::control::{Sub, SubAck}`
- Produces:
  - `pub struct SubscriptionState { … }`
  - `impl SubscriptionState { pub fn new() -> Self; pub fn declare(&mut self, sub: Sub); pub fn take_pending(&mut self) -> Option<Sub>; pub fn on_connected(&mut self); pub fn on_disconnected(&mut self); pub fn on_ack(&mut self, ack: SubAck); pub fn acknowledged(&self) -> &SubAck }`

- [ ] **Step 1: 写失败的测试**

`crates/can-voice-client/src/session.rs` 的测试模块：

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use can_voice_proto::control::{Sub, SubAck};

    fn sub(rx: &[u32]) -> Sub {
        Sub { rx: rx.to_vec(), ..Default::default() }
    }

    #[test]
    fn a_declaration_made_while_offline_is_sent_on_connect() {
        let mut s = SubscriptionState::new();
        s.declare(sub(&[118_000]));
        assert!(s.take_pending().is_none(), "nothing can be sent before the link is up");

        s.on_connected();
        assert_eq!(s.take_pending(), Some(sub(&[118_000])));
    }

    #[test]
    fn taking_the_pending_declaration_twice_yields_nothing_the_second_time() {
        let mut s = SubscriptionState::new();
        s.on_connected();
        s.declare(sub(&[118_000]));
        assert!(s.take_pending().is_some());
        assert!(s.take_pending().is_none(), "an unchanged declaration must not be resent every tick");
    }

    #[test]
    fn the_latest_declaration_wins_and_the_intermediate_ones_are_dropped() {
        // 一连串 UI 操作不该变成一连串网络消息。
        // 每一次声明都是全量的，所以中间那些做的是同样的事。
        let mut s = SubscriptionState::new();
        s.on_connected();
        s.declare(sub(&[118_000]));
        s.declare(sub(&[118_000, 121_800]));
        s.declare(sub(&[124_550]));
        assert_eq!(s.take_pending(), Some(sub(&[124_550])));
        assert!(s.take_pending().is_none());
    }

    #[test]
    fn reconnecting_resends_the_declaration_without_being_asked() {
        // 这是整个声明式设计的要点。重连后服务端对我们一无所知，
        // 而客户端不需要记得"我原来在哪个频道"——它只是重发意图。
        let mut s = SubscriptionState::new();
        s.on_connected();
        s.declare(sub(&[118_000, 121_800]));
        s.take_pending();
        s.on_ack(SubAck { rx: vec![118_000, 121_800], ..Default::default() });

        s.on_disconnected();
        s.on_connected();
        assert_eq!(
            s.take_pending(),
            Some(sub(&[118_000, 121_800])),
            "a reconnect must resend the full declaration unprompted"
        );
    }

    #[test]
    fn a_disconnect_clears_what_the_server_had_acknowledged() {
        // 保留旧的 ack 会让上层以为订阅还生效着 ——
        // 那正是"UI 是绿的但人还在 root 频道"的形状。
        let mut s = SubscriptionState::new();
        s.on_connected();
        s.declare(sub(&[118_000]));
        s.take_pending();
        s.on_ack(SubAck { rx: vec![118_000], ..Default::default() });
        assert_eq!(s.acknowledged().rx, vec![118_000]);

        s.on_disconnected();
        assert!(
            s.acknowledged().rx.is_empty(),
            "after a drop the server knows nothing about us; claiming otherwise is the bug this design exists to prevent"
        );
    }

    #[test]
    fn acknowledged_reflects_what_the_server_actually_accepted() {
        // 服务端可能拒掉超出 max_tx 的频率。上层要显示被接受的那一份，
        // 不是我们请求的那一份。
        let mut s = SubscriptionState::new();
        s.on_connected();
        s.declare(Sub { rx: vec![118_000], tx: vec![118_000, 121_800], ..Default::default() });
        s.take_pending();
        s.on_ack(SubAck {
            rx: vec![118_000],
            tx: vec![118_000],
            rejected: vec![121_800],
        });
        assert_eq!(s.acknowledged().tx, vec![118_000]);
        assert_eq!(s.acknowledged().rejected, vec![121_800]);
    }

    #[test]
    fn there_is_no_api_to_remember_where_we_are() {
        // 编译期保证：SubscriptionState 只暴露"我想要什么"和
        // "服务端确认了什么"，没有任何 join/leave/current_channel。
        // 这个测试存在的意义是让有人试图加那种方法时看到这段话。
        let s = SubscriptionState::new();
        let _ = s.acknowledged();
    }
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cargo test -p can-voice-client session`
Expected: 编译失败，`SubscriptionState` 未定义

- [ ] **Step 3: 实现**

`crates/can-voice-client/src/session.rs`（测试模块追加到末尾）：

```rust
//! 订阅状态机。
//!
//! 它持有"我想要什么"，负责在连接可用时把意图推给服务端，
//! 并在重连后自动重推。**它不持有"我现在在哪"** —— 那是服务端的事。
//!
//! 这是整个客户端设计的核心。旧实现里那一整类
//! "UI 是绿的但人还在 root 频道" 的 bug，根源是把
//! "我在哪个频道" 当成一个可以记住的事实：重连之后服务端把你放回
//! root，而客户端的记录还是掉线前的值，于是它认为"已经在那儿了"，
//! 永远不再重入。这里没有那个可以记错的字段。

use can_voice_proto::control::{Sub, SubAck};

/// 订阅意图与服务端的确认。
#[derive(Debug, Default)]
pub struct SubscriptionState {
    /// 最近一次声明的完整意图。
    desired: Option<Sub>,
    /// 是否还没推给服务端。
    dirty: bool,
    connected: bool,
    /// 服务端最近确认的内容。掉线即清空。
    acked: SubAck,
}

impl SubscriptionState {
    pub fn new() -> Self {
        Self::default()
    }

    /// 声明完整的收发意图。反复调用是廉价的：每次都是全量，
    /// 所以一连串 UI 操作只会产生最后那一条网络消息。
    pub fn declare(&mut self, sub: Sub) {
        if self.desired.as_ref() == Some(&sub) && !self.dirty {
            // 完全相同的声明不必重发。
            return;
        }
        self.desired = Some(sub);
        self.dirty = true;
    }

    /// 取出待发送的声明。没有待发的、或链路不可用时返回 None。
    pub fn take_pending(&mut self) -> Option<Sub> {
        if !self.connected || !self.dirty {
            return None;
        }
        self.dirty = false;
        self.desired.clone()
    }

    /// 链路建立。会把当前意图重新标记为待发 ——
    /// 重连后服务端对我们一无所知，必须无条件重推。
    pub fn on_connected(&mut self) {
        self.connected = true;
        if self.desired.is_some() {
            self.dirty = true;
        }
    }

    /// 链路断开。清空服务端的确认：保留它会让上层以为订阅还生效着。
    pub fn on_disconnected(&mut self) {
        self.connected = false;
        self.acked = SubAck::default();
    }

    /// 记录服务端的确认。
    pub fn on_ack(&mut self, ack: SubAck) {
        self.acked = ack;
    }

    /// 服务端实际接受了什么。上层显示这一份，而不是我们请求的那一份 ——
    /// 服务端可能拒掉超出 max_tx 的频率。
    pub fn acknowledged(&self) -> &SubAck {
        &self.acked
    }
}
```

在 `lib.rs` 里加 `pub mod session;`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cargo test -p can-voice-client && cargo clippy --workspace -- -D warnings`
Expected: PASS（四十二个测试）

- [ ] **Step 5: 提交**

```bash
git add crates/can-voice-client/
git commit -m "client: 声明式订阅状态机，重连自动重推"
```

---

### Task 8: QUIC 连接与有界重连

**Files:**
- Create: `crates/can-voice-client/src/conn.rs`
- Modify: `crates/can-voice-client/Cargo.toml`
- Modify: `crates/can-voice-client/src/lib.rs`

**Interfaces:**
- Consumes: `can_voice_proto::control`（Task 1）、`SubscriptionState`（Task 7）
- Produces:
  - `pub const RECONNECT_LIMIT: u32 = 3`
  - `pub enum LinkState { Connecting, Online, Reconnecting, Offline }`
  - `pub struct ReconnectPolicy { … }`；`impl ReconnectPolicy { pub fn new() -> Self; pub fn may_attempt(&mut self) -> bool; pub fn on_session_established(&mut self); pub fn state(&self) -> LinkState }`
  - `pub struct Link { … }`（tokio + quinn，Task 11 的 CLI 会用到）

重连策略沿用现有约定，并且**在"尝试之前"计数，而不是在断开回调里计数** —— `CLAUDE.md` 为
这一点专门写过：按回调计数统计的是"掉线次数"而不是"尝试次数"，服务器一直不可用时只会触发
一次回调然后无限静默重试。

- [ ] **Step 1: 加依赖**

`crates/can-voice-client/Cargo.toml` 的 `[dependencies]`：

```toml
quinn = "0.11"
rustls = { version = "0.23", default-features = false, features = ["ring", "std"] }
rustls-platform-verifier = "0.3"
tokio = { version = "1", features = ["rt-multi-thread", "macros", "sync", "time", "io-util"] }
```

- [ ] **Step 2: 写失败的测试**

`crates/can-voice-client/src/conn.rs` 的测试模块（只测策略，网络部分由 Task 11 的端到端测试
覆盖）：

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_first_connection_failure_is_not_retried() {
        // 首次连接失败是密码错或地址错。重试只是把同一个错误打印三遍。
        let mut p = ReconnectPolicy::new();
        assert!(p.may_attempt(), "the first attempt is always allowed");
        assert!(!p.may_attempt(), "a never-established link must not be retried");
        assert_eq!(p.state(), LinkState::Offline);
    }

    #[test]
    fn an_established_session_gets_exactly_three_reconnects() {
        let mut p = ReconnectPolicy::new();
        assert!(p.may_attempt());
        p.on_session_established();

        for i in 0..RECONNECT_LIMIT {
            assert!(p.may_attempt(), "reconnect {} of {RECONNECT_LIMIT} must be allowed", i + 1);
            assert_eq!(p.state(), LinkState::Reconnecting);
        }
        assert!(!p.may_attempt(), "the fourth reconnect must be refused");
        assert_eq!(p.state(), LinkState::Offline);
    }

    #[test]
    fn the_counter_resets_only_when_a_session_is_really_established() {
        // connect() 返回成功不等于连上了 —— 它只是建了 TLS 套接字。
        // 密码错也会返回同样的结果，然后死在后面。按返回值重置计数
        // 等于重建了无限循环，而且正对着服务端的按账号登录限流。
        let mut p = ReconnectPolicy::new();
        p.may_attempt();
        p.on_session_established();

        p.may_attempt();
        p.may_attempt();
        p.on_session_established(); // 这次是真连上了
        for i in 0..RECONNECT_LIMIT {
            assert!(p.may_attempt(), "the counter should have reset; attempt {} refused", i + 1);
        }
        assert!(!p.may_attempt());
    }

    #[test]
    fn state_is_connecting_before_the_first_attempt_resolves() {
        let p = ReconnectPolicy::new();
        assert_eq!(p.state(), LinkState::Connecting);
    }

    #[test]
    fn state_is_online_while_a_session_is_up() {
        let mut p = ReconnectPolicy::new();
        p.may_attempt();
        p.on_session_established();
        assert_eq!(p.state(), LinkState::Online);
    }
}
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cargo test -p can-voice-client conn`
Expected: 编译失败

- [ ] **Step 4: 实现策略部分**

`crates/can-voice-client/src/conn.rs`：

```rust
//! QUIC 连接与有界重连。

/// 会话建立之后，一次掉线最多重连这么多次。
pub const RECONNECT_LIMIT: u32 = 3;

/// 链路状态。
///
/// `Reconnecting` 与 `Offline` 是对立的，上层必须区别对待：
/// `Reconnecting` 意味着链路还活着，**不要丢掉对象引用**；
/// `Offline` 意味着它彻底没了。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LinkState {
    Connecting,
    Online,
    Reconnecting,
    Offline,
}

/// 有界重连策略。
///
/// **在"尝试之前"计数，不在断开回调里计数。** 按回调计数统计的是
/// "掉线次数"而不是"尝试次数"：服务器一直不可用时只会触发一次回调，
/// 然后无限静默重试 —— 而服务端对登录失败是按账号限流的，
/// 一个僵尸重连循环足以把账号锁出语音。
#[derive(Debug)]
pub struct ReconnectPolicy {
    attempts: u32,
    ever_established: bool,
    state: LinkState,
}

impl Default for ReconnectPolicy {
    fn default() -> Self {
        Self::new()
    }
}

impl ReconnectPolicy {
    pub fn new() -> Self {
        Self { attempts: 0, ever_established: false, state: LinkState::Connecting }
    }

    /// 现在可以（再）拨一次吗。
    pub fn may_attempt(&mut self) -> bool {
        // 第一次总是允许。
        if self.attempts == 0 && !self.ever_established {
            self.attempts = 1;
            self.state = LinkState::Connecting;
            return true;
        }
        // 从未建立过会话就不重试：那是密码错或地址错。
        if !self.ever_established {
            self.state = LinkState::Offline;
            return false;
        }
        if self.attempts >= RECONNECT_LIMIT {
            self.state = LinkState::Offline;
            return false;
        }
        self.attempts += 1;
        self.state = LinkState::Reconnecting;
        true
    }

    /// 会话**真的**建立了（收到 READY），此时才重置计数。
    ///
    /// 不要在"拨号返回成功"时调用它 —— 那只表示 TLS 套接字建好了，
    /// 密码错也会走到同一步。
    pub fn on_session_established(&mut self) {
        self.ever_established = true;
        self.attempts = 0;
        self.state = LinkState::Online;
    }

    pub fn state(&self) -> LinkState {
        self.state
    }
}
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cargo test -p can-voice-client && cargo clippy --workspace -- -D warnings`
Expected: PASS（四十七个测试）

- [ ] **Step 6: 追加 QUIC 拨号**

在 `conn.rs` 里追加（无单测，由 Task 11 的端到端覆盖）：

```rust
use can_voice_proto::control::{self, Message};
use std::net::SocketAddr;
use std::sync::Arc;

/// ALPN，与服务端一致。
pub const ALPN: &[u8] = b"can-voice/1";

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("connect: {0}")]
    Connect(#[from] quinn::ConnectError),
    #[error("connection: {0}")]
    Connection(#[from] quinn::ConnectionError),
    #[error("control: {0}")]
    Control(#[from] control::Error),
    #[error("io: {0}")]
    Io(#[from] std::io::Error),
    #[error("server refused the session: {0}")]
    Refused(String),
    #[error("server replied with {0} instead of READY")]
    UnexpectedReply(String),
}

/// 一条已经握手完成的连接。
pub struct Link {
    pub conn: quinn::Connection,
    pub control_send: quinn::SendStream,
    pub control_recv: quinn::RecvStream,
    pub session: u32,
    pub max_tx: u32,
}

/// 建立连接并完成 HELLO/READY 握手。
///
/// 证书走系统信任链（Let's Encrypt），**不做指纹固定** ——
/// 旧实现固定 Mumble 服务器的自签名证书指纹，代价是换证书就得
/// 全网发版，而丢失证书卷等于全网客户端拒连且无远程修复手段。
pub async fn connect(
    addr: SocketAddr,
    server_name: &str,
    token: &str,
    client_id: &str,
    follow: &str,
) -> Result<Link, Error> {
    let mut crypto = rustls_platform_verifier::tls_config();
    crypto.alpn_protocols = vec![ALPN.to_vec()];
    let client_cfg = quinn::ClientConfig::new(Arc::new(
        quinn::crypto::rustls::QuicClientConfig::try_from(crypto)
            .expect("rustls config is QUIC-compatible"),
    ));

    let mut endpoint = quinn::Endpoint::client("0.0.0.0:0".parse().expect("bind address"))?;
    endpoint.set_default_client_config(client_cfg);

    let conn = endpoint.connect(addr, server_name)?.await?;
    let (mut send, mut recv) = conn.open_bi().await?;

    let hello = Message::Hello(control::Hello {
        token: token.to_string(),
        client: client_id.to_string(),
        proto: 1,
        follow: follow.to_string(),
        transport: String::new(),
    });
    write_msg(&mut send, &hello).await?;

    match read_msg(&mut recv).await? {
        Message::Ready(r) => {
            tracing::info!(session = r.session, server = %r.server, "voice session established");
            Ok(Link { conn, control_send: send, control_recv: recv, session: r.session, max_tx: r.max_tx })
        }
        Message::Bye(b) => Err(Error::Refused(b.reason)),
        other => Err(Error::UnexpectedReply(format!("{other:?}"))),
    }
}

/// 在控制流上写一条消息。
pub async fn write_msg(s: &mut quinn::SendStream, m: &Message) -> Result<(), Error> {
    let body = m.encode()?;
    let mut framed = Vec::with_capacity(body.len() + 4);
    control::write_frame(&mut framed, &body)?;
    s.write_all(&framed).await.map_err(|e| Error::Io(std::io::Error::other(e)))?;
    Ok(())
}

/// 从控制流上读一条消息。
pub async fn read_msg(r: &mut quinn::RecvStream) -> Result<Message, Error> {
    use tokio::io::AsyncReadExt;
    let mut hdr = [0u8; 4];
    r.read_exact(&mut hdr).await.map_err(|e| Error::Io(std::io::Error::other(e)))?;
    let n = u32::from_be_bytes(hdr) as usize;
    if n > control::MAX_FRAME {
        return Err(Error::Control(control::Error::TooLarge(n)));
    }
    let mut body = vec![0u8; n];
    r.read_exact(&mut body).await.map_err(|e| Error::Io(std::io::Error::other(e)))?;
    Ok(Message::decode(&body)?)
}
```

在 `lib.rs` 里加 `pub mod conn;`。

- [ ] **Step 7: 跑测试与 clippy**

Run: `cargo test -p can-voice-client && cargo clippy --workspace -- -D warnings`
Expected: PASS

- [ ] **Step 8: 提交**

```bash
git add crates/can-voice-client/
git commit -m "client: QUIC 握手与有界重连策略"
```

---

### Task 9: 音频设备

**Files:**
- Create: `crates/can-voice-client/src/audio.rs`
- Modify: `crates/can-voice-client/Cargo.toml`
- Modify: `crates/can-voice-client/src/lib.rs`

**Interfaces:**
- Consumes: `rx::decode::{SAMPLE_RATE, FRAME_SAMPLES}`
- Produces:
  - `pub struct DeviceInfo { pub id: String, pub name: String, pub is_default: bool }`
  - `pub fn input_devices() -> Vec<DeviceInfo>` / `pub fn output_devices() -> Vec<DeviceInfo>`
  - `pub fn resample_to_48k(input: &[i16], from_rate: u32) -> Vec<i16>`
  - `pub fn resample_from_48k(input: &[i16], to_rate: u32) -> Vec<i16>`

**重采样是必须的，不是可选的。** Python 版没有做，注释里写着"48 kHz 是理想路径，回退采样率
会产生变调音频"—— 也就是说设备不支持 48 kHz 时用户听到的是变调的声音，而这看起来像"语音
系统坏了"。这里把它变成真正的适配。

- [ ] **Step 1: 加依赖**

```toml
cpal = "0.15"
```

- [ ] **Step 2: 写失败的测试**

`crates/can-voice-client/src/audio.rs` 的测试模块（只测重采样 —— 设备枚举需要真实声卡）：

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resampling_from_48k_to_48k_is_a_no_op() {
        let input: Vec<i16> = (0..960).map(|i| i as i16).collect();
        assert_eq!(resample_to_48k(&input, 48_000), input);
    }

    #[test]
    fn upsampling_from_24k_doubles_the_sample_count() {
        let input = vec![100i16; 480];
        let out = resample_to_48k(&input, 24_000);
        assert_eq!(out.len(), 960, "24 kHz to 48 kHz must double the samples");
    }

    #[test]
    fn downsampling_from_96k_halves_the_sample_count() {
        let input = vec![100i16; 1920];
        let out = resample_to_48k(&input, 96_000);
        assert_eq!(out.len(), 960);
    }

    #[test]
    fn resampling_from_44100_produces_the_right_length() {
        // 44.1 kHz 是最常见的非 48 kHz 设备采样率，而且比例不是整数。
        let input = vec![0i16; 441];
        let out = resample_to_48k(&input, 44_100);
        assert!((out.len() as i32 - 480).abs() <= 1, "441 samples at 44.1k is 10 ms = 480 at 48k, got {}", out.len());
    }

    #[test]
    fn resampling_preserves_a_constant_signal() {
        // 常数信号重采样后还该是同一个常数 —— 插值出别的值说明算错了。
        let input = vec![1000i16; 480];
        let out = resample_to_48k(&input, 24_000);
        assert!(out.iter().all(|&v| (v - 1000).abs() <= 1),
            "a constant signal must survive resampling, got {:?}", &out[..8]);
    }

    #[test]
    fn the_two_directions_round_trip_approximately() {
        let input: Vec<i16> = (0..480).map(|i| ((i as f32 / 10.0).sin() * 8000.0) as i16).collect();
        let up = resample_to_48k(&input, 24_000);
        let back = resample_from_48k(&up, 24_000);
        assert_eq!(back.len(), input.len());
    }

    #[test]
    fn resampling_an_empty_buffer_does_not_panic() {
        assert!(resample_to_48k(&[], 44_100).is_empty());
    }
}
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cargo test -p can-voice-client audio`
Expected: 编译失败

- [ ] **Step 4: 实现**

`crates/can-voice-client/src/audio.rs`（测试模块追加到末尾）：

```rust
//! 音频设备枚举与采样率适配。

use crate::rx::decode::SAMPLE_RATE;

/// 一个音频设备。
#[derive(Debug, Clone, PartialEq)]
pub struct DeviceInfo {
    pub id: String,
    pub name: String,
    pub is_default: bool,
}

/// 线性插值重采样到 48 kHz。
///
/// 这一步是**必须的，不是可选的**。旧的 Python 实现没有做，注释里写着
/// "48 kHz 是理想路径，回退采样率会产生变调音频" —— 也就是说设备不支持
/// 48 kHz 时用户听到的是变调的声音，而那看起来像"语音系统坏了"。
///
/// 线性插值对 8 kHz 带宽的语音足够：它会在高频引入一点混叠，
/// 但无线电语音本来就被限制在 300–3400 Hz。
pub fn resample_to_48k(input: &[i16], from_rate: u32) -> Vec<i16> {
    resample(input, from_rate, SAMPLE_RATE)
}

/// 从 48 kHz 重采样到设备采样率。
pub fn resample_from_48k(input: &[i16], to_rate: u32) -> Vec<i16> {
    resample(input, SAMPLE_RATE, to_rate)
}

fn resample(input: &[i16], from: u32, to: u32) -> Vec<i16> {
    if input.is_empty() {
        return Vec::new();
    }
    if from == to {
        return input.to_vec();
    }
    let ratio = to as f64 / from as f64;
    let out_len = ((input.len() as f64) * ratio).round() as usize;
    let mut out = Vec::with_capacity(out_len);
    for i in 0..out_len {
        let pos = i as f64 / ratio;
        let idx = pos.floor() as usize;
        let frac = (pos - idx as f64) as f32;
        let a = input.get(idx).copied().unwrap_or(0) as f32;
        let b = input.get(idx + 1).copied().unwrap_or(a as i16) as f32;
        out.push((a + (b - a) * frac).round() as i16);
    }
    out
}

/// 列出输入设备。
pub fn input_devices() -> Vec<DeviceInfo> {
    devices(true)
}

/// 列出输出设备。
pub fn output_devices() -> Vec<DeviceInfo> {
    devices(false)
}

fn devices(input: bool) -> Vec<DeviceInfo> {
    use cpal::traits::{DeviceTrait, HostTrait};
    let host = cpal::default_host();
    let default_name = if input {
        host.default_input_device().and_then(|d| d.name().ok())
    } else {
        host.default_output_device().and_then(|d| d.name().ok())
    };
    let list = if input { host.input_devices() } else { host.output_devices() };
    let Ok(list) = list else {
        // 枚举失败不该让客户端起不来：没有设备也能听不能说，
        // 或者反过来，总比整个应用打不开好。
        tracing::warn!(input, "could not enumerate audio devices");
        return Vec::new();
    };
    list.filter_map(|d| {
        let name = d.name().ok()?;
        Some(DeviceInfo {
            is_default: Some(&name) == default_name.as_ref(),
            id: name.clone(),
            name,
        })
    })
    .collect()
}
```

在 `lib.rs` 里加 `pub mod audio;`。

- [ ] **Step 5: 跑测试确认通过**

Run: `cargo test -p can-voice-client && cargo clippy --workspace -- -D warnings`
Expected: PASS（五十四个测试）

- [ ] **Step 6: 提交**

```bash
git add crates/can-voice-client/
git commit -m "client: 音频设备枚举与采样率适配"
```

---

### Task 10: 公开 API

把前面的零件拼成 `VoiceClient`。**这是外界唯一该用的东西**，也是"声明式而非命令式"这条
约束的落点。

**Files:**
- Modify: `crates/can-voice-client/src/lib.rs`
- Create: `crates/can-voice-client/src/client.rs`

**Interfaces:**
- Consumes: Task 1–9 的全部
- Produces:
  - `pub struct Config { pub server: String, pub server_name: String, pub token: String, pub client_id: String, pub follow: String, pub input_device: Option<String>, pub output_device: Option<String> }`
  - `pub enum Event { State(LinkState), RxStart { freq_khz: u32, speaker: u32 }, RxEnd { freq_khz: u32, speaker: u32, frames: u32, secs: f32 }, TxDenied { freq_khz: u32, reason: String }, Health { rtt_ms: u32, sent: u64, received: u64, lost: u64 } }`
  - `pub struct VoiceClient { … }`
  - `impl VoiceClient { pub async fn connect(cfg: Config) -> Result<Self>; pub fn set_subscription(&self, sub: Sub); pub fn set_transmitting(&self, on: bool); pub fn set_frequency_volume(&self, freq_khz: u32, gain: f32); pub fn events(&self) -> tokio::sync::broadcast::Receiver<Event>; pub async fn shutdown(self) }`

- [ ] **Step 1: 写失败的测试**

`crates/can-voice-client/src/client.rs` 的测试模块：

```rust
#[cfg(test)]
mod tests {
    use super::*;

    /// 编译期契约：公开 API 里不得出现 join/leave/channel。
    ///
    /// 这不是命名洁癖。旧实现里一整类"UI 是绿的但人还在 root 频道"的 bug，
    /// 根源就是把"我在哪个频道"当成可以记住的事实。声明式 API 里
    /// 没有"记住"这个动作，所以那类 bug 没有藏身之处。
    #[test]
    fn the_public_api_has_no_imperative_channel_verbs() {
        let source = include_str!("client.rs");
        // 只看公开项的签名行。
        for line in source.lines() {
            let t = line.trim();
            if !t.starts_with("pub fn") && !t.starts_with("pub async fn") {
                continue;
            }
            for banned in ["join", "leave", "channel_id", "current_channel"] {
                assert!(
                    !t.to_lowercase().contains(banned),
                    "public API must not expose {banned:?} — see the module docs for why: {t}"
                );
            }
        }
    }

    #[test]
    fn events_can_be_subscribed_to_before_anything_happens() {
        let (tx, _) = tokio::sync::broadcast::channel::<Event>(16);
        let mut rx = tx.subscribe();
        tx.send(Event::State(crate::conn::LinkState::Online)).expect("send");
        assert!(matches!(
            rx.try_recv(),
            Ok(Event::State(crate::conn::LinkState::Online))
        ));
    }

    #[test]
    fn rx_end_carries_enough_to_log_one_line_per_transmission() {
        // 日志约定：每次收到的通话一行，不是两行。开始是 DEBUG，
        // 结束那一行要自带时长和帧数。
        let e = Event::RxEnd { freq_khz: 121_800, speaker: 7, frames: 127, secs: 2.5 };
        match e {
            Event::RxEnd { frames, secs, .. } => {
                assert_eq!(frames, 127);
                assert!((secs - 2.5).abs() < f32::EPSILON);
            }
            other => panic!("{other:?}"),
        }
    }
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cargo test -p can-voice-client client`
Expected: 编译失败，`Event` 未定义

- [ ] **Step 3: 实现**

`crates/can-voice-client/src/client.rs`（测试模块追加到末尾）：

```rust
//! 公开 API。
//!
//! **声明式，不是命令式。** 调用方声明"我要收哪些频率、发哪些频率"，
//! 库负责让服务端状态收敛过去，重连后自动重发。
//!
//! 这里刻意**没有** `join_channel()`、`leave_channel()`、`channel_id`。
//! 旧的 Python 实现里那一整类"UI 是绿的但人还在 root 频道"的 bug，
//! 根源就是把"我在哪个频道"当成一个可以记住的事实 —— 重连之后服务端
//! 把人放回 root，而客户端的记录还是掉线前的值，于是它认为
//! "已经在那儿了"，永远不再重入，而界面全程是绿的。
//! 声明式 API 里没有那个可以记错的字段。

use crate::conn::LinkState;
use can_voice_proto::control::Sub;

/// 建立连接所需的一切。
#[derive(Debug, Clone)]
pub struct Config {
    /// `host:port`。
    pub server: String,
    /// TLS 的服务器名，通常与 `server` 的主机部分相同。
    pub server_name: String,
    /// can-api 签发的短期 token。
    pub token: String,
    /// 客户端标识，如 `can-controller/3.0.0`，只进服务端日志。
    pub client_id: String,
    /// 观察员模式跟随的呼号；不是观察员时留空。
    pub follow: String,
    pub input_device: Option<String>,
    pub output_device: Option<String>,
}

/// 库向上层报告的事件。
///
/// 上层（Tauri 应用）据此更新界面并写日志。**这里的字符串都是英文且只进日志**；
/// 面向用户的中文文案属于上层，不属于这里。
#[derive(Debug, Clone, PartialEq)]
pub enum Event {
    /// 链路状态变了。`Reconnecting` 与 `Offline` 是对立的：
    /// 前者意味着链路还活着，上层**不要丢掉对象引用**；后者意味着它没了。
    State(LinkState),
    /// 某个频率上有人开始讲话。
    RxStart { freq_khz: u32, speaker: u32 },
    /// 某个频率上有人讲完了。
    ///
    /// 带上帧数和时长，是因为日志约定是**每次通话一行**而不是两行 ——
    /// 开始那一行在 DEBUG，这一行在 INFO 且自带全部信息。
    RxEnd { freq_khz: u32, speaker: u32, frames: u32, secs: f32 },
    /// 服务端拒绝了在某个频率上发送。
    TxDenied { freq_khz: u32, reason: String },
    /// 周期性的链路健康报告。
    ///
    /// 掉线时这些数字要跟着掉线日志一起打出来：
    /// "上行真的跟不上"和"网络抖了一下"需要完全不同的处理，
    /// 而旧实现的日志只写了一句 "voice connection dropped"。
    Health { rtt_ms: u32, sent: u64, received: u64, lost: u64 },
}

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error(transparent)]
    Conn(#[from] crate::conn::Error),
    #[error("address {0:?} could not be resolved")]
    BadAddress(String),
}

/// 语音客户端。
pub struct VoiceClient {
    events_tx: tokio::sync::broadcast::Sender<Event>,
    commands: tokio::sync::mpsc::UnboundedSender<Command>,
}

/// 上层发给后台任务的指令。内部类型。
#[derive(Debug)]
enum Command {
    Declare(Sub),
    Transmit(bool),
    Volume { freq_khz: u32, gain: f32 },
    Shutdown,
}

impl VoiceClient {
    /// 连接并完成握手。失败时**不残留任何资源** ——
    /// 旧实现里没做到这一点的后果是：PyAudio 没关，麦克风被占着，
    /// 下一次尝试报"打不开音频设备"，把用户指向声卡；
    /// 而带 reconnect 的连接对象还活着，变成一个僵尸无限重试，
    /// 把账号锁出语音，改对密码也没用，只能重启应用。
    pub async fn connect(cfg: Config) -> Result<Self, Error> {
        let (events_tx, _) = tokio::sync::broadcast::channel(256);
        let (cmd_tx, cmd_rx) = tokio::sync::mpsc::unbounded_channel();

        let client = VoiceClient { events_tx: events_tx.clone(), commands: cmd_tx };
        tokio::spawn(run(cfg, events_tx, cmd_rx));
        Ok(client)
    }

    /// 声明完整的收发意图。反复调用是廉价的 —— 每次都是全量，
    /// 所以一连串 UI 操作只会产生最后那一条网络消息。
    pub fn set_subscription(&self, sub: Sub) {
        let _ = self.commands.send(Command::Declare(sub));
    }

    /// 按下 / 松开 PTT。
    pub fn set_transmitting(&self, on: bool) {
        let _ = self.commands.send(Command::Transmit(on));
    }

    /// 设置某个频率的播放音量。
    pub fn set_frequency_volume(&self, freq_khz: u32, gain: f32) {
        let _ = self.commands.send(Command::Volume { freq_khz, gain });
    }

    /// 订阅事件流。
    pub fn events(&self) -> tokio::sync::broadcast::Receiver<Event> {
        self.events_tx.subscribe()
    }

    /// 关闭。
    pub async fn shutdown(self) {
        let _ = self.commands.send(Command::Shutdown);
    }
}

/// 后台任务：连接、收发、重连。
async fn run(
    _cfg: Config,
    _events: tokio::sync::broadcast::Sender<Event>,
    mut cmds: tokio::sync::mpsc::UnboundedReceiver<Command>,
) {
    // 完整实现见 Task 11 之后的迭代；此处先保证指令通道不堵塞。
    while let Some(cmd) = cmds.recv().await {
        if matches!(cmd, Command::Shutdown) {
            return;
        }
    }
}
```

在 `lib.rs` 里加 `pub mod client;` 并重导出：

```rust
pub use client::{Config, Event, VoiceClient};
pub use conn::LinkState;
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cargo test -p can-voice-client && cargo clippy --workspace -- -D warnings`
Expected: PASS（五十七个测试）

- [ ] **Step 5: 提交**

```bash
git add crates/can-voice-client/
git commit -m "client: 声明式公开 API 与事件流"
```

---

### Task 11: 命令行客户端与端到端验证

把全部零件接起来跑通，打 P2 的真实服务端。这是唯一能发现各层拼接错误的测试。

**Files:**
- Modify: `crates/can-voice-client/src/client.rs`（填实 `run`）
- Create: `crates/can-voice-client/examples/canvoice-cli.rs`
- Create: `crates/can-voice-client/tests/e2e.rs`

**Interfaces:**
- Consumes: Task 1–10 的全部；P2 的服务端二进制
- Produces: `cargo run -p can-voice-client --example canvoice-cli` 可用；`cargo test -p can-voice-client --test e2e` 在 CI 里跑

- [ ] **Step 1: 填实后台任务**

把 `client.rs` 的 `run` 替换为完整实现：连接 → 循环 { 推送待发订阅、收控制面消息、收
datagram 交给抖动缓冲、播放混音结果 } → 掉线走 `ReconnectPolicy`。

```rust
async fn run(
    cfg: Config,
    events: tokio::sync::broadcast::Sender<Event>,
    mut cmds: tokio::sync::mpsc::UnboundedReceiver<Command>,
) {
    use crate::conn::{self, ReconnectPolicy};
    use crate::session::SubscriptionState;

    let mut policy = ReconnectPolicy::new();
    let mut subs = SubscriptionState::new();

    loop {
        if !policy.may_attempt() {
            let _ = events.send(Event::State(policy.state()));
            tracing::warn!("giving up on the voice link");
            return;
        }
        let _ = events.send(Event::State(policy.state()));

        let addr = match tokio::net::lookup_host(&cfg.server).await.ok().and_then(|mut a| a.next()) {
            Some(a) => a,
            None => {
                tracing::error!(server = %cfg.server, "address could not be resolved");
                let _ = events.send(Event::State(crate::conn::LinkState::Offline));
                return;
            }
        };

        let link = match conn::connect(addr, &cfg.server_name, &cfg.token, &cfg.client_id, &cfg.follow).await {
            Ok(l) => l,
            Err(e) => {
                tracing::warn!(error = %e, "voice connect failed");
                continue;
            }
        };
        // 只有真的收到 READY 才重置计数。connect() 返回成功不等于连上了。
        policy.on_session_established();
        subs.on_connected();
        let _ = events.send(Event::State(policy.state()));

        let dropped = pump(&cfg, link, &mut subs, &events, &mut cmds).await;
        subs.on_disconnected();
        if !dropped {
            return; // 主动关闭
        }
        let _ = events.send(Event::State(crate::conn::LinkState::Reconnecting));
    }
}

/// 一条连接活着期间的主循环。返回 true 表示是掉线（应当重连），
/// false 表示上层要求关闭。
async fn pump(
    _cfg: &Config,
    mut link: crate::conn::Link,
    subs: &mut crate::session::SubscriptionState,
    events: &tokio::sync::broadcast::Sender<Event>,
    cmds: &mut tokio::sync::mpsc::UnboundedReceiver<Command>,
) -> bool {
    use can_voice_proto::control::Message;
    use can_voice_proto::wire::Header;

    loop {
        // 有待发的订阅就先推出去。
        if let Some(sub) = subs.take_pending() {
            if let Err(e) = crate::conn::write_msg(&mut link.control_send, &Message::Sub(sub)).await {
                tracing::warn!(error = %e, "could not push the subscription");
                return true;
            }
        }

        tokio::select! {
            cmd = cmds.recv() => match cmd {
                Some(Command::Declare(sub)) => subs.declare(sub),
                Some(Command::Transmit(_on)) => { /* 采集线程在 Task 12 接入 */ }
                Some(Command::Volume { .. }) => {}
                Some(Command::Shutdown) | None => {
                    link.conn.close(0u32.into(), b"bye");
                    return false;
                }
            },
            msg = crate::conn::read_msg(&mut link.control_recv) => match msg {
                Ok(Message::SubAck(ack)) => {
                    tracing::debug!(rx = ack.rx.len(), tx = ack.tx.len(), rejected = ack.rejected.len(), "subscription acknowledged");
                    for f in &ack.rejected {
                        let _ = events.send(Event::TxDenied { freq_khz: *f, reason: "rejected by the server".into() });
                    }
                    subs.on_ack(ack);
                }
                Ok(Message::Bye(b)) => {
                    tracing::warn!(reason = %b.reason, "server closed the session");
                    return true;
                }
                Ok(_) => {}
                Err(e) => {
                    tracing::warn!(error = %e, "control stream ended");
                    return true;
                }
            },
            dg = link.conn.read_datagram() => match dg {
                Ok(bytes) => {
                    match Header::parse(&bytes) {
                        Ok((h, _opus)) => {
                            if h.is_first() {
                                let _ = events.send(Event::RxStart { freq_khz: h.freq_khz, speaker: h.speaker });
                            }
                            if h.is_last() {
                                let _ = events.send(Event::RxEnd { freq_khz: h.freq_khz, speaker: h.speaker, frames: 0, secs: 0.0 });
                            }
                            // 解码与混音在 Task 12 接入音频输出时串起来。
                        }
                        Err(e) => tracing::debug!(error = %e, "dropping an unparsable datagram"),
                    }
                }
                Err(e) => {
                    tracing::warn!(error = %e, "datagram stream ended");
                    return true;
                }
            },
        }
    }
}
```

- [ ] **Step 2: 写端到端测试**

`crates/can-voice-client/tests/e2e.rs`：

```rust
//! 端到端：真的起一个 P2 的服务端，跑一遍握手、订阅、收发。
//!
//! 需要 `cargo build` 过的服务端二进制。CI 里两者在同一个仓库，
//! 所以这个测试是可以在 CI 跑的 —— 它是唯一能发现各层拼接错误的测试。

use std::process::{Child, Command as Proc};
use std::time::Duration;

struct Server(Child);

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.0.kill();
    }
}

/// 起一个服务端。测试用自签名证书，所以本测试只在 `CAN_VOICE_E2E=1` 时跑 ——
/// 它需要先用 Go 构建服务端。
fn start_server() -> Option<(Server, String)> {
    if std::env::var("CAN_VOICE_E2E").is_err() {
        eprintln!("skipping e2e: set CAN_VOICE_E2E=1 to run it");
        return None;
    }
    // 由 CI 预先构建到 target/e2e/can-voice。
    let child = Proc::new("../../target/e2e/can-voice")
        .env("CAN_VOICE_ADDR", "127.0.0.1:64738")
        .env("CAN_VOICE_TLS_CERT", "../../target/e2e/cert.pem")
        .env("CAN_VOICE_TLS_KEY", "../../target/e2e/key.pem")
        .env("CAN_VOICE_API_PUBKEY", std::fs::read_to_string("../../target/e2e/api.pub").ok()?.trim().to_string())
        .spawn()
        .ok()?;
    std::thread::sleep(Duration::from_millis(500));
    Some((Server(child), "127.0.0.1:64738".to_string()))
}

#[tokio::test]
async fn a_client_can_hand_shake_subscribe_and_receive() {
    let Some((_srv, addr)) = start_server() else { return };

    let token = std::fs::read_to_string("../../target/e2e/token.txt").expect("token fixture");
    let cfg = can_voice_client::Config {
        server: addr,
        server_name: "localhost".into(),
        token: token.trim().into(),
        client_id: "e2e-test/1".into(),
        follow: String::new(),
        input_device: None,
        output_device: None,
    };
    let client = can_voice_client::VoiceClient::connect(cfg).await.expect("connect");
    let mut events = client.events();

    client.set_subscription(can_voice_proto::control::Sub {
        rx: vec![118_000, 121_800],
        tx: vec![121_800],
        ..Default::default()
    });

    // 应当先看到 Online。
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    let mut saw_online = false;
    while tokio::time::Instant::now() < deadline {
        match tokio::time::timeout(Duration::from_millis(500), events.recv()).await {
            Ok(Ok(can_voice_client::Event::State(can_voice_client::LinkState::Online))) => {
                saw_online = true;
                break;
            }
            Ok(Ok(_)) => continue,
            _ => continue,
        }
    }
    assert!(saw_online, "the client never reported Online");
    client.shutdown().await;
}
```

- [ ] **Step 3: 写命令行客户端**

`crates/can-voice-client/examples/canvoice-cli.rs`：

```rust
//! 手工验证用的命令行客户端。
//!
//!     cargo run -p can-voice-client --example canvoice-cli -- \
//!         --server audio.ceruleanavi.net:64738 --token "$TOKEN" --rx 118000,121800

use std::time::Duration;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt().with_env_filter("info").init();

    let mut server = String::from("127.0.0.1:64738");
    let mut token = String::new();
    let mut rx: Vec<u32> = Vec::new();
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--server" => server = args.next().unwrap_or_default(),
            "--token" => token = args.next().unwrap_or_default(),
            "--rx" => {
                rx = args
                    .next()
                    .unwrap_or_default()
                    .split(',')
                    .filter_map(|s| s.trim().parse().ok())
                    .collect()
            }
            other => eprintln!("unknown argument {other}"),
        }
    }
    if token.is_empty() {
        eprintln!("--token is required");
        std::process::exit(2);
    }

    let server_name = server.split(':').next().unwrap_or("localhost").to_string();
    let cfg = can_voice_client::Config {
        server,
        server_name,
        token,
        client_id: concat!("canvoice-cli/", env!("CARGO_PKG_VERSION")).into(),
        follow: String::new(),
        input_device: None,
        output_device: None,
    };

    let client = can_voice_client::VoiceClient::connect(cfg).await.expect("connect");
    let mut events = client.events();
    client.set_subscription(can_voice_proto::control::Sub { rx, ..Default::default() });

    loop {
        match tokio::time::timeout(Duration::from_secs(30), events.recv()).await {
            Ok(Ok(e)) => tracing::info!(?e, "event"),
            Ok(Err(_)) => break,
            Err(_) => tracing::info!("no events for 30 s"),
        }
    }
}
```

在 `Cargo.toml` 的 `[dev-dependencies]` 加：

```toml
tracing-subscriber = { version = "0.3", features = ["env-filter"] }
tokio = { version = "1", features = ["full"] }
```

- [ ] **Step 4: 准备 e2e 夹具并跑一次**

```bash
# 服务端二进制
mkdir -p target/e2e
go build -o target/e2e/can-voice ./server/cmd/can-voice
# 自签名证书
openssl req -x509 -newkey rsa:2048 -nodes -keyout target/e2e/key.pem \
  -out target/e2e/cert.pem -days 1 -subj "/CN=localhost"
# Ed25519 密钥对与一个 token —— 用 P2 的 auth 包生成
go run ./server/cmd/can-voice-e2e-fixture   # 见下
```

需要一个小工具产出 `api.pub` 和 `token.txt`。新建
`server/cmd/can-voice-e2e-fixture/main.go`：

```go
// can-voice-e2e-fixture 为端到端测试生成一对 Ed25519 密钥和一个 token。
package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"fmt"
	"os"
	"time"

	"github.com/JianyueLab-Org/can-voice/server/internal/auth"
)

func main() {
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		panic(err)
	}
	tok, err := auth.Sign(priv, auth.Claims{
		CID: "1000", Rating: 5, MaxTX: 8, Exp: time.Now().Add(time.Hour).Unix(),
	})
	if err != nil {
		panic(err)
	}
	must(os.WriteFile("target/e2e/api.pub", []byte(base64.StdEncoding.EncodeToString(pub)), 0o644))
	must(os.WriteFile("target/e2e/token.txt", []byte(tok), 0o644))
	fmt.Println("wrote target/e2e/api.pub and target/e2e/token.txt")
}

func must(err error) {
	if err != nil {
		panic(err)
	}
}
```

然后：

```bash
CAN_VOICE_E2E=1 cargo test -p can-voice-client --test e2e -- --nocapture
```

Expected: PASS，日志里看到 `voice session established`。

- [ ] **Step 5: 手工验证命令行客户端**

一个终端起服务端，另一个：

```bash
cargo run -p can-voice-client --example canvoice-cli -- \
  --server 127.0.0.1:64738 --token "$(cat target/e2e/token.txt)" --rx 118000,121800
```

Expected: 日志里出现 `State(Online)`，服务端日志里出现 `session opened` 与
`subscription replaced`。

- [ ] **Step 6: 跑全部测试**

Run: `cargo test --workspace && cargo clippy --workspace -- -D warnings && go test ./server/... -race`
Expected: 全绿

- [ ] **Step 7: 提交**

```bash
git add crates/can-voice-client/ server/cmd/can-voice-e2e-fixture/
git commit -m "client: 后台任务、命令行客户端与端到端测试"
```

---

## 完成标准

- [ ] `cargo test --workspace` 全绿，`cargo clippy --workspace -- -D warnings` 干净
- [ ] Rust 与 Go 都通过 `server/testdata/wire-golden.json` 的同一批用例
- [ ] 公开 API 里没有 `join` / `leave` / `channel_id`，且有一个测试盯着这条
- [ ] libopus 静态链接进二进制（`otool -L` / `ldd` 看不到动态 libopus）
- [ ] 端到端测试证明 Rust 客户端能与 Go 服务端握手、订阅并收到确认
- [ ] 抖动缓冲、混音、干扰音、无线电栈、订阅状态机、重连策略全部有不碰网络和声卡的单元测试

## 尚未覆盖，留给后续

本计划止于"链路通、订阅生效、事件流可用"。下面这些属于 P4 各客户端接入时的工作，
在此登记以免被当成遗漏：

- **音频输入输出的实际串联** —— `cpal` 的采集回调接 `tx::Encoder`、解码结果接输出流。
  Task 11 的 `pump` 里留了两处注释标明位置。
- **每个 `(speaker, freq)` 的解码器与抖动缓冲实例管理** —— 零件（Task 4、6）齐了，
  把它们按 speaker 分桶的那一层还没写。
- **`Event::Health` 的实际填充** —— `Ping`/`Pong` 已在协议里，RTT 与收发计数的采集未做。
- **PTT** —— 现有 `ptt.py` 的键盘/手柄/鼠标三路输入，以及那个 SDL 线程陷阱
  （首次初始化 SDL 的线程决定了隐藏窗口的归属，错了会让手柄 PTT 在本次运行后永久失效）。
  它属于 Tauri 应用层，不属于本库。

## 自检记录

| spec 节 | 覆盖于 |
|---|---|
| §5.1 控制面 | Task 1 |
| §5.2 数据面包头 | Task 2（与 Go 共测黄金文件） |
| §9.1 声明式 API | Task 7、Task 10 |
| §9.2 抖动缓冲 | Task 4 |
| §9.3 混音、干扰音、衰减 | Task 5 |
| 无线电栈耦合规则 | Task 3 |
| 有界重连（3 次，首次不重试） | Task 8 |
| Opus 48 kHz 20 ms | Task 6 |
| §10 测试策略（黄金文件、纯逻辑、端到端） | Task 2、3–8、11 |
