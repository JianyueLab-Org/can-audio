"""电台栈的测试。

    python -m unittest test_radiostack -v      （在 controller 目录下运行）

RX/TX/XC 的联动规则对着 TrackAudio 的 radio.tsx 写，这里逐条钉住，免得以后改
着改着就跑偏了。
"""

import unittest

import radiostack
from radiostack import RadioStack


class FrequencyTest(unittest.TestCase):

    def test_parse_and_format(self):
        self.assertEqual(radiostack.parse_frequency("118.000"), 118000)
        self.assertEqual(radiostack.parse_frequency("128.5"), 128500)
        self.assertEqual(radiostack.parse_frequency(" 121.700 "), 121700)
        self.assertEqual(radiostack.format_frequency(118000), "118.000")

    def test_channel_name_matches_the_network_convention(self):
        # 全网约定：FREQ_ + 6 位千赫，飞行员端和服务端都按这个找频道
        self.assertEqual(radiostack.channel_name(118000), "FREQ_118000")
        self.assertEqual(radiostack.channel_name(99998), "FREQ_099998")

    def test_rejects_bad_input(self):
        for bad in ("", "abc", "88.000", "250.000"):
            with self.assertRaises(ValueError, msg=bad):
                radiostack.parse_frequency(bad)


class StackBasicsTest(unittest.TestCase):

    def setUp(self):
        self.changes = 0
        self.stack = RadioStack(on_change=self.count)

    def count(self):
        self.changes += 1

    def test_add_sorts_and_selects_first(self):
        self.stack.add("128.500", "ZSPD_APP")
        self.stack.add("118.000", "ZSPD_TWR")
        self.assertEqual([r.frequency for r in self.stack], ["118.000", "128.500"])
        self.assertEqual(self.stack.selected_khz, 128500, "第一个加进来的就是主频率")

    def test_duplicate_is_rejected(self):
        self.stack.add("118.000")
        with self.assertRaises(ValueError):
            self.stack.add("118.000")

    def test_remove_moves_the_selection(self):
        self.stack.add("118.000")
        self.stack.add("121.700")
        self.stack.select(118000)
        self.stack.remove(118000)
        self.assertEqual(self.stack.selected_khz, 121700)

    def test_callsign_is_normalised(self):
        radio = self.stack.add("118.000", " zspd_twr ")
        self.assertEqual(radio.callsign, "ZSPD_TWR")
        self.assertEqual(radio.label, "ZSPD_TWR 118.000")

    def test_change_callback_fires(self):
        self.stack.add("118.000")
        before = self.changes
        self.stack.set_rx(118000, True)
        self.assertGreater(self.changes, before)


class CouplingRulesTest(unittest.TestCase):
    """RX/TX/XC 的联动，规则来自 TrackAudio。"""

    def setUp(self):
        self.stack = RadioStack()
        self.stack.add("118.000")

    def radio(self):
        return self.stack.get(118000)

    def test_tx_forces_rx_on(self):
        self.stack.set_tx(118000, True)
        self.assertTrue(self.radio().rx, "不存在只发不收")
        self.assertTrue(self.radio().tx)

    def test_turning_rx_off_clears_tx_and_xc(self):
        self.stack.set_xc(118000, True)          # 顺带把 rx/tx 都打开
        self.stack.set_rx(118000, False)
        self.assertFalse(self.radio().rx)
        self.assertFalse(self.radio().tx)
        self.assertFalse(self.radio().xc)

    def test_xc_forces_rx_and_tx_on(self):
        self.stack.set_xc(118000, True)
        self.assertTrue(self.radio().rx)
        self.assertTrue(self.radio().tx)
        self.assertTrue(self.radio().xc)

    def test_turning_tx_off_clears_xc_but_keeps_rx(self):
        self.stack.set_xc(118000, True)
        self.stack.set_tx(118000, False)
        self.assertTrue(self.radio().rx, "只是不发了，还得继续收")
        self.assertFalse(self.radio().tx)
        self.assertFalse(self.radio().xc)

    def test_toggles(self):
        self.stack.toggle_rx(118000)
        self.assertTrue(self.radio().rx)
        self.stack.toggle_rx(118000)
        self.assertFalse(self.radio().rx)


class FrequencySetsTest(unittest.TestCase):

    def setUp(self):
        self.stack = RadioStack()
        for freq in ("118.000", "121.700", "128.500"):
            self.stack.add(freq)

    def test_rx_and_tx_sets(self):
        self.stack.set_rx(118000, True)
        self.stack.set_tx(121700, True)          # 会连带打开 rx
        self.assertEqual(sorted(self.stack.rx_frequencies()), [118000, 121700])
        self.assertEqual(self.stack.tx_frequencies(), [121700])

    def test_xc_set(self):
        self.stack.set_xc(118000, True)
        self.stack.set_xc(128500, True)
        self.assertEqual(sorted(self.stack.xc_frequencies()), [118000, 128500])


class OnDutyTest(unittest.TestCase):
    """没在数据源上的管制席位时，只许收不许发；正在管的那个频率不许删。

    语音服务器没有花名册检查——任何账号都能进任何 FREQ_* 频道，也都能对着它
    说话。这道闸是客户端自觉守的那一层，对应 can-fsd 上"不在花名册里就不能上
    席位"。
    """

    def setUp(self):
        self.stack = RadioStack()
        for freq in ("118.000", "121.700"):
            self.stack.add(freq)

    def test_tx_cannot_be_turned_on_when_not_on_duty(self):
        self.stack.set_transmit_allowed(False)
        self.stack.set_tx(118000, True)
        self.assertFalse(self.stack.get(118000).tx)
        self.assertEqual(self.stack.tx_frequencies(), [])

    def test_xc_cannot_be_turned_on_when_not_on_duty(self):
        self.stack.set_transmit_allowed(False)
        self.stack.set_xc(118000, True)
        self.assertFalse(self.stack.get(118000).xc)

    def test_rx_still_works_when_not_on_duty(self):
        """只是不许发，收是照常的——听不到反而更危险。"""
        self.stack.set_transmit_allowed(False)
        self.stack.set_rx(118000, True)
        self.assertTrue(self.stack.get(118000).rx)
        self.assertEqual(self.stack.rx_frequencies(), [118000])

    def test_going_off_duty_drops_tx_that_is_already_on(self):
        """光把按钮画灰拦不住：已经开着的 TX 会被 sync 编进 VoiceTarget。"""
        self.stack.set_tx(118000, True)
        self.stack.set_xc(121700, True)
        self.assertEqual(self.stack.tx_frequencies(), [118000, 121700])

        self.stack.set_transmit_allowed(False)
        self.assertEqual(self.stack.tx_frequencies(), [])
        self.assertEqual(self.stack.xc_frequencies(), [])
        self.assertEqual(sorted(self.stack.rx_frequencies()), [118000, 121700],
                         "收不该跟着一起关掉")

    def test_coming_on_duty_allows_tx_again(self):
        self.stack.set_transmit_allowed(False)
        self.stack.set_transmit_allowed(True)
        self.stack.set_tx(118000, True)
        self.assertTrue(self.stack.get(118000).tx)

    def test_non_primary_frequency_still_supports_tx_and_xc(self):
        self.stack.set_locked({118000})
        self.stack.set_tx(121700, True)
        self.stack.set_xc(121700, True)
        self.assertEqual(self.stack.tx_frequencies(), [121700])
        self.assertEqual(self.stack.xc_frequencies(), [121700])

    def test_multiple_duty_frequencies_can_all_be_primary(self):
        self.stack.set_locked({118000, 121700})
        self.stack.select(121700)
        self.assertEqual(self.stack.selected_khz, 121700)
        self.stack.select(128500)
        self.assertEqual(self.stack.selected_khz, 121700)

    def test_the_locked_frequency_cannot_be_removed(self):
        self.stack.set_locked(118000)
        self.assertFalse(self.stack.remove(118000))
        self.assertIsNotNone(self.stack.get(118000))
        # 别的频率照删不误
        self.assertTrue(self.stack.remove(121700))

    def test_unlocking_makes_it_removable_again(self):
        self.stack.set_locked(118000)
        self.stack.set_locked(None)
        self.assertTrue(self.stack.remove(118000))

    def test_nothing_is_locked_by_default(self):
        self.assertFalse(self.stack.is_locked(118000))
        self.assertTrue(self.stack.transmit_allowed,
                        "默认放行——没连数据源时不该把人锁死")

    def test_multiple_locked_frequencies_are_protected(self):
        self.stack.set_locked({118000, 121700})
        self.assertTrue(self.stack.is_locked(118000))
        self.assertTrue(self.stack.is_locked(121700))
        self.assertFalse(self.stack.is_locked(128500))
        self.assertFalse(self.stack.remove(118000))
        self.assertFalse(self.stack.remove(121700))

    def test_selection_can_only_use_a_locked_frequency_when_locked(self):
        self.stack.set_locked({118000, 121700})
        self.stack.select(128500)
        self.assertEqual(self.stack.selected_khz, 118000)
        self.stack.select(121700)
        self.assertEqual(self.stack.selected_khz, 121700)


class RuntimeStateTest(unittest.TestCase):

    def setUp(self):
        self.stack = RadioStack()
        self.stack.add("118.000")
        self.stack.add("121.700")
        self.stack.set_tx(118000, True)

    def test_currently_rx_records_the_caller(self):
        self.stack.set_currently_rx(118000, True, "CES2345", 1750000000.0)
        radio = self.stack.get(118000)
        self.assertTrue(radio.currently_rx)
        self.assertEqual(radio.last_received_callsign, "CES2345")

        self.stack.set_currently_rx(118000, False)
        self.assertFalse(radio.currently_rx)
        self.assertEqual(radio.last_received_callsign, "CES2345", "结束后仍要留着记录")

    def test_currently_tx_only_lights_tx_radios(self):
        self.stack.set_currently_tx(True)
        self.assertTrue(self.stack.get(118000).currently_tx)
        self.assertFalse(self.stack.get(121700).currently_tx,
                         "没开 TX 的频率不该显示正在发话")

    def test_volume_and_mute(self):
        self.stack.set_volume(118000, 250)
        self.assertEqual(self.stack.get(118000).volume, 100, "音量要夹在 0-100")
        self.stack.set_muted(118000, True)
        self.assertEqual(self.stack.get(118000).effective_volume(), 0)


class PersistenceTest(unittest.TestCase):
    """序列化本身还是对的，但**管制端已经不再用它存频率了**。

    电台栈不跨会话保留：频率该从数据源来——上了席位的自动加，别人的席位在
    "在线频率"里点。留着上一场的频率反而危险，那些临时频道多半早就没人了，
    而屏幕上看起来一切正常。

    `to_list` / `load` 留着是因为它们是干净的纯函数，将来导出/导入构型还用得上；
    但眼下没有任何调用方，别看到这个类就以为设置里还存着 radios。
    """

    def test_round_trip(self):
        stack = RadioStack()
        stack.add("118.000", "ZSPD_TWR")
        stack.set_xc(118000, True)
        stack.set_volume(118000, 60)
        stack.add("121.700", "ZSPD_GND")

        restored = RadioStack()
        restored.load(stack.to_list())

        self.assertEqual(len(restored), 2)
        radio = restored.get(118000)
        self.assertEqual(radio.callsign, "ZSPD_TWR")
        self.assertTrue(radio.rx and radio.tx and radio.xc)
        self.assertEqual(radio.volume, 60)

    def test_selection_prefers_an_active_radio(self):
        restored = RadioStack()
        restored.load([
            {"frequency": 118000, "rx": False},
            {"frequency": 121700, "rx": True},
        ])
        self.assertEqual(restored.selected_khz, 121700)

    def test_bad_entries_are_skipped(self):
        restored = RadioStack()
        restored.load([{"frequency": 118000}, {"nope": 1}, None])
        self.assertEqual(len(restored), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
