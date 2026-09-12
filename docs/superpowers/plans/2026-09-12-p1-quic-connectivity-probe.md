# P1：QUIC 连通性探针 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 测出真实用户网络里 QUIC datagram 的连通率、丢包与被中断比例，据此判定 can-voice 是否必须实现 stream 回退通道。

**Architecture:** 一个部署在公网的 Go QUIC echo 服务端，加一个分发给测试用户的 Go 命令行工具。客户端跑两轮定量测试 —— datagram 轮和同一条连接上的 stream 对照轮 —— 输出一份 JSON 报告，用户回传。全部代码在结论产出后删除。

**Tech Stack:** Go 1.23+、quic-go v0.48.x、Let's Encrypt 证书

**Spec:** `docs/superpowers/specs/2026-09-12-can-voice-design.md`（§12 风险与未决问题）

## Global Constraints

- **这是丢弃型代码。** 结论产出后整个 `probe/` 目录删除。不要为它写超出判定所需的抽象、配置系统或测试覆盖。
- **探针与生产客户端用不同的 QUIC 实现。** 探针两侧都是 Go/quic-go，生产客户端将是 Rust/quinn。本探针测的是**网络**（UDP 能否通、能否持续），不是库。quinn 与 quic-go 在中间设备指纹、初始包大小、GSO 行为上的差异不在本次测量范围内 —— 那要在 spec §11.1 的封闭测试里用真实客户端复核。这条必须写进最终报告。
- **不收集任何个人信息。** 报告只含网络测量值、运营商自报名称（用户手填）、操作系统与时区。不上传 IP，不自动回传 —— 用户看得到文件内容后自己发回来。
- **判定标准在 Task 9 定义，不许事后调整。** 先写下阈值再看数据。
- **测试用户覆盖要求**：至少 8 个会话，覆盖 ≥3 家大陆运营商（移动/联通/电信）、≥1 个校园网或企业网、≥2 个操作系统。
- 日志与报告字段用英文；给测试用户看的提示文字用中文（与 can-audio 现有约定一致：**日志是英文，面向用户的是中文**）。

---

### Task 1: 建 can-voice 仓库骨架与 probe 服务端的 QUIC 握手

这是新仓库的第一次提交。`probe/` 之外的目录先不建 —— 它们属于 P2/P3。

**Files:**
- Create: `go.mod`（module `github.com/JianyueLab-Org/can-voice`）
- Create: `probe/server/main.go`
- Create: `probe/server/listen.go`
- Create: `probe/server/listen_test.go`
- Create: `.gitignore`
- Create: `README.md`

**Interfaces:**
- Consumes: 无
- Produces: `probe/server.Listen(addr string, tlsConf *tls.Config) (*quic.Listener, error)`，ALPN 常量 `probe.ALPN = "can-voice-probe/1"`

- [ ] **Step 1: 初始化仓库与 go.mod**

```bash
mkdir -p ~/Documents/Dev/CeruleanAviationNetwork/can-voice/probe/server
cd ~/Documents/Dev/CeruleanAviationNetwork/can-voice
git init
go mod init github.com/JianyueLab-Org/can-voice
go get github.com/quic-go/quic-go@v0.48.2
```

写 `.gitignore`：

```
/probe/dist/
/probe/reports/
*.pem
```

写 `README.md`：

```markdown
# can-voice

Cerulean Aviation Network 的语音层：QUIC 语音服务端与客户端核心库。

设计文档见 can-audio 仓库的 `docs/superpowers/specs/2026-09-12-can-voice-design.md`。

`probe/` 是 P1 的一次性连通性探针，结论产出后删除。
```

- [ ] **Step 2: 写失败的测试**

`probe/server/listen_test.go`：

```go
package server

import (
	"crypto/tls"
	"testing"
)

func TestListenNegotiatesProbeALPN(t *testing.T) {
	tlsConf := selfSignedTLS(t)
	ln, err := Listen("127.0.0.1:0", tlsConf)
	if err != nil {
		t.Fatalf("Listen: %v", err)
	}
	defer ln.Close()

	if got := ln.Addr().String(); got == "" {
		t.Fatal("listener reported no address")
	}
	if ALPN != "can-voice-probe/1" {
		t.Fatalf("ALPN = %q, want can-voice-probe/1", ALPN)
	}
	if len(tlsConf.NextProtos) != 1 || tlsConf.NextProtos[0] != ALPN {
		t.Fatalf("Listen must pin NextProtos to %q, got %v", ALPN, tlsConf.NextProtos)
	}
}

func selfSignedTLS(t *testing.T) *tls.Config {
	t.Helper()
	cert, err := SelfSignedCert()
	if err != nil {
		t.Fatalf("SelfSignedCert: %v", err)
	}
	return &tls.Config{Certificates: []tls.Certificate{cert}}
}
```

- [ ] **Step 3: 跑测试确认失败**

Run: `go test ./probe/server/ -run TestListenNegotiatesProbeALPN -v`
Expected: FAIL，`undefined: Listen`、`undefined: ALPN`、`undefined: SelfSignedCert`

- [ ] **Step 4: 实现**

`probe/server/listen.go`：

```go
// Package server 是 P1 连通性探针的服务端。丢弃型代码，结论产出后删除。
package server

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"math/big"
	"time"

	"github.com/quic-go/quic-go"
)

// ALPN 和生产用的 can-voice/1 刻意不同：探针与真正的语音服务可能同时在线，
// 用不同的 ALPN 保证两者不会互相误连。
const ALPN = "can-voice-probe/1"

// Listen 起一个只接受探针 ALPN 的 QUIC 监听器。
// tlsConf 会被原地改写 NextProtos —— 探针不接受调用方自带的协议列表，
// 否则一个拼错的 ALPN 会表现为"所有连接都超时"，而那正是我们要测量的信号。
func Listen(addr string, tlsConf *tls.Config) (*quic.Listener, error) {
	tlsConf.NextProtos = []string{ALPN}
	return quic.ListenAddr(addr, tlsConf, &quic.Config{
		EnableDatagrams: true,
		// 探针要能分辨"被中间设备掐掉"和"自己超时了"，所以空闲超时设得比
		// 测量轮时长（60s）长一截，让前者成为唯一可能的中断原因。
		MaxIdleTimeout:  90 * time.Second,
		KeepAlivePeriod: 15 * time.Second,
	})
}

// SelfSignedCert 仅供本地测试使用。公网部署用 Let's Encrypt，见 Task 8。
func SelfSignedCert() (tls.Certificate, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return tls.Certificate{}, err
	}
	tmpl := x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: "can-voice-probe"},
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

`probe/server/main.go`：

```go
package main

import (
	"crypto/tls"
	"flag"
	"log"
)

func main() {
	addr := flag.String("addr", ":64739", "UDP listen address")
	certFile := flag.String("cert", "", "TLS certificate chain (PEM); self-signed if empty")
	keyFile := flag.String("key", "", "TLS private key (PEM)")
	flag.Parse()

	var tlsConf tls.Config
	if *certFile != "" {
		cert, err := tls.LoadX509KeyPair(*certFile, *keyFile)
		if err != nil {
			log.Fatalf("load certificate: %v", err)
		}
		tlsConf.Certificates = []tls.Certificate{cert}
	} else {
		cert, err := SelfSignedCert()
		if err != nil {
			log.Fatalf("generate self-signed certificate: %v", err)
		}
		tlsConf.Certificates = []tls.Certificate{cert}
		log.Print("using a self-signed certificate; clients must pass -insecure")
	}

	ln, err := Listen(*addr, &tlsConf)
	if err != nil {
		log.Fatalf("listen on %s: %v", *addr, err)
	}
	log.Printf("probe server listening on %s, alpn=%s", *addr, ALPN)
	Serve(ln)
}
```

`main.go` 与 `listen.go` 同包 —— 探针不值得为跨包导入多建一层目录。把 `listen.go` 的
`package server` 改成 `package main`，测试文件同理。

- [ ] **Step 5: 跑测试确认通过**

Run: `go test ./probe/server/ -v`
Expected: PASS。`Serve` 尚未定义会导致编译失败 —— 先在 `listen.go` 里加一个占位实现：

```go
// Serve 在 Task 2 填实。
func Serve(ln *quic.Listener) { select {} }
```

再跑一次，确认 PASS。

- [ ] **Step 6: 提交**

```bash
git add go.mod go.sum .gitignore README.md probe/
git commit -m "probe: QUIC 监听器骨架与探针 ALPN"
```

---

### Task 2: 服务端 datagram 回显

**Files:**
- Modify: `probe/server/listen.go`（替换 Task 1 的 `Serve` 占位）
- Create: `probe/server/serve.go`
- Create: `probe/server/serve_test.go`

**Interfaces:**
- Consumes: `Listen`、`ALPN`、`SelfSignedCert`（Task 1）
- Produces: `Serve(ln *quic.Listener)`，行为：每条连接上收到的每个 datagram 原样发回

回显必须**原样**，包括载荷里的序号和时间戳 —— 客户端靠它们算 RTT 和丢包，服务端不参与
计算，这样服务端就没有任何可能污染测量结果的状态。

- [ ] **Step 1: 写失败的测试**

`probe/server/serve_test.go`：

```go
package main

import (
	"context"
	"crypto/tls"
	"testing"
	"time"

	"github.com/quic-go/quic-go"
)

func TestServeEchoesDatagramsVerbatim(t *testing.T) {
	ln, err := Listen("127.0.0.1:0", &tls.Config{Certificates: []tls.Certificate{mustCert(t)}})
	if err != nil {
		t.Fatalf("Listen: %v", err)
	}
	defer ln.Close()
	go Serve(ln)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	conn, err := quic.DialAddr(ctx, ln.Addr().String(), &tls.Config{
		InsecureSkipVerify: true,
		NextProtos:         []string{ALPN},
	}, &quic.Config{EnableDatagrams: true})
	if err != nil {
		t.Fatalf("DialAddr: %v", err)
	}
	defer conn.CloseWithError(0, "")

	payload := []byte{0x01, 0x02, 0x03, 0xff, 0x00, 0x42}
	if err := conn.SendDatagram(payload); err != nil {
		t.Fatalf("SendDatagram: %v", err)
	}

	got, err := conn.ReceiveDatagram(ctx)
	if err != nil {
		t.Fatalf("ReceiveDatagram: %v", err)
	}
	if string(got) != string(payload) {
		t.Fatalf("echo = %v, want %v (must be verbatim)", got, payload)
	}
}

func mustCert(t *testing.T) tls.Certificate {
	t.Helper()
	cert, err := SelfSignedCert()
	if err != nil {
		t.Fatalf("SelfSignedCert: %v", err)
	}
	return cert
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `go test ./probe/server/ -run TestServeEchoesDatagramsVerbatim -v`
Expected: FAIL，超时于 `ReceiveDatagram`（占位 `Serve` 什么都不做）

- [ ] **Step 3: 实现**

从 `listen.go` 删掉占位 `Serve`。新建 `probe/server/serve.go`：

```go
package main

import (
	"context"
	"errors"
	"log"

	"github.com/quic-go/quic-go"
)

// Serve 接受连接并为每条起一个回显循环。
func Serve(ln *quic.Listener) {
	for {
		conn, err := ln.Accept(context.Background())
		if err != nil {
			log.Printf("accept failed: %v", err)
			return
		}
		log.Printf("connection from %s", conn.RemoteAddr())
		go echoDatagrams(conn)
	}
}

// echoDatagrams 把收到的每个 datagram 原样发回。
// 不解析、不统计、不重排 —— 服务端保持无状态，所有测量都在客户端完成。
func echoDatagrams(conn quic.Connection) {
	for {
		b, err := conn.ReceiveDatagram(context.Background())
		if err != nil {
			var appErr *quic.ApplicationError
			if errors.As(err, &appErr) && appErr.ErrorCode == 0 {
				log.Printf("connection from %s closed cleanly", conn.RemoteAddr())
			} else {
				log.Printf("connection from %s ended: %v", conn.RemoteAddr(), err)
			}
			return
		}
		if err := conn.SendDatagram(b); err != nil {
			log.Printf("send to %s failed: %v", conn.RemoteAddr(), err)
			return
		}
	}
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `go test ./probe/server/ -v`
Expected: PASS（两个测试都过）

- [ ] **Step 5: 提交**

```bash
git add probe/server/
git commit -m "probe: 服务端原样回显 datagram"
```

---

### Task 3: 服务端 stream 回显（对照组）

同一条 QUIC 连接上开一条双向 stream，跑和 datagram 轮完全一样的流量。有了对照组才能分辨
两种情况：**UDP 整体被阻断**（两轮都失败）和 **只有 datagram 被特殊对待**（stream 通、
datagram 不通）。后者决定了 stream 回退通道到底有没有用。

**Files:**
- Modify: `probe/server/serve.go`
- Modify: `probe/server/serve_test.go`

**Interfaces:**
- Consumes: `Serve`（Task 2）
- Produces: `Serve` 额外接受 stream；帧格式为 2 字节大端长度前缀 + 载荷，原样回显

- [ ] **Step 1: 写失败的测试**

追加到 `probe/server/serve_test.go`：

```go
func TestServeEchoesLengthPrefixedStreamFrames(t *testing.T) {
	ln, err := Listen("127.0.0.1:0", &tls.Config{Certificates: []tls.Certificate{mustCert(t)}})
	if err != nil {
		t.Fatalf("Listen: %v", err)
	}
	defer ln.Close()
	go Serve(ln)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	conn, err := quic.DialAddr(ctx, ln.Addr().String(), &tls.Config{
		InsecureSkipVerify: true,
		NextProtos:         []string{ALPN},
	}, &quic.Config{EnableDatagrams: true})
	if err != nil {
		t.Fatalf("DialAddr: %v", err)
	}
	defer conn.CloseWithError(0, "")

	stream, err := conn.OpenStreamSync(ctx)
	if err != nil {
		t.Fatalf("OpenStreamSync: %v", err)
	}

	payload := []byte{0xde, 0xad, 0xbe, 0xef}
	if err := writeFrame(stream, payload); err != nil {
		t.Fatalf("writeFrame: %v", err)
	}
	got, err := readFrame(stream)
	if err != nil {
		t.Fatalf("readFrame: %v", err)
	}
	if string(got) != string(payload) {
		t.Fatalf("stream echo = %v, want %v", got, payload)
	}
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `go test ./probe/server/ -run TestServeEchoesLengthPrefixedStreamFrames -v`
Expected: FAIL，`undefined: writeFrame`、`undefined: readFrame`

- [ ] **Step 3: 实现帧编解码与 stream 回显**

新建 `probe/server/frame.go`（客户端会复制同一份 —— 探针是丢弃型代码，为两个二进制共享
一个包不值得）：

```go
package main

import (
	"encoding/binary"
	"fmt"
	"io"
)

// maxFrame 比 QUIC datagram 的上限宽裕，够放下测量载荷即可。
const maxFrame = 1400

// writeFrame 写一个 2 字节大端长度前缀加载荷。
func writeFrame(w io.Writer, b []byte) error {
	if len(b) > maxFrame {
		return fmt.Errorf("frame of %d bytes exceeds the %d byte limit", len(b), maxFrame)
	}
	var hdr [2]byte
	binary.BigEndian.PutUint16(hdr[:], uint16(len(b)))
	if _, err := w.Write(hdr[:]); err != nil {
		return err
	}
	_, err := w.Write(b)
	return err
}

// readFrame 读一个长度前缀帧。
func readFrame(r io.Reader) ([]byte, error) {
	var hdr [2]byte
	if _, err := io.ReadFull(r, hdr[:]); err != nil {
		return nil, err
	}
	n := binary.BigEndian.Uint16(hdr[:])
	if n > maxFrame {
		return nil, fmt.Errorf("frame claims %d bytes, over the %d byte limit", n, maxFrame)
	}
	b := make([]byte, n)
	if _, err := io.ReadFull(r, b); err != nil {
		return nil, err
	}
	return b, nil
}
```

在 `serve.go` 的 `Serve` 循环里，把 `go echoDatagrams(conn)` 改成两行：

```go
		go echoDatagrams(conn)
		go echoStreams(conn)
```

并追加：

```go
// echoStreams 接受客户端开的 stream，逐帧原样回显。
func echoStreams(conn quic.Connection) {
	for {
		stream, err := conn.AcceptStream(context.Background())
		if err != nil {
			return
		}
		go func(s quic.Stream) {
			for {
				b, err := readFrame(s)
				if err != nil {
					return
				}
				if err := writeFrame(s, b); err != nil {
					return
				}
			}
		}(stream)
	}
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `go test ./probe/server/ -v`
Expected: PASS（三个测试）

- [ ] **Step 5: 提交**

```bash
git add probe/server/
git commit -m "probe: 服务端 stream 回显作为 datagram 的对照组"
```

---

### Task 4: 客户端连接与握手结果

**Files:**
- Create: `probe/client/main.go`
- Create: `probe/client/frame.go`（从 `probe/server/frame.go` 复制，改 `package main`）
- Create: `probe/client/dial.go`
- Create: `probe/client/dial_test.go`

**Interfaces:**
- Consumes: 服务端的 ALPN `can-voice-probe/1`
- Produces: `Dial(ctx context.Context, addr string, insecure bool) (quic.Connection, HandshakeResult, error)`；
  `type HandshakeResult struct { OK bool; Millis int64; Error string }`

握手本身就是一个测量项：**如果 QUIC 握手都做不完，后面两轮都不用跑了**，而这恰恰是最可能
的失败形态（UDP 被整体阻断）。所以它单独记录耗时和错误文本。

- [ ] **Step 1: 写失败的测试**

`probe/client/dial_test.go`：

```go
package main

import (
	"context"
	"testing"
	"time"
)

func TestDialReportsFailureWithoutReturningAnError(t *testing.T) {
	// 127.0.0.1:1 上没有任何东西在听，握手必然失败。
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	_, res, err := Dial(ctx, "127.0.0.1:1", true)
	if err == nil {
		t.Fatal("Dial to a dead address must return an error")
	}
	if res.OK {
		t.Fatal("HandshakeResult.OK must be false when the handshake failed")
	}
	if res.Error == "" {
		t.Fatal("HandshakeResult.Error must carry the reason; it is the whole point of the probe")
	}
	if res.Millis <= 0 {
		t.Fatal("HandshakeResult.Millis must record how long the attempt took")
	}
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `go test ./probe/client/ -run TestDialReportsFailureWithoutReturningAnError -v`
Expected: FAIL，`undefined: Dial`

- [ ] **Step 3: 实现**

`probe/client/dial.go`：

```go
package main

import (
	"context"
	"crypto/tls"
	"time"

	"github.com/quic-go/quic-go"
)

const ALPN = "can-voice-probe/1"

// HandshakeResult 是探针的第一个测量项：QUIC 握手能不能做完。
// 握手失败是最可能的失败形态（UDP 被整体阻断），所以它的错误文本要原样留下。
type HandshakeResult struct {
	OK     bool   `json:"ok"`
	Millis int64  `json:"millis"`
	Error  string `json:"error,omitempty"`
}

// Dial 建立探针连接。无论成败都返回一个填好的 HandshakeResult。
func Dial(ctx context.Context, addr string, insecure bool) (quic.Connection, HandshakeResult, error) {
	start := time.Now()
	conn, err := quic.DialAddr(ctx, addr, &tls.Config{
		InsecureSkipVerify: insecure,
		NextProtos:         []string{ALPN},
	}, &quic.Config{
		EnableDatagrams: true,
		MaxIdleTimeout:  90 * time.Second,
		KeepAlivePeriod: 15 * time.Second,
	})
	res := HandshakeResult{Millis: time.Since(start).Milliseconds()}
	if res.Millis == 0 {
		res.Millis = 1 // 保证"尝试过"和"没尝试"可区分
	}
	if err != nil {
		res.Error = err.Error()
		return nil, res, err
	}
	res.OK = true
	return conn, res, nil
}
```

`probe/client/frame.go`：把 `probe/server/frame.go` 整份复制过来，包名已是 `main`，无需改动。

`probe/client/main.go` 先写一个最小可跑的壳（Task 5–7 会继续填）：

```go
package main

import (
	"context"
	"flag"
	"log"
	"time"
)

func main() {
	addr := flag.String("server", "probe.ceruleanavi.net:64739", "探针服务器地址")
	insecure := flag.Bool("insecure", false, "跳过证书校验（仅用于本地自签名测试）")
	flag.Parse()

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	conn, hs, err := Dial(ctx, *addr, *insecure)
	if err != nil {
		log.Printf("handshake failed after %d ms: %v", hs.Millis, err)
		return
	}
	defer conn.CloseWithError(0, "")
	log.Printf("handshake succeeded in %d ms", hs.Millis)
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `go test ./probe/client/ -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add probe/client/
git commit -m "probe: 客户端握手并把结果计入测量"
```

---

### Task 5: datagram 测量轮

**Files:**
- Create: `probe/client/round.go`
- Create: `probe/client/round_test.go`
- Modify: `probe/client/main.go`

**Interfaces:**
- Consumes: `Dial`、`HandshakeResult`（Task 4），`writeFrame`/`readFrame`（Task 4 复制的 frame.go）
- Produces:
  - `type Probe struct { Seq uint32; SentUnixNanos int64 }`，编码为 12 字节 + 填充到 `payloadBytes`
  - `encodeProbe(seq uint32, now time.Time, size int) []byte`
  - `decodeProbe(b []byte) (Probe, error)`
  - `type RoundResult struct { Sent, Received int; LossPercent, RTTMedianMs, RTTP95Ms, JitterMs float64; FirstLossAtSecond int; Interrupted bool }`
  - `RunDatagramRound(ctx context.Context, conn quic.Connection, d time.Duration) RoundResult`

**测量参数**（与 spec §5.2 的真实语音负载对齐，这样结论可以直接外推）：

| 参数 | 值 | 依据 |
|---|---|---|
| 发包速率 | 50 包/秒 | Opus 20 ms 帧 |
| 载荷大小 | 73 字节 | spec §5.2：13 字节头 + 约 60 字节 Opus |
| 轮次时长 | 60 秒 | 够长到能撞上运营商的 UDP 流超时 |
| 回显等待 | 轮次结束后再等 2 秒 | 避免把在途的包算成丢失 |

`Interrupted` 的判定：**最后 5 秒一个回显都没收到**。那是"中途被掐"的特征，和均匀丢包
完全不同 —— 前者需要 stream 回退，后者只需要抖动缓冲。

- [ ] **Step 1: 写失败的测试**

`probe/client/round_test.go`：

```go
package main

import (
	"testing"
	"time"
)

func TestProbeRoundTripsThroughEncoding(t *testing.T) {
	now := time.Unix(1757000000, 123456789)
	b := encodeProbe(42, now, 73)
	if len(b) != 73 {
		t.Fatalf("encodeProbe produced %d bytes, want 73 (must match the real voice payload)", len(b))
	}
	p, err := decodeProbe(b)
	if err != nil {
		t.Fatalf("decodeProbe: %v", err)
	}
	if p.Seq != 42 {
		t.Fatalf("Seq = %d, want 42", p.Seq)
	}
	if p.SentUnixNanos != now.UnixNano() {
		t.Fatalf("SentUnixNanos = %d, want %d", p.SentUnixNanos, now.UnixNano())
	}
}

func TestDecodeProbeRejectsShortPayload(t *testing.T) {
	if _, err := decodeProbe([]byte{1, 2, 3}); err == nil {
		t.Fatal("decodeProbe must reject a payload shorter than the 12 byte header")
	}
}

func TestSummariseMarksAnInterruptedRound(t *testing.T) {
	// 60 秒轮次、50 pps：前 55 秒全收到，最后 5 秒全丢。
	const rate = 50
	rtts := make(map[uint32]time.Duration)
	for seq := uint32(0); seq < 55*rate; seq++ {
		rtts[seq] = 30 * time.Millisecond
	}
	got := summarise(60*rate, rtts, 60*time.Second, rate)
	if !got.Interrupted {
		t.Fatal("a round whose last 5 seconds received nothing must be marked Interrupted")
	}
	if got.Received != 55*rate {
		t.Fatalf("Received = %d, want %d", got.Received, 55*rate)
	}
}

func TestSummariseDoesNotMarkUniformLossAsInterrupted(t *testing.T) {
	// 均匀丢 10%，一直到最后都有回显 —— 这需要抖动缓冲，不需要 stream 回退。
	const rate = 50
	rtts := make(map[uint32]time.Duration)
	for seq := uint32(0); seq < 60*rate; seq++ {
		if seq%10 == 0 {
			continue
		}
		rtts[seq] = 30 * time.Millisecond
	}
	got := summarise(60*rate, rtts, 60*time.Second, rate)
	if got.Interrupted {
		t.Fatal("uniform loss must not be reported as an interruption")
	}
	if got.LossPercent < 9 || got.LossPercent > 11 {
		t.Fatalf("LossPercent = %.1f, want about 10", got.LossPercent)
	}
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `go test ./probe/client/ -run 'TestProbe|TestDecode|TestSummarise' -v`
Expected: FAIL，`undefined: encodeProbe`、`decodeProbe`、`summarise`

- [ ] **Step 3: 实现**

`probe/client/round.go`：

```go
package main

import (
	"context"
	"encoding/binary"
	"fmt"
	"sort"
	"time"

	"github.com/quic-go/quic-go"
)

// 与 spec §5.2 的真实语音负载对齐：13 字节头 + 约 60 字节 Opus。
const (
	payloadBytes = 73
	packetsPerS  = 50
	probeHeader  = 12
)

// Probe 是一个探测包的载荷头。回显是原样的，所以 RTT 由客户端自己算，
// 服务端不参与，也就不可能污染测量。
type Probe struct {
	Seq           uint32
	SentUnixNanos int64
}

func encodeProbe(seq uint32, now time.Time, size int) []byte {
	b := make([]byte, size)
	binary.BigEndian.PutUint32(b[0:4], seq)
	binary.BigEndian.PutUint64(b[4:12], uint64(now.UnixNano()))
	// 其余字节留零作填充，把包撑到真实语音包的大小。
	return b
}

func decodeProbe(b []byte) (Probe, error) {
	if len(b) < probeHeader {
		return Probe{}, fmt.Errorf("probe payload is %d bytes, need at least %d", len(b), probeHeader)
	}
	return Probe{
		Seq:           binary.BigEndian.Uint32(b[0:4]),
		SentUnixNanos: int64(binary.BigEndian.Uint64(b[4:12])),
	}, nil
}

// RoundResult 是一轮测量的全部结论。
type RoundResult struct {
	Sent              int     `json:"sent"`
	Received          int     `json:"received"`
	LossPercent       float64 `json:"loss_percent"`
	RTTMedianMs       float64 `json:"rtt_median_ms"`
	RTTP95Ms          float64 `json:"rtt_p95_ms"`
	JitterMs          float64 `json:"jitter_ms"`
	FirstLossAtSecond int     `json:"first_loss_at_second"`
	// Interrupted 表示"最后 5 秒一个回显都没有"，即中途被掐 ——
	// 与均匀丢包是完全不同的故障，需要完全不同的对策。
	Interrupted bool `json:"interrupted"`
}

// RunDatagramRound 以 50 包/秒发 d 时长的探测包，并行收回显，轮次结束后再等 2 秒。
func RunDatagramRound(ctx context.Context, conn quic.Connection, d time.Duration) RoundResult {
	total := int(d.Seconds()) * packetsPerS
	rtts := make(map[uint32]time.Duration, total)
	done := make(chan struct{})

	go func() {
		defer close(done)
		for {
			b, err := conn.ReceiveDatagram(ctx)
			if err != nil {
				return
			}
			p, err := decodeProbe(b)
			if err != nil {
				continue
			}
			rtts[p.Seq] = time.Since(time.Unix(0, p.SentUnixNanos))
		}
	}()

	ticker := time.NewTicker(time.Second / packetsPerS)
	defer ticker.Stop()
	sent := 0
	for seq := uint32(0); int(seq) < total; seq++ {
		select {
		case <-ctx.Done():
			return summarise(sent, rtts, d, packetsPerS)
		case <-ticker.C:
		}
		if err := conn.SendDatagram(encodeProbe(seq, time.Now(), payloadBytes)); err != nil {
			// 发不出去本身就是结论的一部分：继续跑完，让统计反映它。
			continue
		}
		sent++
	}

	// 轮次结束后再等 2 秒，避免把在途的包算成丢失。
	drain, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	<-drain.Done()
	_ = done

	return summarise(sent, rtts, d, packetsPerS)
}

// summarise 把原始 RTT 表折算成结论。与 RunDatagramRound 分开是为了能单独测 ——
// 网络测量的判定逻辑必须可测，否则只能靠跑真网络来验证它对不对。
func summarise(sent int, rtts map[uint32]time.Duration, d time.Duration, rate int) RoundResult {
	res := RoundResult{Sent: sent, Received: len(rtts), FirstLossAtSecond: -1}
	if sent > 0 {
		res.LossPercent = float64(sent-len(rtts)) / float64(sent) * 100
	}

	ms := make([]float64, 0, len(rtts))
	for _, v := range rtts {
		ms = append(ms, float64(v.Microseconds())/1000)
	}
	sort.Float64s(ms)
	if n := len(ms); n > 0 {
		res.RTTMedianMs = ms[n/2]
		res.RTTP95Ms = ms[(n*95)/100]
		res.JitterMs = ms[(n*95)/100] - ms[n/20]
	}

	for seq := 0; seq < sent; seq++ {
		if _, ok := rtts[uint32(seq)]; !ok {
			res.FirstLossAtSecond = seq / rate
			break
		}
	}

	// 最后 5 秒一个都没收到 = 被掐断，而不是均匀丢包。
	tailStart := sent - 5*rate
	if tailStart < 0 {
		tailStart = 0
	}
	res.Interrupted = true
	for seq := tailStart; seq < sent; seq++ {
		if _, ok := rtts[uint32(seq)]; ok {
			res.Interrupted = false
			break
		}
	}
	if sent == 0 {
		res.Interrupted = false
	}
	return res
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `go test ./probe/client/ -v`
Expected: PASS（六个测试）

- [ ] **Step 5: 手工端到端验证**

开两个终端：

```bash
go run ./probe/server -addr 127.0.0.1:64739
```

```bash
go run ./probe/client -server 127.0.0.1:64739 -insecure
```

本机上应当看到握手成功。此时 `main.go` 还没调用 `RunDatagramRound` —— 下一步接上。

- [ ] **Step 6: 在 main.go 里接上 datagram 轮**

把 `main.go` 里 `log.Printf("handshake succeeded…")` 之后改成：

```go
	log.Printf("handshake succeeded in %d ms; running the datagram round for 60 s", hs.Millis)
	dg := RunDatagramRound(ctx, conn, 60*time.Second)
	log.Printf("datagram round: sent=%d received=%d loss=%.1f%% rtt_median=%.1fms interrupted=%v",
		dg.Sent, dg.Received, dg.LossPercent, dg.RTTMedianMs, dg.Interrupted)
```

并把 `main` 里的整体超时从 30 秒放宽：

```go
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
```

再跑一次上面的端到端验证，本机应当报 `loss=0.0%`、`interrupted=false`。

- [ ] **Step 7: 提交**

```bash
git add probe/client/
git commit -m "probe: datagram 测量轮与丢包/抖动/中断判定"
```

---

### Task 6: stream 对照轮

**Files:**
- Modify: `probe/client/round.go`
- Modify: `probe/client/round_test.go`
- Modify: `probe/client/main.go`

**Interfaces:**
- Consumes: `encodeProbe`、`decodeProbe`、`summarise`、`RoundResult`（Task 5），`writeFrame`/`readFrame`（Task 4）
- Produces: `RunStreamRound(ctx context.Context, conn quic.Connection, d time.Duration) RoundResult`

对照轮**必须在同一条 QUIC 连接上**，否则测到的是两次不同的网络路径，比较就没有意义。

- [ ] **Step 1: 写失败的测试**

追加到 `probe/client/round_test.go`：

```go
func TestStreamRoundUsesTheSameSummaryShape(t *testing.T) {
	// summarise 对两轮是共用的；这个测试锁住"stream 轮不自己发明一套统计"。
	const rate = 50
	rtts := map[uint32]time.Duration{0: 10 * time.Millisecond}
	got := summarise(1, rtts, time.Second, rate)
	if got.Sent != 1 || got.Received != 1 || got.LossPercent != 0 {
		t.Fatalf("summarise = %+v, want sent=1 received=1 loss=0", got)
	}
}
```

- [ ] **Step 2: 跑测试确认它通过（这是回归锁，不是新功能）**

Run: `go test ./probe/client/ -run TestStreamRoundUsesTheSameSummaryShape -v`
Expected: PASS

- [ ] **Step 3: 实现 stream 轮**

追加到 `probe/client/round.go`：

```go
// RunStreamRound 在同一条 QUIC 连接上开一条 stream，跑与 datagram 轮完全相同的流量。
// 同一条连接是关键：不同连接会走不同的网络路径，比较就失去意义。
//
// 两轮的差异才是结论：两轮都失败 = UDP 被整体阻断，stream 回退没有意义；
// stream 通而 datagram 不通 = 中间设备在特殊对待 datagram，回退通道有用。
func RunStreamRound(ctx context.Context, conn quic.Connection, d time.Duration) RoundResult {
	stream, err := conn.OpenStreamSync(ctx)
	if err != nil {
		return RoundResult{FirstLossAtSecond: -1}
	}
	defer stream.Close()

	total := int(d.Seconds()) * packetsPerS
	rtts := make(map[uint32]time.Duration, total)
	go func() {
		for {
			b, err := readFrame(stream)
			if err != nil {
				return
			}
			p, err := decodeProbe(b)
			if err != nil {
				continue
			}
			rtts[p.Seq] = time.Since(time.Unix(0, p.SentUnixNanos))
		}
	}()

	ticker := time.NewTicker(time.Second / packetsPerS)
	defer ticker.Stop()
	sent := 0
	for seq := uint32(0); int(seq) < total; seq++ {
		select {
		case <-ctx.Done():
			return summarise(sent, rtts, d, packetsPerS)
		case <-ticker.C:
		}
		if err := writeFrame(stream, encodeProbe(seq, time.Now(), payloadBytes)); err != nil {
			continue
		}
		sent++
	}

	drain, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	<-drain.Done()

	return summarise(sent, rtts, d, packetsPerS)
}
```

- [ ] **Step 4: 在 main.go 里接上对照轮**

在 datagram 轮的 `log.Printf` 之后追加：

```go
	log.Printf("running the stream control round for 60 s")
	st := RunStreamRound(ctx, conn, 60*time.Second)
	log.Printf("stream round: sent=%d received=%d loss=%.1f%% rtt_median=%.1fms interrupted=%v",
		st.Sent, st.Received, st.LossPercent, st.RTTMedianMs, st.Interrupted)
```

并把整体超时放宽到 `10*time.Minute`。

- [ ] **Step 5: 跑测试并手工端到端验证**

Run: `go test ./probe/client/ -v`
Expected: PASS

再跑一次两个终端的端到端验证，确认两轮都报 `loss=0.0%`。本机跑完约需 2 分 5 秒。

- [ ] **Step 6: 提交**

```bash
git add probe/client/
git commit -m "probe: 同连接上的 stream 对照轮"
```

---

### Task 7: 报告输出

**Files:**
- Create: `probe/client/report.go`
- Create: `probe/client/report_test.go`
- Modify: `probe/client/main.go`

**Interfaces:**
- Consumes: `HandshakeResult`（Task 4）、`RoundResult`（Task 5）
- Produces:
  - `type Report struct { SchemaVersion int; ProbeVersion, OS, Arch, Carrier, Timezone string; StartedAt string; Handshake HandshakeResult; Datagram, Stream RoundResult }`
  - `WriteReport(r Report, dir string) (path string, err error)`
  - `Summarise(r Report) string` —— 给用户看的中文摘要

报告文件名带时间戳，写到当前目录。**不自动回传** —— 用户先看到内容再自己发回来（Global
Constraints）。运营商名称由用户用 `-carrier` 手填，探针不去嗅探。

- [ ] **Step 1: 写失败的测试**

`probe/client/report_test.go`：

```go
package main

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
)

func TestWriteReportProducesReadableJSON(t *testing.T) {
	dir := t.TempDir()
	r := Report{
		SchemaVersion: 1,
		ProbeVersion:  "p1-test",
		OS:            "darwin",
		Carrier:       "中国电信",
		Handshake:     HandshakeResult{OK: true, Millis: 42},
		Datagram:      RoundResult{Sent: 3000, Received: 2970, LossPercent: 1.0},
		Stream:        RoundResult{Sent: 3000, Received: 3000},
	}
	path, err := WriteReport(r, dir)
	if err != nil {
		t.Fatalf("WriteReport: %v", err)
	}
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read back: %v", err)
	}
	var got Report
	if err := json.Unmarshal(b, &got); err != nil {
		t.Fatalf("report is not valid JSON: %v", err)
	}
	if got.Carrier != "中国电信" {
		t.Fatalf("Carrier = %q, want 中国电信", got.Carrier)
	}
	if got.Datagram.Sent != 3000 {
		t.Fatalf("Datagram.Sent = %d, want 3000", got.Datagram.Sent)
	}
	if !strings.Contains(string(b), "\n") {
		t.Fatal("report must be indented; the user is asked to read it before sending it")
	}
}

func TestSummariseReportNamesTheDecisiveComparison(t *testing.T) {
	r := Report{
		Handshake: HandshakeResult{OK: true, Millis: 42},
		Datagram:  RoundResult{Sent: 3000, Received: 0, Interrupted: true},
		Stream:    RoundResult{Sent: 3000, Received: 3000},
	}
	s := Summarise(r)
	if !strings.Contains(s, "datagram") && !strings.Contains(s, "数据报") {
		t.Fatalf("summary must name which channel failed, got:\n%s", s)
	}
	if !strings.Contains(s, "100") && !strings.Contains(s, "全部") {
		t.Fatalf("summary must make total datagram loss obvious, got:\n%s", s)
	}
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `go test ./probe/client/ -run 'TestWriteReport|TestSummariseReport' -v`
Expected: FAIL，`undefined: Report`、`WriteReport`、`Summarise`

- [ ] **Step 3: 实现**

`probe/client/report.go`：

```go
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

// Report 是用户回传给我们的全部内容。
// 刻意不含 IP、主机名、用户名 —— 运营商由用户手填，其余全是网络测量值。
type Report struct {
	SchemaVersion int             `json:"schema_version"`
	ProbeVersion  string          `json:"probe_version"`
	OS            string          `json:"os"`
	Arch          string          `json:"arch"`
	Carrier       string          `json:"carrier"`
	Timezone      string          `json:"timezone"`
	StartedAt     string          `json:"started_at"`
	Handshake     HandshakeResult `json:"handshake"`
	Datagram      RoundResult     `json:"datagram"`
	Stream        RoundResult     `json:"stream"`
}

// NewReport 填好与网络无关的字段。
func NewReport(version, carrier string, startedAt time.Time) Report {
	tz, _ := startedAt.Zone()
	return Report{
		SchemaVersion: 1,
		ProbeVersion:  version,
		OS:            runtime.GOOS,
		Arch:          runtime.GOARCH,
		Carrier:       carrier,
		Timezone:      tz,
		StartedAt:     startedAt.UTC().Format(time.RFC3339),
	}
}

// WriteReport 把报告写成带时间戳的 JSON 文件，返回路径。
// 缩进过 —— 用户被要求先读一遍再发回来。
func WriteReport(r Report, dir string) (string, error) {
	b, err := json.MarshalIndent(r, "", "  ")
	if err != nil {
		return "", err
	}
	name := fmt.Sprintf("can-voice-probe-%s.json", time.Now().Format("20060102-150405"))
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, append(b, '\n'), 0o644); err != nil {
		return "", err
	}
	return path, nil
}

// Summarise 给用户看的中文摘要。最重要的一句是两轮的对比 ——
// 那才是决定要不要做 stream 回退通道的依据。
func Summarise(r Report) string {
	var b strings.Builder
	if !r.Handshake.OK {
		fmt.Fprintf(&b, "连接失败（%d 毫秒后放弃）：%s\n", r.Handshake.Millis, r.Handshake.Error)
		b.WriteString("这台机器的网络很可能整体阻断了 UDP。\n")
		return b.String()
	}
	fmt.Fprintf(&b, "连接成功，握手耗时 %d 毫秒。\n\n", r.Handshake.Millis)
	fmt.Fprintf(&b, "数据报通道：发出 %d 个，收到 %d 个，丢失 %.1f%%，延迟中位数 %.0f 毫秒\n",
		r.Datagram.Sent, r.Datagram.Received, r.Datagram.LossPercent, r.Datagram.RTTMedianMs)
	fmt.Fprintf(&b, "对照通道：  发出 %d 个，收到 %d 个，丢失 %.1f%%，延迟中位数 %.0f 毫秒\n\n",
		r.Stream.Sent, r.Stream.Received, r.Stream.LossPercent, r.Stream.RTTMedianMs)

	switch {
	case r.Datagram.Interrupted && !r.Stream.Interrupted:
		b.WriteString("数据报通道中途被掐断，对照通道没有 —— 这正是我们要找的情况。\n")
	case r.Datagram.Interrupted && r.Stream.Interrupted:
		b.WriteString("两个通道都中途被掐断，这台机器的网络对长时间连接不友好。\n")
	case r.Datagram.LossPercent > r.Stream.LossPercent+2:
		b.WriteString("数据报通道明显比对照通道差。\n")
	default:
		b.WriteString("两个通道表现接近，这台机器的网络没有特殊对待数据报。\n")
	}
	return b.String()
}
```

- [ ] **Step 4: 在 main.go 里接上报告**

`probe/client/main.go` 完整替换为：

```go
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"time"
)

const probeVersion = "p1-1"

func main() {
	addr := flag.String("server", "probe.ceruleanavi.net:64739", "探针服务器地址")
	carrier := flag.String("carrier", "", "你的网络（如：中国电信 / 校园网 / 公司网络）")
	insecure := flag.Bool("insecure", false, "跳过证书校验（仅用于本地自签名测试）")
	flag.Parse()

	if *carrier == "" {
		fmt.Println("请用 -carrier 说明你用的是什么网络，例如：")
		fmt.Println("  can-voice-probe -carrier 中国电信")
		os.Exit(2)
	}

	started := time.Now()
	rep := NewReport(probeVersion, *carrier, started)

	fmt.Println("正在测试，大约需要两分半，请不要关闭窗口，也不要切换网络。")

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
	defer cancel()

	conn, hs, err := Dial(ctx, *addr, *insecure)
	rep.Handshake = hs
	if err != nil {
		log.Printf("handshake failed after %d ms: %v", hs.Millis, err)
	} else {
		defer conn.CloseWithError(0, "")
		log.Printf("handshake succeeded in %d ms; running the datagram round", hs.Millis)
		rep.Datagram = RunDatagramRound(ctx, conn, 60*time.Second)
		log.Printf("datagram round: sent=%d received=%d loss=%.1f%% interrupted=%v",
			rep.Datagram.Sent, rep.Datagram.Received, rep.Datagram.LossPercent, rep.Datagram.Interrupted)

		log.Printf("running the stream control round")
		rep.Stream = RunStreamRound(ctx, conn, 60*time.Second)
		log.Printf("stream round: sent=%d received=%d loss=%.1f%% interrupted=%v",
			rep.Stream.Sent, rep.Stream.Received, rep.Stream.LossPercent, rep.Stream.Interrupted)
	}

	fmt.Print("\n" + Summarise(rep))

	path, err := WriteReport(rep, ".")
	if err != nil {
		log.Fatalf("write report: %v", err)
	}
	fmt.Printf("\n报告已保存到：%s\n请把这个文件发回给我们。里面只有网络测量数据，没有你的任何个人信息。\n", path)
}
```

- [ ] **Step 5: 跑测试并手工端到端验证**

Run: `go test ./probe/client/ -v`
Expected: PASS（八个测试）

```bash
go run ./probe/client -server 127.0.0.1:64739 -insecure -carrier 本机测试
```

确认屏幕上出现中文摘要，且当前目录生成了 `can-voice-probe-*.json`。打开它，确认里面**没有**
IP、主机名或用户名。

- [ ] **Step 6: 提交**

```bash
git add probe/client/
git commit -m "probe: JSON 报告与中文摘要输出"
```

---

### Task 8: 三平台构建与服务端部署

**Files:**
- Create: `probe/build.sh`
- Create: `probe/README-测试说明.md`
- Create: `probe/deploy/can-voice-probe.service`

**Interfaces:**
- Consumes: Task 1–7 的全部代码
- Produces: `probe/dist/` 下六个二进制（windows/darwin/linux × amd64/arm64 的组合，见下），以及一份给测试用户的中文说明

非技术用户要能双击运行，所以：**Windows 上做成 `.exe` 且不依赖任何运行时**（Go 静态链接天然
满足），**macOS 上提供 arm64 和 amd64 两份**（Apple Silicon 与 Intel），Linux 只做 amd64。

- [ ] **Step 1: 写构建脚本**

`probe/build.sh`：

```bash
#!/usr/bin/env bash
# 构建探针客户端的分发二进制。丢弃型工具，不签名、不公证 ——
# 测试用户会被告知 macOS 需要右键打开，见 README-测试说明.md。
set -euo pipefail
cd "$(dirname "$0")/.."

out=probe/dist
rm -rf "$out"
mkdir -p "$out"

build() {
  local goos=$1 goarch=$2 ext=${3:-}
  echo "building ${goos}/${goarch}"
  GOOS=$goos GOARCH=$goarch CGO_ENABLED=0 \
    go build -trimpath -ldflags="-s -w" \
    -o "${out}/can-voice-probe-${goos}-${goarch}${ext}" ./probe/client
}

build windows amd64 .exe
build darwin  arm64
build darwin  amd64
build linux   amd64

ls -lh "$out"
```

```bash
chmod +x probe/build.sh
```

- [ ] **Step 2: 跑构建确认四个二进制都出来**

Run: `./probe/build.sh`
Expected: `probe/dist/` 下四个文件，每个约 8–12 MB

- [ ] **Step 3: 写给测试用户的说明**

`probe/README-测试说明.md`：

```markdown
# CAN 语音连通性测试

我们在为 CAN 做新的语音系统，需要先确认一种叫 QUIC 的传输方式在你的网络里通不通。
这个小工具会跑两分半钟，把测试结果存成一个文件，请你把文件发回给我们。

**它不会上传任何东西，也不包含你的 IP、用户名或任何个人信息。** 生成的文件是纯文本，
你可以先打开看一遍再决定发不发。

## 怎么跑

下载对应你系统的文件：

| 系统 | 文件 |
|---|---|
| Windows | `can-voice-probe-windows-amd64.exe` |
| macOS（M1/M2/M3/M4） | `can-voice-probe-darwin-arm64` |
| macOS（Intel） | `can-voice-probe-darwin-amd64` |
| Linux | `can-voice-probe-linux-amd64` |

然后在终端（Windows 上是 PowerShell）里运行，把 `中国电信` 换成你实际用的网络：

Windows：
```
.\can-voice-probe-windows-amd64.exe -carrier 中国电信
```

macOS / Linux：
```
chmod +x ./can-voice-probe-darwin-arm64
./can-voice-probe-darwin-arm64 -carrier 中国电信
```

**macOS 会提示"无法验证开发者"**：在"系统设置 → 隐私与安全性"里点"仍要打开"，或者在终端里
先跑 `xattr -d com.apple.quarantine ./can-voice-probe-darwin-arm64`。这个工具没有签名，
因为它是一次性的测试程序。

## 跑的时候请注意

- 不要切换网络（比如从 Wi-Fi 换到手机热点），那会让这次测试作废
- 不要关窗口，两分半钟就好
- 如果你能在**不同的网络**下各跑一次（家里、公司、手机热点），对我们帮助最大 ——
  每次都用 `-carrier` 说明是哪个网络

## `-carrier` 该填什么

填你能描述的就行，比如：`中国电信`、`中国移动`、`中国联通`、`校园网`、`公司网络`、
`手机热点-移动`。它只是用来分组看结果的。
```

- [ ] **Step 4: 写服务端的 systemd 单元**

`probe/deploy/can-voice-probe.service`：

```ini
[Unit]
Description=can-voice P1 connectivity probe server
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/local/bin/can-voice-probe-server \
  -addr :64739 \
  -cert /etc/letsencrypt/live/probe.ceruleanavi.net/fullchain.pem \
  -key /etc/letsencrypt/live/probe.ceruleanavi.net/privkey.pem
Restart=always
RestartSec=5
DynamicUser=yes
# 证书由 certbot 拥有；DynamicUser 读不到，所以给读权限组。
SupplementaryGroups=ssl-cert
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 5: 部署服务端**

在服务器上：

```bash
# 1. 域名：给 probe.ceruleanavi.net 加一条 A 记录指向探针主机
# 2. 证书
certbot certonly --standalone -d probe.ceruleanavi.net
# 3. 二进制
GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -o can-voice-probe-server ./probe/server
scp can-voice-probe-server <host>:/usr/local/bin/
scp probe/deploy/can-voice-probe.service <host>:/etc/systemd/system/
ssh <host> 'systemctl daemon-reload && systemctl enable --now can-voice-probe'
# 4. 防火墙：放行 UDP 64739
```

- [ ] **Step 6: 从外网验证一次**

在一台不在服务器本机的机器上：

```bash
./probe/dist/can-voice-probe-darwin-arm64 -server probe.ceruleanavi.net:64739 -carrier 自测
```

Expected: 握手成功、两轮丢包率都很低、生成报告文件。**不要带 `-insecure`** —— 这一步同时
验证了 Let's Encrypt 证书链是对的。

- [ ] **Step 7: 提交**

```bash
git add probe/build.sh probe/README-测试说明.md probe/deploy/
git commit -m "probe: 三平台构建、测试说明与服务端部署单元"
```

---

### Task 9: 汇总、判定与结论

**Files:**
- Create: `probe/analyse.go`
- Create: `probe/analyse_test.go`
- Create: `docs/p1-connectivity-findings.md`（结论文档，**不在 `probe/` 下，因此不随探针删除**）

**Interfaces:**
- Consumes: Task 7 的 `Report` JSON 结构（schema_version 1）
- Produces: `Verdict(reports []Report) (needFallback bool, reasons []string)`；一份写死在代码里的判定标准

**判定标准（先写下来，再看数据）：**

满足**任意一条**即判定"必须实现 stream 回退通道"：

| 条件 | 阈值 | 理由 |
|---|---|---|
| 握手失败的会话比例 | ≥ 2% | UDP 被整体阻断，这些人根本连不上 |
| datagram 轮被中断的会话比例 | ≥ 5% | 被掐断无法靠抖动缓冲补救 |
| datagram 丢包率中位数 | > 2% | 超出 Opus PLC 能无感补偿的范围 |
| datagram 与 stream 丢包率之差的中位数 | > 2 个百分点 | 中间设备在特殊对待 datagram，回退确实有用 |

**样本不足时不判定。** 少于 8 个会话，或覆盖的运营商少于 3 家，`Verdict` 必须拒绝出结论 ——
用 6 个人的数据决定一个要维护多年的传输层，比没有数据更危险，因为它看起来像有依据。

- [ ] **Step 1: 写失败的测试**

`probe/analyse_test.go`：

```go
package main

import "testing"

func reportWith(carrier string, hsOK bool, dgLoss, stLoss float64, interrupted bool) Report {
	return Report{
		SchemaVersion: 1,
		Carrier:       carrier,
		Handshake:     HandshakeResult{OK: hsOK, Millis: 40},
		Datagram:      RoundResult{Sent: 3000, Received: 3000, LossPercent: dgLoss, Interrupted: interrupted},
		Stream:        RoundResult{Sent: 3000, Received: 3000, LossPercent: stLoss},
	}
}

func TestVerdictRefusesToDecideOnTooFewSessions(t *testing.T) {
	var rs []Report
	for i := 0; i < 7; i++ {
		rs = append(rs, reportWith("中国电信", true, 0, 0, false))
	}
	_, reasons := Verdict(rs)
	if len(reasons) == 0 || reasons[0][:6] != "sample" {
		t.Fatalf("Verdict must refuse below 8 sessions, got reasons=%v", reasons)
	}
}

func TestVerdictRefusesToDecideOnTooFewCarriers(t *testing.T) {
	var rs []Report
	for i := 0; i < 10; i++ {
		rs = append(rs, reportWith("中国电信", true, 0, 0, false))
	}
	_, reasons := Verdict(rs)
	if len(reasons) == 0 {
		t.Fatal("Verdict must refuse with fewer than 3 carriers")
	}
}

func TestVerdictSaysNoFallbackWhenEverythingIsClean(t *testing.T) {
	carriers := []string{"中国电信", "中国移动", "中国联通", "校园网"}
	var rs []Report
	for i := 0; i < 12; i++ {
		rs = append(rs, reportWith(carriers[i%len(carriers)], true, 0.3, 0.2, false))
	}
	need, reasons := Verdict(rs)
	if need {
		t.Fatalf("clean data must not demand a fallback; reasons=%v", reasons)
	}
}

func TestVerdictDemandsFallbackWhenDatagramsAreInterrupted(t *testing.T) {
	carriers := []string{"中国电信", "中国移动", "中国联通"}
	var rs []Report
	for i := 0; i < 12; i++ {
		// 12 个里 2 个被中断 = 16.7%，超过 5% 的阈值
		rs = append(rs, reportWith(carriers[i%3], true, 0.3, 0.2, i < 2))
	}
	need, reasons := Verdict(rs)
	if !need {
		t.Fatal("2 of 12 sessions interrupted is over the 5% threshold and must demand a fallback")
	}
	if len(reasons) == 0 {
		t.Fatal("Verdict must say which threshold was crossed")
	}
}

func TestVerdictDemandsFallbackWhenDatagramsAreTreatedWorseThanStreams(t *testing.T) {
	carriers := []string{"中国电信", "中国移动", "中国联通"}
	var rs []Report
	for i := 0; i < 12; i++ {
		rs = append(rs, reportWith(carriers[i%3], true, 5.0, 0.2, false))
	}
	need, _ := Verdict(rs)
	if !need {
		t.Fatal("a 4.8 point gap between datagram and stream loss must demand a fallback")
	}
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `go test ./probe/ -run TestVerdict -v`
Expected: FAIL，`undefined: Verdict`

- [ ] **Step 3: 实现**

`probe/analyse.go`（`package main`，与一个小 `main` 同目录；它读一个装满报告 JSON 的目录）：

```go
// can-voice-probe-analyse 汇总测试用户回传的报告并给出判定。
// 判定标准写死在代码里，且在看到任何数据之前就已确定 —— 见 P1 计划 Task 9。
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
)

const (
	minSessions          = 8
	minCarriers          = 3
	maxHandshakeFailRate = 0.02 // 2%
	maxInterruptRate     = 0.05 // 5%
	maxDatagramLoss      = 2.0  // 百分比
	maxLossGap           = 2.0  // 百分点
)

// Verdict 判定是否必须实现 stream 回退通道。
// 样本不足时拒绝出结论：用太少的数据决定一个要维护多年的传输层，
// 比没有数据更危险，因为它看起来像有依据。
func Verdict(rs []Report) (bool, []string) {
	if len(rs) < minSessions {
		return false, []string{fmt.Sprintf(
			"sample too small: %d sessions, need at least %d", len(rs), minSessions)}
	}
	carriers := map[string]bool{}
	for _, r := range rs {
		carriers[r.Carrier] = true
	}
	if len(carriers) < minCarriers {
		return false, []string{fmt.Sprintf(
			"sample too narrow: %d carriers, need at least %d", len(carriers), minCarriers)}
	}

	var reasons []string
	hsFail, interrupted := 0, 0
	var dgLoss, gap []float64
	for _, r := range rs {
		if !r.Handshake.OK {
			hsFail++
			continue
		}
		if r.Datagram.Interrupted {
			interrupted++
		}
		dgLoss = append(dgLoss, r.Datagram.LossPercent)
		gap = append(gap, r.Datagram.LossPercent-r.Stream.LossPercent)
	}

	if rate := float64(hsFail) / float64(len(rs)); rate >= maxHandshakeFailRate {
		reasons = append(reasons, fmt.Sprintf(
			"handshake failed in %.1f%% of sessions (threshold %.0f%%)", rate*100, maxHandshakeFailRate*100))
	}
	if rate := float64(interrupted) / float64(len(rs)); rate >= maxInterruptRate {
		reasons = append(reasons, fmt.Sprintf(
			"datagram round was interrupted in %.1f%% of sessions (threshold %.0f%%)", rate*100, maxInterruptRate*100))
	}
	if m := median(dgLoss); m > maxDatagramLoss {
		reasons = append(reasons, fmt.Sprintf(
			"median datagram loss %.1f%% (threshold %.0f%%)", m, maxDatagramLoss))
	}
	if m := median(gap); m > maxLossGap {
		reasons = append(reasons, fmt.Sprintf(
			"datagrams lose %.1f points more than streams (threshold %.0f)", m, maxLossGap))
	}
	return len(reasons) > 0, reasons
}

func median(xs []float64) float64 {
	if len(xs) == 0 {
		return 0
	}
	s := append([]float64(nil), xs...)
	sort.Float64s(s)
	return s[len(s)/2]
}

func main() {
	dir := "reports"
	if len(os.Args) > 1 {
		dir = os.Args[1]
	}
	paths, err := filepath.Glob(filepath.Join(dir, "*.json"))
	if err != nil || len(paths) == 0 {
		fmt.Fprintf(os.Stderr, "no reports found in %s\n", dir)
		os.Exit(1)
	}
	var rs []Report
	for _, p := range paths {
		b, err := os.ReadFile(p)
		if err != nil {
			fmt.Fprintf(os.Stderr, "skipping %s: %v\n", p, err)
			continue
		}
		var r Report
		if err := json.Unmarshal(b, &r); err != nil {
			fmt.Fprintf(os.Stderr, "skipping %s: %v\n", p, err)
			continue
		}
		rs = append(rs, r)
	}

	need, reasons := Verdict(rs)
	fmt.Printf("%d reports\n", len(rs))
	for _, r := range reasons {
		fmt.Printf("  - %s\n", r)
	}
	if need {
		fmt.Println("VERDICT: a stream fallback channel is required")
	} else if len(reasons) > 0 {
		fmt.Println("VERDICT: inconclusive — collect more data")
	} else {
		fmt.Println("VERDICT: datagram-only is sufficient")
	}
}
```

`Report`、`HandshakeResult`、`RoundResult` 定义在 `probe/client/`。为避免跨包导入，把
`analyse.go` 和 `analyse_test.go` 放进 `probe/client/` 目录、改名为
`probe/client/analyse.go`，并把它的 `main()` 改名为 `analyseMain()`，再在
`probe/client/main.go` 的 `main()` 开头加一个分支：

```go
	if len(os.Args) > 1 && os.Args[1] == "analyse" {
		analyseMain(os.Args[2:])
		return
	}
```

同时把 `analyseMain` 的签名改成 `func analyseMain(args []string)`，内部用 `args` 取目录而
不是 `os.Args`。

- [ ] **Step 4: 跑测试确认通过**

Run: `go test ./probe/client/ -v`
Expected: PASS（十三个测试）

- [ ] **Step 5: 收集数据**

把 `probe/dist/` 的二进制和 `README-测试说明.md` 发给测试用户。按 Global Constraints 的覆盖
要求收够：**≥8 个会话、≥3 家大陆运营商、≥1 个校园网或企业网、≥2 个操作系统**。

回传的报告放进 `probe/reports/`（已在 `.gitignore` 里，不入库 —— 它们是别人网络环境的数据）。

- [ ] **Step 6: 出判定**

```bash
go run ./probe/client analyse probe/reports
```

- [ ] **Step 7: 写结论文档**

`docs/p1-connectivity-findings.md`。这份文档**不在 `probe/` 下**，所以探针删除后它留存 ——
它是 P2/P3 传输层实现范围的依据。内容必须包含：

1. 样本描述：几个会话、哪些运营商、哪些系统、采集时间范围
2. `analyse` 的原始输出
3. 判定结果，以及**据此决定 P2/P3 是否实现 stream 回退通道**
4. Global Constraints 里那条告警的原文：探针用 quic-go、生产用 quinn，两者在中间设备指纹上
   的差异未被测量，必须在 spec §11.1 的封闭测试里用真实客户端复核
5. 如果判定是"样本不足"，写明还缺什么，以及是继续收集还是按保守方案（实现回退）推进

- [ ] **Step 8: 提交**

```bash
git add probe/client/analyse.go probe/client/analyse_test.go docs/p1-connectivity-findings.md
git commit -m "probe: 判定标准、汇总工具与 P1 结论"
```

- [ ] **Step 9: 删除探针**

结论已经落在 `docs/p1-connectivity-findings.md` 里，代码没有继续留存的理由 —— 留着它只会让
人以为 `can-voice` 里有一个叫 probe 的子系统。

```bash
ssh <host> 'systemctl disable --now can-voice-probe && rm /usr/local/bin/can-voice-probe-server /etc/systemd/system/can-voice-probe.service'
git rm -r probe/
git commit -m "probe: 结论已归档，删除一次性探针代码"
```

服务器上的 `probe.ceruleanavi.net` DNS 记录和证书也一并清理。

---

## 完成标准

P1 结束时应当满足：

- [ ] `docs/p1-connectivity-findings.md` 存在，含样本描述、原始输出、判定、以及 quic-go/quinn 的告警
- [ ] 判定明确回答了"P2/P3 要不要实现 stream 回退通道"
- [ ] `probe/` 已删除，探针服务器已下线
- [ ] `can-voice` 仓库存在，含 `go.mod` 与 `README.md`，`go test ./...` 通过（此时应无测试可跑）
