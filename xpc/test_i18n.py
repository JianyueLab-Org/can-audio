"""多语言的测试。

    python -m unittest test_i18n -v      （在 xpc 目录下运行）

最要紧的一条是**每个键两种语言都得有**：只翻一半的话，用户看到的是中英混排，
而这种缺失单看代码是发现不了的——界面上那句话照样显示，只是显示的是另一种
语言。
"""

import os
import re
import unittest

import i18n


class CoverageTest(unittest.TestCase):
    """翻译表本身的完整性。"""

    def test_every_key_has_every_language(self):
        missing = []
        for key, entry in i18n.TEXT.items():
            for code in i18n.LANGUAGES:
                if not entry.get(code):
                    missing.append(f"{key}[{code}]")
        self.assertEqual(missing, [], f"这些条目缺翻译: {missing}")

    def test_no_entry_carries_an_unknown_language(self):
        """打错语言代码（比如写成 'cn'）会静默失效，界面退回默认语言。"""
        unknown = []
        for key, entry in i18n.TEXT.items():
            for code in entry:
                if code not in i18n.LANGUAGES:
                    unknown.append(f"{key}[{code}]")
        self.assertEqual(unknown, [], f"不认识的语言代码: {unknown}")

    def test_placeholders_match_across_languages(self):
        """两种语言的占位符必须一模一样。

        少一个的话 format 出来会丢信息，多一个直接抛 KeyError——而两者都只在
        运行到那一句时才炸，多半是用户先撞上。
        """
        pattern = re.compile(r"\{(\w+)\}")
        bad = []
        for key, entry in i18n.TEXT.items():
            sets = {code: set(pattern.findall(text))
                    for code, text in entry.items()}
            reference = sets.get(i18n.DEFAULT, set())
            for code, names in sets.items():
                if names != reference:
                    bad.append(f"{key}: {code}={sorted(names)} vs "
                               f"{i18n.DEFAULT}={sorted(reference)}")
        self.assertEqual(bad, [], f"占位符对不上: {bad}")

    def test_translations_are_not_just_copies_of_chinese(self):
        """英文条目里不该还留着中文——那是漏翻却看着像翻过了。"""
        chinese = re.compile(r"[一-鿿]")
        untranslated = [key for key, entry in i18n.TEXT.items()
                        if chinese.search(entry.get("en", ""))]
        self.assertEqual(untranslated, [], f"这些英文条目里还有中文: {untranslated}")


class NoHardcodedUiStringTest(unittest.TestCase):
    """源码里不该再有写死的界面文字。

    这条是给"漏翻"兜底的：漏掉一句的话，中文用户完全看不出问题，英文用户会
    在满屏英文里撞见一句中文，而这种事只有真的切到英文跑一遍才会发现。用扫描
    源码代替人眼。

    只看**会显示给用户**的地方（setText / 占位符 / 提示 / 抛给界面的异常 /
    _state 消息）。日志和注释里的中文不管——日志是给排查问题的人看的，翻了
    反而不利于对照。
    """

    # 会把文字送到界面上的调用。Fluent 那套控件（PushButton / CheckBox / …）也要在
    # 里面——换控件之前这张表只认 Q 开头的，换完之后同样一句写死的中文就扫不出来了。
    UI_CALLS = re.compile(
        r"(setText|setPlaceholderText|setToolTip|setWindowTitle|setTitle|addItem|"
        r"addTab|addRow|setSpecialValueText|"
        r"_state|_status|raise ValueError|"
        r"QCheckBox|QLabel|QPushButton|QGroupBox|"
        r"BodyLabel|CaptionLabel|StrongBodyLabel|SubtitleLabel|"
        r"PushButton|CheckBox|SwitchButton)\s*\(")
    CHINESE = re.compile(r"[一-鿿]")

    def offenders(self, filename):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, filename), encoding="utf-8") as f:
            lines = f.readlines()
        bad = []
        for number, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # 文档字符串也放过。有几处的说明里正好写着 `_status(state, message)`
            # 这种签名，会被下面的 UI_CALLS 认成一次界面调用，再把整行的中文说明
            # 报成漏翻——那是注释，不是界面文字。
            quoted = stripped[1:] if stripped[:1] == "r" else stripped
            if quoted[:3] in ('"' * 3, "'" * 3):
                continue
            if not self.UI_CALLS.search(line):
                continue
            # 取出这一行里的字符串字面量，含中文的就是漏翻的
            for match in re.finditer(r"""(['"])((?:(?!\1).)*)\1""", line):
                if self.CHINESE.search(match.group(2)):
                    bad.append(f"{filename}:{number}: {stripped[:70]}")
                    break
        return bad

    def test_gui_has_no_hardcoded_chinese(self):
        """主窗口、设置对话框和飞行计划对话框都在 gui.py 里。"""
        self.assertEqual(self.offenders("gui.py"), [])

    def test_voice_status_messages_are_translated(self):
        """voice.py 的 _status 消息会直接进消息区。"""
        self.assertEqual(self.offenders("voice.py"), [])

    def test_fsd_status_messages_are_translated(self):
        """fsdpilot.py 同理，还包括本地先查一遍的呼号错误。"""
        self.assertEqual(self.offenders("fsdpilot.py"), [])

    def test_sim_link_status_messages_are_translated(self):
        """xplane.py 的 _state 消息经 on_sim_state 直接进消息区。"""
        self.assertEqual(self.offenders("xplane.py"), [])

    def test_calibration_dialog_has_no_hardcoded_chinese(self):
        self.assertEqual(self.offenders("calibration.py"), [])


class LookupTest(unittest.TestCase):

    def setUp(self):
        self._saved = i18n.current()

    def tearDown(self):
        i18n.set_language(self._saved)

    def test_switching_changes_what_comes_out(self):
        i18n.set_language("zh")
        chinese = i18n.t("settings.title")
        i18n.set_language("en")
        english = i18n.t("settings.title")
        self.assertNotEqual(chinese, english)
        self.assertEqual(chinese, "设置")
        self.assertEqual(english, "Settings")

    def test_placeholders_are_filled(self):
        i18n.set_language("zh")
        text = i18n.t("fsd.connecting", callsign="CCA1501", server="fsd.example")
        self.assertIn("CCA1501", text)
        self.assertIn("fsd.example", text)
        self.assertNotIn("{", text)

    def test_a_placeholder_named_key_does_not_collide(self):
        """`t()` 的第一个形参也叫 key。位置限定参数（`/`）挡住了这次撞车——
        没有它的话，任何带 {key} 的文案都会报 "multiple values for argument"。"""
        text = i18n.t("ptt.keyboard", key="V")
        self.assertIn("V", text)

    def test_an_unknown_key_returns_the_key(self):
        """返回键本身，界面上很扎眼——正好当成"这里漏翻了"的信号。"""
        self.assertEqual(i18n.t("没有这个键"), "没有这个键")

    def test_a_missing_placeholder_does_not_crash(self):
        # 调用方漏传参数时，宁可显示带 {} 的原文，也不能让界面炸掉
        text = i18n.t("fsd.connecting", callsign="CCA1501")
        self.assertIsInstance(text, str)

    def test_an_unknown_language_falls_back(self):
        i18n.set_language("kl")            # 克林贡语
        self.assertEqual(i18n.current(), i18n.DEFAULT)
        self.assertEqual(i18n.t("settings.title"), "设置")

    def test_empty_language_falls_back(self):
        i18n.set_language("")
        self.assertEqual(i18n.current(), i18n.DEFAULT)


class SystemLanguageTest(unittest.TestCase):

    def setUp(self):
        self._env = {k: os.environ.get(k)
                     for k in ("CAN_LANG", "LANGUAGE", "LC_ALL", "LANG")}

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_environment_wins(self):
        os.environ["CAN_LANG"] = "en"
        self.assertEqual(i18n.system_language(), "en")

    def test_locale_style_values_are_understood(self):
        os.environ["CAN_LANG"] = "zh_CN.UTF-8"
        self.assertEqual(i18n.system_language(), "zh")

    def test_an_unsupported_locale_is_never_returned_verbatim(self):
        """环境变量说法语（不支持）时，绝不能把 "fr" 原样返回。

        往下退到系统语言是对的——环境说法语、系统是英语时，英语比中文更接近
        用户想要的，所以这里不断言一定是 DEFAULT，只断言结果必须是支持的语言。
        """
        for key in ("CAN_LANG", "LANGUAGE", "LC_ALL", "LANG"):
            os.environ[key] = "fr_FR.UTF-8"
        result = i18n.system_language()
        self.assertNotEqual(result, "fr")
        self.assertIn(result, i18n.LANGUAGES)

    def test_it_always_returns_something_supported(self):
        self.assertIn(i18n.system_language(), i18n.LANGUAGES)


class BindingLabelTest(unittest.TestCase):
    """一条 PTT 绑定在界面上的说法。

    ptt.py 在三个客户端之间逐字节共享，所以它只给键名（"V" / "X1" / "3"），
    说法在 i18n 这边拼。让它自己产出中文的话，同一套文案会躺在三份副本里，
    翻译时必然漏掉其中两份。
    """

    def setUp(self):
        self._saved = i18n.current()
        i18n.set_language("zh")

    def tearDown(self):
        i18n.set_language(self._saved)

    def test_each_kind_reads_differently(self):
        import ptt
        labels = {
            "keyboard": i18n.binding_label(ptt.keyboard_binding("v")),
            "mouse": i18n.binding_label(ptt.Binding(ptt.MOUSE, button="x1")),
            "joystick": i18n.binding_label(ptt.Binding(ptt.JOYSTICK, button=3)),
        }
        self.assertEqual(len(set(labels.values())), 3, labels)
        for text in labels.values():
            self.assertNotIn("{", text)

    def test_a_named_device_is_shown(self):
        import ptt
        binding = ptt.Binding(ptt.JOYSTICK, button=3, device_name="Saitek X52")
        self.assertIn("Saitek X52", i18n.binding_label(binding))

    def test_ptt_module_carries_no_ui_text(self):
        """ptt.py 里的字符串字面量不该有中文（注释和文档字符串不算）。"""
        import ast
        import os
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "ptt.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        chinese = re.compile(r"[一-鿿]")
        # 文档字符串是 Expr 包着的 Constant，而 ast.walk 会连它里面的 Constant
        # 一起走到，所以得先把这些节点记下来再跳过——只在 Expr 那一层 continue
        # 是拦不住的。
        docstrings = {id(node.value) for node in ast.walk(tree)
                      if isinstance(node, ast.Expr)
                      and isinstance(node.value, ast.Constant)}
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in docstrings:
                    continue
                if chinese.search(node.value):
                    offenders.append(node.value[:40])
        self.assertEqual(offenders, [],
                         f"ptt.py 里有中文字面量，应该交给 i18n: {offenders}")


class EveryUsedKeyExistsTest(unittest.TestCase):
    """源码里 t("…") 引用的键，翻译表里必须真有。

    这条是 CoverageTest 补不上的那一半：那边查的是"表里已有的条目翻全了没有"，
    查不出"代码要用的条目根本不在表里"。少一条的结果是界面上明晃晃地显示
    `plugin.install` 这种生键名——两种语言下都一样难看，而 t() 只在日志里留一行
    warning，跑测试的人不会注意到。

    **这不是假想的故障。** 一次给翻译表做整段替换的改动，正好把插进那两条中间
    的一整块键切掉了，四个组件的测试全绿、冒烟全过，直到有人打开那个设置页。

    只认字面量：`t(key)` 这种由变量拼出来的键（状态 → 键名的映射表）扫不到，
    那部分靠 smoke_gui.py 真的把窗口建出来兜底。
    """

    # \bt\( 的词边界不能省：没有它，FSDPilot("example.invalid") 里的那个 t
    # 会被当成一次 t() 调用，把机器名报成缺失的键
    CALL = re.compile(r'\bt\(\s*["\']([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)["\']')

    def test_no_source_file_uses_a_key_that_does_not_exist(self):
        here = os.path.dirname(os.path.abspath(__file__))
        missing = []
        for name in sorted(os.listdir(here)):
            if not name.endswith(".py") or name == "i18n.py":
                continue
            with open(os.path.join(here, name), encoding="utf-8") as f:
                text = f.read()
            for match in self.CALL.finditer(text):
                key = match.group(1)
                if key not in i18n.TEXT:
                    missing.append(f"{name}: {key}")
        self.assertEqual(sorted(set(missing)), [],
                         f"这些键代码在用，翻译表里却没有: {sorted(set(missing))}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
