"""语音客户端里几处并发行为的测试。

    python -m unittest test_voice -v

钉的是"话音发到错误频率"这一类问题：PTT 和交叉耦合共用一条 Mumble 连接，
而 sound_output.target 是全局的一个字段，两条线程一起改就会串频。这种 bug
在真机上只表现为"偶尔有人在别的频率听到我说话"，几乎无法复现，所以必须在
这一层挡住。

不连服务器：Mumble 侧用替身。
"""

import sys
import threading
import time
import types
import unittest
from unittest import mock

for _name in ("opuslib", "opuslib.api", "opuslib.api.decoder",
              "opuslib.api.encoder", "opuslib.api.info", "opuslib.exceptions"):
    sys.modules.setdefault(_name, mock.MagicMock())

import radiostack
import voice
from voice import VoiceClient


class FakeSoundOutput:
    """记下每一块音频是用哪个 target 发出去的。"""

    def __init__(self):
        self.target = 0
        self.sent = []          # (target, 数据长度)
        self.lock = threading.Lock()

    def add_sound(self, pcm):
        with self.lock:
            self.sent.append((self.target, len(pcm)))

    def get_buffer_size(self):
        return 0


class FakeMumble:
    def __init__(self):
        self.sound_output = FakeSoundOutput()
        self.messages = []
        self.users = {}

    def send_message(self, type, message):
        self.messages.append((type, message))

    def is_alive(self):
        return True


def make_client():
    client = VoiceClient("host", "1000", "pw")
    client.mumble = FakeMumble()
    client.connected = True
    client.running = True
    return client


class VoiceTargetTest(unittest.TestCase):
    """PTT 和交叉耦合必须用不同的 VoiceTarget 编号。"""

    def test_ptt_and_cross_couple_use_different_targets(self):
        client = make_client()
        client._xc_channels = [11, 22, 33]
        client._program_cross_couple_targets()

        self.assertNotIn(voice.PTT_TARGET_ID, client._xc_targets.values(),
                         "交叉耦合不能占用 PTT 的编号，否则转发时会把 PTT 的"
                         "目标一起改掉")
        self.assertEqual(len(set(client._xc_targets.values())), 3,
                         "每个来源频率要有自己的编号")

    def test_cross_couple_excludes_its_own_source(self):
        client = make_client()
        client._xc_channels = [11, 22]
        client._program_cross_couple_targets()

        # 检查发出去的 VoiceTarget 消息：11 的目标里不该有 11
        programmed = {}
        for _type, message in client.mumble.messages:
            programmed[message.id] = [t.channel_id for t in message.targets]
        for source, target_id in client._xc_targets.items():
            self.assertNotIn(source, programmed[target_id],
                             "转发不能发回来源频率，否则会回环")

    def test_no_targets_when_fewer_than_two_xc(self):
        client = make_client()
        client._xc_channels = [11]
        client._program_cross_couple_targets()
        self.assertEqual(client._xc_targets, {})


class CrossCoupleDuringPttTest(unittest.TestCase):
    """管制员讲话时不转发——一条连接只有一个发送队列。"""

    def setUp(self):
        self.client = make_client()
        self.client._channel_ids = {118000: 11, 121700: 22}
        self.client._xc_channels = [11, 22]
        self.client._program_cross_couple_targets()
        self.client.mumble.messages.clear()

    def chunk(self):
        return mock.Mock(pcm=b"\x00" * 1920)

    def test_forwards_when_idle(self):
        self.client._forward_cross_couple(118000, self.chunk())
        self.assertEqual(len(self.client.mumble.sound_output.sent), 1)
        target, _ = self.client.mumble.sound_output.sent[0]
        self.assertEqual(target, self.client._xc_targets[11])

    def test_does_not_forward_while_transmitting(self):
        self.client.transmitting = True
        self.client._forward_cross_couple(118000, self.chunk())
        self.assertEqual(self.client.mumble.sound_output.sent, [],
                         "PTT 期间不该插入转发的音频")

    def test_target_is_reset_after_forwarding(self):
        self.client._forward_cross_couple(118000, self.chunk())
        self.assertEqual(self.client.mumble.sound_output.target, 0,
                         "转发完要把 target 归零，否则下一次发话会串频")

    def test_unknown_source_is_ignored(self):
        self.client._forward_cross_couple(999000, self.chunk())
        self.assertEqual(self.client.mumble.sound_output.sent, [])

    def test_concurrent_ptt_and_forwarding_never_mixes_targets(self):
        """并发跑一遍：每块音频的 target 要么是 PTT 的，要么是转发的。"""
        client = self.client
        client._tx_channels = [11]
        client.input_stream = mock.Mock()
        client.input_stream.read.return_value = b"\x01" * 1920

        stop = threading.Event()

        def forwarder():
            while not stop.is_set():
                client._forward_cross_couple(118000, self.chunk())
                time.sleep(0.001)

        thread = threading.Thread(target=forwarder, daemon=True)
        thread.start()
        client.start_transmit()
        time.sleep(0.3)
        client.stop_transmit()
        stop.set()
        thread.join(timeout=2)
        time.sleep(0.2)

        targets = {t for t, _ in client.mumble.sound_output.sent}
        allowed = {voice.PTT_TARGET_ID} | set(client._xc_targets.values())
        self.assertTrue(targets, "应当发出了一些音频")
        self.assertTrue(targets <= allowed,
                        f"出现了意料之外的 target: {targets - allowed}")


class TransmitThreadTest(unittest.TestCase):

    def test_rapid_ptt_does_not_start_two_threads(self):
        client = make_client()
        client._tx_channels = [11]
        client.input_stream = mock.Mock()
        client.input_stream.read.return_value = b"\x01" * 1920

        client.start_transmit()
        first = client._tx_thread
        client.stop_transmit()
        client.start_transmit()          # 立刻再按
        second = client._tx_thread

        self.assertIsNot(first, second)
        self.assertFalse(first.is_alive(),
                         "上一条发话线程要先收完尾，否则它退出时会把 target 清零")
        client.stop_transmit()

    def test_transmit_needs_a_tx_frequency(self):
        client = make_client()
        client._tx_channels = []
        client.start_transmit()
        self.assertFalse(client.transmitting, "没有 TX 频率时不该开始发话")


class RxIndicatorTest(unittest.TestCase):

    def test_continuous_audio_never_reports_rx_end(self):
        """有人一直在讲话时，监控线程不该报 RX 结束——那会让指示灯闪。"""
        client = make_client()
        events = []
        client.on_rx = lambda khz, active, name: events.append((khz, active))

        stop = threading.Event()

        def keep_talking():
            # 模拟回调线程持续收到话音，间隔远小于 RX_TIMEOUT
            while not stop.is_set():
                client._last_rx[118000] = time.time()
                time.sleep(0.02)

        talker = threading.Thread(target=keep_talking, daemon=True)
        monitor = threading.Thread(target=client._rx_monitor_loop, daemon=True)
        talker.start()
        monitor.start()
        time.sleep(0.6)
        stop.set()
        client.running = False
        talker.join(timeout=2)
        monitor.join(timeout=2)

        self.assertNotIn((118000, False), events,
                         "一直在收话音，不该报 RX 结束")

    def test_rx_ends_after_the_timeout(self):
        client = make_client()
        events = []
        client.on_rx = lambda khz, active, name: events.append((khz, active))

        client._last_rx[118000] = time.time() - 10      # 早就超时了
        monitor = threading.Thread(target=client._rx_monitor_loop, daemon=True)
        monitor.start()
        time.sleep(0.3)
        client.running = False
        monitor.join(timeout=2)

        self.assertIn((118000, False), events, "超时之后应当报 RX 结束")


class FakeChannels:
    def __init__(self, server):
        self.server = server

    def find_by_name(self, name):
        import pymumble_py3 as pymumble
        with self.server.lock:
            if name in self.server.by_name:
                return self.server.by_name[name]
        raise pymumble.errors.UnknownChannelError(name)

    def __getitem__(self, channel_id):
        with self.server.lock:
            for channel in self.server.by_name.values():
                if channel["channel_id"] == channel_id:
                    return channel
        raise KeyError(channel_id)

    def new_channel(self, parent_id, name, temporary=False):
        self.server.hang("channels.new_channel")


class FakeMyself:
    def __init__(self, server):
        self.server = server

    def __getitem__(self, key):
        if key == "channel_id":
            return self.server.my_channel
        raise KeyError(key)

    def get(self, key, default=None):
        if key == "channel_id":
            return self.server.my_channel
        return default

    def move_in(self, channel_id, token=None):
        self.server.hang("users.myself.move_in")


class FakeUsers:
    def __init__(self, server, session):
        self.myself = FakeMyself(server)
        self.myself_session = session


class FakeServer:
    """够用的 Mumble 替身，重点是把 pymumble 的两种接口区别开。

    - ``execute_command(cmd, blocking=False)``：命令排队，假服务器在
      ``latency`` 之后才让它生效——真实的 pymumble 就是这样，命令是异步的。
    - ``channels.new_channel()`` / ``users.myself.move_in()``：**永远不返回**。
      这两个入口走 ``execute_command(blocking=True)``，那个 ``lock.acquire()``
      没有任何超时（pymumble 源码里就写着 "TODO: manage a timeout for blocking
      commands"），服务器不处理命令时调用线程就死在那一行，而 sync() 是整条
      电台栈同步链的入口，一起死掉。

    照搬这个行为，任何还在走阻塞接口的代码都会在测试里挂住，被
    ``join(timeout=…)`` 抓出来。
    """

    def __init__(self, latency=0.0, answers=True, my_channel=0, session=42):
        self.lock = threading.Lock()
        self.latency = latency
        self.answers = answers          # False = 服务器收下命令但什么都不做
        # True = 进不存在的频道时什么都不做，和真服务器一样（既不报错也不照做）。
        # 默认关着，因为多数用例根本不建频道就直接 _join(7) 试进频道的语义。
        self.strict_moves = False
        self.by_name = {}
        self.my_channel = my_channel
        self.next_id = 1
        self.commands = []
        self.blocking_calls = []
        self.messages = []
        self.channels = FakeChannels(self)
        self.users = FakeUsers(self, session)
        self.sound_output = FakeSoundOutput()

    def hang(self, what):
        self.blocking_calls.append(what)
        threading.Event().wait()        # 永远不返回，和真的 pymumble 一样

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
                target = params["channel_id"]
                if self.strict_moves and target not in {
                        c["channel_id"] for c in self.by_name.values()}:
                    return                          # 频道没了，服务器直接无视
                self.my_channel = target

    def remove_channel(self, name):
        """服务器销毁一个空掉的临时频道。

        频率频道都是 temporary=True，最后一个人一走服务器当场就把它删了——这
        不需要断线重连，管制员把主频率从 A 换到 B 就够了。
        """
        with self.lock:
            return self.by_name.pop(name, None)

    def live_ids(self):
        """服务器上真实存在的频道号。"""
        with self.lock:
            return {c["channel_id"] for c in self.by_name.values()}

    def send_message(self, type, message):
        self.messages.append((type, message))

    def is_alive(self):
        return True

    def created_names(self):
        return [(c.parameters["parent"], c.parameters["name"],
                 c.parameters["temporary"])
                for c in self.commands if "name" in c.parameters]

    def moves(self):
        return [c.parameters["channel_id"] for c in self.commands
                if "session" in c.parameters]


class ChannelSwitchTest(unittest.TestCase):
    """建频道和进频道都不能走 pymumble 的阻塞接口。

    那个 ``lock.acquire()`` 没有超时，服务器不处理命令就永久卡住，调用线程整个
    死掉——日志停在"建一个临时的"，之后既没有成功也没有任何错误。这里用一个
    会永久挂住的替身把它逼出来。
    """

    def setUp(self):
        self._timeout = voice.CHANNEL_TIMEOUT
        self.client = VoiceClient("host", "1000", "pw")
        self.server = FakeServer(latency=0.2)
        self.client.mumble = self.server
        self.client.connected = True
        self.client.running = True

    def tearDown(self):
        voice.CHANNEL_TIMEOUT = self._timeout

    def call(self, func, budget=None):
        """在独立线程里跑，卡住就当场失败而不是拖死整个测试。"""
        if budget is None:
            budget = voice.CHANNEL_TIMEOUT * 2 + 3
        box = {}

        def work():
            box["value"] = func()

        thread = threading.Thread(target=work, daemon=True)
        started = time.time()
        thread.start()
        thread.join(budget)
        elapsed = time.time() - started
        self.assertFalse(
            thread.is_alive(),
            f"{func} 在 {budget:.1f} 秒内没有返回；"
            f"走过的阻塞接口={self.server.blocking_calls}")
        return box.get("value"), elapsed

    # ---------- 建频道 ----------
    def test_missing_channel_is_created_and_waited_for(self):
        result, elapsed = self.call(lambda: self.client._resolve_channel(118000))
        self.assertEqual(self.server.created_names(), [(0, "FREQ_118000", True)])
        self.assertEqual(result, 1)
        self.assertGreaterEqual(elapsed, 0.2, "要等到服务器回 ChannelState")
        self.assertEqual(self.client._channel_ids[118000], 1)
        self.assertEqual(self.client._channel_to_khz[1], 118000)

    def test_existing_channel_is_not_created_again(self):
        self.server.by_name["FREQ_118000"] = {"channel_id": 7}
        result, _ = self.call(lambda: self.client._resolve_channel(118000))
        self.assertEqual(result, 7)
        self.assertEqual(self.server.commands, [])

    def test_unanswered_creation_gives_up_within_the_timeout(self):
        voice.CHANNEL_TIMEOUT = 0.5
        self.server.answers = False
        result, elapsed = self.call(lambda: self.client._resolve_channel(118000))
        self.assertIsNone(result)
        self.assertGreaterEqual(elapsed, 0.5, "该等的还是要等满")
        self.assertLess(elapsed, 3.0, "但必须有上界——以前这里是永久卡死")
        self.assertEqual(self.server.blocking_calls, [],
                         "不能再走 pymumble 那两个没有超时的阻塞接口")

    def test_a_failed_resolve_is_not_cached(self):
        """失败不能记进表里，否则频道后来建出来了也永远用不上。"""
        voice.CHANNEL_TIMEOUT = 0.3
        self.server.answers = False
        self.call(lambda: self.client._resolve_channel(118000))
        self.assertNotIn(118000, self.client._channel_ids)

    # ---------- 进频道 ----------
    def test_join_waits_until_the_move_took_effect(self):
        result, elapsed = self.call(lambda: self.client._join(7))
        self.assertTrue(result)
        self.assertEqual(self.server.moves(), [7])
        self.assertEqual(self.server.my_channel, 7)
        self.assertGreaterEqual(elapsed, 0.2)

    def test_join_reports_failure_when_the_move_never_takes_effect(self):
        voice.CHANNEL_TIMEOUT = 0.5
        self.server.answers = False
        result, elapsed = self.call(lambda: self.client._join(7))
        self.assertFalse(result)
        self.assertEqual(self.server.moves(), [7], "命令还是要发出去的")
        self.assertGreaterEqual(elapsed, 0.5)
        self.assertLess(elapsed, 3.0)

    def test_join_does_nothing_when_already_there(self):
        self.server.my_channel = 7
        result, _ = self.call(lambda: self.client._join(7))
        self.assertTrue(result)
        self.assertEqual(self.server.commands, [])

    def test_join_does_not_trust_a_reused_id_with_the_wrong_name(self):
        """同一个数字 ID 被复用时，不能把错误频道当成已入频率频道。"""
        self.server.by_name["FREQ_121700"] = {
            "channel_id": 7,
            "name": "FREQ_121700",
            "parent": 0,
            "temporary": True,
        }
        self.server.my_channel = 7
        result, _ = self.call(
            lambda: self.client._join(7, expected_name="FREQ_118000"))
        self.assertFalse(result)
        self.assertEqual(self.server.commands, [],
                         "应先按频道名称重新解析，而不是重复移动到错误频道")

    def test_join_frequency_reresolves_after_id_reuse(self):
        """频道号撞车时，按频率名称重新解析并进入正确频道。"""
        self.server.by_name["FREQ_121700"] = {
            "channel_id": 7,
            "name": "FREQ_121700",
            "parent": 0,
            "temporary": True,
        }
        self.server.my_channel = 7

        result, _ = self.call(
            lambda: self.client._join_frequency(118000, 7),
            budget=voice.CHANNEL_TIMEOUT * 4 + 3)

        self.assertTrue(result)
        self.assertEqual(self.server.by_name["FREQ_118000"]["name"],
                         "FREQ_118000")
        self.assertEqual(
            self.server.my_channel,
            self.server.by_name["FREQ_118000"]["channel_id"])

    # ---------- 整条同步链 ----------
    def test_sync_finishes_even_if_the_server_never_answers(self):
        """sync() 是整条链的入口，卡在这里等于电台栈永远同步不上去。"""
        voice.CHANNEL_TIMEOUT = 0.3
        self.server.answers = False
        stack = radiostack.RadioStack()
        for freq in ("118.000", "121.700"):
            stack.add(freq)
            stack.get(radiostack.parse_frequency(freq)).rx = True
        stack.selected_khz = 118000
        _, elapsed = self.call(lambda: self.client.sync(stack), budget=6.0)
        self.assertLess(elapsed, 5.0, "两个频率各等一个超时，也该结束了")
        self.assertEqual(self.server.blocking_calls, [])

    def test_sync_joins_the_selected_frequency(self):
        stack = radiostack.RadioStack()
        for freq in ("118.000", "121.700"):
            stack.add(freq)
            stack.get(radiostack.parse_frequency(freq)).rx = True
        stack.selected_khz = 121700
        self.call(lambda: self.client.sync(stack), budget=8.0)
        joined = self.client._channel_ids[121700]
        self.assertEqual(self.server.my_channel, joined,
                         "主频率的频道要真的进去，不然服务端不支持监听时一个"
                         "频率都听不到")


class TemporaryChannelRemovedTest(unittest.TestCase):
    """临时频道被销毁之后，频率→频道号的缓存必须跟着失效。

    `_channel_ids` 只在重连时清空，可频道消失根本不需要重连：频率频道是
    temporary 的，管制员把主频率从 A 换到 B、A 上又没别人，A 当场就被服务器
    删掉了。旧号却一直留在缓存里，再用到 A 的时候：

    - `_join(旧号)` 发出去的 MoveCmd 指向一个不存在的频道，服务器不会报错也
      不会照做，日志里只剩一行接一行的"5 秒内没有生效"；
    - `_set_voice_target` 编进 VoiceTarget 的也是那个旧号，话音被服务器直接
      丢掉，同样一声不吭。

    合起来正好是"收得到、发不动、不报错"：监听还落在活着的频道上，所以耳朵
    是好的，嘴是哑的。
    """

    def setUp(self):
        # 失败路径要等满一个超时，缩短一点免得整轮测试拖上半分钟
        self._timeout = voice.CHANNEL_TIMEOUT
        voice.CHANNEL_TIMEOUT = 0.5

        self.client = VoiceClient("host", "1000", "pw")
        self.server = FakeServer(latency=0.05)
        self.server.strict_moves = True
        self.client.mumble = self.server
        self.client.connected = True
        self.client.running = True

        self.stack = radiostack.RadioStack()
        for khz in (118000, 121700):
            self.stack.add(khz)
            radio = self.stack.get(khz)
            radio.rx = True
            radio.tx = True
        self.stack.select(118000)

    def tearDown(self):
        voice.CHANNEL_TIMEOUT = self._timeout

    def sync_and_wait(self):
        thread = threading.Thread(target=self.client.sync, args=(self.stack,),
                                  daemon=True)
        thread.start()
        thread.join(voice.CHANNEL_TIMEOUT * 4 + 5)
        self.assertFalse(thread.is_alive(), "sync 没有在预算内返回")

    def switch_away_and_let_the_old_channel_die(self):
        """切到 121.700，再让服务器把空掉的 FREQ_118000 销毁。"""
        self.sync_and_wait()
        old = self.client._channel_ids[118000]
        self.assertEqual(self.server.my_channel, old, "前提：先进了 118.000")

        self.stack.select(121700)
        self.sync_and_wait()
        self.assertNotEqual(self.server.my_channel, old, "前提：已经换到 121.700")

        self.assertIsNotNone(self.server.remove_channel("FREQ_118000"))
        return old

    def test_a_removed_channel_is_not_joined_by_its_old_id(self):
        old = self.switch_away_and_let_the_old_channel_die()

        self.stack.select(118000)
        self.sync_and_wait()

        self.assertNotEqual(self.client._channel_ids[118000], old,
                            "频道已经被销毁，旧号不能再用")
        rebuilt = self.server.by_name.get("FREQ_118000")
        self.assertIsNotNone(rebuilt, "频道没了就该重建，不是拿着旧号一直试")
        self.assertEqual(self.server.my_channel, rebuilt["channel_id"],
                         "人没有进到 118.000 里去——MoveCmd 指着一个不存在的频道，"
                         "服务器不报错也不照做，日志里只会刷「5 秒内没有生效」")

    def test_a_removed_channel_never_ends_up_in_the_voice_target(self):
        self.switch_away_and_let_the_old_channel_die()

        self.stack.select(118000)
        self.sync_and_wait()

        programmed = None
        for _type, message in self.server.messages:
            if getattr(message, "id", None) == voice.PTT_TARGET_ID:
                programmed = [t.channel_id for t in message.targets]
        self.assertIsNotNone(programmed, "根本没编 PTT 的 VoiceTarget")
        live = self.server.live_ids()
        for channel_id in programmed:
            self.assertIn(channel_id, live,
                          "发话目标里有已经不存在的频道号，话音会被服务器悄悄"
                          "丢掉——收得到、发不动、不报错")


class ReconnectTest(unittest.TestCase):
    """重连之后必须把整个电台栈重新推一遍。

    客户端是 reconnect=True 建的，掉线后 pymumble 自己会连回来，但服务器把重连
    上来的用户放回**根频道**，频道监听和 VoiceTarget 这些跟会话走的注册也一并
    没了。只把界面的灯点回绿色的话，就成了最糟的那种情况：显示一切正常，人却
    待在根频道，一个频率都收不到，PTT 也发不出去。
    """

    def setUp(self):
        self.client = VoiceClient("host", "1000", "pw")
        self.server = FakeServer(latency=0.05)
        self.client.mumble = self.server
        self.client.connected = True
        self.client.running = True

        self.stack = radiostack.RadioStack()
        self.stack.add(118000)
        self.stack.add(121700)
        for radio in self.stack:
            radio.rx = True
            radio.tx = True
        self.stack.select(118000)

    def sync_and_wait(self):
        thread = threading.Thread(target=self.client.sync, args=(self.stack,),
                                  daemon=True)
        thread.start()
        thread.join(voice.CHANNEL_TIMEOUT * 4 + 5)
        self.assertFalse(thread.is_alive(), "sync 没有在预算内返回")

    def test_reconnect_rejoins_and_reprograms_everything(self):
        self.sync_and_wait()
        primary = self.client._channel_ids[118000]
        self.assertEqual(self.server.my_channel, primary, "前提：先进了主频道")

        # 服务器把重连上来的用户放回根频道，会话级的注册全部作废
        self.server.my_channel = 0
        moves_before = len(self.server.moves())
        messages_before = len(self.server.messages)

        self.client._resync_after_reconnect()
        # 重推是在后台线程里做的，等它把该发的都发完
        deadline = time.time() + voice.CHANNEL_TIMEOUT * 4 + 5
        while time.time() < deadline and not (
                self.server.my_channel != 0
                and len(self.server.messages) > messages_before):
            time.sleep(0.05)

        self.assertNotEqual(self.server.my_channel, 0,
                            "重连之后没有回到频率频道，人还在根频道")
        self.assertGreater(len(self.server.moves()), moves_before,
                           "重连之后没有重新发进频道的命令")
        self.assertGreater(len(self.server.messages), messages_before,
                           "重连之后没有重新发频道监听 / 发话目标")

    def test_stale_caches_do_not_suppress_the_resend(self):
        """光调 sync 不够，本地缓存必须一起作废。

        `_listening` 还记着断线前监听了哪些频道，diff 出来是空的，一条监听消息
        都不会再发；`_sent_target` 会让发话目标的去重逻辑以为已经设好了。两个
        加起来的效果就是：重连后看着一切正常，实际上一个频率都收不到。
        """
        self.sync_and_wait()
        self.assertTrue(self.client._listening or self.client._sent_target,
                        "前提：第一次同步之后本地是有缓存的")

        # 把重推挡掉，单看"作废缓存"这一步——否则后台那次 sync 会立刻把缓存
        # 填回来，断言到底看到的是清掉之前还是之后全凭手速
        resynced = []
        self.client.sync = lambda stack: resynced.append(stack)
        self.client._resync_after_reconnect()

        self.assertEqual(self.client._listening, set())
        self.assertIsNone(self.client._sent_target)
        self.assertEqual(self.client._channel_ids, {},
                         "临时频道空了会被销毁，再建是新号，旧的频道号不能留")
        deadline = time.time() + 5
        while time.time() < deadline and not resynced:
            time.sleep(0.02)
        self.assertEqual(resynced, [self.stack], "作废了缓存却没有重新推一遍")


class SyncStormTest(unittest.TestCase):
    """一阵栈变化不能变成一场建频道风暴。

    真实日志（can-controller.log，13 小时）里的样子：建频道 202 次、
    「已经不在了」180 次、进频道请求 40 次没有生效，其中 93 次建频道和上一次
    落在**同一秒**，最密的一分钟建了 34 次，服务器开始用 ChannelName（重名）
    回拒——而管制员那一晚一直留在根频道，听不到也发不出，界面全是绿的。

    三条成因，这一组各钉一条：
    - `RadioStack(on_change=…)` 覆盖了加删、RX/TX/XC、音量、静音、选主频率，
      再加上数据源那条 60 秒定时器，一阵操作排出十几轮 sync，每轮几秒；
    - 一轮 sync 里同一个频率被解析两三遍（RX/TX/XC 各一遍），第一遍的建频道
      没及时回来时，后面几遍会再各发一次 CreateChannel；
    - 被服务器拒绝的建频道还要在 `_sync_lock` 里干等满 CHANNEL_TIMEOUT。
    """

    def setUp(self):
        self._timeout = voice.CHANNEL_TIMEOUT
        voice.CHANNEL_TIMEOUT = 0.4          # 别让每条用例真等 5 秒
        self.client = VoiceClient("host", "1000", "pw")
        self.server = FakeServer(latency=0.02)
        self.client.mumble = self.server
        self.client.connected = True
        self.client.running = True

    def tearDown(self):
        voice.CHANNEL_TIMEOUT = self._timeout

    def stack_with(self, *frequencies, tx=(), xc=()):
        stack = radiostack.RadioStack()
        for khz in frequencies:
            stack.add(khz)
        for radio in stack:
            radio.rx = True
            radio.tx = radio.frequency_khz in tx or radio.frequency_khz in xc
            radio.xc = radio.frequency_khz in xc
        stack.select(frequencies[0])
        return stack

    def fire(self, calls):
        """同时打进来一批 sync，等它们全部返回。"""
        threads = [threading.Thread(target=self.client.sync, args=(stack,),
                                   daemon=True) for stack in calls]
        for thread in threads:
            thread.start()
            time.sleep(0.02)             # 模拟界面上一个个开关按过去
        for thread in threads:
            thread.join(timeout=voice.CHANNEL_TIMEOUT * 4 + 5)
            self.assertFalse(thread.is_alive(), "sync 没有返回")

    # ---------- 合并 ----------
    def test_a_burst_of_changes_collapses_into_two_rounds(self):
        """正在跑的那一轮 + 最多再排一轮，中间的全部丢掉。

        丢得起：每一轮都重读栈，排在中间的那些轮做的是完全一样的事。
        """
        rounds = []

        def slow_sync(stack):
            rounds.append(tuple(r.frequency_khz for r in stack))
            time.sleep(0.3)
        self.client._sync = slow_sync

        stack = self.stack_with(118000)
        self.fire([stack] * 10)
        self.assertLessEqual(len(rounds), 2,
                             f"10 次栈变化推了 {len(rounds)} 轮 sync")
        self.assertGreaterEqual(len(rounds), 1, "一轮都没推")

    def test_the_queued_round_pushes_the_newest_stack(self):
        """排队那一轮读的必须是最新的栈，不是把它排进来时的那一份。

        否则合并就成了丢状态：用户最后按的那个开关不生效。
        """
        pushed = []

        def slow_sync(stack):
            pushed.append(tuple(r.frequency_khz for r in stack))
            time.sleep(0.3)
        self.client._sync = slow_sync

        first = self.stack_with(118000)
        middle = self.stack_with(118000, 121700)
        newest = self.stack_with(118000, 121700, 125900)
        self.fire([first, middle, newest])

        self.assertEqual(pushed[0], (118000,), "第一轮应当立刻跑")
        self.assertEqual(pushed[-1], (118000, 121700, 125900),
                         f"最后推的不是最新的栈：{pushed}")

    def test_a_reconnect_resync_is_not_swallowed(self):
        """合并带来的唯一风险：重连要推的那一轮被"门口有人了"挡掉。

        挡掉的后果最难查——重连之后服务器把人放回根频道，而界面是绿的。
        安全的原因是排队那一轮读的是**最新**的栈和**清过**的缓存：
        `_resync_after_reconnect` 先作废缓存再调 sync，所以它之后跑的那一轮
        必然是一次完整重推。这条把它钉住。
        """
        seen = []
        started = threading.Event()
        release = threading.Event()

        def slow_sync(stack):
            seen.append(dict(self.client._channel_ids))
            started.set()
            release.wait(timeout=3)
        self.client._sync = slow_sync
        self.client._stack = self.stack_with(118000)
        self.client._channel_ids = {118000: 7}       # 断线前的旧号

        # 一轮正在跑，门口再排一轮——此刻门口已经满了
        for _ in range(2):
            threading.Thread(target=self.client.sync,
                             args=(self.client._stack,), daemon=True).start()
            self.assertTrue(started.wait(timeout=3))
            time.sleep(0.05)

        self.client._resync_after_reconnect()        # 这时候重连了
        release.set()

        deadline = time.time() + 3
        while time.time() < deadline and not any(s == {} for s in seen[1:]):
            time.sleep(0.05)
        self.assertTrue(any(s == {} for s in seen[1:]),
                        f"重连之后没有推一轮清过缓存的完整同步：{seen}")

    # ---------- 一轮里只解析一次 ----------
    def test_one_frequency_is_created_once_per_round(self):
        """RX / TX / XC 三个集合里的同一个频率，一轮只能建一次频道。

        `answers=False` 让建频道永远不出现在频道表里（远程服务器慢、或者被拒都
        是这个形状）。修之前：RX 那遍建一次、TX 那遍再建一次、XC 那遍第三次。
        """
        self.server.answers = False
        stack = self.stack_with(118000, 121700, tx=(118000,), xc=(118000, 121700))
        self.fire([stack])

        created = [name for _, name, _ in self.server.created_names()]
        self.assertEqual(sorted(created), ["FREQ_118000", "FREQ_121700"],
                         f"同一轮里重复建了频道：{created}")

    # ---------- 被拒就别等 ----------
    def test_a_refused_create_returns_immediately(self):
        """服务器说了不给建，就不要在锁里干等满 CHANNEL_TIMEOUT。

        那段等待是在 _sync_lock 里面的，会把后面排队的 sync 一起拖住——风暴就是
        这么攒出来的。
        """
        voice.CHANNEL_TIMEOUT = 3.0
        self.server.answers = False
        event = types.SimpleNamespace(type=4, reason="Duplicate channel name")

        def deny():
            time.sleep(0.2)
            self.client._on_permission_denied(event)
        threading.Thread(target=deny, daemon=True).start()

        started = time.time()
        result = self.client._resolve_channel(118000)
        spent = time.time() - started

        self.assertIsNone(result)
        self.assertLess(spent, 1.5,
                        f"被拒之后还等了 {spent:.1f} 秒（上限 {voice.CHANNEL_TIMEOUT} 秒）")

    def test_a_stale_denial_does_not_kill_the_next_create(self):
        """上一次的拒绝说明不能让下一次建频道直接放弃。"""
        self.client._denial = "上一轮被拒了"
        channel_id = self.client._resolve_channel(118000)
        self.assertIsNotNone(channel_id, "旧的拒绝说明把这一次也挡掉了")

    # ---------- 进不去就按名字重解析再试一次 ----------
    def test_a_move_into_a_dead_channel_is_retried_with_the_new_id(self):
        """频率频道是 temporary 的，解析出号到 MoveCmd 被处理之间它可能已经没了。

        服务器对指向不存在频道的 MoveCmd **既不照做也不报错**，所以不重试的话
        这一轮就彻底没进去，日志里只留一行"没有生效"——实测里出现了 40 次，
        管制员整晚留在根频道。
        """
        self.server.strict_moves = True
        first = self.client._resolve_channel(118000)
        self.assertIsNotNone(first)
        # 服务器把这个空掉的临时频道销毁了，我们手里的号就此作废
        self.server.remove_channel("FREQ_118000")

        joined = self.client._join_frequency(118000, first)

        self.assertTrue(joined, "重解析之后还是没进去")
        self.assertNotEqual(self.server.my_channel, 0, "人还留在根频道")
        self.assertIn(self.server.my_channel, self.server.live_ids())
        self.assertEqual(self.server.moves()[0], first,
                         "第一次还是应当按原来的号试")
        self.assertGreaterEqual(len(self.server.moves()), 2, "没有重试")


class ReconnectLimitTest(unittest.TestCase):
    """掉线之后最多重连三次，都失败就整个下线。

    以前是 `reconnect=True` 一路无限重试。后果不是"多试几次"：服务端
    `login.py` 对认证失败**按 CAN ID 限流**，一个在后台不停重连的僵尸足以把这个
    账号的语音锁死——管制员把密码改对了也连不上，直到重启客户端。而界面那边只
    有一句"连接已断开"，没人能说清此刻到底还在不在连。
    """

    FAILED = voice.PYMUMBLE_CONN_STATE_FAILED

    def make_class(self, results):
        """假基类：connect() 按 results 依次返回状态码，并记下调用次数。"""
        test = self

        class FakeBase:
            def __init__(self, *args, **kwargs):
                self.reconnect = kwargs.get("reconnect", False)
                self.connected = 0
                self.calls = 0
                self.stopped = 0
                test.base = self

            def connect(self):
                index = self.calls
                self.calls += 1
                value = results[index] if index < len(results) else test.FAILED
                self.connected = value
                return value

            def stop(self):
                self.stopped += 1

        return voice.bounded_mumble(FakeBase)

    def make_mumble(self, results, limit=3):
        mumble = self.make_class(results)("host", "1000", reconnect=True)
        mumble.reconnect_limit = limit
        return mumble

    # ---------- 计数本身 ----------
    def test_the_limit_stops_the_retrying(self):
        mumble = self.make_mumble([self.FAILED] * 10)
        mumble._session_established()          # 先有过一次真会话

        for expected in (1, 2, 3):
            mumble.connect()
            self.assertEqual(mumble.reconnect_attempts, expected)
            self.assertFalse(mumble.gave_up)
            self.assertTrue(mumble.reconnect, "还有次数就不该停")

        # 第四次连接不再发起，直接放弃
        self.assertEqual(mumble.connect(), self.FAILED)
        self.assertTrue(mumble.gave_up)
        self.assertFalse(mumble.reconnect,
                         "reconnect 必须置假，否则 pymumble 的 run() 还会继续转")
        self.assertEqual(self.base.calls, 3,
                         "放弃那一次不该真的再去连一遍服务器")

    def test_a_real_session_resets_the_count(self):
        mumble = self.make_mumble([self.FAILED] * 10)
        mumble._session_established()
        mumble.connect()
        mumble.connect()
        self.assertEqual(mumble.reconnect_attempts, 2)

        mumble._session_established()          # 第三次连回来了
        self.assertEqual(mumble.reconnect_attempts, 0)
        for _ in range(3):
            mumble.connect()
        self.assertFalse(mumble.gave_up, "重连成功之后没有重新给满三次")

    def test_the_first_connection_is_not_a_reconnect(self):
        """从没连上过就不进这套计数——首连失败多半是密码不对，重试没有意义。"""
        mumble = self.make_mumble([self.FAILED] * 10)
        for _ in range(5):
            mumble.connect()
        self.assertFalse(mumble.gave_up)
        self.assertEqual(mumble.reconnect_attempts, 0)
        self.assertTrue(mumble.reconnect)

    def test_authenticating_is_not_a_successful_connect(self):
        """`connect()` 成功返回的是 AUTHENTICATING（1），不是 CONNECTED（2）。

        密码错的连接同样返回 1，随后才在 loop() 里因为 Reject 结束。要是按返回值
        判定"连上了"就会每次清零计数，于是无限重连——而这一次撞的正好是服务端按
        账号的认证失败限流。计数只能由 ServerSync（CONNECTED 回调）清零。
        """
        authenticating = 1
        mumble = self.make_mumble([authenticating] * 10)
        mumble._session_established()
        for _ in range(3):
            mumble.connect()
        self.assertEqual(mumble.reconnect_attempts, 3)
        mumble.connect()
        self.assertTrue(mumble.gave_up, "被拒的重连被当成了成功")

    def test_each_attempt_is_reported(self):
        seen = []
        mumble = self.make_mumble([self.FAILED] * 10)
        mumble.on_reconnect = lambda attempt, limit: seen.append((attempt, limit))
        mumble._session_established()
        for _ in range(3):
            mumble.connect()
        self.assertEqual(seen, [(1, 3), (2, 3), (3, 3)])

    # ---------- 接到客户端上 ----------
    def test_the_monitor_takes_the_client_offline(self):
        """次数用尽后连接线程就结束了，只剩连接监控还在跑——收摊只能挂在它上面。"""
        client = VoiceClient("host", "1000", "pw")
        states = []
        client.on_state = lambda state, message: states.append((state, message))
        mumble = self.make_mumble([self.FAILED] * 10)
        mumble.gave_up = True
        client.mumble = mumble
        client.running = True

        thread = threading.Thread(target=client._connection_monitor, daemon=True)
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "监控线程没有退出")

        self.assertTrue(client.gave_up)
        self.assertEqual(states[-1][0], 'offline', states)
        self.assertIn("3", states[-1][1])
        self.assertEqual(mumble.stopped, 1,
                         "下线时必须真的 stop() 掉连接，否则麦克风和连接都还占着")
        self.assertIsNone(client.mumble)
        self.assertFalse(client.running)

    def test_an_ordinary_drop_is_reported_as_reconnecting(self):
        """一次抖动不能报成终态错误——原来它和"彻底断开"是同一句话。"""
        client = VoiceClient("host", "1000", "pw")
        states = []
        client.on_state = lambda state, message: states.append((state, message))
        client.mumble = self.make_mumble([self.FAILED] * 10)
        client.running = True

        client._on_disconnected()
        self.assertEqual(states[-1][0], 'reconnecting', states)

        # 已经放弃了就不该再报"正在重连"
        client.mumble.gave_up = True
        states.clear()
        client._on_disconnected()
        self.assertEqual([s for s, _ in states], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
