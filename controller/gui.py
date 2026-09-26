"""管制员语音客户端。

界面按 TrackAudio 的电台栈组织：一个席位可以同时加多个频率，每个频率一行，各自
有 RX / TX / XC 开关和音量，按住 PTT 时对所有开了 TX 的频率一起发话。

外观用 qfluentwidgets（Fluent / Win11 风格），深色主题——管制类工具基本都是深色，
长时间盯着不累，而且频率行上的 RX/TX 高亮在深底上对比度更好。

**颜色只在这里定义一次**（下面那组常量）。散在各处写死颜色的话，换主题时改不干净，
留下几处和整体配色打架的绿字红字——这正是改造前的样子。
"""

import logging
import os
import sys
import threading
import time

from PyQt6.QtCore import (QPoint, QRect, QSize, Qt, QObject, QTimer, QUrl,
                          pyqtSignal)
from PyQt6.QtGui import (QColor, QDesktopServices, QFont, QIcon, QPainter,
                         QPen)
from PyQt6.QtWidgets import (QApplication, QDialog, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QLabel, QLayout, QPushButton,
                             QStackedWidget, QMessageBox)
from qfluentwidgets import (BodyLabel, CaptionLabel, CardWidget, FluentIcon,
                            InfoBar, InfoBarPosition, LineEdit, PasswordLineEdit,
                            PrimaryPushButton, PushButton, ScrollArea, Slider,
                            StrongBodyLabel, SubtitleLabel,
                            PillPushButton, TogglePushButton,
                            TransparentToolButton)

import applog
import calibration
import datafeed
import i18n
import micgain
import ptt
from i18n import t
import radiostack
import update
import version
from radiostack import RadioStack
from settings import Settings, SettingsDialog
from theme import (ACTIVE_COLOR, IDLE_COLOR, MONO_FONT, MUTED_COLOR, OFFLINE_COLOR,
                   OFF_COLOR, ONLINE_COLOR, ON_COLOR, SURFACE_BG, TEXT_COLOR,
                   THEME_COLOR, WINDOW_BG, apply_theme)
from voice import VoiceClient

log = logging.getLogger("gui")

SERVER = "audio.ceruleanavi.net"

# 数据源多久查一次：上了席位之后自动把频率捡回来，同时刷新 CID→呼号 对照表。
# 席位不会秒变，60 秒足够及时，也不至于把数据源打太狠。
DATAFEED_INTERVAL = 60
# 顶部输入框的最大宽度。频率卡片是流式排布的，屏幕越宽一行放得越多（这点照
# TrackAudio，不限宽），但输入框没必要跟着拉到一米长。
INPUT_WIDTH = 340

# 卡片尺寸。TrackAudio 是 205×90 的网格，这里稍宽一点放中文和"最后通话"。
CARD_WIDTH = 232
CARD_HEIGHT = 116

# 页面留白。精简模式下要紧一大截——那个模式的整个意义就是占地方少，留着
# 正常模式的 16px 边距和 12px 间距，一张卡的窗口有一半是空的。
NORMAL_MARGINS = (16, 14, 16, 12)
NORMAL_SPACING = 12
COMPACT_MARGINS = (6, 4, 6, 4)
COMPACT_SPACING = 4


class VoiceSignals(QObject):
    """pymumble 的回调在库线程里跑，必须经信号回到界面线程。"""
    state = pyqtSignal(str, str)
    rx = pyqtSignal(int, bool, str)
    tx = pyqtSignal(bool)
    connection = pyqtSignal(bool)
    # 数据源也是在后台线程取的，同样要过信号
    datafeed = pyqtSignal(object)
    # 查更新同理
    update_found = pyqtSignal(object)


def resource_path(name):
    """找随程序一起分发的资源。

    打包之后当前目录是用户双击时所在的目录，不是程序目录，用相对路径取图标
    会取不到（Qt 不会报错，只是默默用默认图标）。PyInstaller 把 datas 解到
    sys._MEIPASS。
    """
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


icon_path = resource_path("favicon.ico")


class Dot(QWidget):
    """一个纯色圆点。连接状态和 PTT 都用它。

    单独做成控件是为了让颜色只有一个来源——原来是每处各写一串 setStyleSheet，
    改配色要挨个找。
    """

    def __init__(self, color=IDLE_COLOR, size=10, parent=None):
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)
        self.set_color(color)

    def set_color(self, color):
        self.setStyleSheet(
            f"background-color: {color}; border-radius: {self._size // 2}px;")


class FlowLayout(QLayout):
    """从左到右排、排满换行的布局。

    对应 TrackAudio 的 `.radio-grid`（CSS 的 `repeat(auto-fill, minmax(205px,1fr))`）。
    Qt 没有内置的流式布局，而这正是那份设计的关键：频率是**卡片**不是整宽的行，
    所以窗口拉宽只会一行多放几张，不会把一个频率抻成一米长——那正是原来最大化
    之后标题在最左、开关在最右、中间一片空白的原因。
    """

    def __init__(self, parent=None, spacing=8):
        super().__init__(parent)
        self._items = []
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def insertWidget(self, index, widget):
        """把控件插到第 index 位。

        QLayout 没有 insertWidget（那是 QBoxLayout 才有的），但电台栈是**按频率
        排过序**的（radiostack.add 每次都 sort），所以卡片必须能插进中间——一律
        addWidget 追加的话，加一个 119.000 会排到 121.700 后面，屏幕上的频率就
        不是升序了。先让 QLayout.addWidget 走完认领控件那套（reparent + addItem
        追加到末尾），再把它挪到正确的位置。
        """
        self.addWidget(widget)
        item = self._items.pop()
        self._items.insert(max(0, min(index, len(self._items))), item)
        self.invalidate()

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._layout(rect, apply=True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(),
                            margins.top() + margins.bottom())

    def _layout(self, rect, apply):
        """返回排完之后的总高度。apply=False 时只算不摆。"""
        margins = self.contentsMargins()
        left = rect.x() + margins.left()
        top = rect.y() + margins.top()
        right = rect.right() - margins.right()
        x, y, row_height = left, top, 0
        space = self.spacing()

        for item in self._items:
            hint = item.sizeHint()
            if x > left and x + hint.width() > right:
                x = left
                y += row_height + space
                row_height = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + space
            row_height = max(row_height, hint.height())
        return y + row_height - rect.y() + margins.bottom()


class StateToggle(QPushButton):
    """RX / TX / XC 的三态开关：关 / 开 / 正在响。

    配色照 TrackAudio 的语义走（radio.tsx）：关 = primary，开 = success，
    正在收/发 = warning。三个开关同一套，不是每个一个颜色。

    **自绘，不用 qfluentwidgets 的按钮。** 试过 TogglePushButton + setStyleSheet：
    样式确实设上了，但库自己会用 FluentStyleSheet 重新 apply，把 checked 状态
    一律画成主题色——三个不同含义渲染成同一个颜色，等于管制员看不出哪个频率
    开着、哪个正在响，而这是这个界面上最要紧的信息。
    """

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.active = False              # 这一刻真的在收/发
        self.muted = False               # 只有 RX 用：静音时整个变红
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_state(self, checked, active=False, muted=False):
        changed = (checked != self.isChecked() or active != self.active
                   or muted != self.muted)
        self.setChecked(checked)
        self.active = active
        self.muted = muted
        if changed:
            self.update()

    def _fill(self):
        if self.muted:
            return QColor(MUTED_COLOR)
        if not self.isChecked():
            return QColor(OFF_COLOR)
        return QColor(ACTIVE_COLOR if self.active else ON_COLOR)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(0, 0, -1, -1)

        fill = self._fill()
        painter.setPen(QPen(fill.darker(140), 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, 4, 4)

        font = self.font()
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.text())


class RadioRow(CardWidget):
    """电台栈里的一行。"""

    def __init__(self, radio, window, parent=None):
        super().__init__(parent)
        self.khz = radio.frequency_khz
        self.window = window
        self.setBorderRadius(6)
        # 点整张卡片就把这个频率设成主频率（真正进入的那个 Mumble 频道）。
        # 用 CardWidget 自己的 clicked，别覆盖 mousePressEvent——那会把它的
        # 按下动画一起弄没。
        self.clicked.connect(lambda: self.window.select_radio(self.khz))
        self.setup_ui(radio)

    def setup_ui(self, radio):
        self.setFixedSize(CARD_WIDTH, CARD_HEIGHT)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 8, 8)
        layout.setSpacing(6)

        # ---- 上半：左边频率/呼号，右边 RX/TX ----
        # 照 TrackAudio 的 radio-content：左右各约一半，RX/TX 是右侧两个大按钮，
        # 点击目标大，急着切频率时不容易点歪
        upper = QHBoxLayout()
        upper.setSpacing(8)

        left = QVBoxLayout()
        left.setSpacing(0)
        self.freq_label = QLabel()
        self.freq_label.setFont(QFont(MONO_FONT, 17, QFont.Weight.DemiBold))
        self.freq_label.setStyleSheet(f"color: {TEXT_COLOR};")
        left.addWidget(self.freq_label)

        self.callsign_label = QLabel()
        self.callsign_label.setStyleSheet(f"color: {IDLE_COLOR}; font-size: 12px;")
        left.addWidget(self.callsign_label)
        left.addStretch()
        upper.addLayout(left, 1)

        right = QVBoxLayout()
        right.setSpacing(4)
        self.rx_button = self._toggle("RX", lambda: self.window.toggle_rx(self.khz))
        self.tx_button = self._toggle("TX", lambda: self.window.toggle_tx(self.khz))
        for button in (self.rx_button, self.tx_button):
            button.setFixedSize(52, 26)
            right.addWidget(button)
        upper.addLayout(right)
        layout.addLayout(upper)

        # ---- 下半：XC / 静音 / 移除，再下面是音量 ----
        controls = QHBoxLayout()
        controls.setSpacing(4)
        self.xc_button = self._toggle("XC", lambda: self.window.toggle_xc(self.khz))
        self.xc_button.setFixedSize(36, 22)
        controls.addWidget(self.xc_button)

        # TrackAudio 把静音做成 RX 的右键，这里保留一个显式按钮：中文用户里
        # 没人会去猜右键，而误静音了又完全没有提示
        self.mute_button = StateToggle("", self)
        self.mute_button.setFixedSize(46, 22)
        self.mute_button.clicked.connect(
            lambda checked: self.window.set_muted(self.khz, checked))
        controls.addWidget(self.mute_button)

        self.volume = Slider(Qt.Orientation.Horizontal, self)
        self.volume.setRange(0, 100)
        self.volume.setFixedHeight(18)
        self.volume.valueChanged.connect(
            lambda value: self.window.set_volume(self.khz, value))
        controls.addWidget(self.volume, 1)

        self.remove_button = TransparentToolButton(self)
        self.remove_button.setIcon(FluentIcon.CLOSE)
        self.remove_button.setFixedSize(22, 22)
        self.remove_button.clicked.connect(
            lambda: self.window.remove_radio(self.khz))
        controls.addWidget(self.remove_button)
        layout.addLayout(controls)

        self.last_rx = QLabel("")
        self.last_rx.setStyleSheet(f"color: {IDLE_COLOR}; font-size: 11px;")
        layout.addWidget(self.last_rx)

        # 老测试和外部代码认 title 这个名字，留一个只读的合成标签
        self.title = self.freq_label
        self.retranslate()
        self.refresh(radio)

    def _toggle(self, text, handler):
        button = StateToggle(text, self)
        button.clicked.connect(lambda: handler())
        return button

    def retranslate(self):
        """卡片上不随状态变化的那些文字。

        单独拎出来是因为 refresh() 每收到一次话音就会跑一遍，而这些字一次都不会
        变；反过来换语言时 refresh() 又碰不到它们——卡片是复用的（rebuild_rows
        只对已有的行调 refresh），于是切到英文之后按钮上还写着"静音"、RX 的悬停
        提示还是中文。
        """
        self.rx_button.setToolTip(t("radio.rx_tip"))
        self.tx_button.setToolTip(t("radio.tx_tip"))
        self.xc_button.setToolTip(t("radio.xc_tip"))
        self.mute_button.setText(t("radio.mute"))
        self.mute_button.setToolTip(t("radio.mute_tip"))
        self.remove_button.setToolTip(t("radio.remove_tip"))

    def refresh(self, radio):
        selected = self.window.stack.selected_khz == radio.frequency_khz
        marker = "▸ " if selected else ""
        self.freq_label.setText(f"{marker}{radio.frequency}")
        self.callsign_label.setText(radio.callsign or t("radio.no_callsign"))

        # 三态和 TrackAudio 一致：关 / 开 / 正在响，静音时 RX 整个变红
        self.rx_button.set_state(radio.rx, radio.currently_rx, radio.muted)
        self.tx_button.set_state(radio.tx, radio.currently_tx)
        self.xc_button.set_state(radio.xc)
        self.mute_button.set_state(radio.muted, muted=radio.muted)

        # 没在数据源上的管制席位时只许收：TX / XC 直接禁用，别让人按了没反应
        # 又不知道为什么
        stack = self.window.stack
        may_transmit = stack.transmit_allowed
        for button in (self.tx_button, self.xc_button):
            button.setEnabled(may_transmit)
            button.setToolTip(t("radio.tx_tip") if may_transmit
                              else t("radio.tx_needs_duty"))

        # 本人正在管的席位频率不许删——手滑删掉等于把自己从工作频率上摘下去
        locked = stack.is_locked(radio.frequency_khz)
        self.remove_button.setEnabled(not locked)
        self.remove_button.setToolTip(t("radio.remove_locked") if locked
                                      else t("radio.remove_tip"))

        self.volume.blockSignals(True)
        self.volume.setValue(radio.volume)
        self.volume.blockSignals(False)

        if radio.last_received_callsign:
            # 存的是 Mumble 的用户名（纯数字 CID），显示时才查表翻成呼号——
            # 这样数据源晚一步到齐，之前记下的那条也会跟着变过来
            who = self.window.callsign_for(radio.last_received_callsign)
            stamp = time.strftime('%H:%M:%S', time.localtime(radio.last_received_at))
            self.last_rx.setText(t("radio.last_rx", who=who, stamp=stamp))
        else:
            self.last_rx.setText(t("radio.last_rx_none"))

        # 主频率用主题色描边，比原来那圈蓝框更贴合整体
        self.setStyleSheet(
            f"RadioRow {{ border: 1px solid {THEME_COLOR}; }}" if selected else "")


class ControllerWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowIcon(QIcon(icon_path))
        self.setWindowTitle(t("app.title"))
        self.setMinimumSize(620, 480)

        self.settings = Settings()
        # 语言：存过就用存的，第一次启动跟系统走
        i18n.set_language(self.settings.language or i18n.system_language())
        self.voice = None
        self.cid = ""
        self.rows = {}
        self.stack = RadioStack(on_change=self.on_stack_changed)

        # CID → 呼号。Mumble 的用户名是纯数字的 CAN 号，靠这张表才能把
        # "最后通话: 1005" 显示成 "最后通话: CES2345"
        self.roster = {}
        self._user_removed = set()    # 用户自己删掉的，别再自动加回去跟他较劲
        self.online = []              # 数据源上在线的席位 [(呼号, 千赫, facility)]
        self._online_buttons = []

        self.signals = VoiceSignals()
        self.signals.state.connect(self.on_voice_state)
        self.signals.rx.connect(self.on_voice_rx)
        self.signals.tx.connect(self.on_voice_tx)
        self.signals.connection.connect(self.on_connection_change)
        self.signals.datafeed.connect(self.on_datafeed)
        self.signals.update_found.connect(self.on_update_found)

        self.datafeed_timer = QTimer(self)
        self.datafeed_timer.timeout.connect(self.refresh_datafeed)

        self.setup_ui()
        self.setup_ptt()
        # 上次开着置顶 / 精简的话，这次起来就还是那样
        self.apply_always_on_top()
        if self.settings.compact:
            self.toggle_compact(True)

        # 起来之后在后台问一次有没有新版。查不到就当没这回事。
        self.check_for_update()

        # 所选麦克风在当前降噪状态下没校准过就弹一次。open() 不阻塞，窗口先显示出来。
        QTimer.singleShot(0, self.maybe_prompt_calibration)

        # 电台栈**不做持久化**：每次启动都是空的。频率该从数据源来——上了席位
        # 的自动加，别人的席位在"在线频率"里点。留着上一场的频率反而危险：
        # 那些临时频道多半早就没人了，而屏幕上看起来一切正常。

    # ---------- 界面 ----------
    def setup_ui(self):
        # 主题只管 qfluentwidgets 的控件，普通 QWidget 的底色还得自己给，
        # 否则深色控件会浮在一片白底上
        self.setStyleSheet(f"QMainWindow, QStackedWidget {{ background-color: {WINDOW_BG}; }}"
                           f"QWidget#page {{ background-color: {WINDOW_BG}; }}")

        self.pages = QStackedWidget()
        self.setCentralWidget(self.pages)

        self.pages.addWidget(self.build_login_page())
        self.pages.addWidget(self.build_main_page())

        self.username_input.setText(self.settings.last_username)

    def build_login_page(self):
        page = QWidget()
        page.setObjectName("page")
        outer = QVBoxLayout(page)
        outer.addStretch()

        # 登录框收进一张卡片，居中——原来是几行光秃秃的输入框贴在窗口上
        card = CardWidget()
        card.setBorderRadius(8)
        card.setFixedWidth(340)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)

        self.login_title = title = SubtitleLabel(t("app.name"))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        self.username_input = LineEdit()
        self.username_input.setPlaceholderText(t("login.username"))
        self.username_input.setClearButtonEnabled(True)
        layout.addWidget(self.username_input)

        self.password_input = PasswordLineEdit()
        self.password_input.setPlaceholderText(t("login.password"))
        self.password_input.returnPressed.connect(self.connect_voice)
        layout.addWidget(self.password_input)

        self.connect_button = PrimaryPushButton()
        self.connect_button.setText(t("login.connect"))
        self.connect_button.clicked.connect(self.connect_voice)
        layout.addWidget(self.connect_button)

        self.login_status = CaptionLabel(t("login.disconnected"))
        self.login_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.login_status.setStyleSheet(f"color: {IDLE_COLOR};")
        self.login_status.setWordWrap(True)
        layout.addWidget(self.login_status)

        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(card)
        row.addStretch()
        outer.addLayout(row)

        # 版本号放登录页：用户报问题时第一句就该是"你用的哪个版本"，而这时候
        # 他多半还没连上。不进 i18n——"v1.1.0 (build 71.667d9b4)"两种语言下
        # 是同一串，翻了反而对不上日志里的那一行。
        self.version_label = CaptionLabel(version.full())
        self.version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.version_label.setStyleSheet(f"color: {IDLE_COLOR};")
        outer.addSpacing(8)
        outer.addWidget(self.version_label)
        outer.addStretch()
        return page

    def build_main_page(self):
        page = QWidget()
        page.setObjectName("page")
        self.main_layout = layout = QVBoxLayout(page)
        layout.setContentsMargins(*NORMAL_MARGINS)
        layout.setSpacing(NORMAL_SPACING)

        # ---- 顶栏：连接状态 + 席位 + 操作 ----
        # 四个横条各包一层 QWidget：精简模式要整条整条地藏起来，而 QHBoxLayout
        # 本身没有 setVisible，只能藏控件
        self.top_bar = QWidget()
        self.top_bar.setStyleSheet("background: transparent;")
        top = QHBoxLayout(self.top_bar)
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        # 掉线要能一眼看出来，不然管制员会对着死掉的连接一直喊
        self.conn_indicator = Dot(ONLINE_COLOR)
        self.conn_label = BodyLabel(t("main.connected"))
        top.addWidget(self.conn_indicator)
        top.addWidget(self.conn_label)
        top.addSpacing(12)

        self.session_label = StrongBodyLabel(t("login.disconnected"))
        top.addWidget(self.session_label)
        top.addStretch()

        # 置顶：管制员多半把语音压在雷达/模拟器上面用，被别的窗口盖住就等于
        # 看不见谁在呼叫。做成常驻的开关而不是塞进设置对话框——它是要频繁切的。
        self.top_button = TogglePushButton()
        self.top_button.setText(t("main.pin"))
        self.top_button.setIcon(FluentIcon.PIN)
        self.top_button.setToolTip(t("main.pin_tip"))
        self.top_button.setChecked(self.settings.always_on_top)
        self.top_button.clicked.connect(self.toggle_always_on_top)
        top.addWidget(self.top_button)

        # 精简模式：只留电台卡片。管制员真正要盯的就是那几张卡，把输入框、
        # 在线频率、状态栏都收起来之后窗口能小到压在雷达角落里。
        self.compact_button = TogglePushButton()
        self.compact_button.setText(t("main.compact"))
        self.compact_button.setIcon(FluentIcon.MINIMIZE)
        self.compact_button.setToolTip(t("main.compact_tip"))
        self.compact_button.setChecked(self.settings.compact)
        self.compact_button.clicked.connect(self.toggle_compact)
        top.addWidget(self.compact_button)

        self.settings_button = settings_button = PushButton()
        settings_button.setText(t("main.settings"))
        settings_button.setIcon(FluentIcon.SETTING)
        settings_button.clicked.connect(self.open_settings)
        top.addWidget(settings_button)

        self.disconnect_button = disconnect_button = PushButton()
        disconnect_button.setText(t("main.disconnect"))
        disconnect_button.setIcon(FluentIcon.POWER_BUTTON)
        disconnect_button.clicked.connect(self.disconnect_voice)
        top.addWidget(disconnect_button)
        layout.addWidget(self.top_bar)

        # ---- 添加频率 ----
        self.add_bar = QWidget()
        self.add_bar.setStyleSheet("background: transparent;")
        add_row = QHBoxLayout(self.add_bar)
        add_row.setContentsMargins(0, 0, 0, 0)
        add_row.setSpacing(8)
        # 输入框按比例伸缩，不写死宽度。写死的话（180 + 340 + 按钮 + 边距）正好
        # 顶到窗口最小宽度，"添加频率"就被挤到窗口边上，看着像被裁掉了。
        # 上限还是 INPUT_WIDTH——屏幕再宽也没必要把输入框拉到一米长。
        self.freq_input = LineEdit()
        self.freq_input.setPlaceholderText(t("main.freq_hint"))
        self.freq_input.setMinimumWidth(120)
        self.freq_input.setMaximumWidth(200)
        self.freq_input.returnPressed.connect(self.add_radio)
        self.callsign_input = LineEdit()
        self.callsign_input.setPlaceholderText(t("main.callsign_hint"))
        self.callsign_input.setMinimumWidth(160)
        self.callsign_input.setMaximumWidth(INPUT_WIDTH)
        self.callsign_input.returnPressed.connect(self.add_radio)
        self.add_button = add_button = PrimaryPushButton()
        add_button.setText(t("main.add"))
        add_button.setIcon(FluentIcon.ADD)
        # 图标加中文标签，给够宽度，别让 Qt 把文字压成省略号
        add_button.setMinimumWidth(112)
        add_button.clicked.connect(self.add_radio)
        # 两个输入框分掉多余的宽度（呼号那个占大头），按钮紧跟其后、保持自然
        # 宽度，多出来的空间留在最右边。窗口窄下去时先压缩输入框，按钮不会被
        # 挤出可视区。
        add_row.addWidget(self.freq_input, 1)
        add_row.addWidget(self.callsign_input, 2)
        add_row.addWidget(add_button, 0)
        add_row.addStretch(1)
        layout.addWidget(self.add_bar)

        # ---- 电台栈 ----
        self.stack_area = ScrollArea()
        self.stack_area.setWidgetResizable(True)
        self.stack_area.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        holder = QWidget()
        holder.setStyleSheet("background: transparent;")
        # 流式网格：卡片排满一行就换行，窗口拉宽只是一行多放几张
        self.stack_layout = FlowLayout(holder, spacing=8)
        self.stack_area.setWidget(holder)
        layout.addWidget(self.stack_area)

        self.empty_hint = BodyLabel(t("main.empty"))
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_hint.setStyleSheet(f"color: {IDLE_COLOR};")
        layout.addWidget(self.empty_hint)

        # ---- 在线频率：数据源上此刻有人的席位，点一下加进电台栈 ----
        self.online_bar = QWidget()
        self.online_bar.setStyleSheet("background: transparent;")
        online_bar = QHBoxLayout(self.online_bar)
        online_bar.setContentsMargins(0, 0, 0, 0)
        online_bar.setSpacing(6)
        self.online_title = CaptionLabel(t("online.title"))
        self.online_title.setStyleSheet(f"color: {IDLE_COLOR};")
        online_bar.addWidget(self.online_title)
        self.online_box = QHBoxLayout()
        self.online_box.setSpacing(4)
        online_bar.addLayout(self.online_box, 1)
        layout.addWidget(self.online_bar)

        # ---- 底栏：PTT + 状态 ----
        self.bottom_bar = QWidget()
        self.bottom_bar.setStyleSheet("background: transparent;")
        bottom = QHBoxLayout(self.bottom_bar)
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(8)
        self.ptt_indicator = Dot(IDLE_COLOR, size=12)
        bottom.addWidget(self.ptt_indicator)
        self.ptt_label = CaptionLabel('PTT')
        bottom.addWidget(self.ptt_label)
        bottom.addSpacing(10)
        self.status_label = CaptionLabel(t("status.ready"))
        self.status_label.setStyleSheet(f"color: {IDLE_COLOR};")
        self.status_label.setWordWrap(True)
        bottom.addWidget(self.status_label, 1)
        # 值守状态：在不在管制席位上，决定了能不能发信，必须一眼看得见
        self.duty_label = CaptionLabel(t("duty.observer"))
        self.duty_label.setStyleSheet(f"color: {IDLE_COLOR};")
        bottom.addWidget(self.duty_label)
        layout.addWidget(self.bottom_bar)
        return page

    # ---------- 精简模式 ----------
    def toggle_compact(self, enabled=None):
        """只留电台卡片，其余整条收起来。

        管制员真正要盯的就是那几张卡；输入框、在线频率、状态栏都是配置和参考
        用的，收起来之后窗口能小到压在雷达角落里——和置顶那个开关是一对。

        顶栏不整条藏：留着连接指示灯、置顶和这个开关本身，否则精简之后就没有
        任何路径能切回来了。
        """
        if enabled is None:
            enabled = self.compact_button.isChecked()
        enabled = bool(enabled)
        self.compact_button.setChecked(enabled)

        for widget in (self.add_bar, self.online_bar, self.bottom_bar):
            widget.setVisible(not enabled)
        # 没有频率时那句提示在精简模式下也没意义
        self.empty_hint.setVisible(not enabled and len(self.stack) == 0)
        for widget in (self.conn_label, self.session_label,
                       self.settings_button, self.disconnect_button):
            widget.setVisible(not enabled)

        # 留白和按钮也要跟着缩。精简模式的意义就是占地方少，留着正常模式的
        # 边距、间距和带文字的大按钮，一张卡的窗口里有一半是空的。
        self.main_layout.setContentsMargins(
            *(COMPACT_MARGINS if enabled else NORMAL_MARGINS))
        self.main_layout.setSpacing(COMPACT_SPACING if enabled else NORMAL_SPACING)
        for button, label in ((self.top_button, t("main.pin")),
                              (self.compact_button, t("main.compact"))):
            # 精简时只留图标：那两个字在这种窗口里占的是卡片的地方
            button.setText("" if enabled else label)
            if enabled:
                button.setFixedSize(30, 26)
            else:
                button.setMinimumSize(0, 0)
                button.setMaximumSize(16777215, 16777215)

        # 最小尺寸要跟着放开，否则窗口缩不下去——一张卡加上留白就够了
        if enabled:
            self.setMinimumSize(CARD_WIDTH + 2 * COMPACT_MARGINS[0] + 4,
                                CARD_HEIGHT + 70)
            self.resize(self.minimumWidth(), self.minimumHeight())
        else:
            self.setMinimumSize(620, 480)
            self.resize(max(self.width(), 620), max(self.height(), 480))

        self.settings.compact = enabled
        self.settings.save_settings()

    def _set_connection_style(self, connected):
        color = ONLINE_COLOR if connected else OFFLINE_COLOR
        self.conn_indicator.set_color(color)
        self.conn_label.setText(t("main.connected") if connected else t("main.disconnected"))
        self.conn_label.setStyleSheet(f"color: {color};")

    def _set_ptt_style(self, active):
        self.ptt_indicator.set_color(ACTIVE_COLOR if active else IDLE_COLOR)
        self.ptt_label.setStyleSheet(
            f"color: {ACTIVE_COLOR}; font-weight: bold;" if active
            else f"color: {IDLE_COLOR};")

    def toggle_always_on_top(self, checked=None):
        """切换窗口置顶。

        Windows 上改 window flag 会把窗口**隐藏**掉，必须再 show() 一次，否则
        用户点一下置顶、整个窗口就没了。而 show() 又会把最大化状态丢掉，所以
        先记下来再还原——不然最大化的人一点置顶，窗口就缩回小尺寸。
        """
        if checked is None:
            checked = not self.settings.always_on_top
            self.top_button.setChecked(checked)
        self.settings.always_on_top = bool(checked)
        self.settings.save_settings()
        self.apply_always_on_top()

    def apply_always_on_top(self):
        on = bool(self.settings.always_on_top)
        if bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint) == on:
            return
        # 可见性和最大化都要在改 flag **之前**问：setWindowFlag 本身就会把窗口
        # 隐藏掉，改完再问 isHidden() 永远是 True，show() 就被跳过了，表现出来
        # 正是"点一下置顶窗口就没了"。
        was_visible = self.isVisible()
        maximised = self.isMaximized()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
        # 启动时还没 show 过就别抢焦点
        if was_visible:
            self.showMaximized() if maximised else self.show()
        log.info("always on top: %s", "on" if on else "off")

    def retranslate(self):
        """换语言之后把界面上的字全部重刷一遍。

        不做这一步就得让用户重启才能看到新语言——一个只在设置里生效、要重启才
        看得见的开关，用户第一反应是"是不是没保存"。

        运行时才产生的文字（语音层报上来的错误）留到下一次事件自然更新，卡片这里
        主动重刷一次，因为"最后通话"可能停在屏幕上很久。
        """
        self.setWindowTitle(t("app.title"))
        self.login_title.setText(t("app.name"))
        self.username_input.setPlaceholderText(t("login.username"))
        self.password_input.setPlaceholderText(t("login.password"))
        self.connect_button.setText(t("login.connect"))
        self.top_button.setText(t("main.pin"))
        self.top_button.setToolTip(t("main.pin_tip"))
        self.settings_button.setText(t("main.settings"))
        self.disconnect_button.setText(t("main.disconnect"))
        self.add_button.setText(t("main.add"))
        self.freq_input.setPlaceholderText(t("main.freq_hint"))
        self.callsign_input.setPlaceholderText(t("main.callsign_hint"))
        self.empty_hint.setText(t("main.empty"))
        self._set_connection_style(bool(self.voice))
        if not self.voice:
            self.login_status.setText(t("login.disconnected"))
            self.session_label.setText(t("login.disconnected"))
        # 不能无条件写"就绪"：频道监听的警告要是正挂着，换个语言就被抹掉了，
        # 而问题一点没变。update_hint 会按当前状态重新决定写哪一条。
        if self.voice:
            self.update_hint()
        else:
            self.status_label.setStyleSheet(f"color: {IDLE_COLOR};")
            self.status_label.setText(t("status.ready"))
        for row in self.rows.values():
            row.retranslate()
        self.rebuild_rows()

    def warn(self, title, content):
        """轻提示。

        频率填错这种校验用不着模态对话框——弹一个会打断输入，还得伸手去点。
        InfoBar 从右上角滑出来、自己消失，不挡任何操作。
        """
        InfoBar.warning(title=title, content=content, parent=self,
                        position=InfoBarPosition.TOP_RIGHT, duration=4000)

    # ---------- 电台栈 ----------
    def on_stack_changed(self):
        """栈变了：重画界面，推给服务器。**不存盘**——频率不跨会话保留。"""
        self.rebuild_rows()
        self.refresh_online_list()      # 加/删之后"在线频率"里的置灰要跟着变
        if self.voice:
            # sync 要建频道、发协议消息，还要等服务器回话——放在界面线程里做的话，
            # 每加一个频率界面就会卡一下
            threading.Thread(target=self.voice.sync, args=(self.stack,),
                             daemon=True).start()

    def rebuild_rows(self):
        current = {radio.frequency_khz for radio in self.stack}

        for khz in list(self.rows):
            if khz not in current:
                row = self.rows.pop(khz)
                self.stack_layout.removeWidget(row)
                row.deleteLater()

        # 按栈的顺序插，不能一律追加：栈是按频率排过序的，新加的频率可能要插到
        # 中间去，追加会让屏幕上的频率不再是升序
        for index, radio in enumerate(self.stack):
            row = self.rows.get(radio.frequency_khz)
            if row is None:
                row = RadioRow(radio, self)
                self.rows[radio.frequency_khz] = row
                self.stack_layout.insertWidget(index, row)
            else:
                row.refresh(radio)

        self.empty_hint.setVisible(len(self.stack) == 0)

    def add_radio(self):
        try:
            self.stack.add(self.freq_input.text(), self.callsign_input.text())
        except ValueError as e:
            self.warn(t("main.add_failed"), str(e))
            return
        self.freq_input.clear()
        self.callsign_input.clear()

    def remove_radio(self, khz):
        # 记下来：用户手动删掉的频率，就算数据源上他还挂着这个席位，也不要再
        # 自动加回去——每 60 秒跟用户抢一次是最招人烦的那种"智能"
        self._user_removed.add(khz)
        self.stack.remove(khz)

    # ---------- 数据源（席位频率 / 呼号） ----------
    def callsign_for(self, name):
        """把 Mumble 报上来的名字翻成呼号。

        语音层给的是 user["name"]，而按 server/login.py 的设计那就是纯数字的
        CAN 号。查不到就原样返回——数据源没拉到、或者对方刚上线还没进数据源时，
        显示个数字也比显示空白强。
        """
        return self.roster.get(str(name), name)

    def refresh_datafeed(self):
        """去数据源要一次。网络在后台线程做，结果过信号回来。"""
        if not self.cid:
            return

        def work():
            self.signals.datafeed.emit(datafeed.fetch())

        threading.Thread(target=work, daemon=True).start()

    def on_datafeed(self, data):
        """数据源回来了（已在界面线程）。"""
        if not data:
            return
        self.roster = datafeed.roster(data)
        self.online = datafeed.online_positions(data)
        self.apply_duty_state(data)
        # 之前记下的是 CID，重画一次就变成呼号了——晚到的对照表也能补上
        self.rebuild_rows()
        self.refresh_online_list()
        self.adopt_controlled_frequency(data)

    def refresh_online_list(self):
        """把数据源上的在线席位画成一排按钮，点一下就加进电台栈。

        已经在栈里的置灰而不是藏起来——藏起来的话，一个频率是"没人上"还是
        "我已经加过了"就分不出来了。
        """
        # 整个清空：除了按钮，末尾还有一个 addStretch 加进来的空白项，只删控件
        # 的话它每刷新一次就多一个，席位一多整排就被挤到左边去了
        while self.online_box.count():
            item = self.online_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._online_buttons = []

        if not self.online:
            label = CaptionLabel(t("online.empty"))
            label.setStyleSheet(f"color: {IDLE_COLOR};")
            self.online_box.addWidget(label)
            self._online_buttons.append(label)
            return

        for callsign, khz, _facility in self.online:
            frequency = radiostack.format_frequency(khz)
            # 只显示呼号：频率在按钮上写不下（实测被压成 "ZBAA ...8.500"），
            # 而且管制员认的本来就是呼号。频率放进悬停提示，要看还是看得到。
            button = PillPushButton(callsign, self)
            button.setCheckable(False)
            button.setFixedHeight(24)
            # 按文字量给宽度，别让 Qt 去压缩它
            width = button.fontMetrics().horizontalAdvance(callsign) + 24
            button.setFixedWidth(width)
            already = self.stack.get(khz) is not None
            button.setEnabled(not already)
            button.setToolTip(
                t("online.already", frequency=frequency) if already
                else t("online.add_tip", callsign=callsign, frequency=frequency))
            if not already:
                button.clicked.connect(
                    lambda _checked=False, k=khz, c=callsign:
                    self.add_online_frequency(k, c))
            self.online_box.addWidget(button)
            self._online_buttons.append(button)

        # 席位少的时候不要平摊到整行，靠左排
        self.online_box.addStretch(1)

    def add_online_frequency(self, khz, callsign):
        """从在线一览里加一个频率。"""
        if self.stack.get(khz):
            return
        try:
            self.stack.add(khz, callsign)
        except ValueError as e:
            self.warn(t("main.add_failed"), str(e))
            return
        # 手动加回来的，就不算"用户删掉的"了
        self._user_removed.discard(khz)
        self.refresh_online_list()

    def apply_duty_state(self, data):
        """按数据源判定本人在不在管制席位，据此开闸或落闸。

        语音服务器没有花名册检查——任何账号都能进任何 FREQ_* 频道，也都能对着
        它说话。这一层是客户端自觉守的，和 can-fsd 上「不在花名册里就不能上
        席位」对应。判定用的是 datafeed.controller_for，和自动加频率同一条：
        要在 controllers[] 里，且席位类型高于观察员。
        """
        entry = datafeed.controller_for(self.cid, data)
        khz = datafeed.frequency_khz(entry) if entry else None

        dropped = self.stack.set_transmit_allowed(entry is not None)
        self.stack.set_locked(khz)

        if entry is None:
            self.duty_label.setStyleSheet(f"color: {IDLE_COLOR};")
            self.duty_label.setText(t("duty.observer"))
            if dropped:
                # 值守掉了会把已经开着的 TX 落下来，这事必须说出来，否则管制员
                # 会对着一个自以为开着的频率讲话
                log.info("no longer staffing a position on the datafeed, dropping "
                         "every TX / XC")
                self.status_label.setStyleSheet(f"color: {IDLE_COLOR};")
                self.status_label.setText(t("duty.tx_dropped"))
        else:
            self.duty_label.setStyleSheet(f"color: {ON_COLOR};")
            self.duty_label.setText(t("duty.on", callsign=str(
                entry.get("callsign", "")).strip()))

    def adopt_controlled_frequency(self, data):
        """本人在网上上了席位的话，把那个频率自动加进电台栈。"""
        entry = datafeed.controller_for(self.cid, data)
        if not entry:
            return
        khz = datafeed.frequency_khz(entry)
        if khz is None or khz in self._user_removed:
            return
        if self.stack.get(khz):
            return
        callsign = str(entry.get("callsign", "")).strip()
        try:
            self.stack.add(khz, callsign)
        except ValueError as e:
            log.warning("could not auto-add the position frequency: %s", e)
            return
        # 自己的席位频率默认就开 RX + TX：管制员上了席位，本来就是要在这个
        # 频率上收发的，还要手动点两下才能说话是多余的一步——而且漏点了不会
        # 报错，只是喊了半天没人应。set_tx 会连带打开 RX（不存在只发不收）。
        #
        # 只在**新加进来**的时候设（上面已经挡掉了栈里已有的），所以之后管制员
        # 自己把 TX 关掉，下一轮刷新不会又给他打开。
        self.stack.set_tx(khz, True)
        self.stack.select(khz)          # 顺带设成主频率：真正进入的那个频道

        log.info("auto-added %s from position %s on the datafeed (RX + TX by "
             "default)",
                 callsign, radiostack.format_frequency(khz))
        self.status_label.setStyleSheet(f"color: {ON_COLOR};")
        self.status_label.setText(t(
            "status.adopted", callsign=callsign,
            frequency=radiostack.format_frequency(khz)))

    def select_radio(self, khz):
        self.stack.select(khz)

    def toggle_rx(self, khz):
        self.stack.toggle_rx(khz)

    def toggle_tx(self, khz):
        self.stack.toggle_tx(khz)

    def toggle_xc(self, khz):
        self.stack.toggle_xc(khz)

    def set_volume(self, khz, value):
        self.stack.set_volume(khz, value)

    def set_muted(self, khz, muted):
        self.stack.set_muted(khz, muted)

    # ---------- 连接 ----------
    def connect_voice(self):
        username = self.username_input.text().strip()
        password = self.password_input.text()
        if not username or not password:
            self.login_status.setText(t("login.need_credentials"))
            self.login_status.setStyleSheet(f"color: {OFFLINE_COLOR};")
            return

        self.connect_button.setEnabled(False)
        self.login_status.setStyleSheet(f"color: {IDLE_COLOR};")
        self.login_status.setText(t("login.connecting"))
        QApplication.processEvents()

        self.voice = VoiceClient(
            SERVER, username, password,
            on_state=self.signals.state.emit,
            on_rx=self.signals.rx.emit,
            on_tx=self.signals.tx.emit,
            on_connection_change=self.signals.connection.emit)
        self.voice.set_mic_volume(self.settings.mic_volume)
        self.apply_mic_baseline()
        self.voice.set_speaker_volume(self.settings.speaker_volume)
        self.voice._input_device = self.settings.input_device_index
        self.voice._output_device = self.settings.output_device_index

        if not self.voice.connect():
            # 必须显式收尾：connect 失败时 PyAudio 已经建好了，pymumble 线程也
            # 可能还在（reconnect=True 会一直重试）。只把引用置空的话，每失败
            # 一次就多一条后台重连线程和一个没释放的音频设备。
            self.voice.disconnect()
            self.voice = None
            self.connect_button.setEnabled(True)
            return

        self.connect_button.setEnabled(True)
        self.cid = username
        self.settings.last_username = username
        self.settings.save_settings()
        self.session_label.setText(f'{username} · {SERVER}')
        self._set_connection_style(True)
        self.pages.setCurrentIndex(1)
        threading.Thread(target=self.voice.sync, args=(self.stack,),
                         daemon=True).start()
        # 立刻查一次（上了席位就把频率捡回来），之后按周期刷
        self.refresh_datafeed()
        self.datafeed_timer.start(DATAFEED_INTERVAL * 1000)
        self.update_hint()

    def update_hint(self):
        if not self.voice:
            return
        self.status_label.setStyleSheet(f"color: {IDLE_COLOR};")
        self.status_label.setText(t("status.ready"))

    def disconnect_voice(self):
        self.datafeed_timer.stop()
        self.cid = ""
        if self.voice:
            self.voice.disconnect()
            self.voice = None
        # 值班锁和发话许可来自数据源，而数据源定时器刚停了：不复位的话，
        # 上一班锁住的频率断开后还是删不掉，提示写着"你正在扇区里管它"。
        self.stack.set_locked(None)
        self.stack.set_transmit_allowed(True)
        for radio in self.stack:
            radio.currently_rx = False
            radio.currently_tx = False
        self.rebuild_rows()
        self.pages.setCurrentIndex(0)
        self.login_status.setStyleSheet(f"color: {IDLE_COLOR};")
        self.login_status.setText(t("login.disconnected"))

    # ---------- 语音回调（已在界面线程） ----------
    def on_voice_state(self, state, message):
        if state == 'reconnecting':
            # 还在重连：连接没死，别弹框也别退回登录页——一次抖动就把值守中的
            # 管制员踢回登录页是不能接受的。状态栏用"正在收发"的橙色，一眼能看
            # 出不是正常状态。
            self.status_label.setStyleSheet(f"color: {ACTIVE_COLOR};")
            self.status_label.setText(message)
            return
        if state == 'offline':
            # 重连次数用尽：语音已经自己收摊了，这里把界面一起退回登录页，
            # 并留下原因——不然只剩一句"已断开"，看不出是掉线还是自己点的。
            self.disconnect_voice()
            self.login_status.setStyleSheet(f"color: {OFFLINE_COLOR};")
            self.login_status.setText(message)
            QMessageBox.warning(self, t("login.failed"), message)
            return
        if state == 'error':
            self.status_label.setStyleSheet(f"color: {OFFLINE_COLOR};")
            self.status_label.setText(message)
            self.login_status.setStyleSheet(f"color: {OFFLINE_COLOR};")
            self.login_status.setText(message)
            if self.pages.currentIndex() == 0:
                QMessageBox.critical(self, t("login.failed"), message)
        elif state == 'denied':
            # 服务器挡了某个动作（多半是监听频道的上限），红字留在状态栏
            self.status_label.setStyleSheet(f"color: {OFFLINE_COLOR};")
            self.status_label.setText(message)
        else:
            self.status_label.setStyleSheet(f"color: {IDLE_COLOR};")
            self.status_label.setText(message)

    def on_voice_rx(self, khz, active, callsign):
        self.stack.set_currently_rx(khz, active, callsign, time.time())
        if active:
            self.update_hint()

    def on_voice_tx(self, active):
        self._set_ptt_style(active)
        self.stack.set_currently_tx(active)

    def on_connection_change(self, connected):
        self._set_connection_style(connected)
        if not connected:
            # 掉线时把所有频率的收发状态灭掉，免得停在"正在通话"上
            for radio in self.stack:
                radio.currently_rx = False
                radio.currently_tx = False
            self.rebuild_rows()

    # ---------- PTT ----------
    def setup_ptt(self):
        """键盘、摇杆按钮、鼠标侧键都从 ptt.PttWatcher 来，任意一个按住就发话。"""
        self.ptt_watcher = ptt.PttWatcher(self.settings.ptt_bindings,
                                          on_change=self.on_ptt_change)
        self.ptt_watcher.start()

    def on_ptt_change(self, down):
        """在监听线程上跑（pynput 的线程或摇杆轮询线程）。

        **故意不经 pyqtSignal**。这里一个控件都不碰——发话灯是 voice 的 on_tx 回调
        经 signals.tx 点亮的——而绕一趟 Qt 事件循环会让 PTT 的响应跟着界面忙不忙
        走：重画电台栈的那一帧里按下 PTT，话头就被切掉了。
        """
        voice = self.voice
        if not voice:
            return
        try:
            if down:
                voice.start_transmit()
            else:
                voice.stop_transmit()
        except Exception as e:
            log.warning("could not switch the transmitter: %s", e)

    # ---------- 设置 ----------
    # ---------- 更新 ----------
    def check_for_update(self, manual=False):
        """后台问一次有没有新版。

        `manual=True` 是用户自己点的，那种情况下没有新版也要回一句。启动时那次
        是静默的——管制员开着这个窗口是为了值班，不是为了看更新提示。
        """
        if not manual and not getattr(self.settings, "update_check", True):
            return

        def work():
            found = update.check("audio-for-can", version.version(),
                                 getattr(self.settings, "update_url", None))
            self.signals.update_found.emit(found or (manual and "none" or None))

        threading.Thread(target=work, daemon=True).start()

    def on_update_found(self, found):
        """已经在界面线程。found 是 Update、"none"（手动查但没有新版）或 None。"""
        if found is None:
            return
        if found == "none":
            QMessageBox.information(self, t("update.check"),
                                    t("update.current", version=version.version()))
            return
        if found.version == getattr(self.settings, "skipped_version", ""):
            log.info("update %s was skipped by the user", found.version)
            return

        # **连着的时候不打断。** 管制员正在席位上，一个模态框会盖住电台栈，
        # 而 PTT 是全局热键——他可能正对着频率讲话。等断开之后再说。
        if self.voice:
            log.info("update %s available, not prompting while connected",
                     found.version)
            self.status_label.setStyleSheet(f"color: {ACTIVE_COLOR};")
            self.status_label.setText(t("update.title") + f" {found.version}")
            return

        size = t("update.size", size=found.size_label) if found.size_label else ""
        box = QMessageBox(self)
        box.setWindowTitle(t("update.title"))
        box.setText(t("update.body", version=found.version, size=size,
                      current=version.version()))
        box.setInformativeText(t("update.detail"))
        download = box.addButton(t("update.download"),
                                 QMessageBox.ButtonRole.AcceptRole)
        notes = box.addButton(t("update.notes"), QMessageBox.ButtonRole.HelpRole)
        skip = box.addButton(t("update.skip"),
                             QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(t("update.later"), QMessageBox.ButtonRole.RejectRole)
        box.exec()

        clicked = box.clickedButton()
        if clicked is download and found.download:
            QDesktopServices.openUrl(QUrl(found.download))
        elif clicked is notes and found.notes:
            QDesktopServices.openUrl(QUrl(found.notes))
        elif clicked is skip:
            self.settings.skipped_version = found.version
            self.settings.save_settings()

    # ---------- 麦克风校准 ----------
    def apply_mic_baseline(self):
        """按当前输入设备和降噪状态取基准增益，交给语音客户端，并记一行日志。"""
        name = calibration.input_device_name(self.settings.input_device_index)
        baseline = self.settings.baseline_for(name)
        log.info("mic baseline for %s: %s (denoise %s)", name or "unknown device",
                 "uncalibrated" if baseline is None else f"{baseline:+.1f} dB",
                 "on" if self.settings.denoise_active() else "off")
        if self.voice:
            self.voice.set_mic_baseline(baseline or 0.0)
            self.voice.set_mic_denoise(self.settings.mic_denoise)
        return name, baseline

    def maybe_prompt_calibration(self):
        name, baseline = self.apply_mic_baseline()
        if name is not None and baseline is None:
            self.run_calibration(name)

    def run_calibration(self, device_name):
        was_running = hasattr(self, 'ptt_watcher')
        if was_running:
            self.ptt_watcher.stop()
        dialog = calibration.CalibrationDialog(
            self.settings.input_device_index, device_name,
            self.settings.denoise_active(), self)

        def done(code):
            if code == QDialog.DialogCode.Accepted and dialog.entry():
                self.settings.mic_calibration[device_name] = dialog.entry()
                self.settings.save_settings()
            else:
                log.info("calibration skipped on %s", device_name)
            self.apply_mic_baseline()
            if was_running:
                self.ptt_watcher.set_bindings(self.settings.ptt_bindings)
                self.ptt_watcher.start()

        dialog.finished.connect(done)
        dialog.open()

    def open_settings(self):
        # 开设置期间把监听整个停掉。录绑定时 PttCapture 要独占 SDL 的事件队列
        # （两个线程一起 pump 不是线程安全的），而且用户为了录绑定按的那一下
        # 不该真的发出去一段语音。
        if hasattr(self, 'ptt_watcher'):
            self.ptt_watcher.stop()
        old_mic = self.settings.mic_volume
        dialog = SettingsDialog(self.settings, self)
        accepted = dialog.exec()
        if hasattr(self, 'ptt_watcher'):
            self.ptt_watcher.set_bindings(self.settings.ptt_bindings)
            self.ptt_watcher.start()
        if accepted:
            self.retranslate()
            if self.settings.mic_volume != old_mic:
                _, baseline = self.apply_mic_baseline()
                log.info(micgain.describe_multiplier(
                    old_mic, self.settings.mic_volume, baseline or 0.0))
            if self.voice:
                self.voice.set_mic_volume(self.settings.mic_volume)
                self.voice.set_speaker_volume(self.settings.speaker_volume)
                try:
                    self.voice.setup_audio(self.settings.input_device_index,
                                           self.settings.output_device_index)
                except Exception as e:
                    QMessageBox.warning(self, t("status.audio_device"), t("status.audio_failed", error=e))
            # 换了输入设备或切了降噪，且新状态下没校准过，就再提示一次
            QTimer.singleShot(0, self.maybe_prompt_calibration)

    def closeEvent(self, event):
        try:
            if hasattr(self, 'ptt_watcher'):
                self.ptt_watcher.stop()
            if self.voice:
                self.voice.disconnect()
        except Exception as e:
            log.warning(f"error while closing: {e}")
        finally:
            event.accept()


if __name__ == '__main__':
    # --debug 会把协议层面的细节也记进日志（进出的频道、发话目标、断线判定）。
    # 设置里的开关是给拿不到命令行的用户准备的，两者任一打开即生效。
    debug = '--debug' in sys.argv or Settings().debug
    applog.setup(debug=debug)
    # 版本号必须进日志：用户发上来的日志是唯一能确认他跑的是哪个 build 的东西
    log.info("Audio for CAN starting, %s", version.full())

    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(icon_path))
    apply_theme()
    window = ControllerWindow()
    window.show()
    sys.exit(app.exec())
