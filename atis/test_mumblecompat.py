"""ssl.wrap_socket 兼容补丁的测试。

    python -m unittest test_mumblecompat -v

这个补丁是语音能不能连上的前提：Python 3.12 删掉了 ssl.wrap_socket，而
pymumble 1.6.1 还在用它，没有补丁的话连接线程一起来就抛 AttributeError，
界面上表现为"服务器拒绝了连接"——排查方向完全被带偏。
"""

import os
import socket
import ssl
import threading
import unittest

import mumblecompat


class InstallTest(unittest.TestCase):

    def setUp(self):
        self.had_wrap = hasattr(ssl, "wrap_socket")
        self.original = getattr(ssl, "wrap_socket", None)
        self.addCleanup(self.restore)

    def restore(self):
        if self.had_wrap:
            ssl.wrap_socket = self.original
        elif hasattr(ssl, "wrap_socket"):
            del ssl.wrap_socket

    def test_installs_when_missing(self):
        if hasattr(ssl, "wrap_socket"):
            del ssl.wrap_socket
        self.assertTrue(mumblecompat.install())
        self.assertTrue(hasattr(ssl, "wrap_socket"))

    def test_does_not_touch_an_existing_implementation(self):
        sentinel = object()
        ssl.wrap_socket = sentinel
        self.assertFalse(mumblecompat.install())
        self.assertIs(ssl.wrap_socket, sentinel)

    def test_wraps_a_real_tls_connection(self):
        """pymumble 的用法：先包再 connect。"""
        if hasattr(ssl, "wrap_socket"):
            del ssl.wrap_socket
        mumblecompat.install()

        # 起一个本地 TLS 服务器要证书，这里只验证包出来的对象形态正确，
        # 真正的握手在连服务器的那次诊断里验过了
        plain = socket.socket()
        wrapped = ssl.wrap_socket(plain)
        self.addCleanup(wrapped.close)
        self.assertIsInstance(wrapped, ssl.SSLSocket)
        self.assertEqual(wrapped.context.verify_mode, ssl.CERT_NONE)
        self.assertFalse(wrapped.context.check_hostname,
                         "Mumble 用自签证书，开了主机名校验就连不上了")

    def test_accepts_the_arguments_pymumble_passes(self):
        if hasattr(ssl, "wrap_socket"):
            del ssl.wrap_socket
        mumblecompat.install()

        # pymumble 传的是 certfile/keyfile/ssl_version 三个关键字
        plain = socket.socket()
        wrapped = ssl.wrap_socket(plain, certfile=None, keyfile=None,
                                  ssl_version=ssl.PROTOCOL_TLS_CLIENT)
        self.addCleanup(wrapped.close)
        self.assertIsInstance(wrapped, ssl.SSLSocket)


class CertificatePinTest(unittest.TestCase):
    """服务器证书按指纹认。

    Mumble 用自签证书，CA 链对它没意义——官方客户端认的就是指纹。以前这里
    只做了"关掉校验"那一半：注释拿"官方客户端靠指纹认"当理由，指纹校验却
    谁也没写，pymumble 自己也不做。于是两半都不成立，任何能插到中间的人握
    个手就能收下接下来那条 `Authenticate`，里面的 password 是成员的**网站
    密码**，拿到就是整个账号。

    时序是这件事的全部（pymumble 1.6.1 `mumble.py` 的 `connect`）：

        control_socket = ssl.wrap_socket(std_sock, ...)   # 还没连
        control_socket.connect((host, port))              # 握手在这里
        send_message(VERSION, ...)
        send_message(AUTHENTICATE, ...)                   # 密码在这里才出去

    所以校验挂在 socket 的 `connect()` 里：验不过就抛，凭据一个字节都不出去。
    挂在 `Mumble.connect()` 外面是不行的，那时候密码早发完了。

    指纹是实测取的：
        openssl s_client -connect audio.ceruleanavi.net:64738 </dev/null \\
            | openssl x509 -noout -fingerprint -sha256
    两条 A 记录（116.62.139.121 / 208.68.182.85）给的是同一张证书，都比对过；
    用这里这套机制真的连一次服务器，算出来的也是同一个值。
    """

    # 和 PINNED_FINGERPRINTS 里那个不一样就行——扮演"中间人递过来的证书"
    OTHER = "a" * 64

    def fake_cert(self, body=b"pretend-certificate"):
        """一段假 DER 和它真实的 SHA-256。"""
        import hashlib
        return body, hashlib.sha256(body).hexdigest()

    def setUp(self):
        self._pins = mumblecompat.PINNED_FINGERPRINTS
        self._env = os.environ.pop(mumblecompat.FINGERPRINT_ENV, None)
        self.addCleanup(self.restore)

    def restore(self):
        mumblecompat.PINNED_FINGERPRINTS = self._pins
        if self._env is None:
            os.environ.pop(mumblecompat.FINGERPRINT_ENV, None)
        else:
            os.environ[mumblecompat.FINGERPRINT_ENV] = self._env

    # ---------- 钉住的那个值本身 ----------
    def test_a_real_fingerprint_is_pinned(self):
        """**不能是空的，也不能是占位符。**

        空的话 check_certificate 会走"没配置就不校验"那条，警告一行然后放行
        ——机制齐全、一点用没有，而且从外面看和真的钉住了长得一样。
        """
        self.assertTrue(mumblecompat.PINNED_FINGERPRINTS,
                        "一个指纹都没钉，等于没做")
        for value in mumblecompat.PINNED_FINGERPRINTS:
            normalised = mumblecompat.normalise_fingerprint(value)
            self.assertRegex(normalised, r"^[0-9a-f]{64}$",
                             f"{value!r} 不像一个 SHA-256 指纹")

    def test_the_pinned_host_is_the_voice_server(self):
        self.assertEqual(mumblecompat.PINNED_HOST, "audio.ceruleanavi.net")

    # ---------- 归一化 ----------
    def test_fingerprints_normalise(self):
        """openssl 打出来是带冒号大写的，源码里写的是小写连写，得认同一个。"""
        n = mumblecompat.normalise_fingerprint
        self.assertEqual(n("B7:B6:6E:EB"), "b7b66eeb")
        self.assertEqual(n("  b7 b6 6e eb "), "b7b66eeb")
        self.assertEqual(n(None), "")

    # ---------- 认与不认 ----------
    def test_the_matching_certificate_is_accepted(self):
        body, digest = self.fake_cert()
        mumblecompat.PINNED_FINGERPRINTS = (digest,)
        self.assertEqual(
            mumblecompat.check_certificate(mumblecompat.PINNED_HOST, body),
            digest)

    def test_a_certificate_that_is_not_pinned_is_refused(self):
        """中间人那一下。"""
        body, digest = self.fake_cert()
        mumblecompat.PINNED_FINGERPRINTS = (self.OTHER,)
        with self.assertLogs("mumblecompat", level="ERROR") as caught:
            with self.assertRaises(mumblecompat.CertificatePinError):
                mumblecompat.check_certificate(mumblecompat.PINNED_HOST, body)
        message = "\n".join(caught.output)
        self.assertIn(digest, message, "日志里要写清看到的是哪个指纹")
        self.assertIn(mumblecompat.FINGERPRINT_ENV, message,
                      "换过证书的话得告诉人怎么救")

    def test_a_missing_certificate_is_refused(self):
        """拿不到证书不能当成"没检查所以放行"。"""
        mumblecompat.PINNED_FINGERPRINTS = (self.OTHER,)
        with self.assertLogs("mumblecompat", level="ERROR"):
            with self.assertRaises(mumblecompat.CertificatePinError):
                mumblecompat.check_certificate(mumblecompat.PINNED_HOST, None)

    def test_the_host_match_is_not_case_sensitive(self):
        body, digest = self.fake_cert()
        mumblecompat.PINNED_FINGERPRINTS = (self.OTHER,)
        for host in ("AUDIO.CERULEANAVI.NET", "Audio.CeruleanAvi.Net",
                     "audio.ceruleanavi.net."):
            with self.assertLogs("mumblecompat", level="ERROR"):
                with self.assertRaises(mumblecompat.CertificatePinError,
                                       msg=f"{host} 应该也算官方服务器"):
                    mumblecompat.check_certificate(host, body)

    def test_another_host_is_not_pinned(self):
        """自建服务器 / 内网镜像 / 直接填 IP：指到别处是当真的，别挡人。

        `xpc`/`msfs` 的 mumble_host 是能改的，CLAUDE.md 里写着"一个把客户端
        指到测试服或者内网镜像的成员是有意的"。所以只记一行 INFO，把指纹告诉
        他，想钉的人有的可钉。
        """
        body, digest = self.fake_cert()
        mumblecompat.PINNED_FINGERPRINTS = (self.OTHER,)
        with self.assertLogs("mumblecompat", level="INFO") as caught:
            seen = mumblecompat.check_certificate("mumble.example.invalid", body)
        self.assertEqual(seen, digest)
        self.assertIn(digest, "\n".join(caught.output))

    # ---------- 环境变量这个救急口子 ----------
    def test_the_env_override_replaces_the_pin(self):
        """换证书的急救口：不用等发版就能连上。"""
        body, digest = self.fake_cert()
        mumblecompat.PINNED_FINGERPRINTS = (self.OTHER,)
        os.environ[mumblecompat.FINGERPRINT_ENV] = digest.upper()
        self.assertEqual(
            mumblecompat.check_certificate(mumblecompat.PINNED_HOST, body),
            digest)

    def test_the_env_override_takes_a_list(self):
        """换证书时新旧两个一起收，等大家都更新了再删旧的。"""
        body, digest = self.fake_cert()
        mumblecompat.PINNED_FINGERPRINTS = (self.OTHER,)
        os.environ[mumblecompat.FINGERPRINT_ENV] = f"{self.OTHER}, {digest}"
        self.assertIn(digest, mumblecompat.accepted_fingerprints())
        self.assertEqual(
            mumblecompat.check_certificate(mumblecompat.PINNED_HOST, body),
            digest)

    def test_an_empty_env_var_is_not_a_bypass(self):
        """空的环境变量等于没设，不是"清空指纹"。

        否则 `CAN_MUMBLE_FINGERPRINTS=` 就成了一个现成的关校验开关。
        """
        body, _ = self.fake_cert()
        mumblecompat.PINNED_FINGERPRINTS = (self.OTHER,)
        for value in ("", "   ", ",,"):
            os.environ[mumblecompat.FINGERPRINT_ENV] = value
            self.assertEqual(mumblecompat.accepted_fingerprints(),
                             (self.OTHER,))
            with self.assertLogs("mumblecompat", level="ERROR"):
                with self.assertRaises(mumblecompat.CertificatePinError):
                    mumblecompat.check_certificate(mumblecompat.PINNED_HOST, body)

    def test_no_pin_at_all_warns_instead_of_locking_everyone_out(self):
        """源码里那组被清空了是构建期的失误。为它把所有人的语音停掉不划算，
        但必须吵一声，不能安静地放行。"""
        body, digest = self.fake_cert()
        mumblecompat.PINNED_FINGERPRINTS = ()
        with self.assertLogs("mumblecompat", level="WARNING") as caught:
            self.assertEqual(
                mumblecompat.check_certificate(mumblecompat.PINNED_HOST, body),
                digest)
        self.assertIn("not checked", "\n".join(caught.output))

    # ---------- 抛出来的东西必须穿得出去 ----------
    def test_a_pin_failure_is_not_an_oserror(self):
        """**这条是设计，不是细节。**

        pymumble 的 `connect()` 里，握手和发 Authenticate 都在同一个
        `except socket.error`（也就是 OSError）里。继承 OSError 的话，指纹对不
        上会被吞成"连接失败"，然后 BoundedReconnect 再试三次——中间人重试三次
        还是中间人，只是把一条本该刺眼的记录冲淡成三条普通的网络错误。
        """
        self.assertFalse(issubclass(mumblecompat.CertificatePinError, OSError))

    def test_verify_peer_closes_the_socket_on_failure(self):
        """验不过就别留一条谁都以为还能用的 socket。"""
        body, _ = self.fake_cert()
        mumblecompat.PINNED_FINGERPRINTS = (self.OTHER,)

        class Sock:
            closed = False

            def getpeercert(self, binary_form=False):
                return body

            def close(self):
                Sock.closed = True

        with self.assertLogs("mumblecompat", level="ERROR"):
            with self.assertRaises(mumblecompat.CertificatePinError):
                mumblecompat.verify_peer(Sock(), (mumblecompat.PINNED_HOST, 64738))
        self.assertTrue(Sock.closed)

    def test_a_socket_that_cannot_produce_a_certificate_is_refused(self):
        mumblecompat.PINNED_FINGERPRINTS = (self.OTHER,)

        class Sock:
            def getpeercert(self, binary_form=False):
                raise ValueError("握手没完成")

            def close(self):
                pass

        with self.assertLogs("mumblecompat", level="ERROR"):
            with self.assertRaises(mumblecompat.CertificatePinError):
                mumblecompat.verify_peer(Sock(), (mumblecompat.PINNED_HOST, 64738))

    # ---------- 真的挂上去了吗 ----------
    def test_a_wrapped_socket_is_the_pinning_class(self):
        """`ssl.wrap_socket` 包出来的必须是会验指纹的那个子类。

        做成 `ssl.SSLSocket` 的子类而不是套代理：下游要拿它当真 socket 用
        （pymumble 主循环里的 select、再包一层的 _RetryingSocket）。
        """
        had = hasattr(ssl, "wrap_socket")
        original = getattr(ssl, "wrap_socket", None)
        try:
            if had:
                del ssl.wrap_socket
            mumblecompat.install()
            plain = socket.socket()
            wrapped = ssl.wrap_socket(plain)
            self.addCleanup(wrapped.close)
            self.assertIsInstance(wrapped, mumblecompat._PinnedSSLSocket)
            self.assertIsInstance(wrapped, ssl.SSLSocket)
        finally:
            if had:
                ssl.wrap_socket = original
            elif hasattr(ssl, "wrap_socket"):
                del ssl.wrap_socket

    def test_connect_checks_before_it_returns(self):
        """`connect()` 必须在返回之前验完——返回之后 pymumble 就发密码了。"""
        had = hasattr(ssl, "wrap_socket")
        original = getattr(ssl, "wrap_socket", None)
        seen = []
        try:
            if had:
                del ssl.wrap_socket
            mumblecompat.install()
            plain = socket.socket()
            wrapped = ssl.wrap_socket(plain)
            self.addCleanup(wrapped.close)

            # 真的去连会握手，这里只要证明这一步被调用了，所以把底下那层
            # 换成不做事的
            base = ssl.SSLSocket.connect
            ssl.SSLSocket.connect = lambda self, addr: None
            self.addCleanup(setattr, ssl.SSLSocket, "connect", base)
            check = mumblecompat.verify_peer
            mumblecompat.verify_peer = lambda sock, addr: seen.append(addr)
            self.addCleanup(setattr, mumblecompat, "verify_peer", check)

            wrapped.connect(("audio.ceruleanavi.net", 64738))
        finally:
            if had:
                ssl.wrap_socket = original
            elif hasattr(ssl, "wrap_socket"):
                del ssl.wrap_socket
        self.assertEqual(seen, [("audio.ceruleanavi.net", 64738)],
                         "connect() 没有验证书就返回了")


class PymumbleIntegrationTest(unittest.TestCase):
    """确认补丁之后 pymumble 真的能建起连接对象。"""

    def test_pymumble_connect_no_longer_raises_attributeerror(self):
        import sys
        from unittest import mock
        for name in ("opuslib", "opuslib.api", "opuslib.api.decoder",
                     "opuslib.api.encoder", "opuslib.api.info", "opuslib.exceptions"):
            sys.modules.setdefault(name, mock.MagicMock())

        mumblecompat.install()
        import pymumble_py3 as pymumble

        errors = []
        previous = threading.excepthook
        threading.excepthook = lambda args: errors.append(args.exc_type.__name__)
        self.addCleanup(lambda: setattr(threading, "excepthook", previous))

        # 连一个必然连不上的地址：重点是不能再出现 AttributeError
        client = pymumble.Mumble("127.0.0.1", "test", port=1, reconnect=False)
        client.start()
        client.join(timeout=15)

        self.assertNotIn("AttributeError", errors,
                         "补丁之后不该再因为 ssl.wrap_socket 缺失而崩")


class SendBufferTest(unittest.TestCase):
    """发送缓冲满了不等于连接断了。

    这是"一发话就掉线"的病根。pymumble 没有 UDP 通道，话音塞进 UDPTUNNEL 和
    控制消息走同一条 TCP，而那条 socket 是非阻塞的，发送循环却按 C 的写法兜错
    （`if sent < 0`——Python 里 send 是抛异常）。于是上行堵一下，
    `BlockingIOError` 一路穿到 `Mumble.run()` 的 `except socket.error`，
    连接被判死。真实日志里：掉线都在按下 PTT 两三秒后，而且每次第一下重连就成功
    ——因为网络上根本没断。
    """

    def make(self, blocks, error=None, timeout=1.0):
        """一个前 `blocks` 次写不动、之后正常的 socket。blocks=None 表示永远写不动。"""
        outer = self

        class Sock:
            def __init__(self):
                self.attempts = 0
                self.sent = []
                self.selected = 0

            def send(self, data):
                self.attempts += 1
                if blocks is None or self.attempts <= blocks:
                    raise (error or BlockingIOError(10035, "would block"))
                self.sent.append(bytes(data))
                return len(data)

            def fileno(self):
                return 1

            def recv(self, size):
                return b"payload"

        sock = Sock()
        guarded = mumblecompat._RetryingSocket(sock, timeout=timeout)
        # 不真的去 select 一个假 fd
        original_select = mumblecompat.select.select
        mumblecompat.select.select = lambda r, w, x, t=None: (
            sock.__setattr__("selected", sock.selected + 1), ([], [], []))[1]
        outer.addCleanup(setattr, mumblecompat.select, "select", original_select)
        return sock, guarded

    def test_a_full_buffer_is_retried_not_raised(self):
        sock, guarded = self.make(blocks=3)
        self.assertEqual(guarded.send(b"audio"), 5)
        self.assertEqual(sock.attempts, 4, "应当重试到写得进去为止")
        self.assertEqual(sock.sent, [b"audio"])
        self.assertGreaterEqual(sock.selected, 1, "重试之间要等 socket 可写")

    def test_tls_want_write_is_the_same_case(self):
        """TLS 上缓冲满抛的是 SSLWantWriteError，不是 BlockingIOError。"""
        sock, guarded = self.make(blocks=2, error=ssl.SSLWantWriteError())
        self.assertEqual(guarded.send(b"x"), 1)
        self.assertEqual(sock.attempts, 3)

    def test_a_socket_that_never_drains_still_reports_a_failure(self):
        """真的堵死了还是要当断线——不能无限期挂在那里。"""
        sock, guarded = self.make(blocks=None, timeout=0.05)
        with self.assertRaises(BlockingIOError):
            guarded.send(b"x")

    def test_everything_else_passes_through(self):
        sock, guarded = self.make(blocks=0)
        self.assertEqual(guarded.recv(7), b"payload")
        self.assertEqual(guarded.fileno(), 1, "select() 要靠 fileno()")

    def test_every_connection_gets_the_guard_including_reconnects(self):
        """必须挂在 connect() 上：pymumble 每次重连都会重建 socket。

        漏掉重连那一次，那条连接就退回旧行为——而这个症状只在网络不好的用户
        那里出现，自己这边根本复现不了。
        """
        import pymumble_py3.mumble as mumble_module

        original = mumble_module.Mumble.connect
        self.addCleanup(setattr, mumble_module.Mumble, "connect", original)

        def fake_connect(self):
            self.control_socket = object()      # 每次连接都是一个新 socket
            return 1

        mumble_module.Mumble.connect = fake_connect
        self.assertTrue(mumblecompat.patch_send(), "补丁没打上")

        client = mumble_module.Mumble.__new__(mumble_module.Mumble)
        for round_number in (1, 2):             # 首连，然后一次重连
            client.control_socket = None
            mumble_module.Mumble.connect(client)
            self.assertIsInstance(
                client.control_socket, mumblecompat._RetryingSocket,
                f"第 {round_number} 次连接之后 socket 没有被包住")

    def test_the_guard_is_not_stacked(self):
        client = type("M", (), {"control_socket": object()})()
        plain = client.control_socket
        self.assertTrue(mumblecompat.guard_control_socket(client))
        self.assertIs(client.control_socket._sock, plain)
        self.assertFalse(mumblecompat.guard_control_socket(client),
                         "已经包过就不该再套一层")


if __name__ == "__main__":
    unittest.main(verbosity=2)
