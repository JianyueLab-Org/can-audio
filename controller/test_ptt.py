"""ptt.py 的单元测试。

    python -m unittest test_ptt -v      （在 controller 目录下）

不碰真的键盘、鼠标和摇杆：pynput 和 pygame 都是从 ptt.py 里的两个惰性导入函数
拿的，测试把那两个函数换成假的，于是可以在没有输入设备、没有图形界面、也没装
pygame 的机器上跑——CI 就是这样的机器。
"""

import hashlib
import os
import threading
import time
import unittest

import ptt


# ---------- 假的 pynput ----------
class FakeKey:
    """pynput 的按键对象：可打印键有 char，功能键有 name，都没有的只有 vk。"""

    def __init__(self, char=None, name=None, vk=None):
        if char is not None:
            self.char = char
        if name is not None:
            self.name = name
        if vk is not None:
            self.vk = vk


class FakeButton:
    def __init__(self, name):
        self.name = name


class FakeListener:
    """记下回调，让测试可以自己把事件喂进去。"""

    instances = []

    def __init__(self, on_press=None, on_release=None, on_click=None):
        self.on_press = on_press
        self.on_release = on_release
        self.on_click = on_click
        self.started = False
        self.stopped = False
        self.daemon = False
        FakeListener.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class FakeModule:
    Listener = FakeListener


def fake_pynput():
    return FakeModule, FakeModule


# ---------- 假的 pygame ----------
class FakeStick:
    def __init__(self, index, name="Fake Stick", buttons=8):
        self.index = index
        self._name = name
        self._buttons = [False] * buttons
        self.quit_called = False

    def init(self):
        pass

    def quit(self):
        self.quit_called = True

    def get_name(self):
        return self._name

    def get_numbuttons(self):
        return len(self._buttons)

    def get_button(self, index):
        return self._buttons[index]

    def press(self, index):
        self._buttons[index] = True

    def release(self, index):
        self._buttons[index] = False


class FakePygame:
    def __init__(self, sticks=()):
        self.sticks = list(sticks)
        self.event = self
        self.joystick = self
        self.pumped = 0

    # pygame.event.pump()
    def pump(self):
        self.pumped += 1

    # pygame.joystick.*
    def get_init(self):
        return True

    def get_count(self):
        return len(self.sticks)

    def Joystick(self, index):        # noqa: N802 —— 照 pygame 的大写名字
        return self.sticks[index]


class Collector:
    """收 on_change 的回调，顺便记下调用次数。"""

    def __init__(self):
        self.events = []
        self.lock = threading.Lock()

    def __call__(self, value):
        with self.lock:
            self.events.append(value)

    @property
    def last(self):
        return self.events[-1] if self.events else None


class NameTest(unittest.TestCase):
    def test_a_printable_key_is_lowercased(self):
        # 按住 Shift 时 pynput 给的是 'V'，录的时候是 'v'——不转小写就按不响
        self.assertEqual(ptt.key_name(FakeKey(char="v")), "v")
        self.assertEqual(ptt.key_name(FakeKey(char="V")), "v")

    def test_a_function_key_uses_its_name(self):
        self.assertEqual(ptt.key_name(FakeKey(name="space")), "space")
        self.assertEqual(ptt.key_name(FakeKey(name="ctrl_l")), "ctrl_l")

    def test_a_key_with_neither_falls_back_to_the_virtual_code(self):
        self.assertEqual(ptt.key_name(FakeKey(vk=1001)), "vk1001")

    def test_only_the_side_buttons_are_accepted(self):
        self.assertEqual(ptt.mouse_name(FakeButton("x1")), "x1")
        self.assertEqual(ptt.mouse_name(FakeButton("x2")), "x2")
        for name in ("left", "right", "middle"):
            self.assertEqual(ptt.mouse_name(FakeButton(name)), "",
                             "左右中键绝不能当 PTT——全局监听下会在任何窗口发话")

    def test_the_x11_names_are_the_same_buttons(self):
        # X11 上 pynput 管同一个物理键叫 button8 / button9
        self.assertEqual(ptt.mouse_name(FakeButton("button8")), "x1")
        self.assertEqual(ptt.mouse_name(FakeButton("button9")), "x2")


class BindingTest(unittest.TestCase):
    def test_round_trip(self):
        for binding in (ptt.Binding(ptt.KEYBOARD, key="v"),
                        ptt.Binding(ptt.MOUSE, button="x2"),
                        ptt.Binding(ptt.JOYSTICK, button=3, device=1,
                                    device_name="Saitek")):
            self.assertEqual(ptt.Binding.from_dict(binding.to_dict()), binding)

    def test_a_broken_entry_is_dropped_not_raised(self):
        # 配置文件用户能手改，坏一条不该让整个客户端起不来
        for bad in (None, {}, {"kind": "telepathy"}, {"kind": "keyboard"},
                    {"kind": "mouse", "button": "left"},
                    {"kind": "joystick", "button": "三"},
                    {"kind": "joystick", "button": -1}):
            self.assertIsNone(ptt.Binding.from_dict(bad), bad)

    def test_the_token_carries_no_translatable_words(self):
        # 界面文案由调用方拿 i18n 拼，这里只给键名
        self.assertEqual(ptt.Binding(ptt.KEYBOARD, key="v").token(), "V")
        self.assertEqual(ptt.Binding(ptt.KEYBOARD, key="space").token(), "space")
        self.assertEqual(ptt.Binding(ptt.MOUSE, button="x1").token(), "X1")
        self.assertEqual(ptt.Binding(ptt.JOYSTICK, button=7).token(), "7")

    def test_the_device_name_does_not_affect_identity(self):
        a = ptt.Binding(ptt.JOYSTICK, button=1, device=0, device_name="A")
        b = ptt.Binding(ptt.JOYSTICK, button=1, device=0, device_name="B")
        self.assertEqual(a, b)


class LoadTest(unittest.TestCase):
    def test_the_old_two_field_settings_are_upgraded(self):
        # 升级之后老的 PTT 键还得管用，否则界面一切正常但没人听得见
        bindings = ptt.load(None, legacy_key="`", legacy_joystick=2)
        self.assertEqual(bindings, [ptt.Binding(ptt.KEYBOARD, key="`"),
                                    ptt.Binding(ptt.JOYSTICK, button=2)])

    def test_no_joystick_means_no_joystick_binding(self):
        self.assertEqual(ptt.load(None, legacy_key="v", legacy_joystick=None),
                         [ptt.Binding(ptt.KEYBOARD, key="v")])

    def test_an_explicit_empty_list_is_not_overwritten_by_the_legacy_fields(self):
        # 用户可能真的把绑定全删了，这时不能又把老字段捡回来
        self.assertEqual(ptt.load([], legacy_key="v", legacy_joystick=1), [])

    def test_duplicates_collapse(self):
        data = [{"kind": "keyboard", "key": "v"}, {"kind": "keyboard", "key": "v"}]
        self.assertEqual(len(ptt.load(data)), 1)

    def test_a_non_list_is_ignored(self):
        self.assertEqual(ptt.load("v"), [])


class WatcherTest(unittest.TestCase):
    def setUp(self):
        FakeListener.instances = []
        self._real_pynput = ptt._import_pynput
        ptt._import_pynput = fake_pynput
        self.addCleanup(setattr, ptt, "_import_pynput", self._real_pynput)
        self.collector = Collector()

    def _listener(self, index=0):
        return FakeListener.instances[index]

    def test_only_the_bound_sources_are_started(self):
        # macOS 上建全局键盘监听会弹辅助功能授权。只绑了鼠标却被要求监听键盘，
        # 看起来像流氓软件。
        watcher = ptt.PttWatcher([ptt.Binding(ptt.MOUSE, button="x1")],
                                 on_change=self.collector)
        watcher.start()
        self.addCleanup(watcher.stop)
        self.assertEqual(len(FakeListener.instances), 1)
        self.assertIsNotNone(self._listener().on_click)
        self.assertIsNone(self._listener().on_press)

    def test_a_key_press_and_release_toggles_once_each(self):
        watcher = ptt.PttWatcher([ptt.keyboard_binding("v")], on_change=self.collector)
        watcher.start()
        self.addCleanup(watcher.stop)
        listener = self._listener()
        listener.on_press(FakeKey(char="v"))
        listener.on_release(FakeKey(char="v"))
        self.assertEqual(self.collector.events, [True, False])

    def test_auto_repeat_does_not_fire_again(self):
        # 按住不放时 pynput 会一直送 press，回调只该来一次
        watcher = ptt.PttWatcher([ptt.keyboard_binding("v")], on_change=self.collector)
        watcher.start()
        self.addCleanup(watcher.stop)
        listener = self._listener()
        for _ in range(5):
            listener.on_press(FakeKey(char="v"))
        self.assertEqual(self.collector.events, [True])

    def test_an_unbound_key_does_nothing(self):
        watcher = ptt.PttWatcher([ptt.keyboard_binding("v")], on_change=self.collector)
        watcher.start()
        self.addCleanup(watcher.stop)
        self._listener().on_press(FakeKey(char="b"))
        self.assertEqual(self.collector.events, [])

    def test_two_bindings_are_ored_and_the_release_waits_for_both(self):
        # 键盘和鼠标各按一个，松开其中一个还该在发话——不然双手操作时话会被切断
        watcher = ptt.PttWatcher([ptt.keyboard_binding("v"),
                                  ptt.Binding(ptt.MOUSE, button="x2")],
                                 on_change=self.collector)
        watcher.start()
        self.addCleanup(watcher.stop)
        kb = [l for l in FakeListener.instances if l.on_press][0]
        mouse = [l for l in FakeListener.instances if l.on_click][0]
        kb.on_press(FakeKey(char="v"))
        mouse.on_click(0, 0, FakeButton("x2"), True)
        kb.on_release(FakeKey(char="v"))
        self.assertEqual(self.collector.events, [True])
        mouse.on_click(0, 0, FakeButton("x2"), False)
        self.assertEqual(self.collector.events, [True, False])

    def test_a_normal_click_is_never_ptt(self):
        watcher = ptt.PttWatcher([ptt.Binding(ptt.MOUSE, button="x1")],
                                 on_change=self.collector)
        watcher.start()
        self.addCleanup(watcher.stop)
        self._listener().on_click(0, 0, FakeButton("left"), True)
        self.assertEqual(self.collector.events, [])

    def test_dropping_a_held_binding_releases_ptt(self):
        # 设置里把正按着的那条删掉，麦克风必须落下——否则它一直开着，而界面上
        # 已经没有任何东西显示是谁按住的
        watcher = ptt.PttWatcher([ptt.keyboard_binding("v")], on_change=self.collector)
        watcher.start()
        self.addCleanup(watcher.stop)
        self._listener().on_press(FakeKey(char="v"))
        self.assertEqual(self.collector.last, True)
        watcher.set_bindings([ptt.keyboard_binding("b")])
        self.assertEqual(self.collector.last, False)

    def test_stopping_while_held_releases_ptt(self):
        watcher = ptt.PttWatcher([ptt.keyboard_binding("v")], on_change=self.collector)
        watcher.start()
        self._listener().on_press(FakeKey(char="v"))
        watcher.stop()
        self.assertEqual(self.collector.last, False)
        self.assertFalse(watcher.is_down())

    def test_a_raising_callback_does_not_kill_the_listener(self):
        def boom(value):
            raise RuntimeError("界面炸了")

        watcher = ptt.PttWatcher([ptt.keyboard_binding("v")], on_change=boom)
        watcher.start()
        self.addCleanup(watcher.stop)
        self._listener().on_press(FakeKey(char="v"))     # 不该往外抛
        self.assertTrue(watcher.is_down())

    def test_the_callback_runs_outside_the_lock(self):
        # 回调里回头调 set_bindings 是设置对话框的正常操作；在锁里回调就是死锁
        watcher = ptt.PttWatcher([ptt.keyboard_binding("v")])
        watcher.on_change = lambda value: watcher.set_bindings(watcher.bindings())
        watcher.start()
        self.addCleanup(watcher.stop)
        done = threading.Event()

        def press():
            self._listener().on_press(FakeKey(char="v"))
            done.set()

        threading.Thread(target=press, daemon=True).start()
        self.assertTrue(done.wait(2), "on_change 是在锁里调的，卡死了")


class JoystickWatcherTest(unittest.TestCase):
    def setUp(self):
        self.stick = FakeStick(0, buttons=4)
        self.pygame = FakePygame([self.stick])
        real = ptt._import_pygame
        ptt._import_pygame = lambda: self.pygame
        self.addCleanup(setattr, ptt, "_import_pygame", real)
        # 轮询测试不该真的等 3 秒才开设备
        self._retry = ptt.JOYSTICK_RETRY
        ptt.JOYSTICK_RETRY = 0.0
        self.addCleanup(setattr, ptt, "JOYSTICK_RETRY", self._retry)
        self.collector = Collector()

    def _wait_for(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_a_joystick_button_drives_ptt(self):
        watcher = ptt.PttWatcher([ptt.Binding(ptt.JOYSTICK, button=2)],
                                 on_change=self.collector)
        watcher.start()
        self.addCleanup(watcher.stop)
        self.stick.press(2)
        self.assertTrue(self._wait_for(lambda: self.collector.last is True),
                        "摇杆按下了但 PTT 没起来")
        self.stick.release(2)
        self.assertTrue(self._wait_for(lambda: self.collector.last is False))

    def test_a_button_beyond_the_device_is_not_pressed(self):
        # 换了个按钮更少的手柄。要的是"不响"，而不是 IndexError 把轮询线程打死
        watcher = ptt.PttWatcher([ptt.Binding(ptt.JOYSTICK, button=9)],
                                 on_change=self.collector)
        watcher.start()
        self.addCleanup(watcher.stop)
        self.assertFalse(self._wait_for(lambda: self.collector.events, timeout=0.3))
        self.assertTrue(self.pygame.pumped > 0, "轮询线程死了")

    def test_the_device_is_released_on_stop(self):
        watcher = ptt.PttWatcher([ptt.Binding(ptt.JOYSTICK, button=0)],
                                 on_change=self.collector)
        watcher.start()
        self.assertTrue(self._wait_for(lambda: self.pygame.pumped > 0))
        watcher.stop()
        self.assertTrue(self._wait_for(lambda: self.stick.quit_called),
                        "停了之后没把摇杆放开")

    def test_no_pygame_means_the_other_sources_still_work(self):
        # pygame 在有些平台上装不上。没有摇杆不该连键盘 PTT 一起没有
        ptt._import_pygame = lambda: None
        FakeListener.instances = []
        real = ptt._import_pynput
        ptt._import_pynput = fake_pynput
        self.addCleanup(setattr, ptt, "_import_pynput", real)
        watcher = ptt.PttWatcher([ptt.Binding(ptt.JOYSTICK, button=0),
                                  ptt.keyboard_binding("v")],
                                 on_change=self.collector)
        watcher.start()
        self.addCleanup(watcher.stop)
        kb = [l for l in FakeListener.instances if l.on_press][0]
        kb.on_press(FakeKey(char="v"))
        self.assertEqual(self.collector.last, True)


class SdlThreadTest(unittest.TestCase):
    """SDL 必须由界面线程第一个初始化，不能由摇杆的后台线程。

    这条是从一份真实日志里来的：01:28:11 录到了 `<Binding joystick 1>`，两秒后
    再录就 `IDirectInputDevice8::SetCooperativeLevel() DirectX error 0x80070006`，
    之后监听线程每 3 秒一条打不开，一直到用户退出客户端。

    原因不在摇杆上，也不在 SDL_VIDEODRIVER 上。SDL 在 SDL_Init(SDL_INIT_JOYSTICK)
    里给 DirectInput 建一个隐藏的辅助窗口，开每个摇杆时拿它调
    SetCooperativeLevel；Win32 的窗口属于建它的线程，线程一退窗口就被销毁，而
    SDL 只建这一次（SDL_HelperWindow 非空就直接返回）。所以第一次初始化落在录制
    线程上的话，那个线程一结束，句柄就野了，E_HANDLE 是"无效窗口句柄"。

    修法只有一个：让第一次初始化落在和进程同寿的线程上。所以这里测的是"谁调用
    了 _import_pygame"，而不是别的什么现象。
    """

    def setUp(self):
        self.stick = FakeStick(0, buttons=4)
        self.pygame = FakePygame([self.stick])
        self.threads = []
        real = ptt._import_pygame
        self.addCleanup(setattr, ptt, "_import_pygame", real)
        ptt._import_pygame = self._record
        realp = ptt._import_pynput
        ptt._import_pynput = lambda: (None, None)
        self.addCleanup(setattr, ptt, "_import_pynput", realp)

    def _record(self):
        self.threads.append(threading.current_thread())
        return self.pygame

    def test_the_watcher_brings_sdl_up_on_the_calling_thread(self):
        watcher = ptt.PttWatcher([ptt.Binding(ptt.JOYSTICK, button=0)])
        watcher.start()
        self.addCleanup(watcher.stop)
        self.assertTrue(self.threads, "start() 之后 SDL 还没起来")
        self.assertIs(self.threads[0], threading.current_thread(),
                      "SDL 是在后台线程上起来的：那个线程一退，DirectInput 的"
                      "辅助窗口就没了，之后所有摇杆都打不开")

    def test_the_capture_brings_sdl_up_on_the_calling_thread(self):
        capture = ptt.PttCapture(lambda binding: None)
        capture.start()
        self.addCleanup(capture.stop)
        self.assertTrue(self.threads, "start() 之后 SDL 还没起来")
        self.assertIs(self.threads[0], threading.current_thread(),
                      "录制线程第一个碰了 SDL：这一次能录上，下一次就再也打不开了")

    def test_a_device_that_will_not_open_is_only_reported_once(self):
        # 打不开一般是个稳定状态，而重试永不停止。真实日志里同一行刷了整整一屏，
        # 1 MB 的滚动窗口就是这么被顶满的。
        class Refusing(FakePygame):
            def Joystick(self, index):    # noqa: N802
                raise OSError("IDirectInputDevice8::SetCooperativeLevel() "
                              "DirectX error 0x80070006")

        self.pygame = Refusing([self.stick])
        retry = ptt.JOYSTICK_RETRY
        ptt.JOYSTICK_RETRY = 0.0
        self.addCleanup(setattr, ptt, "JOYSTICK_RETRY", retry)
        watcher = ptt.PttWatcher([ptt.Binding(ptt.JOYSTICK, button=0)])
        with self.assertLogs("ptt", level="DEBUG") as caught:
            watcher.start()
            time.sleep(0.2)
            watcher.stop()
        warnings = [r for r in caught.records
                    if r.levelname == "WARNING" and "0x80070006" in r.getMessage()]
        self.assertEqual(len(warnings), 1,
                         "同一个打不开的设备报了 %d 次" % len(warnings))


class CaptureTest(unittest.TestCase):
    def setUp(self):
        FakeListener.instances = []
        real = ptt._import_pynput
        ptt._import_pynput = fake_pynput
        self.addCleanup(setattr, ptt, "_import_pynput", real)
        self.captured = []

    def test_the_first_key_wins_and_the_listeners_stop(self):
        capture = ptt.PttCapture(self.captured.append, joystick=False)
        capture.start()
        listener = [l for l in FakeListener.instances if l.on_press][0]
        listener.on_press(FakeKey(name="f13"))
        listener.on_press(FakeKey(char="q"))     # 第二下不该覆盖第一下
        self.assertEqual(self.captured, [ptt.Binding(ptt.KEYBOARD, key="f13")])
        self.assertTrue(listener.stopped)

    def test_a_side_button_can_be_captured(self):
        capture = ptt.PttCapture(self.captured.append, joystick=False)
        capture.start()
        mouse = [l for l in FakeListener.instances if l.on_click][0]
        mouse.on_click(0, 0, FakeButton("x2"), True)
        self.assertEqual(self.captured, [ptt.Binding(ptt.MOUSE, button="x2")])

    def test_a_left_click_is_not_captured(self):
        # 否则用户点"重设"那一下就把左键录成 PTT 了
        capture = ptt.PttCapture(self.captured.append, joystick=False)
        self.addCleanup(capture.stop)
        capture.start()
        mouse = [l for l in FakeListener.instances if l.on_click][0]
        mouse.on_click(0, 0, FakeButton("left"), True)
        self.assertEqual(self.captured, [])


class JoystickCaptureTest(unittest.TestCase):
    def setUp(self):
        self.stick = FakeStick(0, name="Fake Yoke", buttons=4)
        self.pygame = FakePygame([self.stick])
        real = ptt._import_pygame
        ptt._import_pygame = lambda: self.pygame
        self.addCleanup(setattr, ptt, "_import_pygame", real)
        realp = ptt._import_pynput
        ptt._import_pynput = lambda: (None, None)
        self.addCleanup(setattr, ptt, "_import_pynput", realp)
        self.captured = []

    def _wait(self, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self.captured:
            time.sleep(0.01)
        return bool(self.captured)

    def test_a_button_press_is_captured_with_its_device(self):
        capture = ptt.PttCapture(self.captured.append)
        capture.start()
        self.addCleanup(capture.stop)
        time.sleep(0.05)
        self.stick.press(3)
        self.assertTrue(self._wait(), "摇杆按钮没被录到")
        self.assertEqual(self.captured[0], ptt.Binding(ptt.JOYSTICK, button=3, device=0))
        self.assertEqual(self.captured[0].device_name, "Fake Yoke")

    def test_a_button_already_held_when_capture_starts_is_ignored(self):
        # 油门上常年压着的那个开关不该被录成 PTT
        self.stick.press(1)
        capture = ptt.PttCapture(self.captured.append)
        capture.start()
        self.addCleanup(capture.stop)
        self.assertFalse(self._wait(timeout=0.3))
        self.stick.release(1)
        # 轮询要真的看见"松开了"这一帧，才会把它从初始状态里划掉。摇杆是
        # POLL_INTERVAL 的粒度，快到一帧之内的松开再按确实会被漏掉——录绑定时
        # 再按一下就是了，比把常年压着的开关录成 PTT 好。
        time.sleep(ptt.POLL_INTERVAL * 3)
        self.stick.press(1)
        self.assertTrue(self._wait(), "松开再按应该算数")


class SharedCopyTest(unittest.TestCase):
    """各客户端里的 ptt.py 必须逐字节相同。

    这个仓库靠复制共享代码，不靠 import。现在只有 controller 带 ptt.py，
    以后再有客户端加副本时把它加进 COMPONENTS。
    """

    COMPONENTS = ("controller",)

    def test_every_copy_is_byte_identical(self):
        here = os.path.dirname(os.path.abspath(__file__))
        root = os.path.dirname(here)
        digests = {}
        for name in self.COMPONENTS:
            path = os.path.join(root, name, "ptt.py")
            if not os.path.exists(path):
                continue
            with open(path, "rb") as f:
                digests[name] = hashlib.md5(f.read()).hexdigest()
        if len(digests) < 2:
            self.skipTest("边上没有别的组件目录")
        self.assertEqual(len(set(digests.values())), 1,
                         "ptt.py 的副本不一致了: %s" % digests)


if __name__ == "__main__":
    unittest.main()
