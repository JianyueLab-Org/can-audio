"""服务端通播机队的频道逻辑测试。

    python -m unittest test_mumble -v      （在 server/ATIS 目录下运行）

不连服务器、不合成语音、不碰音频设备：Mumble 侧用替身，上游数据源和 edge-tts
在导入前就换成假的。

钉的是那个最难查的故障：pymumble 的 channels.new_channel() 和
users.myself.move_in() 都走 execute_command(blocking=True)，那个 lock.acquire()
没有任何超时（pymumble 源码里就写着 "TODO: manage a timeout for blocking
commands"）。命令一旦没被服务器处理，调用线程就永久卡死在那一行——这条通播
线程整个没了，日志停在建频道那里，既没有成功也没有任何错误，而管理器还以为
它在播。
"""

import os
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch


def _stub(name, **attrs):
    """给没装的第三方包做个占位，只为了让 mumble.py 能导进来。"""
    if name in sys.modules:
        return
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


_stub("requests", get=lambda *a, **k: None)
_stub("tabulate", tabulate=lambda *a, **k: "")
_stub("edge_tts", Communicate=object)

import mumble as mumble_module


class StationIdentityTest(unittest.TestCase):
    def test_same_frequency_airports_have_distinct_login_names(self):
        with patch.object(mumble_module.serverconf, "atis_account", return_value="1005"), \
                patch.object(mumble_module.serverconf, "atis_password", return_value="secret"), \
                patch.object(mumble_module.process, "process_single_atis_text", side_effect=lambda text, **kw: text):
            stations = [mumble_module.ATISBroadcaster(callsign, "128.800", "test")
                        for callsign in ("RJTT_ATIS", "RJOO_ATIS")]
        try:
            self.assertEqual([s.user for s in stations],
                             ["1005_atis128800_RJTT_ATIS", "1005_atis128800_RJOO_ATIS"])
            self.assertEqual([s.channel_name for s in stations], ["FREQ_128800"] * 2)
        finally:
            for station in stations:
                station.loop.close()


class FakeChannels:
    def __init__(self, server):
        self.server = server

    def find_by_name(self, name):
        import pymumble_py3 as pymumble
        with self.server.lock:
            if name in self.server.by_name:
                return self.server.by_name[name]
        raise pymumble.errors.UnknownChannelError(name)

    def new_channel(self, parent_id, name, temporary=False):
        self.server.hang("channels.new_channel")


class FakeMyself:
    def __init__(self, server):
        self.server = server

    def __getitem__(self, key):
        if key == "channel_id":
            return self.server.my_channel
        raise KeyError(key)

    def move_in(self, channel_id, token=None):
        self.server.hang("users.myself.move_in")


class FakeUsers:
    def __init__(self, server, session):
        self.myself = FakeMyself(server)
        self.myself_session = session


class FakeMumble:
    """够用的 Mumble 替身，重点是把 pymumble 的两种接口区别开。

    - ``execute_command(cmd, blocking=False)``：命令排队，假服务器在
      ``latency`` 之后才让它生效——真实的 pymumble 就是这样，命令是异步的，
      发出去不等于已经生效。
    - ``channels.new_channel()`` / ``users.myself.move_in()``：**永远不返回**，
      和真的 pymumble 一样。任何还在走阻塞接口的代码都会在测试里挂住，被
      ``join(timeout=…)`` 抓出来——这比断言"有没有调用某个函数"结实得多。
    """

    def __init__(self, latency=0.0, answers=True, my_channel=0, session=42):
        self.lock = threading.Lock()
        self.latency = latency
        self.answers = answers          # False = 服务器收下命令但什么都不做
        self.by_name = {}
        self.my_channel = my_channel
        self.next_id = 1
        self.commands = []
        self.blocking_calls = []
        self.channels = FakeChannels(self)
        self.users = FakeUsers(self, session)

    def hang(self, what):
        self.blocking_calls.append(what)
        threading.Event().wait()

    def execute_command(self, cmd, blocking=True):
        if blocking:
            self.hang("execute_command(blocking=True)")
        self.commands.append(cmd)
        if self.answers:
            timer = threading.Timer(self.latency, self._apply, args=(cmd,))
            timer.daemon = True
            timer.start()

    def _apply(self, cmd):
        params = cmd.parameters
        with self.lock:
            if "name" in params:                    # CreateChannel
                self.by_name[params["name"]] = {
                    "channel_id": self.next_id,
                    "name": params["name"],
                    "parent": params["parent"],
                    "temporary": params["temporary"],
                }
                self.next_id += 1
            elif "session" in params:               # MoveCmd
                self.my_channel = params["channel_id"]

    def created_names(self):
        return [(c.parameters["parent"], c.parameters["name"],
                 c.parameters["temporary"])
                for c in self.commands if "name" in c.parameters]

    def moves(self):
        return [c.parameters["channel_id"] for c in self.commands
                if "session" in c.parameters]


class JoinChannelTest(unittest.TestCase):

    def setUp(self):
        self._timeout = mumble_module.CHANNEL_TIMEOUT
        # 不跑真的构造函数——它会拉起 TTS 和事件循环
        self.caster = mumble_module.ATISBroadcaster.__new__(
            mumble_module.ATISBroadcaster)
        self.caster.channel_name = "FREQ_127800"
        self.caster.running = True
        self.server = FakeMumble(latency=0.2)
        self.caster.mumble = self.server

    def tearDown(self):
        mumble_module.CHANNEL_TIMEOUT = self._timeout

    def join(self, budget=None):
        """在独立线程里调 _join_channel，卡住不会拖死整个测试。"""
        if budget is None:
            budget = mumble_module.CHANNEL_TIMEOUT * 2 + 3
        box = {}

        def work():
            box["value"] = self.caster._join_channel()

        thread = threading.Thread(target=work, daemon=True)
        started = time.time()
        thread.start()
        thread.join(budget)
        elapsed = time.time() - started
        self.assertFalse(
            thread.is_alive(),
            f"_join_channel 在 {budget:.1f} 秒内没有返回；"
            f"走过的阻塞接口={self.server.blocking_calls}")
        return box.get("value"), elapsed

    def test_existing_channel_is_used_directly(self):
        self.server.by_name["FREQ_127800"] = {"channel_id": 7}
        result, _ = self.join()
        self.assertTrue(result)
        self.assertEqual(self.server.created_names(), [], "已存在就不该再建")
        self.assertEqual(self.server.my_channel, 7, "要真的进去，不是发完就算")

    def test_already_in_the_channel_needs_no_command_at_all(self):
        self.server.by_name["FREQ_127800"] = {"channel_id": 7}
        self.server.my_channel = 7
        result, _ = self.join()
        self.assertTrue(result)
        self.assertEqual(self.server.commands, [])

    def test_missing_channel_is_created_as_temporary(self):
        result, elapsed = self.join()
        self.assertTrue(result)
        self.assertEqual(self.server.created_names(), [(0, "FREQ_127800", True)])
        self.assertEqual(self.server.my_channel, self.server.moves()[0])
        self.assertGreaterEqual(elapsed, 0.2, "要等到服务器回 ChannelState")

    def test_a_server_that_never_answers_does_not_hang_the_thread(self):
        """这才是那个 bug：阻塞接口下服务器不回话，通播线程永远回不来。"""
        mumble_module.CHANNEL_TIMEOUT = 0.5
        self.server.answers = False
        result, elapsed = self.join()
        self.assertFalse(result)
        self.assertGreaterEqual(elapsed, 0.5, "该等的还是要等满")
        self.assertLess(elapsed, 3.0, "但必须有上界")
        self.assertEqual(self.server.blocking_calls, [],
                         "不能再走 pymumble 那两个没有超时的阻塞接口")

    def test_move_that_never_takes_effect_is_reported_as_failure(self):
        """进频道也是异步的，没确认就返回 True，通播会对着根频道播一整轮。"""
        mumble_module.CHANNEL_TIMEOUT = 0.5
        self.server.by_name["FREQ_127800"] = {"channel_id": 7}
        self.server.answers = False
        result, elapsed = self.join()
        self.assertFalse(result)
        self.assertEqual(self.server.moves(), [7], "命令还是要发出去的")
        self.assertNotEqual(self.server.my_channel, 7)
        self.assertGreaterEqual(elapsed, 0.5)

    def test_waits_rather_than_giving_up_immediately(self):
        # 服务器 0.4 秒后才回报频道——固定 sleep(0.1) 的老写法会在这里失败
        self.server.latency = 0.4
        result, elapsed = self.join()
        self.assertTrue(result)
        self.assertGreaterEqual(elapsed, 0.4)

    def test_stopping_aborts_the_wait(self):
        mumble_module.CHANNEL_TIMEOUT = 30.0
        self.server.answers = False

        def stop():
            time.sleep(0.2)
            self.caster.running = False

        threading.Thread(target=stop, daemon=True).start()
        result, elapsed = self.join(budget=5.0)
        self.assertFalse(result)
        self.assertLess(elapsed, 2.0, "停止之后不该继续等")


class RetireBroadcasterTest(unittest.TestCase):
    """一个卡住的席位不能把整队拖死。

    管理线程每 30 秒一轮，撤下席位时会 join 它。原来那个 join 没有超时——通播
    线程一旦卡住（比如卡在 pymumble 的阻塞命令里），管理线程就跟着永久卡死，
    之后所有席位都不再新建、换稿或撤下，而外面完全看不出异常。
    """

    def setUp(self):
        self._timeout = mumble_module.JOIN_TIMEOUT
        mumble_module.JOIN_TIMEOUT = 0.3        # 测试里不真的等
        self.manager = mumble_module.ATISManager()

    def tearDown(self):
        mumble_module.JOIN_TIMEOUT = self._timeout

    def make_broadcaster(self, stuck):
        """stuck=True 的线程永远不退出，和真的卡死一样。"""
        release = threading.Event()

        class Broadcaster(threading.Thread):
            def __init__(self):
                super().__init__(daemon=True)
                self.stopped = False

            def run(self):
                release.wait()          # stuck 时永远等下去

            def stop(self):
                self.stopped = True
                if not stuck:
                    release.set()

        broadcaster = Broadcaster()
        broadcaster.start()
        broadcaster._release = release
        return broadcaster

    def test_a_healthy_station_is_retired_cleanly(self):
        broadcaster = self.make_broadcaster(stuck=False)
        self.manager.broadcasters["ZSPD_ATIS"] = broadcaster
        self.manager._retire("ZSPD_ATIS")
        self.assertTrue(broadcaster.stopped)
        self.assertNotIn("ZSPD_ATIS", self.manager.broadcasters)
        self.assertFalse(broadcaster.is_alive())

    def test_a_stuck_station_does_not_block_the_manager(self):
        broadcaster = self.make_broadcaster(stuck=True)
        self.manager.broadcasters["ZSPD_ATIS"] = broadcaster
        started = time.time()
        self.manager._retire("ZSPD_ATIS")
        elapsed = time.time() - started
        self.assertLess(elapsed, 3.0, "卡住的席位把管理线程一起拖死了")
        self.assertGreaterEqual(elapsed, 0.3, "该等的还是要等满")
        self.assertNotIn("ZSPD_ATIS", self.manager.broadcasters,
                         "等不到也要从表里摘掉，否则每一轮都重来一次")
        broadcaster._release.set()

    def test_one_stuck_station_does_not_stop_the_others_from_being_retired(self):
        stuck = self.make_broadcaster(stuck=True)
        healthy = self.make_broadcaster(stuck=False)
        self.manager.broadcasters["ZSPD_ATIS"] = stuck
        self.manager.broadcasters["ZBAA_ATIS"] = healthy

        started = time.time()
        self.manager.stop()
        self.assertLess(time.time() - started, 4.0)
        self.assertTrue(healthy.stopped, "另一个席位照样要被收掉")
        self.assertEqual(self.manager.broadcasters, {})
        stuck._release.set()

    def test_retiring_something_that_is_not_there_is_harmless(self):
        self.manager._retire("不存在的席位")


class ConnectGateTest(unittest.TestCase):
    """登录没被认下来就不该往下走。

    start() 返回不代表连上了：登录被拒时 ConnectionRejectedError 死在 pymumble
    自己的线程里，这边什么都收不到。原来紧接着 sleep(1) 就去进频道，一个被拒的
    登录于是表现成"频道不存在"或者"进频道请求没有生效"——排查方向被带到 ACL 和
    权限上，而问题在账号。
    """

    def setUp(self):
        from pymumble_py3.constants import (PYMUMBLE_CONN_STATE_AUTHENTICATING,
                                            PYMUMBLE_CONN_STATE_CONNECTED,
                                            PYMUMBLE_CONN_STATE_FAILED)
        self.AUTHENTICATING = PYMUMBLE_CONN_STATE_AUTHENTICATING
        self.CONNECTED = PYMUMBLE_CONN_STATE_CONNECTED
        self.FAILED = PYMUMBLE_CONN_STATE_FAILED
        self._timeout = mumble_module.CONNECT_TIMEOUT
        self.caster = mumble_module.ATISBroadcaster.__new__(
            mumble_module.ATISBroadcaster)

    def tearDown(self):
        mumble_module.CONNECT_TIMEOUT = self._timeout

    def wait(self, states, timeout=0.5):
        """states 是依次返回的连接状态，最后一个之后一直保持不变。"""
        remaining = list(states)

        class Fake:
            @property
            def connected(_self):
                return remaining.pop(0) if len(remaining) > 1 else remaining[0]

        self.caster.mumble = Fake()
        return self.caster._wait_until_connected(timeout)

    def test_a_connection_that_comes_up_is_accepted(self):
        self.assertTrue(self.wait([self.AUTHENTICATING, self.CONNECTED]))

    def test_an_explicit_failure_is_not_waited_out(self):
        started = time.time()
        self.assertFalse(self.wait([self.FAILED], timeout=5))
        self.assertLess(time.time() - started, 2,
                        "服务端已经说了失败，不该再等满超时")

    def test_a_rejected_login_that_keeps_retrying_still_gives_up(self):
        """reconnect=True 时被拒的登录多半停在 AUTHENTICATING 反复重试。

        它永远不会落到 FAILED，所以只认 FAILED 的话这里会一直等下去——超时
        必须也算失败。
        """
        self.assertFalse(self.wait([self.AUTHENTICATING], timeout=0.3))

    def test_the_failure_message_names_the_account_and_the_three_conditions(self):
        """报错要说清是账号的事，不是频道的事。

        这队机器人没有免验证通道（login.py 那条旁路已删，且有测试钉着不许加
        回来），所以三个条件缺一不可，而三者的现象一模一样。
        """
        import io
        import contextlib
        self.caster.user = "900_atis127800"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.caster._report_login_failure()
        text = buffer.getvalue()
        self.assertIn("900_atis127800", text)
        self.assertIn("ATIS_CID", text, "要告诉人怎么换账号")
        self.assertIn("rating", text, "未评级会员会被上游拒绝，这条最容易漏")
        self.assertNotIn("频道", text, "别把人往频道和权限上带")


class CompatPatchTest(unittest.TestCase):
    """导入 mumble.py 就该把 pymumble 需要的两个补丁打上。

    断言的是补丁真的生效了，不是"源码里有那一行"：漏掉它的症状是通播机在
    Python 3.12+ 上一律"连接错误"，而排查方向会被带到密码和服务器上去。
    """

    def test_ssl_wrap_socket_is_available(self):
        import ssl
        self.assertTrue(hasattr(ssl, "wrap_socket"),
                        "pymumble 建 TLS 就靠这个函数，3.12 起要自己补回来")

    def test_the_send_path_is_guarded(self):
        """发送缓冲满了不该被当成掉线——通播是持续在发音频的。"""
        from pymumble_py3.mumble import Mumble
        self.assertTrue(getattr(Mumble.connect, "_can_guarded", False))

    # 所有带 mumblecompat.py 的组件。这里列全而不是只比 xpc 一份：原来只钉了
    # server/ATIS ↔ xpc，于是两个 legacy 飞行员端悄悄停在了老版本上——只有
    # ssl.wrap_socket 那一半，没有 patch_send()，也就是说"一发话就掉线"那个病
    # 在它们身上一直还在，而没有任何测试会发现。
    COMPONENTS = ("client", "xplane_client", "controller", "atis",
                  "xpc", "msfs")

    def test_the_copy_matches_the_clients(self):
        """mumblecompat.py 必须和**每一个**客户端那份逐字节相同。

        这个仓库靠复制共享（msfs 对 xpc 也是这么钉的），漂移了就意味着某个
        补丁只修了一边——而这个文件里的两个补丁，缺哪一个都是"连不上"或者
        "一发话就掉线"，排查方向还都会被带到密码和网络上去。
        """
        here = os.path.dirname(os.path.abspath(__file__))
        repo = os.path.join(here, "..", "..")
        with open(os.path.join(here, "mumblecompat.py"), "rb") as f:
            ours = f.read()

        checked = 0
        for component in self.COMPONENTS:
            theirs = os.path.join(repo, component, "mumblecompat.py")
            if not os.path.exists(theirs):
                continue        # 容器里只带了 server/，比不了
            with open(theirs, "rb") as f:
                reference = f.read()
            self.assertEqual(
                ours, reference,
                f"server/ATIS 和 {component} 的 mumblecompat.py 不一致了")
            checked += 1

        if not checked:
            self.skipTest("不在完整仓库里（比如容器里只带了 server/）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
