# P2：Go 语音服务端 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 can-voice 服务端 —— 一个无状态的 QUIC 语音扇出服务，按频率路由 Opus 包，不解码音频，按射程过滤并标注信号质量。

**Architecture:** 一条 QUIC 连接承载全部：控制面走 bidirectional stream（长度前缀 JSON），音频走 unreliable datagram（13 字节头 + Opus 载荷）。服务端持三张内存表（会话、频率→订阅者、交叉耦合）加一份来自 can-fsd SSE 的位置快照。收到上行 datagram 只做四件事：校验发送权、查订阅者、算信号质量、原样转发。无数据库、无持久化、无 ACL。

**Tech Stack:** Go 1.23+、quic-go v0.48.x、标准库 `crypto/ed25519`、`encoding/json`

**Spec:** `docs/superpowers/specs/2026-09-12-can-voice-design.md`

**前置依赖:** P1 的结论文档 `docs/p1-connectivity-findings.md`。如果它判定"必须实现 stream 回退通道"，本计划末尾的 Task 12 生效；否则跳过 Task 12。

## Global Constraints

- **服务端绝不解码音频。** 它没有 Opus 依赖，datagram 的载荷部分对它是不透明字节。任何引入音频解码的改动都违背设计（spec §2、§8）。
- **服务端无持久化。** 没有数据库、没有文件状态、没有 ACL。进程重启等于所有人重连并重发 `SUB`。
- **`SUB` 是全量声明，不是增量。** 收到即整体替换该会话的订阅集合。这是消除 sync 风暴的根本机制（spec §5.1），任何"增量更新订阅"的实现都是 bug。
- **包头布局是跨实现契约**，由 `testdata/wire-golden.json` 钉住。Rust 侧（P3）测同一份文件。改这个文件等于改协议。
- 字段与常量的权威值（spec §5.2、§7.4）：包头 13 字节；`ver` 恒为 1；`flags` bit0=首帧、bit1=尾帧；Opus 48 kHz 单声道 20 ms 帧；频率单位 kHz（`121800` = 121.800 MHz）。
- 信号质量曲线（spec §7.4）：`d/range ≤ 0.8` → `qual=255`；`0.8→1.1` 线性 `255→0`；`>1.1` 不扇出。
- **日志文本与日志字段名一律英文**（沿用 can-audio 的既有约定：日志是英文，面向用户的是中文）。服务端没有面向用户的字符串。
- Go 代码注释用中文，与 can-fsd 一致。
- 每个任务结束时 `go vet ./...` 与 `go test ./...` 都必须干净。

---

## 文件结构

```
can-voice/
  go.mod
  server/
    cmd/can-voice/main.go       进程入口、配置、信号处理
    internal/wire/              数据面包头编解码（跨实现契约）
      header.go
      header_test.go
    internal/control/           控制面消息与长度前缀帧
      message.go
      frame.go
      *_test.go
    internal/auth/              Ed25519 token 验签
      token.go
      token_test.go
    internal/router/            会话、订阅表、扇出、交叉耦合
      session.go
      router.go
      *_test.go
    internal/geo/               射程与信号质量
      range.go
      range_test.go
    internal/fsdfeed/           can-fsd SSE 消费与位置快照
      feed.go
      feed_test.go
    testdata/
      wire-golden.json          跨实现黄金文件
      datafeed_sample.json      从 can-fsd 复制的 datafeed golden
```

按职责切分而不是按技术分层：`wire` 是契约，`router` 是业务，`geo` 是纯数学，`fsdfeed` 是外部输入。四者都能单独测试，`router` 是唯一需要组合它们的地方。

---

### Task 1: 数据面包头与跨实现黄金文件

这是整个协议的地基，也是 P3 唯一需要逐字节对齐的东西。**黄金文件手写在先，实现在后** ——
反过来做的话，实现里的 bug 会被写进黄金文件，然后 Rust 侧照着错的实现，两边一致地错。

**Files:**
- Create: `server/internal/wire/header.go`
- Create: `server/internal/wire/header_test.go`
- Create: `server/testdata/wire-golden.json`

**Interfaces:**
- Consumes: 无
- Produces:
  - `const HeaderSize = 13`
  - `const (FlagFirst uint8 = 1 << 0; FlagLast uint8 = 1 << 1)`
  - `type Header struct { Ver, Flags, Qual uint8; Seq uint16; FreqKHz, Speaker uint32 }`
  - `func (h Header) AppendTo(dst []byte) []byte`
  - `func Parse(b []byte) (Header, []byte, error)` —— 返回头和 Opus 载荷切片

- [ ] **Step 1: 手写黄金文件**

`server/testdata/wire-golden.json`。每个用例的 `encoded_hex` 是**手工按 spec §5.2 的布局
推出来的**，不是从任何实现里导出的：

```json
{
  "comment": "can-voice 数据面包头的跨实现黄金文件。Go 与 Rust 两侧都测这份。改它等于改协议。布局见 spec 5.2：ver(1) flags(1) qual(1) seq(2 BE) freq_khz(4 BE) speaker(4 BE)，其后是 Opus 载荷。",
  "header_size": 13,
  "cases": [
    {
      "name": "上行首帧：客户端填 qual=0 speaker=0",
      "header": {"ver": 1, "flags": 1, "qual": 0, "seq": 0, "freq_khz": 121800, "speaker": 0},
      "opus_hex": "fc",
      "encoded_hex": "01010000000001dbc800000000fc"
    },
    {
      "name": "下行普通帧：服务端填了 qual 和 speaker",
      "header": {"ver": 1, "flags": 0, "qual": 255, "seq": 1234, "freq_khz": 118000, "speaker": 42},
      "opus_hex": "78563412",
      "encoded_hex": "0100ff04d20001ccf00000002a78563412"
    },
    {
      "name": "尾帧：flags bit1",
      "header": {"ver": 1, "flags": 2, "qual": 128, "seq": 65535, "freq_khz": 136975, "speaker": 4294967295},
      "opus_hex": "",
      "encoded_hex": "010280ffff0002170fffffffff"
    },
    {
      "name": "首帧且尾帧：单帧发言",
      "header": {"ver": 1, "flags": 3, "qual": 200, "seq": 0, "freq_khz": 199998, "speaker": 1},
      "opus_hex": "00",
      "encoded_hex": "0103c8000000030d3e0000000100"
    }
  ]
}
```

核对第二个用例：`01`(ver) `00`(flags) `ff`(qual=255) `04d2`(seq=1234) `0001ccf0`(freq=118000)
`0000002a`(speaker=42) `78563412`(opus)。118000 = 0x1CCF0 ✓。

- [ ] **Step 2: 写失败的测试**

`server/internal/wire/header_test.go`：

```go
package wire

import (
	"encoding/hex"
	"encoding/json"
	"os"
	"testing"
)

type goldenFile struct {
	HeaderSize int `json:"header_size"`
	Cases      []struct {
		Name   string `json:"name"`
		Header struct {
			Ver     uint8  `json:"ver"`
			Flags   uint8  `json:"flags"`
			Qual    uint8  `json:"qual"`
			Seq     uint16 `json:"seq"`
			FreqKHz uint32 `json:"freq_khz"`
			Speaker uint32 `json:"speaker"`
		} `json:"header"`
		OpusHex    string `json:"opus_hex"`
		EncodedHex string `json:"encoded_hex"`
	} `json:"cases"`
}

func loadGolden(t *testing.T) goldenFile {
	t.Helper()
	b, err := os.ReadFile("../../testdata/wire-golden.json")
	if err != nil {
		t.Fatalf("read golden: %v", err)
	}
	var g goldenFile
	if err := json.Unmarshal(b, &g); err != nil {
		t.Fatalf("parse golden: %v", err)
	}
	if len(g.Cases) == 0 {
		t.Fatal("golden file has no cases")
	}
	return g
}

func TestHeaderSizeMatchesGolden(t *testing.T) {
	if g := loadGolden(t); g.HeaderSize != HeaderSize {
		t.Fatalf("HeaderSize = %d, golden says %d", HeaderSize, g.HeaderSize)
	}
}

func TestAppendToMatchesGolden(t *testing.T) {
	for _, c := range loadGolden(t).Cases {
		t.Run(c.Name, func(t *testing.T) {
			opus, err := hex.DecodeString(c.OpusHex)
			if err != nil {
				t.Fatalf("bad opus_hex: %v", err)
			}
			h := Header{
				Ver: c.Header.Ver, Flags: c.Header.Flags, Qual: c.Header.Qual,
				Seq: c.Header.Seq, FreqKHz: c.Header.FreqKHz, Speaker: c.Header.Speaker,
			}
			got := hex.EncodeToString(append(h.AppendTo(nil), opus...))
			if got != c.EncodedHex {
				t.Fatalf("encoded = %s, golden = %s", got, c.EncodedHex)
			}
		})
	}
}

func TestParseMatchesGolden(t *testing.T) {
	for _, c := range loadGolden(t).Cases {
		t.Run(c.Name, func(t *testing.T) {
			raw, err := hex.DecodeString(c.EncodedHex)
			if err != nil {
				t.Fatalf("bad encoded_hex: %v", err)
			}
			h, opus, err := Parse(raw)
			if err != nil {
				t.Fatalf("Parse: %v", err)
			}
			if h.Ver != c.Header.Ver || h.Flags != c.Header.Flags || h.Qual != c.Header.Qual ||
				h.Seq != c.Header.Seq || h.FreqKHz != c.Header.FreqKHz || h.Speaker != c.Header.Speaker {
				t.Fatalf("header = %+v, golden = %+v", h, c.Header)
			}
			if hex.EncodeToString(opus) != c.OpusHex {
				t.Fatalf("opus = %s, golden = %s", hex.EncodeToString(opus), c.OpusHex)
			}
		})
	}
}

func TestParseRejectsShortPacket(t *testing.T) {
	if _, _, err := Parse(make([]byte, HeaderSize-1)); err == nil {
		t.Fatal("Parse must reject a packet shorter than the header")
	}
}

func TestParseRejectsUnknownVersion(t *testing.T) {
	b := Header{Ver: 2, FreqKHz: 118000}.AppendTo(nil)
	if _, _, err := Parse(b); err == nil {
		t.Fatal("Parse must reject a version it does not know; silently accepting it would mean decoding a future layout as if it were this one")
	}
}

func TestParseAcceptsHeaderWithNoPayload(t *testing.T) {
	// 尾帧可以不带 Opus 载荷。
	b := Header{Ver: 1, Flags: FlagLast, FreqKHz: 118000}.AppendTo(nil)
	_, opus, err := Parse(b)
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if len(opus) != 0 {
		t.Fatalf("opus = %v, want empty", opus)
	}
}
```

- [ ] **Step 3: 跑测试确认失败**

Run: `go test ./server/internal/wire/ -v`
Expected: FAIL，`undefined: HeaderSize`、`Header`、`Parse`

- [ ] **Step 4: 实现**

`server/internal/wire/header.go`：

```go
// Package wire 是 can-voice 数据面的包头编解码。
//
// 这是一个跨实现契约：Rust 侧（can-voice-client）有一份独立实现，两边都测
// testdata/wire-golden.json。改这里的布局等于改协议，必须同时改黄金文件和 Rust 侧。
package wire

import (
	"encoding/binary"
	"fmt"
)

// HeaderSize 是数据面包头的字节数。布局见 spec 5.2：
//
//	 0        1        2        3               5               9              13
//	 +--------+--------+--------+---------------+---------------+--------------+
//	 |  ver   | flags  |  qual  |    seq(2)     |  freq_khz(4)  | speaker(4)   | opus…
//	 +--------+--------+--------+---------------+---------------+--------------+
const HeaderSize = 13

// Version 是本实现能处理的唯一协议版本。
const Version uint8 = 1

// flags 位。
const (
	// FlagFirst 标记一次发言的首帧：接收端据此立刻点亮 RX 指示灯并重置抖动缓冲。
	FlagFirst uint8 = 1 << 0
	// FlagLast 标记一次发言的尾帧：接收端据此立刻熄灭 RX 指示灯，
	// 而不是靠一个超时循环——那会让指示灯在松开 PTT 后多亮半秒。
	FlagLast uint8 = 1 << 1
)

// Header 是数据面包头。
//
// Qual 与 Speaker 上行时由客户端填 0，由服务端填入后再扇出：
// 客户端因此不知道任何人的位置，而 Speaker 让接收端能分辨
// "同一频率上有两个人在讲"与"一个人的包乱序了"。
type Header struct {
	Ver     uint8
	Flags   uint8
	Qual    uint8
	Seq     uint16
	FreqKHz uint32
	Speaker uint32
}

// AppendTo 把包头追加到 dst 并返回新切片。
func (h Header) AppendTo(dst []byte) []byte {
	var b [HeaderSize]byte
	b[0] = h.Ver
	b[1] = h.Flags
	b[2] = h.Qual
	binary.BigEndian.PutUint16(b[3:5], h.Seq)
	binary.BigEndian.PutUint32(b[5:9], h.FreqKHz)
	binary.BigEndian.PutUint32(b[9:13], h.Speaker)
	return append(dst, b[:]...)
}

// Parse 解出包头，并返回其后的 Opus 载荷（原切片的子切片，不复制）。
// 载荷可以为空——尾帧不必携带音频。
func Parse(b []byte) (Header, []byte, error) {
	if len(b) < HeaderSize {
		return Header{}, nil, fmt.Errorf("packet is %d bytes, need at least %d", len(b), HeaderSize)
	}
	h := Header{
		Ver:     b[0],
		Flags:   b[1],
		Qual:    b[2],
		Seq:     binary.BigEndian.Uint16(b[3:5]),
		FreqKHz: binary.BigEndian.Uint32(b[5:9]),
		Speaker: binary.BigEndian.Uint32(b[9:13]),
	}
	// 版本不认识就拒绝：默默接受等于把未来的布局当成现在的来解，
	// 那会表现为音频乱码而不是一条清晰的错误。
	if h.Ver != Version {
		return Header{}, nil, fmt.Errorf("unknown protocol version %d, this build speaks %d", h.Ver, Version)
	}
	return h, b[HeaderSize:], nil
}
```

- [ ] **Step 5: 跑测试确认通过**

Run: `go test ./server/internal/wire/ -v && go vet ./server/...`
Expected: PASS，vet 干净

- [ ] **Step 6: 提交**

```bash
git add server/internal/wire/ server/testdata/wire-golden.json
git commit -m "wire: 数据面包头与跨实现黄金文件"
```

---

### Task 2: 控制面消息与长度前缀帧

**Files:**
- Create: `server/internal/control/frame.go`
- Create: `server/internal/control/message.go`
- Create: `server/internal/control/frame_test.go`
- Create: `server/internal/control/message_test.go`

**Interfaces:**
- Consumes: 无
- Produces:
  - `func WriteFrame(w io.Writer, b []byte) error` / `func ReadFrame(r io.Reader) ([]byte, error)` —— 4 字节大端长度前缀，上限 `MaxFrame = 64 << 10`
  - 消息类型（均带 `Type` 判别字段）：`Hello`、`Ready`、`Sub`、`SubAck`、`Notice`、`Ping`、`Pong`、`Bye`
  - `func Decode(b []byte) (any, error)` —— 按 `type` 字段分发
  - `func Encode(m any) ([]byte, error)`

控制面长度前缀用 4 字节而不是数据面那样的 2 字节：`SUB` 携带订阅列表，一个管制员可能订阅
几十个频率，2 字节虽然也够但没有理由把上限压到 64 KB 以下。

- [ ] **Step 1: 写失败的测试**

`server/internal/control/frame_test.go`：

```go
package control

import (
	"bytes"
	"strings"
	"testing"
)

func TestFrameRoundTrip(t *testing.T) {
	var buf bytes.Buffer
	payload := []byte(`{"type":"PING","t":1}`)
	if err := WriteFrame(&buf, payload); err != nil {
		t.Fatalf("WriteFrame: %v", err)
	}
	got, err := ReadFrame(&buf)
	if err != nil {
		t.Fatalf("ReadFrame: %v", err)
	}
	if string(got) != string(payload) {
		t.Fatalf("frame = %q, want %q", got, payload)
	}
}

func TestWriteFrameRejectsOversizedPayload(t *testing.T) {
	var buf bytes.Buffer
	if err := WriteFrame(&buf, make([]byte, MaxFrame+1)); err == nil {
		t.Fatal("WriteFrame must reject a payload over MaxFrame")
	}
}

func TestReadFrameRejectsOversizedLengthPrefix(t *testing.T) {
	// 一个恶意的长度前缀不能让服务端去分配 4 GB。
	hdr := []byte{0xff, 0xff, 0xff, 0xff}
	if _, err := ReadFrame(bytes.NewReader(hdr)); err == nil {
		t.Fatal("ReadFrame must reject a length prefix over MaxFrame before allocating")
	} else if !strings.Contains(err.Error(), "limit") {
		t.Fatalf("error should name the limit, got %v", err)
	}
}
```

`server/internal/control/message_test.go`：

```go
package control

import "testing"

func TestDecodeDispatchesOnTypeField(t *testing.T) {
	b := []byte(`{"type":"HELLO","token":"abc","client":"can-controller/3.0.0","proto":1}`)
	m, err := Decode(b)
	if err != nil {
		t.Fatalf("Decode: %v", err)
	}
	h, ok := m.(*Hello)
	if !ok {
		t.Fatalf("Decode returned %T, want *Hello", m)
	}
	if h.Token != "abc" || h.Proto != 1 {
		t.Fatalf("Hello = %+v", h)
	}
}

func TestDecodeRejectsUnknownType(t *testing.T) {
	if _, err := Decode([]byte(`{"type":"NOPE"}`)); err == nil {
		t.Fatal("Decode must reject an unknown message type")
	}
}

func TestSubCarriesFullDeclarationNotADelta(t *testing.T) {
	// SUB 是全量声明。它的字段名与结构不能暗示增量语义——
	// 没有 add/remove，只有完整的 rx/tx/xc 三张表。
	b := []byte(`{"type":"SUB","rx":[118000,121800],"tx":[121800],"xc":[[121800,124550]]}`)
	m, err := Decode(b)
	if err != nil {
		t.Fatalf("Decode: %v", err)
	}
	s := m.(*Sub)
	if len(s.RX) != 2 || s.RX[0] != 118000 || s.RX[1] != 121800 {
		t.Fatalf("RX = %v", s.RX)
	}
	if len(s.TX) != 1 || s.TX[0] != 121800 {
		t.Fatalf("TX = %v", s.TX)
	}
	if len(s.XC) != 1 || s.XC[0] != [2]uint32{121800, 124550} {
		t.Fatalf("XC = %v", s.XC)
	}
}

func TestEncodeRoundTripsThroughDecode(t *testing.T) {
	in := &Ready{Type: "READY", Session: 7, Server: "can-voice/1.0.0", MaxTX: 8, MaxRX: 32}
	b, err := Encode(in)
	if err != nil {
		t.Fatalf("Encode: %v", err)
	}
	m, err := Decode(b)
	if err != nil {
		t.Fatalf("Decode: %v", err)
	}
	out := m.(*Ready)
	if *out != *in {
		t.Fatalf("round trip: %+v != %+v", out, in)
	}
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `go test ./server/internal/control/ -v`
Expected: FAIL，一堆 undefined

- [ ] **Step 3: 实现**

`server/internal/control/frame.go`：

```go
// Package control 是 can-voice 的控制面：长度前缀 JSON 帧。
//
// 控制面刻意用 JSON 而不是 protobuf：消息频率极低（登录、订阅变更、偶发通知），
// 可读性和排障便利压过字节效率——出问题时要能直接在日志里读懂发生了什么。
// 高频的音频走 internal/wire 的紧凑二进制。
package control

import (
	"encoding/binary"
	"fmt"
	"io"
)

// MaxFrame 是一个控制帧的上限。SUB 会携带订阅列表，
// 一个管制员可能订阅几十个频率，64 KB 留了充足余量。
const MaxFrame = 64 << 10

// WriteFrame 写一个 4 字节大端长度前缀加载荷。
func WriteFrame(w io.Writer, b []byte) error {
	if len(b) > MaxFrame {
		return fmt.Errorf("control frame of %d bytes exceeds the %d byte limit", len(b), MaxFrame)
	}
	var hdr [4]byte
	binary.BigEndian.PutUint32(hdr[:], uint32(len(b)))
	if _, err := w.Write(hdr[:]); err != nil {
		return err
	}
	_, err := w.Write(b)
	return err
}

// ReadFrame 读一个长度前缀帧。
// 长度检查在分配之前：一个恶意的前缀否则能让服务端去要 4 GB。
func ReadFrame(r io.Reader) ([]byte, error) {
	var hdr [4]byte
	if _, err := io.ReadFull(r, hdr[:]); err != nil {
		return nil, err
	}
	n := binary.BigEndian.Uint32(hdr[:])
	if n > MaxFrame {
		return nil, fmt.Errorf("control frame claims %d bytes, over the %d byte limit", n, MaxFrame)
	}
	b := make([]byte, n)
	if _, err := io.ReadFull(r, b); err != nil {
		return nil, err
	}
	return b, nil
}
```

`server/internal/control/message.go`：

```go
package control

import (
	"encoding/json"
	"fmt"
)

// Hello 是客户端的第一条消息。Follow 只有观察员模式用——
// 观察员没有 FSD 连接，位置取自它跟随的那架飞机（spec 7.3）。
type Hello struct {
	Type   string `json:"type"`
	Token  string `json:"token"`
	Client string `json:"client"`
	Proto  int    `json:"proto"`
	Follow string `json:"follow,omitempty"`
}

// Ready 是服务端对 Hello 的回应。
// MaxTX 在这里只是回显，权威值在 token 里（spec 6）。
type Ready struct {
	Type    string `json:"type"`
	Session uint32 `json:"session"`
	Server  string `json:"server"`
	MaxTX   int    `json:"max_tx"`
	MaxRX   int    `json:"max_rx"`
}

// Sub 是**全量**收发声明，不是增量。服务端收到即整体替换该会话的订阅集合。
// 这是消除 sync 风暴的根本机制：幂等，无状态推导，重连后重发一次即恢复。
// 刻意没有 add/remove 字段——任何增量语义都会把那一类 bug 请回来。
type Sub struct {
	Type string      `json:"type"`
	RX   []uint32    `json:"rx"`
	TX   []uint32    `json:"tx"`
	XC   [][2]uint32 `json:"xc"`
}

// SubAck 回显服务端实际接受的集合，并列出被拒的频率。
type SubAck struct {
	Type     string   `json:"type"`
	RX       []uint32 `json:"rx"`
	TX       []uint32 `json:"tx"`
	Rejected []uint32 `json:"rejected"`
}

// Notice 是服务端的单向通知，Kind 取值见 KindTxDenied 等常量。
type Notice struct {
	Type   string `json:"type"`
	Kind   string `json:"kind"`
	Freq   uint32 `json:"freq,omitempty"`
	Reason string `json:"reason,omitempty"`
}

// Notice 的 Kind 取值。
const (
	KindTxDenied         = "tx_denied"
	KindRangeUnavailable = "range_unavailable"
	KindSubRejected      = "sub_rejected"
)

// Ping/Pong 只用来测 RTT；保活由 QUIC 自己做。
type Ping struct {
	Type string `json:"type"`
	T    int64  `json:"t"`
}

type Pong struct {
	Type    string `json:"type"`
	T       int64  `json:"t"`
	ServerT int64  `json:"server_t"`
}

// Bye 是服务端主动断开前的最后一条消息。
type Bye struct {
	Type   string `json:"type"`
	Reason string `json:"reason"`
}

// Encode 把消息编成 JSON，并填好 Type 判别字段。
func Encode(m any) ([]byte, error) {
	switch v := m.(type) {
	case *Hello:
		v.Type = "HELLO"
	case *Ready:
		v.Type = "READY"
	case *Sub:
		v.Type = "SUB"
	case *SubAck:
		v.Type = "SUBACK"
	case *Notice:
		v.Type = "NOTICE"
	case *Ping:
		v.Type = "PING"
	case *Pong:
		v.Type = "PONG"
	case *Bye:
		v.Type = "BYE"
	default:
		return nil, fmt.Errorf("cannot encode %T as a control message", m)
	}
	return json.Marshal(m)
}

// Decode 按 type 字段分发。未知类型直接拒绝，不静默忽略——
// 静默忽略会让一个拼错的类型表现为"消息发出去了但什么都没发生"。
func Decode(b []byte) (any, error) {
	var probe struct {
		Type string `json:"type"`
	}
	if err := json.Unmarshal(b, &probe); err != nil {
		return nil, fmt.Errorf("control frame is not valid JSON: %w", err)
	}
	var m any
	switch probe.Type {
	case "HELLO":
		m = &Hello{}
	case "READY":
		m = &Ready{}
	case "SUB":
		m = &Sub{}
	case "SUBACK":
		m = &SubAck{}
	case "NOTICE":
		m = &Notice{}
	case "PING":
		m = &Ping{}
	case "PONG":
		m = &Pong{}
	case "BYE":
		m = &Bye{}
	default:
		return nil, fmt.Errorf("unknown control message type %q", probe.Type)
	}
	if err := json.Unmarshal(b, m); err != nil {
		return nil, fmt.Errorf("decode %s: %w", probe.Type, err)
	}
	return m, nil
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `go test ./server/internal/control/ -v && go vet ./server/...`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add server/internal/control/
git commit -m "control: 长度前缀 JSON 控制面消息"
```

---

### Task 3: Ed25519 token 验签

**Files:**
- Create: `server/internal/auth/token.go`
- Create: `server/internal/auth/token_test.go`

**Interfaces:**
- Consumes: 无
- Produces:
  - `type Claims struct { CID string; Rating int; MaxTX int; Exp int64 }`
  - `func Sign(priv ed25519.PrivateKey, c Claims) (string, error)` —— 仅供测试与 can-api 参考实现
  - `func Verify(pub ed25519.PublicKey, token string, now time.Time) (Claims, error)`

token 格式：`base64url(claims_json) + "." + base64url(signature)`。不用 JWT 库 —— 只有一种
算法、一种用途，一个几十行的实现比一个可配置算法的库更难出错（JWT 的 `alg: none` 类漏洞
在这里根本不可能存在）。

- [ ] **Step 1: 写失败的测试**

`server/internal/auth/token_test.go`：

```go
package auth

import (
	"crypto/ed25519"
	"crypto/rand"
	"strings"
	"testing"
	"time"
)

func keys(t *testing.T) (ed25519.PublicKey, ed25519.PrivateKey) {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatalf("GenerateKey: %v", err)
	}
	return pub, priv
}

func TestVerifyAcceptsAFreshToken(t *testing.T) {
	pub, priv := keys(t)
	now := time.Unix(1757000000, 0)
	in := Claims{CID: "1000", Rating: 5, MaxTX: 8, Exp: now.Add(60 * time.Second).Unix()}
	tok, err := Sign(priv, in)
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	got, err := Verify(pub, tok, now)
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	if got != in {
		t.Fatalf("claims = %+v, want %+v", got, in)
	}
}

func TestVerifyRejectsAnExpiredToken(t *testing.T) {
	pub, priv := keys(t)
	now := time.Unix(1757000000, 0)
	tok, _ := Sign(priv, Claims{CID: "1000", Exp: now.Add(-time.Second).Unix()})
	if _, err := Verify(pub, tok, now); err == nil {
		t.Fatal("Verify must reject an expired token")
	}
}

func TestVerifyRejectsATamperedPayload(t *testing.T) {
	pub, priv := keys(t)
	now := time.Unix(1757000000, 0)
	tok, _ := Sign(priv, Claims{CID: "1000", Rating: 1, Exp: now.Add(60 * time.Second).Unix()})

	// 改一个字符，签名必须失配。
	parts := strings.SplitN(tok, ".", 2)
	tampered := parts[0][:len(parts[0])-1] + "A" + "." + parts[1]
	if tampered == tok {
		t.Fatal("test did not actually change the payload")
	}
	if _, err := Verify(pub, tampered, now); err == nil {
		t.Fatal("Verify must reject a tampered payload")
	}
}

func TestVerifyRejectsATokenSignedByAnotherKey(t *testing.T) {
	pub, _ := keys(t)
	_, otherPriv := keys(t)
	now := time.Unix(1757000000, 0)
	tok, _ := Sign(otherPriv, Claims{CID: "1000", Exp: now.Add(60 * time.Second).Unix()})
	if _, err := Verify(pub, tok, now); err == nil {
		t.Fatal("Verify must reject a token signed by a key it does not trust")
	}
}

func TestVerifyRejectsMalformedTokens(t *testing.T) {
	pub, _ := keys(t)
	now := time.Unix(1757000000, 0)
	for _, tok := range []string{"", "nodot", "a.b.c", ".", "!!!.###"} {
		if _, err := Verify(pub, tok, now); err == nil {
			t.Fatalf("Verify must reject malformed token %q", tok)
		}
	}
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `go test ./server/internal/auth/ -v`
Expected: FAIL，`undefined: Claims`、`Sign`、`Verify`

- [ ] **Step 3: 实现**

`server/internal/auth/token.go`：

```go
// Package auth 校验 can-api 签发的短期语音 token。
//
// can-voice 持公钥本地验签，不回调 can-api——所以 can-api 挂掉不影响已连接的用户，
// 也不影响持有未过期 token 的重连。这与旧的 Ice 认证器架构相反：那里认证器一挂，
// 每次登录都被拒，而客户端显示的是"密码错误"（spec 6）。
//
// 格式是 base64url(claims_json) + "." + base64url(signature)。
// 不用 JWT 库：只有一种算法、一种用途，几十行的实现比一个可配置算法的库更难出错——
// JWT 那类 "alg: none" 的洞在这里根本不存在。
package auth

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

// Claims 是 token 的载荷。MaxTX 是这个会话允许同时发送的频率数上限，
// 权威值就是这里——控制面 READY 里的同名字段只是回显（spec 6）。
type Claims struct {
	CID    string `json:"cid"`
	Rating int    `json:"rating"`
	MaxTX  int    `json:"max_tx"`
	Exp    int64  `json:"exp"`
}

var enc = base64.RawURLEncoding

// Sign 签发一个 token。生产里由 can-api 做，这里的实现供测试与 can-api 对齐参考。
func Sign(priv ed25519.PrivateKey, c Claims) (string, error) {
	payload, err := json.Marshal(c)
	if err != nil {
		return "", err
	}
	body := enc.EncodeToString(payload)
	sig := ed25519.Sign(priv, []byte(body))
	return body + "." + enc.EncodeToString(sig), nil
}

// Verify 校验签名与有效期，返回载荷。
func Verify(pub ed25519.PublicKey, token string, now time.Time) (Claims, error) {
	body, sigPart, ok := strings.Cut(token, ".")
	if !ok || body == "" || sigPart == "" {
		return Claims{}, fmt.Errorf("token is not in the <payload>.<signature> form")
	}
	if strings.Contains(sigPart, ".") {
		return Claims{}, fmt.Errorf("token has more than two parts")
	}
	sig, err := enc.DecodeString(sigPart)
	if err != nil {
		return Claims{}, fmt.Errorf("token signature is not base64url: %w", err)
	}
	// 先验签再解载荷：解一个未经验证的 JSON 等于把攻击者的输入喂给解析器。
	if !ed25519.Verify(pub, []byte(body), sig) {
		return Claims{}, fmt.Errorf("token signature does not verify")
	}
	payload, err := enc.DecodeString(body)
	if err != nil {
		return Claims{}, fmt.Errorf("token payload is not base64url: %w", err)
	}
	var c Claims
	if err := json.Unmarshal(payload, &c); err != nil {
		return Claims{}, fmt.Errorf("token payload is not valid JSON: %w", err)
	}
	if now.Unix() >= c.Exp {
		return Claims{}, fmt.Errorf("token expired at %d, now is %d", c.Exp, now.Unix())
	}
	if c.CID == "" {
		return Claims{}, fmt.Errorf("token carries no cid")
	}
	return c, nil
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `go test ./server/internal/auth/ -v && go vet ./server/...`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add server/internal/auth/
git commit -m "auth: Ed25519 短期 token 验签"
```

---

### Task 4: 射程与信号质量

纯数学，无 I/O，无依赖。放在扇出之前实现，因为扇出要用它。

**Files:**
- Create: `server/internal/geo/range.go`
- Create: `server/internal/geo/range_test.go`

**Interfaces:**
- Consumes: 无
- Produces:
  - `func DistanceNM(lat1, lon1, lat2, lon2 float64) float64` —— 大圆距离
  - `func LineOfSightNM(alt1Ft, alt2Ft float64) float64` —— `1.23 × (√h₁ + √h₂)`
  - `func FallbackRangeNM(callsign string) float64` —— 席位后缀兜底表
  - `func Quality(distNM, rangeNM float64) (qual uint8, inRange bool)`
  - `const (CutoffRatio = 1.1; FullRatio = 0.8)`

- [ ] **Step 1: 写失败的测试**

`server/internal/geo/range_test.go`：

```go
package geo

import (
	"math"
	"testing"
)

func TestLineOfSightMatchesTheSpecExamples(t *testing.T) {
	// spec 7.2 的三个例子。
	cases := []struct {
		name       string
		a, b       float64
		wantAround float64
	}{
		{"FL350 对 100 英尺地面台", 35000, 100, 240},
		{"两架 FL350 之间", 35000, 35000, 460},
		{"地面两架之间", 0, 0, 0},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := LineOfSightNM(c.a, c.b)
			if math.Abs(got-c.wantAround) > 15 {
				t.Fatalf("LineOfSightNM(%v, %v) = %.0f, want about %.0f", c.a, c.b, got, c.wantAround)
			}
		})
	}
}

func TestFallbackRangeReadsTheSuffix(t *testing.T) {
	cases := map[string]float64{
		"ZSSS_GND":  15,
		"ZSSS_DEL":  15,
		"ZSPD_TWR":  30,
		"ZSSS_APP":  80,
		"ZGGG_DEP":  80,
		"ZSHA_CTR":  250,
		"PRC_FSS":   600,
		"ZSSS_ATIS": 60,
	}
	for cs, want := range cases {
		if got := FallbackRangeNM(cs); got != want {
			t.Fatalf("FallbackRangeNM(%q) = %v, want %v", cs, got, want)
		}
	}
}

func TestFallbackRangeHandlesAnUnknownSuffix(t *testing.T) {
	// 未知后缀必须给一个保守的非零值：给 0 会让那个席位谁都听不见，
	// 而这在语音系统里比听得太远糟糕得多。
	if got := FallbackRangeNM("ZSSS_XYZ"); got <= 0 {
		t.Fatalf("FallbackRangeNM on an unknown suffix = %v, must be a conservative non-zero default", got)
	}
}

func TestQualityIsFullInsideEightyPercent(t *testing.T) {
	for _, ratio := range []float64{0, 0.3, 0.79, 0.8} {
		q, in := Quality(ratio*100, 100)
		if !in || q != 255 {
			t.Fatalf("Quality at d/range=%.2f = (%d, %v), want (255, true)", ratio, q, in)
		}
	}
}

func TestQualityFallsLinearlyInTheEdgeBand(t *testing.T) {
	// 0.8 → 1.1 之间线性 255 → 0。中点 0.95 应当接近 127。
	q, in := Quality(95, 100)
	if !in {
		t.Fatal("d/range=0.95 must still be in range")
	}
	if q < 120 || q > 135 {
		t.Fatalf("Quality at d/range=0.95 = %d, want about 127", q)
	}
}

func TestQualityIsOutOfRangeBeyondTheCutoff(t *testing.T) {
	for _, ratio := range []float64{1.1, 1.11, 5} {
		if _, in := Quality(ratio*100, 100); in {
			t.Fatalf("d/range=%.2f must be out of range", ratio)
		}
	}
}

func TestQualityTreatsAZeroRangeAsOutOfRange(t *testing.T) {
	// range 为 0 意味着上游没给出有效射程。宁可不扇出，也不要除零。
	if _, in := Quality(0, 0); in {
		t.Fatal("a zero range must be treated as out of range, not as infinite range")
	}
}

func TestDistanceBetweenKnownAirports(t *testing.T) {
	// ZSSS (31.198, 121.336) 到 ZBAA (40.080, 116.585) 约 590 海里。
	got := DistanceNM(31.198, 121.336, 40.080, 116.585)
	if math.Abs(got-590) > 20 {
		t.Fatalf("DistanceNM ZSSS→ZBAA = %.0f, want about 590", got)
	}
}

func TestDistanceIsZeroForTheSamePoint(t *testing.T) {
	if got := DistanceNM(31.198, 121.336, 31.198, 121.336); got > 0.001 {
		t.Fatalf("DistanceNM to the same point = %v, want 0", got)
	}
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `go test ./server/internal/geo/ -v`
Expected: FAIL，一堆 undefined

- [ ] **Step 3: 实现**

`server/internal/geo/range.go`：

```go
// Package geo 是 can-voice 的射程与信号质量计算。
//
// 纯数学，无 I/O。射程过滤放在服务端而不是客户端，是因为带宽：
// 全球 200 人同时在 121.500 时，客户端要收下 200 路流才能丢掉 199 路，
// 约 5.8 Mbps 下行（spec 7.1）。客户端衰减解决"吵"，解决不了"爆炸"。
package geo

import (
	"math"
	"strings"
)

// 信号质量曲线的两个拐点（spec 7.4）。
const (
	// FullRatio 以内是满格。
	FullRatio = 0.8
	// CutoffRatio 以外服务端不扇出。它比真实射程放宽 10%，
	// 中间这段边缘带交给客户端做平滑衰减——否则飞机停在射程线上时
	// 音频会忽有忽无。
	CutoffRatio = 1.1
)

const earthRadiusNM = 3440.065

// DistanceNM 是两点间的大圆距离，单位海里。
func DistanceNM(lat1, lon1, lat2, lon2 float64) float64 {
	φ1, φ2 := lat1*math.Pi/180, lat2*math.Pi/180
	dφ := (lat2 - lat1) * math.Pi / 180
	dλ := (lon2 - lon1) * math.Pi / 180
	a := math.Sin(dφ/2)*math.Sin(dφ/2) +
		math.Cos(φ1)*math.Cos(φ2)*math.Sin(dλ/2)*math.Sin(dλ/2)
	return 2 * earthRadiusNM * math.Asin(math.Min(1, math.Sqrt(a)))
}

// LineOfSightNM 是 VHF 视距射程：1.23 × (√h₁ + √h₂)，高度单位英尺。
//
// 这个公式本身就产生了正确的行为——地面上听不到远处、高空能听很远——
// 所以飞行员之间不需要任何额外规则。管制席位不能用它：一个 ACC 席位
// 现实中是一组分布式电台，见 FallbackRangeNM 与 datafeed 的 visual_range。
func LineOfSightNM(alt1Ft, alt2Ft float64) float64 {
	return 1.23 * (math.Sqrt(math.Max(0, alt1Ft)) + math.Sqrt(math.Max(0, alt2Ft)))
}

// suffixRange 是席位后缀的兜底半径，单位海里。
//
// 只在 can-fsd datafeed 的 visual_range 为 0 时使用——那是权威值，
// 由管制员在 #AA 里声明。ATIS 席位的 visual_range 就常常是 0。
//
// 这张表的数值是估的，需要按中国 FIR 的实际尺寸校准（spec 12）。
// 它是服务端配置而非编译期常量的理由也在这里：调它不该需要发版。
var suffixRange = map[string]float64{
	"DEL":  15,
	"GND":  15,
	"TWR":  30,
	"APP":  80,
	"DEP":  80,
	"CTR":  250,
	"FSS":  600,
	"ATIS": 60,
}

// unknownSuffixRange 是认不出后缀时的保守默认值。
// 刻意不是 0：0 会让那个席位谁都听不见，而在语音系统里
// "听不见"比"听得太远"糟糕得多。
const unknownSuffixRange = 80

// FallbackRangeNM 按呼号后缀给出兜底半径。
func FallbackRangeNM(callsign string) float64 {
	i := strings.LastIndex(callsign, "_")
	if i < 0 {
		return unknownSuffixRange
	}
	if r, ok := suffixRange[strings.ToUpper(callsign[i+1:])]; ok {
		return r
	}
	return unknownSuffixRange
}

// Quality 把距离与射程之比折算成 0–255 的信号质量。
// inRange 为 false 时服务端不扇出这一包。
//
// 客户端只会收到 qual 大于 0 的包，qual 越低说明越接近射程边缘——
// 它因此完全不知道别人在哪，隐私与防作弊是免费拿到的（spec 7.4）。
func Quality(distNM, rangeNM float64) (uint8, bool) {
	// range 为 0 意味着上游没给出有效射程。宁可不扇出，也不要把它当成无限远。
	if rangeNM <= 0 {
		return 0, false
	}
	ratio := distNM / rangeNM
	switch {
	case ratio <= FullRatio:
		return 255, true
	case ratio >= CutoffRatio:
		return 0, false
	default:
		frac := (CutoffRatio - ratio) / (CutoffRatio - FullRatio)
		q := int(math.Round(frac * 255))
		if q < 1 {
			// 还在射程内就不能报 0——0 是"出界"的信号。
			q = 1
		}
		return uint8(q), true
	}
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `go test ./server/internal/geo/ -v && go vet ./server/...`
Expected: PASS（九个测试）

- [ ] **Step 5: 提交**

```bash
git add server/internal/geo/
git commit -m "geo: VHF 视距射程与信号质量曲线"
```

---

### Task 5: can-fsd SSE 消费与位置快照

**Files:**
- Create: `server/internal/fsdfeed/feed.go`
- Create: `server/internal/fsdfeed/feed_test.go`
- Create: `server/testdata/datafeed_sample.json`

**Interfaces:**
- Consumes: `geo.LineOfSightNM`、`geo.FallbackRangeNM`（Task 4）
- Produces:
  - `type Position struct { Lat, Lon, AltFt, RangeNM float64; Callsign string; IsATC bool }`
  - `type Snapshot struct { ByCID map[string]Position; ByCallsign map[string]Position }`
  - `func ParseDatafeed(b []byte) (Snapshot, error)`
  - `type Feed struct{ … }`；`func NewFeed(url string) *Feed`；`func (f *Feed) Run(ctx context.Context)`；`func (f *Feed) Snapshot() Snapshot`；`func (f *Feed) Degraded() bool`

**这个任务有一个会静默出错的陷阱，必须靠测试钉住：飞行员的 `latitude`/`longitude` 是 JSON
数字（`25.10232`），管制员和 ATIS 的是 JSON 字符串（`"31.20466"`）。** 这是 can-fsd 刻意的、
由它自己的 `testdata/datafeed_golden.json` 钉住的契约。只当数字解析的话，所有管制员的位置
会变成 0,0（几内亚湾），表现为**管制员谁都听不见**，而日志里一条错误都没有。

- [ ] **Step 1: 取一份 can-fsd 的 golden 作为样本**

```bash
cp ~/Documents/Dev/CeruleanAviationNetwork/can-fsd/internal/api/testdata/datafeed_golden.json \
   server/testdata/datafeed_sample.json
```

- [ ] **Step 2: 写失败的测试**

`server/internal/fsdfeed/feed_test.go`：

```go
package fsdfeed

import (
	"math"
	"os"
	"testing"
)

func sample(t *testing.T) Snapshot {
	t.Helper()
	b, err := os.ReadFile("../../testdata/datafeed_sample.json")
	if err != nil {
		t.Fatalf("read sample: %v", err)
	}
	s, err := ParseDatafeed(b)
	if err != nil {
		t.Fatalf("ParseDatafeed: %v", err)
	}
	return s
}

// 这是本包最重要的测试。can-fsd 的 datafeed 里飞行员的经纬度是 JSON 数字，
// 管制员和 ATIS 的是 JSON 字符串。只当数字解析的话所有管制员会落在 0,0
// （几内亚湾），表现为管制员谁都听不见，而日志里一条错误都没有。
func TestControllerCoordinatesParseFromStrings(t *testing.T) {
	s := sample(t)
	p, ok := s.ByCallsign["ZSHA_CTR"]
	if !ok {
		t.Fatal("ZSHA_CTR missing from the snapshot")
	}
	if math.Abs(p.Lat-31.20466) > 0.001 || math.Abs(p.Lon-121.45272) > 0.001 {
		t.Fatalf("ZSHA_CTR at %v,%v — controller coordinates are JSON strings and must still parse", p.Lat, p.Lon)
	}
	if p.Lat == 0 && p.Lon == 0 {
		t.Fatal("ZSHA_CTR landed at 0,0: the string/number asymmetry was not handled")
	}
}

func TestPilotCoordinatesParseFromNumbers(t *testing.T) {
	s := sample(t)
	p, ok := s.ByCallsign["CCA5852"]
	if !ok {
		t.Fatal("CCA5852 missing from the snapshot")
	}
	if math.Abs(p.Lat-25.10232) > 0.001 || math.Abs(p.Lon-102.933) > 0.001 {
		t.Fatalf("CCA5852 at %v,%v, want 25.10232,102.933", p.Lat, p.Lon)
	}
	if p.AltFt != 6897 {
		t.Fatalf("CCA5852 altitude = %v, want 6897", p.AltFt)
	}
}

func TestPilotRangeComesFromAltitudeNotVisualRange(t *testing.T) {
	// 飞行员的 visual_range 在样本里是 40，但 VHF 视距由高度决定：
	// 6897 英尺约 102 海里。用 visual_range 会把射程砍掉 60%。
	s := sample(t)
	p := s.ByCallsign["CCA5852"]
	if p.RangeNM < 90 || p.RangeNM > 115 {
		t.Fatalf("pilot RangeNM = %.0f, want about 102 (line of sight from 6897 ft)", p.RangeNM)
	}
}

func TestControllerRangeUsesVisualRange(t *testing.T) {
	s := sample(t)
	if p := s.ByCallsign["ZSHA_CTR"]; p.RangeNM != 600 {
		t.Fatalf("ZSHA_CTR RangeNM = %v, want 600 from visual_range", p.RangeNM)
	}
}

func TestZeroVisualRangeFallsBackToTheSuffixTable(t *testing.T) {
	// 样本里 ZSSS_ATIS 的 visual_range 正是 0。
	s := sample(t)
	p, ok := s.ByCallsign["ZSSS_ATIS"]
	if !ok {
		t.Fatal("ZSSS_ATIS missing from the snapshot")
	}
	if p.RangeNM != 60 {
		t.Fatalf("ZSSS_ATIS RangeNM = %v, want the 60 nm _ATIS fallback (its visual_range is 0)", p.RangeNM)
	}
}

func TestSnapshotIsIndexedByCID(t *testing.T) {
	// cid 是关联键：token 里本来就有它，所以正常情况下协议里一个字段都不用加。
	s := sample(t)
	if _, ok := s.ByCID["1012"]; !ok {
		t.Fatal("snapshot must be indexed by cid; 1012 (CCA5852) is missing")
	}
}

func TestParseDatafeedRejectsGarbage(t *testing.T) {
	if _, err := ParseDatafeed([]byte("not json")); err == nil {
		t.Fatal("ParseDatafeed must reject non-JSON input")
	}
}
```

- [ ] **Step 3: 跑测试确认失败**

Run: `go test ./server/internal/fsdfeed/ -v`
Expected: FAIL，`undefined: Snapshot`、`ParseDatafeed`

- [ ] **Step 4: 实现**

`server/internal/fsdfeed/feed.go`：

```go
// Package fsdfeed 从 can-fsd 的 SSE 流维护一份位置快照，供射程过滤使用。
//
// 这是 can-voice 唯一的出站依赖，而且它的降级是安全的：SSE 断开时
// 退回"不做射程过滤"（等于 Mumble 时代的全球互通），而不是拒绝服务。
// 语音能不能通，比射程真实感重要得多（spec 7.3）。
package fsdfeed

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/JianyueLab-Org/can-voice/server/internal/geo"
)

// Position 是一个网络参与者的位置与射程。
type Position struct {
	Callsign string
	Lat      float64
	Lon      float64
	AltFt    float64
	RangeNM  float64
	IsATC    bool
}

// Snapshot 是某一时刻的全网位置，按 cid 和呼号两路索引。
// cid 是主键——token 里本来就有它；呼号只给观察员模式的 follow 用。
type Snapshot struct {
	ByCID      map[string]Position
	ByCallsign map[string]Position
}

// flexFloat 吃 JSON 数字也吃 JSON 字符串。
//
// can-fsd 的 datafeed 里飞行员的经纬度是数字而管制员的是字符串，
// 这是它刻意的、由 testdata/datafeed_golden.json 钉住的契约。
// 只当数字解析会让所有管制员落在 0,0（几内亚湾），表现为管制员谁都听不见，
// 而日志里一条错误都没有。
type flexFloat float64

func (f *flexFloat) UnmarshalJSON(b []byte) error {
	s := strings.TrimSpace(string(b))
	if s == "null" || s == `""` {
		*f = 0
		return nil
	}
	s = strings.Trim(s, `"`)
	v, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return fmt.Errorf("coordinate %q is neither a JSON number nor a numeric string: %w", b, err)
	}
	*f = flexFloat(v)
	return nil
}

type datafeed struct {
	Pilots []struct {
		Callsign string    `json:"callsign"`
		CID      string    `json:"cid"`
		Lat      flexFloat `json:"latitude"`
		Lon      flexFloat `json:"longitude"`
		Altitude flexFloat `json:"altitude"`
	} `json:"pilots"`
	Controllers []atcEntry `json:"controllers"`
	ATIS        []atcEntry `json:"atis"`
}

type atcEntry struct {
	Callsign    string    `json:"callsign"`
	CID         string    `json:"cid"`
	Lat         flexFloat `json:"latitude"`
	Lon         flexFloat `json:"longitude"`
	VisualRange int       `json:"visual_range"`
}

// ParseDatafeed 把一份 datafeed 文档折算成位置快照。
func ParseDatafeed(b []byte) (Snapshot, error) {
	var d datafeed
	if err := json.Unmarshal(b, &d); err != nil {
		return Snapshot{}, fmt.Errorf("parse datafeed: %w", err)
	}
	s := Snapshot{
		ByCID:      make(map[string]Position, len(d.Pilots)+len(d.Controllers)+len(d.ATIS)),
		ByCallsign: make(map[string]Position, len(d.Pilots)+len(d.Controllers)+len(d.ATIS)),
	}
	add := func(p Position) {
		if p.CallsignEmpty() {
			return
		}
		s.ByCallsign[p.Callsign] = p
	}
	for _, e := range d.Pilots {
		// 飞行员的射程由高度算，不用 datafeed 的 visual_range：
		// VHF 是视距传播，6897 英尺就是约 102 海里，而样本里的
		// visual_range 是 40——用它会把射程砍掉 60%。
		p := Position{
			Callsign: e.Callsign,
			Lat:      float64(e.Lat),
			Lon:      float64(e.Lon),
			AltFt:    float64(e.Altitude),
			RangeNM:  geo.LineOfSightNM(float64(e.Altitude), 0),
		}
		add(p)
		if e.CID != "" {
			s.ByCID[e.CID] = p
		}
	}
	for _, group := range [][]atcEntry{d.Controllers, d.ATIS} {
		for _, e := range group {
			// visual_range 是权威值，由管制员在 #AA 里声明。
			// 为 0 时（样本里 ZSSS_ATIS 正是 0）退回席位后缀兜底表。
			r := float64(e.VisualRange)
			if r <= 0 {
				r = geo.FallbackRangeNM(e.Callsign)
			}
			p := Position{
				Callsign: e.Callsign,
				Lat:      float64(e.Lat),
				Lon:      float64(e.Lon),
				RangeNM:  r,
				IsATC:    true,
			}
			add(p)
			if e.CID != "" {
				s.ByCID[e.CID] = p
			}
		}
	}
	return s, nil
}

// CallsignEmpty 报告这条记录有没有呼号。没有呼号的记录无法被 follow 引用。
func (p Position) CallsignEmpty() bool { return p.Callsign == "" }

// Feed 持续消费 can-fsd 的 SSE 流并维护最新快照。
type Feed struct {
	url string

	mu       sync.RWMutex
	snap     Snapshot
	degraded bool
}

// NewFeed 建一个 Feed。初始状态是降级的——在第一个 snapshot 到达之前，
// 我们对谁在哪里一无所知，此时必须全部扇出而不是全部屏蔽。
func NewFeed(url string) *Feed {
	return &Feed{
		url:      url,
		snap:     Snapshot{ByCID: map[string]Position{}, ByCallsign: map[string]Position{}},
		degraded: true,
	}
}

// Snapshot 返回当前快照。
func (f *Feed) Snapshot() Snapshot {
	f.mu.RLock()
	defer f.mu.RUnlock()
	return f.snap
}

// Degraded 报告位置信息当前是否不可用。
// 为 true 时调用方必须跳过射程过滤而全部扇出——退化成 Mumble 时代的行为，
// 而不是把所有人都屏蔽掉。
func (f *Feed) Degraded() bool {
	f.mu.RLock()
	defer f.mu.RUnlock()
	return f.degraded
}

// Run 连接 SSE 流并持续更新，直到 ctx 取消。断开后自动重连。
func (f *Feed) Run(ctx context.Context) {
	for ctx.Err() == nil {
		if err := f.stream(ctx); err != nil && ctx.Err() == nil {
			slog.Warn("fsd feed dropped, falling back to no range filtering", "error", err)
		}
		f.mu.Lock()
		f.degraded = true
		f.mu.Unlock()

		select {
		case <-ctx.Done():
			return
		case <-time.After(5 * time.Second):
		}
	}
}

func (f *Feed) stream(ctx context.Context) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, f.url, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Accept", "text/event-stream")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("fsd feed returned %s", resp.Status)
	}

	slog.Info("fsd feed connected", "url", f.url)
	sc := bufio.NewScanner(resp.Body)
	sc.Buffer(make([]byte, 0, 64<<10), 8<<20)
	for sc.Scan() {
		line := sc.Text()
		// 只关心 data 行；事件名（snapshot / update）不影响我们的处理，
		// 因为两者携带的都是同一个文档形状，我们整体替换快照。
		data, ok := strings.CutPrefix(line, "data:")
		if !ok {
			continue
		}
		snap, err := ParseDatafeed([]byte(strings.TrimSpace(data)))
		if err != nil {
			slog.Warn("skipping an unparsable feed event", "error", err)
			continue
		}
		f.mu.Lock()
		f.snap = snap
		f.degraded = false
		f.mu.Unlock()
	}
	return sc.Err()
}
```

**注意 `update` 事件是增量的**（spec 引用 can-fsd：*"an `update` per tick carrying only the
entries that changed and the callsigns that went away"*）。上面的实现对 `update` 做的是整体
替换，这会让未变化的条目消失。**Task 6 修正它** —— 先让 `snapshot` 路径通过测试。

- [ ] **Step 5: 跑测试确认通过**

Run: `go test ./server/internal/fsdfeed/ -v && go vet ./server/...`
Expected: PASS（七个测试）

- [ ] **Step 6: 提交**

```bash
git add server/internal/fsdfeed/ server/testdata/datafeed_sample.json
git commit -m "fsdfeed: 位置快照解析，含管制员经纬度的字符串/数字不对称"
```

---

### Task 6: 正确处理 SSE 的 snapshot 与 update 语义

Task 5 把 `update` 当成全量替换了。can-fsd 的 `update` 只携带**变化的条目**和**消失的呼号**，
整体替换会让所有未变化的飞机在下一个 tick 消失 —— 表现为"射程过滤时灵时不灵"。

**Files:**
- Modify: `server/internal/fsdfeed/feed.go`
- Modify: `server/internal/fsdfeed/feed_test.go`

**Interfaces:**
- Consumes: Task 5 的全部
- Produces: `func (f *Feed) applyEvent(event string, data []byte) error`；`snapshot` 替换，`update` 合并

- [ ] **Step 1: 写失败的测试**

追加到 `server/internal/fsdfeed/feed_test.go`：

```go
func TestSnapshotEventReplacesWhileUpdateEventMerges(t *testing.T) {
	f := NewFeed("http://example.invalid")

	full := []byte(`{"pilots":[
		{"callsign":"CCA1","cid":"1","latitude":30.0,"longitude":120.0,"altitude":10000},
		{"callsign":"CCA2","cid":"2","latitude":31.0,"longitude":121.0,"altitude":20000}
	],"controllers":[],"atis":[]}`)
	if err := f.applyEvent("snapshot", full); err != nil {
		t.Fatalf("applyEvent snapshot: %v", err)
	}
	if got := len(f.Snapshot().ByCallsign); got != 2 {
		t.Fatalf("after snapshot: %d entries, want 2", got)
	}

	// update 只带 CCA1 的新位置。CCA2 必须留下——
	// 整体替换会让它消失，表现为射程过滤时灵时不灵。
	upd := []byte(`{"pilots":[
		{"callsign":"CCA1","cid":"1","latitude":30.5,"longitude":120.5,"altitude":11000}
	],"controllers":[],"atis":[]}`)
	if err := f.applyEvent("update", upd); err != nil {
		t.Fatalf("applyEvent update: %v", err)
	}
	s := f.Snapshot()
	if len(s.ByCallsign) != 2 {
		t.Fatalf("after update: %d entries, want 2 (an update is a delta, not a replacement)", len(s.ByCallsign))
	}
	if p := s.ByCallsign["CCA1"]; p.Lat != 30.5 {
		t.Fatalf("CCA1 lat = %v, want the updated 30.5", p.Lat)
	}
	if _, ok := s.ByCallsign["CCA2"]; !ok {
		t.Fatal("CCA2 vanished after an update that did not mention it")
	}
}

func TestUpdateEventRemovesDepartedCallsigns(t *testing.T) {
	f := NewFeed("http://example.invalid")
	full := []byte(`{"pilots":[
		{"callsign":"CCA1","cid":"1","latitude":30.0,"longitude":120.0,"altitude":10000},
		{"callsign":"CCA2","cid":"2","latitude":31.0,"longitude":121.0,"altitude":20000}
	],"controllers":[],"atis":[]}`)
	if err := f.applyEvent("snapshot", full); err != nil {
		t.Fatalf("applyEvent snapshot: %v", err)
	}
	upd := []byte(`{"pilots":[],"controllers":[],"atis":[],"removed":["CCA2"]}`)
	if err := f.applyEvent("update", upd); err != nil {
		t.Fatalf("applyEvent update: %v", err)
	}
	s := f.Snapshot()
	if _, ok := s.ByCallsign["CCA2"]; ok {
		t.Fatal("CCA2 was listed as removed and must be gone")
	}
	if _, ok := s.ByCID["2"]; ok {
		t.Fatal("a removed callsign must also leave the cid index")
	}
	if _, ok := s.ByCallsign["CCA1"]; !ok {
		t.Fatal("CCA1 must survive an update that removed someone else")
	}
}

func TestAnUnparsableEventDoesNotWipeTheSnapshot(t *testing.T) {
	f := NewFeed("http://example.invalid")
	full := []byte(`{"pilots":[{"callsign":"CCA1","cid":"1","latitude":30.0,"longitude":120.0,"altitude":10000}],"controllers":[],"atis":[]}`)
	if err := f.applyEvent("snapshot", full); err != nil {
		t.Fatalf("applyEvent: %v", err)
	}
	if err := f.applyEvent("update", []byte("garbage")); err == nil {
		t.Fatal("applyEvent must report a parse failure")
	}
	if _, ok := f.Snapshot().ByCallsign["CCA1"]; !ok {
		t.Fatal("a bad event must leave the previous snapshot intact, not wipe it")
	}
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `go test ./server/internal/fsdfeed/ -run 'TestSnapshotEvent|TestUpdateEvent|TestAnUnparsable' -v`
Expected: FAIL，`undefined: applyEvent`

- [ ] **Step 3: 实现**

在 `feed.go` 的 `datafeed` 结构里加上 removed 字段：

```go
	// Removed 是本次 tick 里离线的呼号。只出现在 update 事件里。
	Removed []string `json:"removed"`
```

追加 `applyEvent`：

```go
// applyEvent 把一个 SSE 事件并入快照。
//
// snapshot 是全量，整体替换；update 是增量，只带变化的条目和消失的呼号，
// 必须合并。把 update 当成全量替换会让所有未变化的飞机在下一个 tick 消失，
// 表现为"射程过滤时灵时不灵"。
func (f *Feed) applyEvent(event string, data []byte) error {
	incoming, err := ParseDatafeed(data)
	if err != nil {
		// 解析失败时保留上一份快照：一个坏事件不该让全网瞬间失去射程。
		return err
	}
	var removed []string
	if event != "snapshot" {
		var d struct {
			Removed []string `json:"removed"`
		}
		// 这里不可能失败——ParseDatafeed 已经解过同一段字节。
		_ = json.Unmarshal(data, &d)
		removed = d.Removed
	}

	f.mu.Lock()
	defer f.mu.Unlock()

	if event == "snapshot" {
		f.snap = incoming
		f.degraded = false
		return nil
	}

	// 合并：incoming 覆盖同名条目，其余留下。
	for cs, p := range incoming.ByCallsign {
		f.snap.ByCallsign[cs] = p
	}
	for cid, p := range incoming.ByCID {
		f.snap.ByCID[cid] = p
	}
	for _, cs := range removed {
		if p, ok := f.snap.ByCallsign[cs]; ok {
			delete(f.snap.ByCallsign, cs)
			// cid 索引也要清，否则一个离线的会话仍然有位置。
			for cid, q := range f.snap.ByCID {
				if q.Callsign == p.Callsign {
					delete(f.snap.ByCID, cid)
				}
			}
		}
	}
	f.degraded = false
	return nil
}
```

把 `stream()` 里的行处理改成累积事件名和数据：

```go
	sc := bufio.NewScanner(resp.Body)
	sc.Buffer(make([]byte, 0, 64<<10), 8<<20)
	event := "message"
	for sc.Scan() {
		line := sc.Text()
		switch {
		case line == "":
			// 事件边界；名字回到默认值。
			event = "message"
		case strings.HasPrefix(line, "event:"):
			event = strings.TrimSpace(strings.TrimPrefix(line, "event:"))
		case strings.HasPrefix(line, "data:"):
			data := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
			if err := f.applyEvent(event, []byte(data)); err != nil {
				slog.Warn("skipping an unparsable feed event", "event", event, "error", err)
			}
		}
	}
	return sc.Err()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `go test ./server/internal/fsdfeed/ -v && go vet ./server/...`
Expected: PASS（十个测试）

- [ ] **Step 5: 提交**

```bash
git add server/internal/fsdfeed/
git commit -m "fsdfeed: snapshot 整体替换、update 增量合并与离线清理"
```

---

### Task 7: 会话与订阅表

**Files:**
- Create: `server/internal/router/session.go`
- Create: `server/internal/router/router.go`
- Create: `server/internal/router/router_test.go`

**Interfaces:**
- Consumes: `control.Sub`（Task 2）、`auth.Claims`（Task 3）
- Produces:
  - `type SessionID uint32`
  - `type Session struct { ID SessionID; CID, Callsign, Follow string; MaxTX int; … }`
  - `type Router struct{ … }`；`func New() *Router`
  - `func (r *Router) Add(cid, follow string, maxTX int, send func([]byte)) *Session`
  - `func (r *Router) Remove(id SessionID)`
  - `func (r *Router) Subscribe(id SessionID, s control.Sub) control.SubAck`
  - `func (r *Router) Listeners(freq uint32) []*Session`
  - `func (r *Router) MayTransmit(id SessionID, freq uint32) bool`

- [ ] **Step 1: 写失败的测试**

`server/internal/router/router_test.go`：

```go
package router

import (
	"testing"

	"github.com/JianyueLab-Org/can-voice/server/internal/control"
)

func newSession(t *testing.T, r *Router, cid string, maxTX int) *Session {
	t.Helper()
	s := r.Add(cid, "", maxTX, func([]byte) {})
	if s == nil {
		t.Fatal("Add returned nil")
	}
	return s
}

func TestSubscribeReplacesWholesaleRatherThanMerging(t *testing.T) {
	// SUB 是全量声明。第二次订阅必须整体取代第一次，
	// 而不是并集——任何增量语义都会把 sync 风暴那一类 bug 请回来。
	r := New()
	s := newSession(t, r, "1000", 8)

	r.Subscribe(s.ID, control.Sub{RX: []uint32{118000, 121800}})
	r.Subscribe(s.ID, control.Sub{RX: []uint32{124550}})

	if got := r.Listeners(118000); len(got) != 0 {
		t.Fatalf("118000 still has %d listeners; the second SUB must replace the first, not merge", len(got))
	}
	if got := r.Listeners(124550); len(got) != 1 {
		t.Fatalf("124550 has %d listeners, want 1", len(got))
	}
}

func TestTxImpliesRx(t *testing.T) {
	// 没有"只发不收"的电台（无线电栈耦合规则，spec 9.1）。
	r := New()
	s := newSession(t, r, "1000", 8)
	ack := r.Subscribe(s.ID, control.Sub{RX: []uint32{118000}, TX: []uint32{121800}})

	if len(r.Listeners(121800)) != 1 {
		t.Fatal("a frequency declared for TX must also be received")
	}
	if !contains(ack.RX, 121800) {
		t.Fatalf("SubAck.RX = %v, must include the TX frequency", ack.RX)
	}
}

func TestSubscribeRejectsTxBeyondMaxTX(t *testing.T) {
	r := New()
	s := newSession(t, r, "1000", 2)
	ack := r.Subscribe(s.ID, control.Sub{TX: []uint32{118000, 121800, 124550}})

	if len(ack.TX) != 2 {
		t.Fatalf("accepted TX = %v, want 2 (max_tx from the token)", ack.TX)
	}
	if len(ack.Rejected) != 1 {
		t.Fatalf("Rejected = %v, want exactly the one over the limit", ack.Rejected)
	}
	if r.MayTransmit(s.ID, ack.Rejected[0]) {
		t.Fatal("a rejected frequency must not be transmittable")
	}
}

func TestMayTransmitOnlyOnDeclaredFrequencies(t *testing.T) {
	r := New()
	s := newSession(t, r, "1000", 8)
	r.Subscribe(s.ID, control.Sub{RX: []uint32{118000, 121800}, TX: []uint32{121800}})

	if !r.MayTransmit(s.ID, 121800) {
		t.Fatal("121800 was declared for TX")
	}
	if r.MayTransmit(s.ID, 118000) {
		t.Fatal("118000 is RX only; transmitting on it must be refused")
	}
	if r.MayTransmit(s.ID, 999000) {
		t.Fatal("an undeclared frequency must not be transmittable")
	}
}

func TestRemoveClearsEveryFrequency(t *testing.T) {
	r := New()
	s := newSession(t, r, "1000", 8)
	r.Subscribe(s.ID, control.Sub{RX: []uint32{118000, 121800, 124550}})
	r.Remove(s.ID)

	for _, f := range []uint32{118000, 121800, 124550} {
		if got := r.Listeners(f); len(got) != 0 {
			t.Fatalf("%d still has %d listeners after Remove", f, len(got))
		}
	}
}

func TestSubscribeDeduplicatesFrequencies(t *testing.T) {
	r := New()
	s := newSession(t, r, "1000", 8)
	r.Subscribe(s.ID, control.Sub{RX: []uint32{118000, 118000, 118000}})
	if got := r.Listeners(118000); len(got) != 1 {
		t.Fatalf("a repeated frequency produced %d listener entries, want 1", len(got))
	}
}

func contains(xs []uint32, v uint32) bool {
	for _, x := range xs {
		if x == v {
			return true
		}
	}
	return false
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `go test ./server/internal/router/ -v`
Expected: FAIL，一堆 undefined

- [ ] **Step 3: 实现**

`server/internal/router/session.go`：

```go
package router

import "sync/atomic"

// SessionID 是一个连接的标识，也是数据面包头里的 speaker 字段。
type SessionID uint32

// Session 是一条连接。
//
// 刻意不持有任何"我在哪个频道"之类的可记忆状态——
// 订阅集合是服务端的唯一真相，由客户端每次用全量 SUB 声明。
type Session struct {
	ID       SessionID
	CID      string
	Callsign string
	// Follow 只有观察员模式填：观察员没有 FSD 连接，
	// 位置取自它跟随的那架飞机（spec 7.3）。
	Follow string
	MaxTX  int

	// send 把一个数据面包发给这个会话。由传输层注入，
	// router 因此不依赖 QUIC，可以纯逻辑测试。
	send func([]byte)

	rx map[uint32]struct{}
	tx map[uint32]struct{}
	xc [][2]uint32
}

// Send 把一个已编好的数据面包发出去。
func (s *Session) Send(b []byte) {
	if s.send != nil {
		s.send(b)
	}
}

var nextID atomic.Uint32

func newSessionID() SessionID { return SessionID(nextID.Add(1)) }
```

`server/internal/router/router.go`：

```go
// Package router 是 can-voice 的全部业务状态：会话、订阅表、交叉耦合。
//
// 它不认识 QUIC——发送通过注入的回调完成，所以整个路由逻辑可以纯逻辑测试。
// 没有数据库、没有持久化：进程重启等于所有人重连并重发 SUB。
package router

import (
	"sync"

	"github.com/JianyueLab-Org/can-voice/server/internal/control"
)

// Router 持有全部服务端状态。
type Router struct {
	mu       sync.RWMutex
	sessions map[SessionID]*Session
	// rx 是频率到订阅者的倒排索引，扇出时只查这一张表。
	rx map[uint32]map[SessionID]struct{}
}

// New 建一个空的 Router。
func New() *Router {
	return &Router{
		sessions: map[SessionID]*Session{},
		rx:       map[uint32]map[SessionID]struct{}{},
	}
}

// Add 登记一条新会话。send 会在扇出时被调用。
func (r *Router) Add(cid, follow string, maxTX int, send func([]byte)) *Session {
	s := &Session{
		ID:     newSessionID(),
		CID:    cid,
		Follow: follow,
		MaxTX:  maxTX,
		send:   send,
		rx:     map[uint32]struct{}{},
		tx:     map[uint32]struct{}{},
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	r.sessions[s.ID] = s
	return s
}

// Remove 注销会话并把它从每一个频率的订阅者集合里摘掉。
func (r *Router) Remove(id SessionID) {
	r.mu.Lock()
	defer r.mu.Unlock()
	s, ok := r.sessions[id]
	if !ok {
		return
	}
	for f := range s.rx {
		r.unindex(f, id)
	}
	delete(r.sessions, id)
}

// Subscribe 用一次**全量**声明整体替换该会话的订阅集合。
//
// 这是消除 sync 风暴的根本机制（spec 5.1）：幂等，没有"这次是加还是减"
// 的状态推导，重连后重发一次即恢复。刻意不提供任何增量入口。
func (r *Router) Subscribe(id SessionID, sub control.Sub) control.SubAck {
	r.mu.Lock()
	defer r.mu.Unlock()

	ack := control.SubAck{RX: []uint32{}, TX: []uint32{}, Rejected: []uint32{}}
	s, ok := r.sessions[id]
	if !ok {
		return ack
	}

	// 先把旧的索引全部摘掉，再按新声明重建。
	for f := range s.rx {
		r.unindex(f, id)
	}

	newTX := map[uint32]struct{}{}
	for _, f := range dedup(sub.TX) {
		// TX 超出 token 里的 max_tx 就拒绝。权威值是 token，
		// 控制面 READY 里的同名字段只是回显（spec 6）。
		if len(newTX) >= s.MaxTX {
			ack.Rejected = append(ack.Rejected, f)
			continue
		}
		newTX[f] = struct{}{}
		ack.TX = append(ack.TX, f)
	}

	newRX := map[uint32]struct{}{}
	// TX ⊆ RX：没有"只发不收"的电台（spec 9.1 的无线电栈耦合规则）。
	for f := range newTX {
		newRX[f] = struct{}{}
	}
	for _, f := range dedup(sub.RX) {
		newRX[f] = struct{}{}
	}

	s.rx, s.tx, s.xc = newRX, newTX, sub.XC
	for f := range newRX {
		r.index(f, id)
		ack.RX = append(ack.RX, f)
	}
	return ack
}

// Listeners 返回订阅了该频率的会话。
func (r *Router) Listeners(freq uint32) []*Session {
	r.mu.RLock()
	defer r.mu.RUnlock()
	ids := r.rx[freq]
	out := make([]*Session, 0, len(ids))
	for id := range ids {
		if s, ok := r.sessions[id]; ok {
			out = append(out, s)
		}
	}
	return out
}

// MayTransmit 报告该会话有没有声明在这个频率上发送。
// 这是上行包的第一道校验：没声明就丢弃，否则任何人都能往任意频率喊话。
func (r *Router) MayTransmit(id SessionID, freq uint32) bool {
	r.mu.RLock()
	defer r.mu.RUnlock()
	s, ok := r.sessions[id]
	if !ok {
		return false
	}
	_, ok = s.tx[freq]
	return ok
}

// Get 取一条会话。
func (r *Router) Get(id SessionID) (*Session, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	s, ok := r.sessions[id]
	return s, ok
}

func (r *Router) index(freq uint32, id SessionID) {
	if r.rx[freq] == nil {
		r.rx[freq] = map[SessionID]struct{}{}
	}
	r.rx[freq][id] = struct{}{}
}

func (r *Router) unindex(freq uint32, id SessionID) {
	if m, ok := r.rx[freq]; ok {
		delete(m, id)
		if len(m) == 0 {
			delete(r.rx, freq)
		}
	}
}

func dedup(xs []uint32) []uint32 {
	seen := make(map[uint32]struct{}, len(xs))
	out := make([]uint32, 0, len(xs))
	for _, x := range xs {
		if _, ok := seen[x]; ok {
			continue
		}
		seen[x] = struct{}{}
		out = append(out, x)
	}
	return out
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `go test ./server/internal/router/ -v && go vet ./server/...`
Expected: PASS（六个测试）

- [ ] **Step 5: 提交**

```bash
git add server/internal/router/
git commit -m "router: 会话与全量声明式订阅表"
```

---

### Task 8: 扇出与射程过滤

**Files:**
- Modify: `server/internal/router/router.go`
- Create: `server/internal/router/fanout.go`
- Create: `server/internal/router/fanout_test.go`

**Interfaces:**
- Consumes: `wire.Header`/`wire.Parse`（Task 1）、`geo.Quality`/`geo.DistanceNM`（Task 4）、`fsdfeed.Snapshot`（Task 5）、`Router`（Task 7）
- Produces:
  - `type Locator interface { Snapshot() fsdfeed.Snapshot; Degraded() bool }`
  - `func (r *Router) SetLocator(l Locator)`
  - `func (r *Router) Fanout(from SessionID, packet []byte) (delivered int, err error)`

扇出路径（spec §8）：校验发送权 → 查订阅者 → 逐个算 `qual` → 填 `speaker`/`qual` 后原样转发。
**不解码音频。**

- [ ] **Step 1: 写失败的测试**

`server/internal/router/fanout_test.go`：

```go
package router

import (
	"testing"

	"github.com/JianyueLab-Org/can-voice/server/internal/control"
	"github.com/JianyueLab-Org/can-voice/server/internal/fsdfeed"
	"github.com/JianyueLab-Org/can-voice/server/internal/wire"
)

// stubLocator 让射程测试不必碰网络。
type stubLocator struct {
	snap     fsdfeed.Snapshot
	degraded bool
}

func (s stubLocator) Snapshot() fsdfeed.Snapshot { return s.snap }
func (s stubLocator) Degraded() bool             { return s.degraded }

func packet(freq uint32, opus ...byte) []byte {
	return append(wire.Header{Ver: wire.Version, Flags: wire.FlagFirst, FreqKHz: freq}.AppendTo(nil), opus...)
}

func TestFanoutDeliversToListenersButNotBackToTheSender(t *testing.T) {
	r := New()
	var gotA, gotB [][]byte
	a := r.Add("1000", "", 8, func(b []byte) { gotA = append(gotA, append([]byte(nil), b...)) })
	b := r.Add("1001", "", 8, func(p []byte) { gotB = append(gotB, append([]byte(nil), p...)) })
	r.Subscribe(a.ID, control.Sub{TX: []uint32{121800}})
	r.Subscribe(b.ID, control.Sub{RX: []uint32{121800}})

	n, err := r.Fanout(a.ID, packet(121800, 0xaa))
	if err != nil {
		t.Fatalf("Fanout: %v", err)
	}
	if n != 1 {
		t.Fatalf("delivered = %d, want 1", n)
	}
	if len(gotB) != 1 {
		t.Fatalf("listener received %d packets, want 1", len(gotB))
	}
	if len(gotA) != 0 {
		t.Fatal("the sender must not receive its own transmission back")
	}
}

func TestFanoutStampsSpeakerAndLeavesOpusUntouched(t *testing.T) {
	r := New()
	var got []byte
	a := r.Add("1000", "", 8, func([]byte) {})
	b := r.Add("1001", "", 8, func(p []byte) { got = append([]byte(nil), p...) })
	r.Subscribe(a.ID, control.Sub{TX: []uint32{121800}})
	r.Subscribe(b.ID, control.Sub{RX: []uint32{121800}})

	opus := []byte{0xde, 0xad, 0xbe, 0xef}
	if _, err := r.Fanout(a.ID, packet(121800, opus...)); err != nil {
		t.Fatalf("Fanout: %v", err)
	}
	h, payload, err := wire.Parse(got)
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if h.Speaker != uint32(a.ID) {
		t.Fatalf("Speaker = %d, want the sender's session id %d", h.Speaker, a.ID)
	}
	if string(payload) != string(opus) {
		t.Fatalf("opus payload = %v, want %v — the server must never touch the audio", payload, opus)
	}
	if h.Flags != wire.FlagFirst {
		t.Fatalf("Flags = %d, want the sender's flags preserved", h.Flags)
	}
}

func TestFanoutRefusesAnUndeclaredTransmitFrequency(t *testing.T) {
	r := New()
	a := r.Add("1000", "", 8, func([]byte) {})
	r.Subscribe(a.ID, control.Sub{RX: []uint32{121800}}) // 只收不发

	if _, err := r.Fanout(a.ID, packet(121800)); err == nil {
		t.Fatal("Fanout must refuse a frequency the sender did not declare for TX")
	}
}

func TestFanoutSkipsListenersOutOfRange(t *testing.T) {
	r := New()
	var got [][]byte
	a := r.Add("1000", "", 8, func([]byte) {})
	b := r.Add("1001", "", 8, func(p []byte) { got = append(got, p) })
	r.Subscribe(a.ID, control.Sub{TX: []uint32{121800}})
	r.Subscribe(b.ID, control.Sub{RX: []uint32{121800}})

	// 两架地面飞机相距 500 海里，视距射程约 0，必然出界。
	r.SetLocator(stubLocator{snap: fsdfeed.Snapshot{ByCID: map[string]fsdfeed.Position{
		"1000": {Lat: 30, Lon: 120, AltFt: 0, RangeNM: 20},
		"1001": {Lat: 38, Lon: 120, AltFt: 0, RangeNM: 20},
	}}})

	n, err := r.Fanout(a.ID, packet(121800))
	if err != nil {
		t.Fatalf("Fanout: %v", err)
	}
	if n != 0 || len(got) != 0 {
		t.Fatalf("delivered %d packets to a listener 500 nm away", n)
	}
}

func TestFanoutStampsQualityForListenersInTheEdgeBand(t *testing.T) {
	r := New()
	var got []byte
	a := r.Add("1000", "", 8, func([]byte) {})
	b := r.Add("1001", "", 8, func(p []byte) { got = append([]byte(nil), p...) })
	r.Subscribe(a.ID, control.Sub{TX: []uint32{121800}})
	r.Subscribe(b.ID, control.Sub{RX: []uint32{121800}})

	// 同一点上：距离 0，必然满格。
	r.SetLocator(stubLocator{snap: fsdfeed.Snapshot{ByCID: map[string]fsdfeed.Position{
		"1000": {Lat: 30, Lon: 120, RangeNM: 100},
		"1001": {Lat: 30, Lon: 120, RangeNM: 100},
	}}})

	if _, err := r.Fanout(a.ID, packet(121800)); err != nil {
		t.Fatalf("Fanout: %v", err)
	}
	h, _, err := wire.Parse(got)
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if h.Qual != 255 {
		t.Fatalf("Qual = %d for a co-located listener, want 255", h.Qual)
	}
}

// 降级必须是"全部扇出"，不是"全部屏蔽"。语音能不能通，
// 比射程真实感重要得多（spec 7.3）。
func TestFanoutDeliversToEveryoneWhenTheFeedIsDegraded(t *testing.T) {
	r := New()
	var got [][]byte
	a := r.Add("1000", "", 8, func([]byte) {})
	b := r.Add("1001", "", 8, func(p []byte) { got = append(got, p) })
	r.Subscribe(a.ID, control.Sub{TX: []uint32{121800}})
	r.Subscribe(b.ID, control.Sub{RX: []uint32{121800}})

	r.SetLocator(stubLocator{degraded: true})

	n, err := r.Fanout(a.ID, packet(121800))
	if err != nil {
		t.Fatalf("Fanout: %v", err)
	}
	if n != 1 {
		t.Fatal("a degraded position feed must fall back to delivering to everyone, not to blocking everyone")
	}
}

// 位置未知的会话也必须收到。一个只连了语音没连 FSD 的人
// 不该因此变成聋子。
func TestFanoutDeliversWhenAPositionIsUnknown(t *testing.T) {
	r := New()
	var got [][]byte
	a := r.Add("1000", "", 8, func([]byte) {})
	b := r.Add("9999", "", 8, func(p []byte) { got = append(got, p) })
	r.Subscribe(a.ID, control.Sub{TX: []uint32{121800}})
	r.Subscribe(b.ID, control.Sub{RX: []uint32{121800}})

	r.SetLocator(stubLocator{snap: fsdfeed.Snapshot{ByCID: map[string]fsdfeed.Position{
		"1000": {Lat: 30, Lon: 120, RangeNM: 100},
	}}})

	n, err := r.Fanout(a.ID, packet(121800))
	if err != nil {
		t.Fatalf("Fanout: %v", err)
	}
	if n != 1 {
		t.Fatal("a listener with no known position must still be delivered to")
	}
}

func TestFanoutForwardsToCrossCoupledFrequencies(t *testing.T) {
	r := New()
	var onB, onC [][]byte
	a := r.Add("1000", "", 8, func([]byte) {})
	b := r.Add("1001", "", 8, func(p []byte) { onB = append(onB, p) })
	c := r.Add("1002", "", 8, func(p []byte) { onC = append(onC, p) })

	// a 把 121800 和 124550 交叉耦合。
	r.Subscribe(a.ID, control.Sub{TX: []uint32{121800}, XC: [][2]uint32{{121800, 124550}}})
	r.Subscribe(b.ID, control.Sub{RX: []uint32{121800}})
	r.Subscribe(c.ID, control.Sub{RX: []uint32{124550}})

	if _, err := r.Fanout(a.ID, packet(121800)); err != nil {
		t.Fatalf("Fanout: %v", err)
	}
	if len(onB) != 1 {
		t.Fatalf("121800 listener got %d packets, want 1", len(onB))
	}
	if len(onC) != 1 {
		t.Fatal("cross-coupling must forward the transmission onto 124550; doing it server-side is what lets the client drop that logic entirely")
	}
	h, _, err := wire.Parse(onC[0])
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if h.FreqKHz != 124550 {
		t.Fatalf("forwarded packet carries freq %d, want 124550 — the listener routes audio by this field", h.FreqKHz)
	}
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `go test ./server/internal/router/ -run TestFanout -v`
Expected: FAIL，`undefined: Fanout`、`SetLocator`、`Locator`

- [ ] **Step 3: 实现**

`server/internal/router/fanout.go`：

```go
package router

import (
	"fmt"

	"github.com/JianyueLab-Org/can-voice/server/internal/fsdfeed"
	"github.com/JianyueLab-Org/can-voice/server/internal/geo"
	"github.com/JianyueLab-Org/can-voice/server/internal/wire"
)

// Locator 提供位置快照。抽成接口是为了让扇出的测试不必碰网络。
type Locator interface {
	Snapshot() fsdfeed.Snapshot
	Degraded() bool
}

// SetLocator 装上位置来源。不装等于永久降级：全部扇出。
func (r *Router) SetLocator(l Locator) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.locator = l
}

// Fanout 把一个上行数据包转发给订阅者，返回实际投递的份数。
//
// 路径（spec 8）：校验发送权 → 查订阅者 → 逐个算 qual → 填 speaker/qual 后原样转发。
// **载荷全程不解码** —— 服务端没有 Opus 依赖，音频对它是不透明字节。
func (r *Router) Fanout(from SessionID, packet []byte) (int, error) {
	h, opus, err := wire.Parse(packet)
	if err != nil {
		return 0, err
	}
	// 第一道校验：没声明在这个频率上发送就丢弃，
	// 否则任何人都能往任意频率喊话。
	if !r.MayTransmit(from, h.FreqKHz) {
		return 0, fmt.Errorf("session %d has not declared transmit on %d", from, h.FreqKHz)
	}
	sender, ok := r.Get(from)
	if !ok {
		return 0, fmt.Errorf("session %d is gone", from)
	}

	delivered := r.deliver(sender, h, opus, h.FreqKHz)

	// 交叉耦合：把这次发言同时送到耦合的频率上。
	// 做在服务端而不是客户端，是因为服务端知道全部路由——
	// 客户端那侧因此不再需要"临时改 voice target 再改回来"的那段逻辑。
	for _, pair := range sender.crossCoupled(h.FreqKHz) {
		delivered += r.deliver(sender, h, opus, pair)
	}
	return delivered, nil
}

// deliver 把一个包投递到单个频率的全部订阅者。
func (r *Router) deliver(sender *Session, h wire.Header, opus []byte, freq uint32) int {
	snap, degraded := r.positions()
	var senderPos fsdfeed.Position
	var senderKnown bool
	if !degraded {
		senderPos, senderKnown = lookup(snap, sender)
	}

	n := 0
	for _, l := range r.Listeners(freq) {
		// 发言者不会收到自己的声音。
		if l.ID == sender.ID {
			continue
		}

		qual := uint8(255)
		if !degraded && senderKnown {
			if lp, ok := lookup(snap, l); ok {
				// 射程取两者中较小的：一方够不着就是够不着。
				rng := senderPos.RangeNM
				if lp.RangeNM < rng {
					rng = lp.RangeNM
				}
				// 双方都是飞行员时用视距公式，它比任何声明值都准。
				if !senderPos.IsATC && !lp.IsATC {
					rng = geo.LineOfSightNM(senderPos.AltFt, lp.AltFt)
				}
				d := geo.DistanceNM(senderPos.Lat, senderPos.Lon, lp.Lat, lp.Lon)
				q, inRange := geo.Quality(d, rng)
				if !inRange {
					continue
				}
				qual = q
			}
			// 位置未知的订阅者照常投递：一个只连了语音没连 FSD 的人
			// 不该因此变成聋子。
		}

		out := wire.Header{
			Ver:     wire.Version,
			Flags:   h.Flags,
			Qual:    qual,
			Seq:     h.Seq,
			FreqKHz: freq,
			Speaker: uint32(sender.ID),
		}.AppendTo(nil)
		l.Send(append(out, opus...))
		n++
	}
	return n
}

func (r *Router) positions() (fsdfeed.Snapshot, bool) {
	r.mu.RLock()
	l := r.locator
	r.mu.RUnlock()
	if l == nil {
		return fsdfeed.Snapshot{}, true
	}
	return l.Snapshot(), l.Degraded()
}

// lookup 找一个会话的位置。观察员模式下 Follow 指向它跟随的飞机，
// 因为观察员自己没有 FSD 连接（spec 7.3）。
func lookup(s fsdfeed.Snapshot, sess *Session) (fsdfeed.Position, bool) {
	if sess.Follow != "" {
		p, ok := s.ByCallsign[sess.Follow]
		return p, ok
	}
	p, ok := s.ByCID[sess.CID]
	return p, ok
}

// crossCoupled 返回与 freq 耦合的其它频率。
func (s *Session) crossCoupled(freq uint32) []uint32 {
	var out []uint32
	for _, pair := range s.xc {
		switch freq {
		case pair[0]:
			out = append(out, pair[1])
		case pair[1]:
			out = append(out, pair[0])
		}
	}
	return out
}
```

在 `router.go` 的 `Router` 结构体里加上字段：

```go
	locator Locator
```

- [ ] **Step 4: 跑测试确认通过**

Run: `go test ./server/internal/router/ -v && go vet ./server/...`
Expected: PASS（十四个测试）

- [ ] **Step 5: 提交**

```bash
git add server/internal/router/
git commit -m "router: 扇出、射程过滤与服务端交叉耦合"
```

---

### Task 9: QUIC 传输层与握手

**Files:**
- Create: `server/internal/transport/server.go`
- Create: `server/internal/transport/conn.go`
- Create: `server/internal/transport/conn_test.go`

**Interfaces:**
- Consumes: 全部前序包
- Produces:
  - `const ALPN = "can-voice/1"`
  - `type Config struct { Addr string; TLS *tls.Config; PublicKey ed25519.PublicKey; MaxRX int; ServerName string }`
  - `func Serve(ctx context.Context, cfg Config, r *router.Router) error`
  - `func handleControl(…)` —— 内部，但由测试覆盖

- [ ] **Step 1: 写失败的测试**

`server/internal/transport/conn_test.go`：

```go
package transport

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/tls"
	"testing"
	"time"

	"github.com/JianyueLab-Org/can-voice/server/internal/auth"
	"github.com/JianyueLab-Org/can-voice/server/internal/control"
	"github.com/JianyueLab-Org/can-voice/server/internal/router"
	"github.com/quic-go/quic-go"
)

func testServer(t *testing.T) (addr string, priv ed25519.PrivateKey, r *router.Router) {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatalf("GenerateKey: %v", err)
	}
	cert, err := SelfSignedCert()
	if err != nil {
		t.Fatalf("SelfSignedCert: %v", err)
	}
	r = router.New()
	ln, err := listen(Config{
		Addr:      "127.0.0.1:0",
		TLS:       &tls.Config{Certificates: []tls.Certificate{cert}},
		PublicKey: pub,
		MaxRX:     32,
	})
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(func() { cancel(); ln.Close() })
	go accept(ctx, ln, Config{PublicKey: pub, MaxRX: 32}, r)
	return ln.Addr().String(), priv, r
}

func dial(t *testing.T, addr string) quic.Connection {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	t.Cleanup(cancel)
	conn, err := quic.DialAddr(ctx, addr, &tls.Config{
		InsecureSkipVerify: true,
		NextProtos:         []string{ALPN},
	}, &quic.Config{EnableDatagrams: true})
	if err != nil {
		t.Fatalf("DialAddr: %v", err)
	}
	t.Cleanup(func() { conn.CloseWithError(0, "") })
	return conn
}

func hello(t *testing.T, conn quic.Connection, token string) (quic.Stream, any) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	st, err := conn.OpenStreamSync(ctx)
	if err != nil {
		t.Fatalf("OpenStreamSync: %v", err)
	}
	b, _ := control.Encode(&control.Hello{Token: token, Client: "test/1", Proto: 1})
	if err := control.WriteFrame(st, b); err != nil {
		t.Fatalf("WriteFrame: %v", err)
	}
	resp, err := control.ReadFrame(st)
	if err != nil {
		t.Fatalf("ReadFrame: %v", err)
	}
	m, err := control.Decode(resp)
	if err != nil {
		t.Fatalf("Decode: %v", err)
	}
	return st, m
}

func TestHelloWithAValidTokenGetsReady(t *testing.T) {
	addr, priv, r := testServer(t)
	tok, err := auth.Sign(priv, auth.Claims{CID: "1000", Rating: 5, MaxTX: 8, Exp: time.Now().Add(time.Minute).Unix()})
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	_, m := hello(t, dial(t, addr), tok)

	ready, ok := m.(*control.Ready)
	if !ok {
		t.Fatalf("server replied %T, want *control.Ready", m)
	}
	if ready.Session == 0 {
		t.Fatal("READY must carry a session id; it becomes the speaker field in every packet")
	}
	if ready.MaxTX != 8 {
		t.Fatalf("READY.MaxTX = %d, want the token's 8", ready.MaxTX)
	}
	if _, ok := r.Get(router.SessionID(ready.Session)); !ok {
		t.Fatal("the session must be registered with the router")
	}
}

func TestHelloWithABadTokenGetsByeAndNoSession(t *testing.T) {
	addr, _, r := testServer(t)
	_, otherPriv, _ := ed25519.GenerateKey(rand.Reader)
	tok, _ := auth.Sign(otherPriv, auth.Claims{CID: "1000", Exp: time.Now().Add(time.Minute).Unix()})

	_, m := hello(t, dial(t, addr), tok)
	bye, ok := m.(*control.Bye)
	if !ok {
		t.Fatalf("server replied %T, want *control.Bye", m)
	}
	if bye.Reason == "" {
		t.Fatal("BYE must say why")
	}
	if _, ok := r.Get(router.SessionID(1)); ok {
		t.Fatal("a rejected HELLO must not leave a session behind")
	}
}

func TestSubGetsSubAckAndTakesEffect(t *testing.T) {
	addr, priv, r := testServer(t)
	tok, _ := auth.Sign(priv, auth.Claims{CID: "1000", MaxTX: 8, Exp: time.Now().Add(time.Minute).Unix()})
	st, m := hello(t, dial(t, addr), tok)
	ready := m.(*control.Ready)

	b, _ := control.Encode(&control.Sub{RX: []uint32{118000, 121800}, TX: []uint32{121800}})
	if err := control.WriteFrame(st, b); err != nil {
		t.Fatalf("WriteFrame: %v", err)
	}
	resp, err := control.ReadFrame(st)
	if err != nil {
		t.Fatalf("ReadFrame: %v", err)
	}
	dm, err := control.Decode(resp)
	if err != nil {
		t.Fatalf("Decode: %v", err)
	}
	if _, ok := dm.(*control.SubAck); !ok {
		t.Fatalf("server replied %T, want *control.SubAck", dm)
	}
	if len(r.Listeners(118000)) != 1 {
		t.Fatal("SUB did not take effect in the router")
	}
	if !r.MayTransmit(router.SessionID(ready.Session), 121800) {
		t.Fatal("SUB did not register the TX frequency")
	}
}

func TestAFirstMessageOtherThanHelloIsRefused(t *testing.T) {
	addr, _, _ := testServer(t)
	conn := dial(t, addr)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	st, err := conn.OpenStreamSync(ctx)
	if err != nil {
		t.Fatalf("OpenStreamSync: %v", err)
	}
	b, _ := control.Encode(&control.Sub{RX: []uint32{118000}})
	if err := control.WriteFrame(st, b); err != nil {
		t.Fatalf("WriteFrame: %v", err)
	}
	resp, err := control.ReadFrame(st)
	if err != nil {
		t.Fatalf("ReadFrame: %v", err)
	}
	m, _ := control.Decode(resp)
	if _, ok := m.(*control.Bye); !ok {
		t.Fatalf("server replied %T, want *control.Bye — nothing may precede HELLO", m)
	}
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `go test ./server/internal/transport/ -v`
Expected: FAIL，`undefined: listen`、`accept`、`ALPN`、`Config`、`SelfSignedCert`

- [ ] **Step 3: 实现**

`server/internal/transport/server.go`：

```go
// Package transport 把 QUIC 接到 router 上。
//
// 一条 QUIC 连接承载全部：控制面走 bidirectional stream（长度前缀 JSON），
// 音频走 unreliable datagram。QUIC 的 datagram 扩展给了 UDP 的延迟特性，
// 同时免掉自写 DTLS 握手与密钥协商（spec 5）。
package transport

import (
	"context"
	"crypto/ecdsa"
	"crypto/ed25519"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"log/slog"
	"math/big"
	"time"

	"github.com/JianyueLab-Org/can-voice/server/internal/router"
	"github.com/quic-go/quic-go"
)

// ALPN 是 can-voice 的应用层协议标识。
const ALPN = "can-voice/1"

// ServerVersion 出现在 READY 里，供客户端记录与排障。
const ServerVersion = "can-voice/1.0.0"

// Config 是传输层的全部配置。
type Config struct {
	Addr string
	TLS  *tls.Config
	// PublicKey 是 can-api 的 Ed25519 公钥，用来本地验签 token。
	PublicKey ed25519.PublicKey
	// MaxRX 是单个会话允许订阅的频率数上限。
	MaxRX int
}

// Serve 起监听并接受连接，直到 ctx 取消。
func Serve(ctx context.Context, cfg Config, r *router.Router) error {
	ln, err := listen(cfg)
	if err != nil {
		return err
	}
	defer ln.Close()
	slog.Info("listening", "addr", ln.Addr().String(), "alpn", ALPN)
	accept(ctx, ln, cfg, r)
	return ctx.Err()
}

func listen(cfg Config) (*quic.Listener, error) {
	tlsConf := cfg.TLS.Clone()
	tlsConf.NextProtos = []string{ALPN}
	return quic.ListenAddr(cfg.Addr, tlsConf, &quic.Config{
		EnableDatagrams: true,
		// 空闲超时比任何一次正常静默都长：管制员可能几分钟不说话，
		// 但 QUIC 的保活会撑住连接。
		MaxIdleTimeout:  60 * time.Second,
		KeepAlivePeriod: 15 * time.Second,
	})
}

func accept(ctx context.Context, ln *quic.Listener, cfg Config, r *router.Router) {
	for {
		conn, err := ln.Accept(ctx)
		if err != nil {
			if ctx.Err() == nil {
				slog.Error("accept failed", "error", err)
			}
			return
		}
		go handleConn(ctx, conn, cfg, r)
	}
}

// SelfSignedCert 仅供测试。生产用 Let's Encrypt 证书链——
// 客户端因此不需要指纹固定，也就没有"证书换了则全网拒连"的风险（spec 6）。
func SelfSignedCert() (tls.Certificate, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return tls.Certificate{}, err
	}
	tmpl := x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: "can-voice-test"},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(24 * time.Hour),
	}
	der, err := x509.CreateCertificate(rand.Reader, &tmpl, &tmpl, &key.PublicKey, key)
	if err != nil {
		return tls.Certificate{}, err
	}
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}, nil
}
```

`server/internal/transport/conn.go`：

```go
package transport

import (
	"context"
	"log/slog"
	"time"

	"github.com/JianyueLab-Org/can-voice/server/internal/auth"
	"github.com/JianyueLab-Org/can-voice/server/internal/control"
	"github.com/JianyueLab-Org/can-voice/server/internal/router"
	"github.com/quic-go/quic-go"
)

// handleConn 处理一条连接的完整生命周期。
func handleConn(ctx context.Context, conn quic.Connection, cfg Config, r *router.Router) {
	st, err := conn.AcceptStream(ctx)
	if err != nil {
		return
	}
	sess, err := handshake(st, conn, cfg, r)
	if err != nil {
		slog.Info("handshake refused", "peer", conn.RemoteAddr().String(), "error", err)
		sendBye(st, err.Error())
		conn.CloseWithError(1, "handshake refused")
		return
	}
	slog.Info("session opened", "session", sess.ID, "cid", sess.CID, "peer", conn.RemoteAddr().String())
	defer func() {
		r.Remove(sess.ID)
		slog.Info("session closed", "session", sess.ID, "cid", sess.CID)
	}()

	go readDatagrams(ctx, conn, r, sess.ID)
	readControl(st, r, sess)
}

// handshake 要求第一条消息必须是 HELLO，验签通过后登记会话。
func handshake(st quic.Stream, conn quic.Connection, cfg Config, r *router.Router) (*router.Session, error) {
	b, err := control.ReadFrame(st)
	if err != nil {
		return nil, err
	}
	m, err := control.Decode(b)
	if err != nil {
		return nil, err
	}
	h, ok := m.(*control.Hello)
	if !ok {
		// 什么都不能排在 HELLO 前面：一个未鉴权的连接不该能改动任何状态。
		return nil, errFirstMessageMustBeHello
	}
	claims, err := auth.Verify(cfg.PublicKey, h.Token, time.Now())
	if err != nil {
		return nil, err
	}

	sess := r.Add(claims.CID, h.Follow, claims.MaxTX, func(p []byte) {
		// datagram 发送失败不是错误：它本来就是不可靠的，
		// 丢了就丢了，下一帧 20 毫秒后就到。
		_ = conn.SendDatagram(p)
	})
	ready := &control.Ready{
		Session: uint32(sess.ID),
		Server:  ServerVersion,
		MaxTX:   claims.MaxTX,
		MaxRX:   cfg.MaxRX,
	}
	out, err := control.Encode(ready)
	if err != nil {
		r.Remove(sess.ID)
		return nil, err
	}
	if err := control.WriteFrame(st, out); err != nil {
		r.Remove(sess.ID)
		return nil, err
	}
	return sess, nil
}

type helloError string

func (e helloError) Error() string { return string(e) }

const errFirstMessageMustBeHello = helloError("the first control message must be HELLO")

// readControl 处理握手之后的控制面消息。
func readControl(st quic.Stream, r *router.Router, sess *router.Session) {
	for {
		b, err := control.ReadFrame(st)
		if err != nil {
			return
		}
		m, err := control.Decode(b)
		if err != nil {
			slog.Warn("undecodable control frame", "session", sess.ID, "error", err)
			continue
		}
		switch v := m.(type) {
		case *control.Sub:
			ack := r.Subscribe(sess.ID, *v)
			slog.Debug("subscription replaced", "session", sess.ID,
				"rx", len(ack.RX), "tx", len(ack.TX), "rejected", len(ack.Rejected))
			if out, err := control.Encode(&ack); err == nil {
				_ = control.WriteFrame(st, out)
			}
		case *control.Ping:
			if out, err := control.Encode(&control.Pong{T: v.T, ServerT: time.Now().UnixMilli()}); err == nil {
				_ = control.WriteFrame(st, out)
			}
		case *control.Hello:
			// 重复的 HELLO 是客户端 bug。忽略而不是重建会话——
			// 重建会换掉 session id，而那正是每个包里的 speaker 字段。
			slog.Warn("ignoring a second HELLO on an established session", "session", sess.ID)
		default:
			slog.Warn("unexpected control message from a client", "session", sess.ID, "message", m)
		}
	}
}

// readDatagrams 把上行音频交给 router 扇出。
func readDatagrams(ctx context.Context, conn quic.Connection, r *router.Router, id router.SessionID) {
	for {
		p, err := conn.ReceiveDatagram(ctx)
		if err != nil {
			return
		}
		if _, err := r.Fanout(id, p); err != nil {
			// 这里刻意是 Debug：一个还没发完 SUB 就开始说话的客户端
			// 会刷满日志，而它并不是服务端的问题。
			slog.Debug("dropped an inbound packet", "session", id, "error", err)
		}
	}
}

func sendBye(st quic.Stream, reason string) {
	if out, err := control.Encode(&control.Bye{Reason: reason}); err == nil {
		_ = control.WriteFrame(st, out)
	}
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `go test ./server/... -v && go vet ./server/...`
Expected: PASS（全部包）

- [ ] **Step 5: 提交**

```bash
git add server/internal/transport/
git commit -m "transport: QUIC 握手、控制面循环与 datagram 接收"
```

---

### Task 10: 端到端语音路径测试

前面每个包都单独测过，但"一个客户端说话，另一个客户端听见"这条完整路径还没有被验证过。
这是唯一能发现各层拼接错误的测试。

**Files:**
- Create: `server/internal/transport/e2e_test.go`

**Interfaces:**
- Consumes: Task 1–9 的全部
- Produces: 无新接口，纯测试

- [ ] **Step 1: 写测试**

`server/internal/transport/e2e_test.go`：

```go
package transport

import (
	"context"
	"testing"
	"time"

	"github.com/JianyueLab-Org/can-voice/server/internal/auth"
	"github.com/JianyueLab-Org/can-voice/server/internal/control"
	"github.com/JianyueLab-Org/can-voice/server/internal/wire"
	"github.com/quic-go/quic-go"
)

// client 是端到端测试用的最小客户端。
type client struct {
	conn quic.Connection
	st   quic.Stream
	sess uint32
}

func connect(t *testing.T, addr, cid string, maxTX int, priv []byte) *client {
	t.Helper()
	tok, err := auth.Sign(priv, auth.Claims{
		CID: cid, Rating: 5, MaxTX: maxTX, Exp: time.Now().Add(time.Minute).Unix(),
	})
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	conn := dial(t, addr)
	st, m := hello(t, conn, tok)
	ready, ok := m.(*control.Ready)
	if !ok {
		t.Fatalf("handshake for %s got %T, want *control.Ready", cid, m)
	}
	return &client{conn: conn, st: st, sess: ready.Session}
}

func (c *client) subscribe(t *testing.T, sub control.Sub) {
	t.Helper()
	b, _ := control.Encode(&sub)
	if err := control.WriteFrame(c.st, b); err != nil {
		t.Fatalf("WriteFrame: %v", err)
	}
	if _, err := control.ReadFrame(c.st); err != nil {
		t.Fatalf("ReadFrame (SUBACK): %v", err)
	}
}

// TestOnePersonSpeaksAndAnotherHearsThem 是这个服务端存在的理由。
func TestOnePersonSpeaksAndAnotherHearsThem(t *testing.T) {
	addr, priv, _ := testServer(t)

	speaker := connect(t, addr, "1000", 8, priv)
	listener := connect(t, addr, "1001", 8, priv)

	speaker.subscribe(t, control.Sub{TX: []uint32{121800}})
	listener.subscribe(t, control.Sub{RX: []uint32{121800}})

	opus := []byte{0x01, 0x02, 0x03, 0x04}
	pkt := append(wire.Header{
		Ver: wire.Version, Flags: wire.FlagFirst, Seq: 7, FreqKHz: 121800,
	}.AppendTo(nil), opus...)

	if err := speaker.conn.SendDatagram(pkt); err != nil {
		t.Fatalf("SendDatagram: %v", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	got, err := listener.conn.ReceiveDatagram(ctx)
	if err != nil {
		t.Fatalf("listener received nothing: %v", err)
	}

	h, payload, err := wire.Parse(got)
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if h.FreqKHz != 121800 {
		t.Fatalf("FreqKHz = %d, want 121800", h.FreqKHz)
	}
	if h.Speaker != speaker.sess {
		t.Fatalf("Speaker = %d, want the speaker's session %d", h.Speaker, speaker.sess)
	}
	if h.Seq != 7 {
		t.Fatalf("Seq = %d, want the sender's 7", h.Seq)
	}
	if h.Flags != wire.FlagFirst {
		t.Fatalf("Flags = %d, want FlagFirst preserved", h.Flags)
	}
	if h.Qual == 0 {
		t.Fatal("Qual = 0; a delivered packet must carry a non-zero quality")
	}
	if string(payload) != string(opus) {
		t.Fatalf("opus = %v, want %v — the server must never touch the audio", payload, opus)
	}
}

// TestResendingSubAfterAReconnectRestoresEverything 钉住声明式订阅的核心价值：
// 重连后客户端只需重发一次 SUB，不需要任何"我原来在哪个频道"的记忆。
func TestResendingSubAfterAReconnectRestoresEverything(t *testing.T) {
	addr, priv, r := testServer(t)

	c := connect(t, addr, "1000", 8, priv)
	c.subscribe(t, control.Sub{RX: []uint32{118000, 121800}})
	if len(r.Listeners(118000)) != 1 {
		t.Fatal("first subscription did not take effect")
	}

	// 模拟掉线。
	c.conn.CloseWithError(0, "simulated drop")
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) && len(r.Listeners(118000)) != 0 {
		time.Sleep(20 * time.Millisecond)
	}
	if len(r.Listeners(118000)) != 0 {
		t.Fatal("a dropped connection must leave no listeners behind")
	}

	// 重连并重发同一份 SUB。
	c2 := connect(t, addr, "1000", 8, priv)
	c2.subscribe(t, control.Sub{RX: []uint32{118000, 121800}})
	if len(r.Listeners(118000)) != 1 || len(r.Listeners(121800)) != 1 {
		t.Fatal("resending SUB after a reconnect must fully restore the subscription")
	}
}

// TestAListenerOnAnotherFrequencyHearsNothing 钉住频率隔离。
func TestAListenerOnAnotherFrequencyHearsNothing(t *testing.T) {
	addr, priv, _ := testServer(t)

	speaker := connect(t, addr, "1000", 8, priv)
	other := connect(t, addr, "1002", 8, priv)
	speaker.subscribe(t, control.Sub{TX: []uint32{121800}})
	other.subscribe(t, control.Sub{RX: []uint32{124550}})

	pkt := wire.Header{Ver: wire.Version, FreqKHz: 121800}.AppendTo(nil)
	if err := speaker.conn.SendDatagram(pkt); err != nil {
		t.Fatalf("SendDatagram: %v", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer cancel()
	if _, err := other.conn.ReceiveDatagram(ctx); err == nil {
		t.Fatal("a listener on 124550 must not hear a transmission on 121800")
	}
}
```

- [ ] **Step 2: 跑测试**

Run: `go test ./server/internal/transport/ -v -race`
Expected: PASS。`-race` 是必须的 —— router 的表被 datagram 线程和控制面线程同时访问。

- [ ] **Step 3: 提交**

```bash
git add server/internal/transport/e2e_test.go
git commit -m "transport: 端到端语音路径与重连恢复测试"
```

---

### Task 11: 进程入口、配置与可观测性

**Files:**
- Create: `server/cmd/can-voice/main.go`
- Create: `server/cmd/can-voice/config.go`
- Create: `server/cmd/can-voice/config_test.go`
- Create: `server/deploy/can-voice.service`
- Create: `server/README.md`

**Interfaces:**
- Consumes: `transport.Serve`、`router.New`、`fsdfeed.NewFeed`
- Produces: `can-voice` 可执行文件；全部配置来自环境变量，缺一不可的项启动即失败

沿用 can-api 的做法：**全部环境变量，没有配置文件，启动时缺任何必需项就失败**。一个语音
服务端带着半份配置跑起来，比根本起不来危险得多。

- [ ] **Step 1: 写失败的测试**

`server/cmd/can-voice/config_test.go`：

```go
package main

import (
	"strings"
	"testing"
)

func env(m map[string]string) func(string) string {
	return func(k string) string { return m[k] }
}

func TestLoadConfigRequiresEveryEssentialValue(t *testing.T) {
	for _, missing := range []string{"CAN_VOICE_ADDR", "CAN_VOICE_TLS_CERT", "CAN_VOICE_TLS_KEY", "CAN_VOICE_API_PUBKEY"} {
		full := map[string]string{
			"CAN_VOICE_ADDR":       ":64738",
			"CAN_VOICE_TLS_CERT":   "/tmp/c.pem",
			"CAN_VOICE_TLS_KEY":    "/tmp/k.pem",
			"CAN_VOICE_API_PUBKEY": "3q2+7w==",
		}
		delete(full, missing)
		if _, err := LoadConfig(env(full)); err == nil {
			t.Fatalf("LoadConfig must fail when %s is missing", missing)
		} else if !strings.Contains(err.Error(), missing) {
			t.Fatalf("error must name the missing variable %s, got: %v", missing, err)
		}
	}
}

func TestLoadConfigRejectsAnUnparsablePublicKey(t *testing.T) {
	_, err := LoadConfig(env(map[string]string{
		"CAN_VOICE_ADDR":       ":64738",
		"CAN_VOICE_TLS_CERT":   "/tmp/c.pem",
		"CAN_VOICE_TLS_KEY":    "/tmp/k.pem",
		"CAN_VOICE_API_PUBKEY": "!!! not base64 !!!",
	}))
	if err == nil {
		t.Fatal("LoadConfig must reject a public key it cannot decode")
	}
}

func TestLoadConfigRejectsAPublicKeyOfTheWrongLength(t *testing.T) {
	// Ed25519 公钥恰好 32 字节。长度不对却能解码的话，
	// 每一次验签都会失败，而错误信息会指向 token 而不是配置。
	_, err := LoadConfig(env(map[string]string{
		"CAN_VOICE_ADDR":       ":64738",
		"CAN_VOICE_TLS_CERT":   "/tmp/c.pem",
		"CAN_VOICE_TLS_KEY":    "/tmp/k.pem",
		"CAN_VOICE_API_PUBKEY": "AAAA",
	}))
	if err == nil {
		t.Fatal("LoadConfig must reject an Ed25519 public key that is not 32 bytes")
	}
}

func TestLoadConfigDefaultsTheOptionalValues(t *testing.T) {
	cfg, err := LoadConfig(env(map[string]string{
		"CAN_VOICE_ADDR":       ":64738",
		"CAN_VOICE_TLS_CERT":   "/tmp/c.pem",
		"CAN_VOICE_TLS_KEY":    "/tmp/k.pem",
		"CAN_VOICE_API_PUBKEY": "MCowBQYDK2VwAyEAGb9ECWmEzf6FQbrBZ9w7lshQhqowtrbLDFw4rXAxZuE=",
	}))
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	if cfg.MaxRX != 32 {
		t.Fatalf("MaxRX = %d, want the 32 default", cfg.MaxRX)
	}
	if cfg.FeedURL == "" {
		t.Fatal("FeedURL must have a default; it is how range filtering gets its input")
	}
}
```

注：最后一个用例里的 base64 是一段 44 字符的 DER 包装公钥；`LoadConfig` 只接受**裸 32 字节**
的 base64。把它换成一个真正的 32 字节值再跑 —— 用
`openssl rand -base64 32` 生成一个即可，或直接用 `AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=`
（32 个零字节）。

- [ ] **Step 2: 跑测试确认失败**

Run: `go test ./server/cmd/can-voice/ -v`
Expected: FAIL，`undefined: LoadConfig`

- [ ] **Step 3: 实现**

`server/cmd/can-voice/config.go`：

```go
package main

import (
	"crypto/ed25519"
	"encoding/base64"
	"fmt"
	"strconv"
)

// Config 是进程的全部配置。
//
// 全部来自环境变量，没有配置文件，缺任何必需项就启动失败——
// 与 can-api 同样的做法。一个语音服务端带着半份配置跑起来，
// 比根本起不来危险得多。
type Config struct {
	Addr    string
	Cert    string
	Key     string
	PubKey  ed25519.PublicKey
	FeedURL string
	MaxRX   int
}

// defaultFeedURL 是 can-fsd 的 SSE 流。射程过滤的输入就来自这里；
// 连不上时服务端降级为不做射程过滤，而不是拒绝服务（spec 7.3）。
const defaultFeedURL = "https://data.ceruleanavi.net/v1/events"

// LoadConfig 从一个取环境变量的函数里读配置。
// 传函数而不是直接读 os.Getenv，是为了让它可以被测试。
func LoadConfig(get func(string) string) (Config, error) {
	var cfg Config
	for _, req := range []struct {
		name string
		dst  *string
	}{
		{"CAN_VOICE_ADDR", &cfg.Addr},
		{"CAN_VOICE_TLS_CERT", &cfg.Cert},
		{"CAN_VOICE_TLS_KEY", &cfg.Key},
	} {
		v := get(req.name)
		if v == "" {
			return Config{}, fmt.Errorf("%s is required", req.name)
		}
		*req.dst = v
	}

	raw := get("CAN_VOICE_API_PUBKEY")
	if raw == "" {
		return Config{}, fmt.Errorf("CAN_VOICE_API_PUBKEY is required")
	}
	key, err := base64.StdEncoding.DecodeString(raw)
	if err != nil {
		return Config{}, fmt.Errorf("CAN_VOICE_API_PUBKEY is not base64: %w", err)
	}
	// Ed25519 公钥恰好 32 字节。长度不对却能解码的话每次验签都会失败，
	// 而错误信息会指向 token 而不是这份配置。
	if len(key) != ed25519.PublicKeySize {
		return Config{}, fmt.Errorf(
			"CAN_VOICE_API_PUBKEY decodes to %d bytes, an Ed25519 public key is %d",
			len(key), ed25519.PublicKeySize)
	}
	cfg.PubKey = ed25519.PublicKey(key)

	cfg.FeedURL = get("CAN_VOICE_FSD_FEED")
	if cfg.FeedURL == "" {
		cfg.FeedURL = defaultFeedURL
	}
	cfg.MaxRX = 32
	if v := get("CAN_VOICE_MAX_RX"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n <= 0 {
			return Config{}, fmt.Errorf("CAN_VOICE_MAX_RX must be a positive integer, got %q", v)
		}
		cfg.MaxRX = n
	}
	return cfg, nil
}
```

`server/cmd/can-voice/main.go`：

```go
// can-voice 是 Cerulean Aviation Network 的语音服务端。
//
// 无状态：没有数据库、没有持久化、没有 ACL。进程重启等于所有人重连并重发 SUB。
// 这也意味着没有"丢了某个卷就全网瘫痪且无法远程修复"的风险（spec 8）。
package main

import (
	"context"
	"crypto/tls"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/JianyueLab-Org/can-voice/server/internal/fsdfeed"
	"github.com/JianyueLab-Org/can-voice/server/internal/router"
	"github.com/JianyueLab-Org/can-voice/server/internal/transport"
)

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, &slog.HandlerOptions{
		Level: levelFromEnv(),
	})))

	cfg, err := LoadConfig(os.Getenv)
	if err != nil {
		slog.Error("configuration is incomplete", "error", err)
		os.Exit(1)
	}
	cert, err := tls.LoadX509KeyPair(cfg.Cert, cfg.Key)
	if err != nil {
		slog.Error("cannot load the TLS key pair", "error", err)
		os.Exit(1)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	r := router.New()
	feed := fsdfeed.NewFeed(cfg.FeedURL)
	r.SetLocator(feed)
	go feed.Run(ctx)

	slog.Info("starting", "addr", cfg.Addr, "feed", cfg.FeedURL, "max_rx", cfg.MaxRX)
	if err := transport.Serve(ctx, transport.Config{
		Addr:      cfg.Addr,
		TLS:       &tls.Config{Certificates: []tls.Certificate{cert}},
		PublicKey: cfg.PubKey,
		MaxRX:     cfg.MaxRX,
	}, r); err != nil && ctx.Err() == nil {
		slog.Error("server stopped", "error", err)
		os.Exit(1)
	}
	slog.Info("shut down cleanly")
}

func levelFromEnv() slog.Level {
	if os.Getenv("CAN_VOICE_DEBUG") != "" {
		return slog.LevelDebug
	}
	return slog.LevelInfo
}
```

`server/deploy/can-voice.service`：

```ini
[Unit]
Description=can-voice QUIC voice server
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=/etc/can-voice.env
ExecStart=/usr/local/bin/can-voice
Restart=always
RestartSec=5
DynamicUser=yes
SupplementaryGroups=ssl-cert
AmbientCapabilities=CAP_NET_BIND_SERVICE
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
```

`server/README.md`：

```markdown
# can-voice 服务端

QUIC 语音扇出服务。无状态：没有数据库、没有持久化、没有 ACL。

## 配置

全部环境变量，缺任何必需项启动即失败。

| 变量 | 必需 | 说明 |
|---|---|---|
| `CAN_VOICE_ADDR` | 是 | UDP 监听地址，如 `:64738` |
| `CAN_VOICE_TLS_CERT` | 是 | 证书链 PEM（Let's Encrypt） |
| `CAN_VOICE_TLS_KEY` | 是 | 私钥 PEM |
| `CAN_VOICE_API_PUBKEY` | 是 | can-api 的 Ed25519 公钥，裸 32 字节的 base64 |
| `CAN_VOICE_FSD_FEED` | 否 | can-fsd 的 SSE，默认 `https://data.ceruleanavi.net/v1/events` |
| `CAN_VOICE_MAX_RX` | 否 | 单会话订阅频率上限，默认 32 |
| `CAN_VOICE_DEBUG` | 否 | 非空则日志降到 DEBUG |

## 运行

    go build -o can-voice ./server/cmd/can-voice
    CAN_VOICE_ADDR=:64738 \
    CAN_VOICE_TLS_CERT=/etc/letsencrypt/live/audio.ceruleanavi.net/fullchain.pem \
    CAN_VOICE_TLS_KEY=/etc/letsencrypt/live/audio.ceruleanavi.net/privkey.pem \
    CAN_VOICE_API_PUBKEY=$(cat /etc/can-voice/api.pub) \
    ./can-voice

## 排障

- **所有人都连不上**：`CAN_VOICE_API_PUBKEY` 与 can-api 的私钥不配对，日志里每条
  会是 `token signature does not verify`。
- **管制员谁都听不见**：位置解析问题。can-fsd 的 datafeed 里管制员的经纬度是 JSON
  **字符串**而飞行员的是数字；解析错会让管制员全部落在 0,0。
  `internal/fsdfeed` 的 `TestControllerCoordinatesParseFromStrings` 钉着这条。
- **射程完全不起作用**：检查日志里有没有 `fsd feed dropped`。SSE 断开时服务端刻意
  降级为不做射程过滤——语音能不能通比射程真实感重要得多。
```

- [ ] **Step 4: 跑测试确认通过**

Run: `go test ./server/... -race && go vet ./server/...`
Expected: PASS

- [ ] **Step 5: 本地起一次**

```bash
go build -o /tmp/can-voice ./server/cmd/can-voice
CAN_VOICE_ADDR=:64738 /tmp/can-voice
```

Expected: 报 `CAN_VOICE_TLS_CERT is required` 并以 1 退出。这验证了"缺配置就不启动"。

- [ ] **Step 6: 提交**

```bash
git add server/cmd/ server/deploy/ server/README.md
git commit -m "can-voice: 进程入口、环境变量配置与部署单元"
```

---

### Task 12: stream 回退通道（**条件任务**）

**只有当 `docs/p1-connectivity-findings.md` 判定"必须实现 stream 回退通道"时才做这个任务。**
判定为"datagram-only is sufficient"时跳过，并在 `server/README.md` 里记一句为什么没有回退。

**Files:**
- Create: `server/internal/transport/fallback.go`
- Create: `server/internal/transport/fallback_test.go`
- Modify: `server/internal/transport/conn.go`

**Interfaces:**
- Consumes: `control.WriteFrame`/`ReadFrame`（Task 2）、`wire`（Task 1）、`router.Fanout`（Task 8）
- Produces: 第二条 QUIC stream 承载与 datagram 完全相同的 13 字节头加 Opus 帧；`Session.Send` 按会话的通道模式选择出口

设计要点：**帧格式一个字节都不变**（spec §12 已经为此预留），所以回退只是换一个出口，
不是第二套协议。客户端在 `HELLO` 里加一个 `"transport":"stream"` 字段声明它要走回退。

- [ ] **Step 1: 读 P1 结论**

```bash
grep -A2 VERDICT docs/p1-connectivity-findings.md
```

若结论是 `datagram-only is sufficient`：跳过本任务，在 `server/README.md` 末尾加一节
「为什么没有 stream 回退」，引用 P1 的样本量与判定，然后提交。本计划到此结束。

若结论是 `a stream fallback channel is required`：继续。

- [ ] **Step 2: 写失败的测试**

`server/internal/transport/fallback_test.go`：

```go
package transport

import (
	"context"
	"testing"
	"time"

	"github.com/JianyueLab-Org/can-voice/server/internal/auth"
	"github.com/JianyueLab-Org/can-voice/server/internal/control"
	"github.com/JianyueLab-Org/can-voice/server/internal/wire"
	"github.com/quic-go/quic-go"
)

// 回退通道承载的必须是与 datagram 完全相同的字节。
// 两套帧格式意味着两套 bug，也意味着 Rust 侧要写两遍解析。
func TestStreamFallbackCarriesTheIdenticalWireFormat(t *testing.T) {
	addr, priv, _ := testServer(t)

	tok, _ := auth.Sign(priv, auth.Claims{CID: "1000", MaxTX: 8, Exp: time.Now().Add(time.Minute).Unix()})
	speakerConn := dial(t, addr)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	sst, err := speakerConn.OpenStreamSync(ctx)
	if err != nil {
		t.Fatalf("OpenStreamSync: %v", err)
	}
	b, _ := control.Encode(&control.Hello{Token: tok, Client: "test/1", Proto: 1, Transport: "stream"})
	if err := control.WriteFrame(sst, b); err != nil {
		t.Fatalf("WriteFrame: %v", err)
	}
	if _, err := control.ReadFrame(sst); err != nil {
		t.Fatalf("ReadFrame (READY): %v", err)
	}

	tok2, _ := auth.Sign(priv, auth.Claims{CID: "1001", MaxTX: 8, Exp: time.Now().Add(time.Minute).Unix()})
	listenerConn := dial(t, addr)
	lst, err := listenerConn.OpenStreamSync(ctx)
	if err != nil {
		t.Fatalf("OpenStreamSync: %v", err)
	}
	b2, _ := control.Encode(&control.Hello{Token: tok2, Client: "test/1", Proto: 1, Transport: "stream"})
	if err := control.WriteFrame(lst, b2); err != nil {
		t.Fatalf("WriteFrame: %v", err)
	}
	if _, err := control.ReadFrame(lst); err != nil {
		t.Fatalf("ReadFrame (READY): %v", err)
	}

	subscribeOn(t, sst, control.Sub{TX: []uint32{121800}})
	subscribeOn(t, lst, control.Sub{RX: []uint32{121800}})

	// 音频走各自的第二条 stream。
	sAudio, err := speakerConn.OpenStreamSync(ctx)
	if err != nil {
		t.Fatalf("OpenStreamSync (audio): %v", err)
	}
	lAudio, err := listenerConn.OpenStreamSync(ctx)
	if err != nil {
		t.Fatalf("OpenStreamSync (audio): %v", err)
	}
	// 每条音频 stream 上先发一个空帧，让服务端认领它属于哪个会话。
	if err := control.WriteFrame(lAudio, nil); err != nil {
		t.Fatalf("claim audio stream: %v", err)
	}
	if err := control.WriteFrame(sAudio, nil); err != nil {
		t.Fatalf("claim audio stream: %v", err)
	}

	opus := []byte{0x11, 0x22, 0x33}
	pkt := append(wire.Header{Ver: wire.Version, Flags: wire.FlagFirst, Seq: 5, FreqKHz: 121800}.AppendTo(nil), opus...)
	if err := control.WriteFrame(sAudio, pkt); err != nil {
		t.Fatalf("send over stream: %v", err)
	}

	got, err := control.ReadFrame(lAudio)
	if err != nil {
		t.Fatalf("listener received nothing over the fallback: %v", err)
	}
	h, payload, err := wire.Parse(got)
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if h.FreqKHz != 121800 || h.Seq != 5 || string(payload) != string(opus) {
		t.Fatalf("fallback delivered %+v %v, want the identical datagram bytes", h, payload)
	}
}

func subscribeOn(t *testing.T, st quic.Stream, sub control.Sub) {
	t.Helper()
	b, _ := control.Encode(&sub)
	if err := control.WriteFrame(st, b); err != nil {
		t.Fatalf("WriteFrame: %v", err)
	}
	if _, err := control.ReadFrame(st); err != nil {
		t.Fatalf("ReadFrame (SUBACK): %v", err)
	}
}
```

- [ ] **Step 3: 跑测试确认失败**

Run: `go test ./server/internal/transport/ -run TestStreamFallback -v`
Expected: FAIL，`control.Hello` 没有 `Transport` 字段

- [ ] **Step 4: 实现**

在 `server/internal/control/message.go` 的 `Hello` 里加：

```go
	// Transport 为 "stream" 时音频走第二条 QUIC stream 而不是 datagram。
	// 帧格式完全相同——回退只是换一个出口，不是第二套协议。
	Transport string `json:"transport,omitempty"`
```

新建 `server/internal/transport/fallback.go`：

```go
package transport

import (
	"context"
	"log/slog"

	"github.com/JianyueLab-Org/can-voice/server/internal/control"
	"github.com/JianyueLab-Org/can-voice/server/internal/router"
	"github.com/quic-go/quic-go"
)

// serveAudioStream 是 datagram 的回退出口。
//
// 承载的是**完全相同**的 13 字节头加 Opus 帧，只是外面套了控制面那个
// 长度前缀——两套帧格式意味着两套 bug，也意味着 Rust 侧要写两遍解析。
//
// 代价是队头阻塞：stream 是可靠有序的，丢一个包会卡住它后面的全部。
// 这换来的是连通性，只给 datagram 走不通的人用。
func serveAudioStream(ctx context.Context, conn quic.Connection, r *router.Router, id router.SessionID, send chan<- []byte) {
	st, err := conn.AcceptStream(ctx)
	if err != nil {
		return
	}
	// 第一帧是认领帧，内容忽略。
	if _, err := control.ReadFrame(st); err != nil {
		return
	}
	slog.Debug("session is using the stream fallback for audio", "session", id)

	go func() {
		for p := range send {
			if err := control.WriteFrame(st, p); err != nil {
				return
			}
		}
	}()

	for {
		p, err := control.ReadFrame(st)
		if err != nil {
			return
		}
		if len(p) == 0 {
			continue
		}
		if _, err := r.Fanout(id, p); err != nil {
			slog.Debug("dropped an inbound packet from the stream fallback", "session", id, "error", err)
		}
	}
}
```

在 `conn.go` 的 `handshake` 里，根据 `h.Transport` 选择 `send` 回调：

```go
	var audioOut chan []byte
	send := func(p []byte) { _ = conn.SendDatagram(p) }
	if h.Transport == "stream" {
		// 带缓冲：回退通道是可靠的，写阻塞会拖住整个扇出循环。
		// 缓冲满了就丢——音频本来就是可丢的。
		audioOut = make(chan []byte, 256)
		out := audioOut
		send = func(p []byte) {
			select {
			case out <- p:
			default:
			}
		}
	}
	sess := r.Add(claims.CID, h.Follow, claims.MaxTX, send)
```

并在 `handleConn` 里，`READY` 发出之后按模式起对应的接收循环：

```go
	if audioOut != nil {
		go serveAudioStream(ctx, conn, r, sess.ID, audioOut)
	} else {
		go readDatagrams(ctx, conn, r, sess.ID)
	}
```

（`handshake` 需要把 `audioOut` 返回给 `handleConn`，改它的签名为
`(*router.Session, chan []byte, error)`。）

- [ ] **Step 5: 跑测试确认通过**

Run: `go test ./server/... -race && go vet ./server/...`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add server/internal/
git commit -m "transport: datagram 走不通时的 stream 回退出口"
```

---

## 完成标准

- [ ] `go test ./server/... -race` 全绿，`go vet ./server/...` 干净
- [ ] `server/testdata/wire-golden.json` 存在，Go 侧四个用例全过 —— P3 会测同一份文件
- [ ] 端到端测试证明"一个人说话、另一个人听见"，且载荷字节未被改动
- [ ] `TestControllerCoordinatesParseFromStrings` 存在并通过（那个会静默出错的陷阱）
- [ ] 缺任何必需环境变量时进程拒绝启动
- [ ] Task 12 要么完成，要么 `server/README.md` 里写明了为什么不需要回退

## 自检记录

对照 spec 逐节检查本计划的覆盖：

| spec 节 | 覆盖于 |
|---|---|
| §5.1 控制面 | Task 2、Task 9 |
| §5.2 数据面包头 | Task 1 |
| §6 鉴权 | Task 3、Task 9 |
| §7.1–7.2 射程 | Task 4 |
| §7.3 位置来源 | Task 5、Task 6 |
| §7.4 信号质量 | Task 4、Task 8 |
| §8 服务端状态与扇出、交叉耦合 | Task 7、Task 8 |
| §10 测试策略（协议黄金文件、扇出表驱动、SSE 解析、端到端） | Task 1、8、5、10 |
| §12 stream 回退 | Task 12（条件） |

spec §9（客户端核心库）与 §11（切换路径）不属于 P2，见 P3 计划与 P5。
