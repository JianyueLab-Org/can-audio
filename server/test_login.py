"""Ice 认证器的测试。

    python -m unittest test_login -v      （在 server 目录下运行）

不连 Murmur、不连 can-web：Ice、MumbleServer 和 requests 都在导入前换成替身，
所以开发机上不装 zeroc-ice 也能跑。

钉的是三件会让"全网都登不上语音"的事：
- 问 can-web 必须带超时。authenticate 跑在 Ice 的服务端分发线程上，上游变成
  黑洞就会把线程池占满，而进程看起来完全正常。
- 密码不能进日志。FSD 密码就是会员的网站密码，这个进程又是前台跑的。
- 验证不了和密码错了必须分开，否则用户会一直去改密码。
"""

import contextlib
import io
import sys
import threading
import time
import types
import unittest


def _install_stubs():
    """把 login.py 导入时要的三个外部模块换成替身。"""
    if "Ice" not in sys.modules:
        ice = types.ModuleType("Ice")
        ice.Exception = type("Exception", (Exception,), {})
        ice.ConnectionTimeoutException = type(
            "ConnectionTimeoutException", (ice.Exception,), {})
        ice.InitializationData = lambda: types.SimpleNamespace(properties=None)
        ice.createProperties = lambda: types.SimpleNamespace(
            setProperty=lambda *a: None)
        ice.initialize = lambda *a, **k: None
        sys.modules["Ice"] = ice

    if "MumbleServer" not in sys.modules:
        mumble = types.ModuleType("MumbleServer")
        # 这两个是要被继承的，必须是真的类
        mumble.ServerAuthenticator = type("ServerAuthenticator", (), {})
        mumble.ServerCallback = type("ServerCallback", (), {})
        for name in ("ServerPrx", "MetaPrx", "ServerAuthenticatorPrx",
                     "ServerCallbackPrx"):
            setattr(mumble, name, types.SimpleNamespace(
                checkedCast=lambda p: p, uncheckedCast=lambda p: p))
        sys.modules["MumbleServer"] = mumble

    if "requests" not in sys.modules:
        req = types.ModuleType("requests")
        exceptions = types.ModuleType("requests.exceptions")
        exceptions.RequestException = type("RequestException", (Exception,), {})
        exceptions.Timeout = type("Timeout", (exceptions.RequestException,), {})
        req.exceptions = exceptions
        req.post = lambda *a, **k: None
        sys.modules["requests"] = req
        sys.modules["requests.exceptions"] = exceptions


_install_stubs()

import login as login_module


class Response:
    def __init__(self, status_code, text="{}"):
        self.status_code = status_code
        self.text = text


class FakeUpstream:
    """假的 can-web。记下每一次调用，按剧本回应。"""

    def __init__(self, *outcomes):
        self.outcomes = outcomes
        self.calls = []          # 每次的 (url, kwargs)

    def post(self, url, headers=None, data=None, **kwargs):
        self.calls.append((url, dict(kwargs, headers=headers, data=data)))
        outcome = self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class UpstreamTestCase(unittest.TestCase):

    def setUp(self):
        self._delay = login_module.HTTP_RETRY_DELAY
        login_module.HTTP_RETRY_DELAY = 0.0      # 测试里不真的等
        self._post = login_module.requests.post

    def tearDown(self):
        login_module.HTTP_RETRY_DELAY = self._delay
        login_module.requests.post = self._post

    def upstream(self, *outcomes):
        fake = FakeUpstream(*outcomes)
        login_module.requests.post = fake.post
        return fake

    def network_error(self, message="连不上"):
        return login_module.requests.exceptions.RequestException(message)


class TimeoutTest(UpstreamTestCase):
    """问上游必须带超时。

    requests 没有默认超时。authenticate 跑在 Ice 的分发线程上，线程池
    SizeMax 是 8——上游一旦不回包，八个登录就能把认证器整个堵死，之后谁都
    连不上语音，而进程还活着、端口还听着，从外面完全看不出问题。
    """

    def test_every_request_carries_a_finite_timeout(self):
        fake = self.upstream(Response(200))
        login_module.verify("1000", "pw")
        self.assertTrue(fake.calls, "应当真的发出去了")
        for _url, kwargs in fake.calls:
            self.assertIn("timeout", kwargs,
                          "没有 timeout，requests 会一直等下去")
            self.assertIsInstance(kwargs["timeout"], (int, float))
            self.assertGreater(kwargs["timeout"], 0)

    def test_a_timing_out_upstream_does_not_hang_the_caller(self):
        """上游超时时 authenticate 必须照常返回，不能挂在分发线程上。"""
        self.upstream(login_module.requests.exceptions.Timeout("超时"))
        box = {}

        def work():
            box["value"] = login_module.verify("1000", "pw")

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive(), "verify 没有返回")
        self.assertEqual(box["value"], login_module.VERIFY_UNREACHABLE)


class RetryTest(UpstreamTestCase):

    def test_network_error_is_retried_and_can_still_succeed(self):
        fake = self.upstream(self.network_error(), Response(200))
        self.assertEqual(login_module.verify("1000", "pw"),
                         login_module.VERIFY_OK)
        self.assertEqual(len(fake.calls), 2, "网络错误应当再试一次")

    def test_a_rejection_is_never_retried(self):
        """接口明确说不行就别再打了。

        can-web 按 CAN 对认证失败限流，把明确的拒绝重发一遍等于自己把这个
        账号往锁死里推——密码本来就错，重试也不会变对。
        """
        fake = self.upstream(Response(400, '{"error":"bad"}'))
        self.assertEqual(login_module.verify("1000", "pw"),
                         login_module.VERIFY_REJECTED)
        self.assertEqual(len(fake.calls), 1)

    def test_it_gives_up_after_the_retries(self):
        fake = self.upstream(self.network_error())
        self.assertEqual(login_module.verify("1000", "pw"),
                         login_module.VERIFY_UNREACHABLE)
        self.assertEqual(len(fake.calls), login_module.HTTP_RETRIES + 1,
                         "不能无限重试")


class PasswordLoggingTest(UpstreamTestCase):
    """密码不能出现在任何一条日志里。

    FSD 密码就是会员的网站密码，而 start.sh 是前台跑 login.py 的，输出直接
    进终端和 journal。
    """

    SECRET = "hunter2-非常机密"

    def capture(self, call):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            call()
        return buffer.getvalue()

    def test_success_does_not_log_the_password(self):
        self.upstream(Response(200))
        output = self.capture(lambda: login_module.verify("1000", self.SECRET))
        self.assertNotIn(self.SECRET, output)
        self.assertIn("1000", output, "cid 还是要留着，否则没法查")

    def test_rejection_does_not_log_the_password(self):
        self.upstream(Response(400, '{"error":"bad"}'))
        output = self.capture(lambda: login_module.verify("1000", self.SECRET))
        self.assertNotIn(self.SECRET, output)

    def test_network_error_does_not_log_the_password(self):
        self.upstream(self.network_error())
        output = self.capture(lambda: login_module.verify("1000", self.SECRET))
        self.assertNotIn(self.SECRET, output)

    def test_atis_login_does_not_log_the_password(self):
        self.upstream(Response(200))
        output = self.capture(
            lambda: login_module.login_ATIS("1005_atis118000", self.SECRET))
        self.assertNotIn(self.SECRET, output)

    def test_no_atis_password_reaches_the_log_whatever_the_cid(self):
        """哪个 cid 都一样，口令不进日志。

        以前 900 这个保留账号走的是另一条分支，所以单独测过一遍；旁路去掉之后
        只剩一条路，但这条断言仍然值得留着——它盯的是"别把口令 print 出来"，
        和走哪条分支无关。
        """
        secret = "另一个账号的口令"
        self.upstream(Response(200))
        output = self.capture(
            lambda: login_module.login_ATIS("900_atis118000", secret))
        self.assertNotIn(secret, output)


class AuthenticateTest(UpstreamTestCase):
    """authenticate 的返回值决定用户看到什么。"""

    def setUp(self):
        super().setUp()
        self.kicked = []
        self.auth = login_module.AuthenticatorI.__new__(
            login_module.AuthenticatorI)
        self.auth.online_users = {}
        self.auth.context = {}
        self.auth.kick_previous_session = self.kicked.append

    def authenticate(self, name, password):
        return self.auth.authenticate(name, password, [], "", False)

    def test_a_good_login_returns_the_cid_as_the_user_id(self):
        self.upstream(Response(200))
        self.assertEqual(self.authenticate("1000", "pw"), (1000, "1000", []))

    def test_a_bad_password_is_a_plain_failure(self):
        self.upstream(Response(400))
        user_id, _, _ = self.authenticate("1000", "pw")
        self.assertEqual(user_id, login_module.AUTH_FAILED)

    def test_an_upstream_5xx_is_not_reported_as_a_bad_password(self):
        """can-web 部署中的 502 不是密码错误。

        报 AUTH_FAILED 的话，全网用户在那两分钟里都看到"密码错误"，反复改
        密码重连，每次都计进按账号的限流——上游恢复了人也被锁死了。
        """
        self.upstream(Response(502))
        user_id, _, _ = self.authenticate("1000", "pw")
        self.assertEqual(user_id, login_module.AUTH_TEMPORARY_FAILURE)

    def test_a_transient_5xx_recovers_within_the_retries(self):
        self.upstream(Response(503), Response(200))
        self.assertEqual(self.authenticate("1000", "pw"), (1000, "1000", []))

    def test_an_unreachable_upstream_is_not_reported_as_a_bad_password(self):
        """上游连不上时报 -1，用户看到的是"密码错误"，会一直去改密码。

        -3 是"暂时验证不了"，服务端不会把它当成密码问题。
        """
        self.upstream(self.network_error())
        user_id, _, _ = self.authenticate("1000", "pw")
        self.assertEqual(user_id, login_module.AUTH_TEMPORARY_FAILURE)
        self.assertNotEqual(user_id, login_module.AUTH_FAILED)

    def test_atis_user_id_is_the_frequency(self):
        # 同一个账号开多个不同频率的通播不能互相踢掉
        self.upstream(Response(200))
        user_id, name, _ = self.authenticate("1005_atis118000", "pw")
        self.assertEqual(user_id, 118000)
        self.assertEqual(name, "1005_atis118000")

    def test_there_is_no_shortcut_for_any_account(self):
        """保留账号的旁路已经去掉了，谁都得去问上游。

        默认部署只起 login.py，server/ATIS/ 那队服务端通播机不跑，那条免验证的
        旁路就是死代码。去掉之后连 900 也得老老实实过上游接口——这条断言就是钉
        住"没有任何账号能绕过认证"，免得哪天又被顺手加回来。
        """
        fake = self.upstream(Response(400))
        user_id, _, _ = self.authenticate("900_atis127800", "随便什么口令")
        self.assertEqual(user_id, login_module.AUTH_FAILED)
        self.assertEqual(len(fake.calls), 1, "应当老老实实去问上游")

    def test_a_normal_atis_client_still_authenticates(self):
        """管制员手里的桌面通播客户端不能被误伤。

        can-atis 登录用的是 `{自己的cid}_atis{频率}`，走的正是 login_ATIS。
        清理保留账号那条旁路时，很容易顺手把整条 _atis 路径一起删掉——那样管制员
        的通播客户端会直接登不上，而这是个还在用的功能。
        """
        self.upstream(Response(200))
        user_id, name, _ = self.authenticate("1005_atis127800", "pw")
        self.assertEqual(user_id, 127800, "用户 id 要是那 6 位频率")
        self.assertEqual(name, "1005_atis127800")

    def test_a_successful_login_kicks_the_previous_session(self):
        self.upstream(Response(200))
        self.authenticate("1000", "pw")
        self.assertEqual(self.kicked, ["1000"])

    def test_a_failed_login_does_not_kick_anyone(self):
        self.upstream(Response(400))
        self.authenticate("1000", "pw")
        self.assertEqual(self.kicked, [], "登录失败不该把在线的自己踢下去")

    def test_superuser_can_still_log_in(self):
        self.upstream(Response(200))
        user_id, _, _ = self.authenticate("SuperUser", "pw")
        self.assertEqual(user_id, login_module.AUTH_FALLTHROUGH)

    def test_a_name_this_authenticator_does_not_own_is_refused(self):
        """本认证器不认识的名字要**拒绝**，不是交回 Murmur。

        这里原来回的是 -2（fallthrough），依据是"内部库不认识的名字最终照样
        登不进来"。那个前提不成立：-2 的含义是"这个用户不归我管，用你自己的
        账号库"，而 Murmur 只在 serverpassword 非空时才拦未注册的访客——这个
        部署里 serverpassword 从没设过（Dockerfile 只改 ice= 和 logfile=，
        start.sh 只写 icesecretwrite，全仓 grep 不到第三处）。与此同时
        fix_acl.py 把 Enter/Speak/Whisper/Listen/MakeTempChannel 授给 `all`
        组，而 Mumble 的 `all` 是**包含未认证用户**的（`auth` 才不包含）。

        于是用户名填 guest、口令随便填就能落地，并在任意 FREQ_* 频道上对实时
        流量收发话音——verify() 一次都没被调用过。
        """
        fake = self.upstream(Response(200))
        user_id, _, _ = self.authenticate("guest", "无所谓")
        self.assertEqual(user_id, login_module.AUTH_FAILED)
        self.assertEqual(fake.calls, [], "不归我们管的名字不该拿去问上游")

    def test_only_the_exact_superuser_name_falls_through(self):
        """放行名单要精确匹配，否则它自己就成了新的绕过口子。

        Murmur 的 SuperUser 就是这个拼写，不可改名。任何变体都不是它。
        """
        self.upstream(Response(200))
        for name in ("superuser", "SUPERUSER", "SuperUser ", "SuperUserX",
                     "_SuperUser"):
            with self.subTest(name=name):
                user_id, _, _ = self.authenticate(name, "pw")
                self.assertEqual(user_id, login_module.AUTH_FAILED)

    def test_a_malformed_atis_name_is_not_given_a_bogus_id(self):
        """`_atis` 后面不是正好 6 位的，不能当通播账号放行。

        旧正则不卡结尾，1000_atis1180001 也算匹配，取 id 时 split 拿到 7 位的
        1180001——凭空造出一个谁也不认识的用户 id，而且它和任何真实频率都对不上。
        现在这种名字既不匹配通播格式、也不是纯数字，且不在
        MURMUR_LOCAL_ACCOUNTS 里，所以直接被拒，**绝不会**拿到任何用户 id。
        """
        self.upstream(Response(200))
        user_id, _, _ = self.authenticate("1000_atis1180001", "pw")
        self.assertEqual(user_id, login_module.AUTH_FAILED)

    def test_a_fullwidth_numeric_name_is_not_the_same_account(self):
        """int("１０００") == 1000——全角数字的名字绝不能拿到 1000 的用户 id。

        名字不同踢不掉对方的会话，id 相同却继承对方的全部 ACL。
        """
        self.upstream(Response(200))
        user_id, _, _ = self.authenticate("１０００", "pw")
        self.assertNotEqual(user_id, 1000)
        self.assertEqual(user_id, login_module.AUTH_FAILED)


class UserIdAgreementTest(unittest.TestCase):
    """nameToId 必须和 authenticate 对同一个名字给出同一个 id。

    两个方法原来各写各的：authenticate 认得 `1000_atis118000` 并给 118000，
    nameToId 在 int() 上抛异常、回落到 -2。后果是按名字把通播账号写进 ACL
    不生效——setACL 收下了，权限却落在一个不存在的用户上，看着完全成功。
    """

    def setUp(self):
        self.auth = login_module.AuthenticatorI.__new__(
            login_module.AuthenticatorI)

    def test_a_plain_account_is_its_asn_id(self):
        self.assertEqual(self.auth.nameToId("1000"), 1000)
        self.assertEqual(login_module.user_id_for("1000"), 1000)

    def test_an_atis_account_is_its_frequency(self):
        self.assertEqual(self.auth.nameToId("1000_atis118000"), 118000)

    def test_it_agrees_with_authenticate(self):
        for name in ("1000", "1005_atis127800", "900_atis118000"):
            with self.subTest(name=name):
                self.assertEqual(self.auth.nameToId(name),
                                 login_module.user_id_for(name))

    def test_an_unknown_name_falls_through_to_the_server(self):
        # -2 = 不认识这个用户，交回服务端自己的账号库
        self.assertEqual(self.auth.nameToId("SuperUser"),
                         login_module.AUTH_FALLTHROUGH)


class WatchConnectionTest(unittest.TestCase):
    """守着和 Murmur 的链路。断了要退出，不能假装还活着。"""

    def tearDown(self):
        login_module._shutting_down = False

    def test_a_broken_link_returns_false_quickly(self):
        class Dead:
            def isRunning(self, context):
                raise RuntimeError("连接没了")

        started = time.time()
        alive = login_module.watch_connection(Dead(), {}, interval=0.05)
        self.assertFalse(alive)
        self.assertLess(time.time() - started, 2.0)

    def test_a_healthy_link_keeps_watching(self):
        class Alive:
            def __init__(self):
                self.checks = 0

            def isRunning(self, context):
                self.checks += 1
                if self.checks >= 3:
                    login_module._shutting_down = True
                return True

        server = Alive()
        self.assertTrue(login_module.watch_connection(server, {}, interval=0.01))
        self.assertGreaterEqual(server.checks, 3, "应当反复确认，而不是只看一次")

    def test_the_secret_is_passed_on_every_check(self):
        # 不带 secret 的 Ice 调用会抛 InvalidSecretException
        seen = []

        class Server:
            def isRunning(self, context):
                seen.append(context)
                login_module._shutting_down = True
                return True

        login_module.watch_connection(Server(), {"secret": "s"}, interval=0.01)
        self.assertEqual(seen, [{"secret": "s"}])


class KickTest(unittest.TestCase):
    """真的去踢人的那一半。

    上面那组测试把 kick_previous_session 整个换成了一个 list.append，所以它从
    来没碰过 Ice —— 踢人代码用 oneway 代理调一个带 throws 子句的操作、每一次
    都抛 TwowayOnlyException、功能一次都没工作过，而测试全绿。这一组盯的就是
    那一层。
    """

    class TwowayOnlyException(Exception):
        pass

    class InvalidSessionException(Exception):
        pass

    class FakeFuture:
        def __init__(self, error=None):
            self.error = error
            self.callback = None

        def add_done_callback(self, fn):
            self.callback = fn
            fn(self)

        def result(self):
            if self.error:
                raise self.error

    class FakeServer:
        """照 Ice 的规则办事的假服务器。

        kickUser 的 slice 带 throws 子句，所以 Ice 不允许单向调用它。这个假货
        因此在 oneway 代理上遇到 kickUser 就抛 TwowayOnlyException，和真的
        Murmur 一模一样 —— 换句话说，重新引入那个「优化」会让这里变红。
        """

        def __init__(self, oneway=False, error=None):
            self.oneway = oneway
            self.error = error
            self.sync_calls = []
            self.async_calls = []

        def ice_oneway(self):
            return type(self)(oneway=True, error=self.error)

        def kickUser(self, session, reason, context=None):
            if self.oneway:
                raise KickTest.TwowayOnlyException(
                    "exception ::Ice::TwowayOnlyException")
            self.sync_calls.append(session)

        def kickUserAsync(self, session, reason, context=None):
            self.async_calls.append((session, reason, context))
            return KickTest.FakeFuture(self.error)

    def make(self, server):
        auth = login_module.AuthenticatorI.__new__(login_module.AuthenticatorI)
        auth.server = server
        auth.adapter = None
        auth.context = {"secret": "s"}
        auth.online_users = {"1000": 111}
        return auth

    def capture(self, fn):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            fn()
        return buffer.getvalue()

    def test_the_kick_goes_out_asynchronously(self):
        """异步而不是同步：Murmur 正卡在 authenticate 上等我们回话，同步的
        kickUser 要它处理完才返回，而它要我们返回才能处理 —— 真死锁。"""
        server = self.FakeServer()
        self.make(server).kick_previous_session("1000")

        self.assertEqual([call[0] for call in server.async_calls], [111])
        self.assertEqual(server.sync_calls, [],
                         "同步调用会和 Murmur 互等")

    def test_the_secret_context_is_carried(self):
        """少了它 Murmur 抛 InvalidSecretException，踢人静默失效。"""
        server = self.FakeServer()
        self.make(server).kick_previous_session("1000")
        self.assertEqual(server.async_calls[0][2], {"secret": "s"})

    def test_nobody_online_means_no_call_at_all(self):
        server = self.FakeServer()
        auth = self.make(server)
        auth.online_users = {}
        auth.kick_previous_session("1000")
        self.assertEqual(server.async_calls, [])

    def test_a_failed_kick_is_logged_rather_than_swallowed(self):
        """future 不看结果的话，踢人失败就退回成静默失效，而且连日志都没有。"""
        server = self.FakeServer(error=RuntimeError("boom"))
        output = self.capture(
            lambda: self.make(server).kick_previous_session("1000"))
        self.assertIn("踢出用户失败", output)

    def test_a_session_that_is_already_gone_is_not_an_error(self):
        """Murmur 自己也认同名会话（Disconnecting ghost），旧会话可能在我们的
        踢人到达之前就没了。那不是故障，不该每次登录都往日志里写一行。"""
        server = self.FakeServer(error=self.InvalidSessionException("gone"))
        output = self.capture(
            lambda: self.make(server).kick_previous_session("1000"))
        self.assertEqual(output, "")


# **必须放在文件末尾。** 原来它在 KickTest 之前，于是 `python test_login.py`
# 在 KickTest 还没定义时就开跑，静默少收 5 个测试（32 而不是 37）——而漏掉的
# 恰好是踢人那一组，也就是当年"整组静默失效、其余照常绿"的那个盲区。
if __name__ == "__main__":
    unittest.main(verbosity=2)
