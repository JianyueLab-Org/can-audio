"""情报通播客户端。

界面按 vATIS 组织：左边是席位列表，选中一个席位后可以挑预设、看生成出来的
文字通播和语音稿，然后连上语音服务器循环播出。

    席位 (Station)   一个机场的通播：ICAO、频率、类型、情报字母范围
    预设 (Preset)    一份模板 + 机场条件 + NOTAM，随跑道构型切换
    模板             [WIND] [CLOUDS] 这类变量，生成时替换成实际天气

天气来自 METAR，报文变了就自动推进一格情报字母。
"""

import logging
import os
import sys
import threading

from PyQt6.QtCore import Qt, QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QIcon
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QListWidgetItem,
                             QMessageBox, QDialog, QSplitter, QFileDialog,
                             QSizePolicy)
from qfluentwidgets import (BodyLabel, CaptionLabel, ComboBox, FluentIcon,
                            LineEdit, ListWidget, PasswordLineEdit,
                            PrimaryPushButton, PushButton, StrongBodyLabel,
                            SubtitleLabel, TextEdit, TogglePushButton)

import airports
import applog
import update
import version
import chinese
import datafeed
import metar as metar_module
import netconfig
import rules
import script
import template as template_module
import fsdclient
import vatis_import
import weather
from broadcast import Broadcaster
import profile as profile_module
from profile import (LANGUAGES, LANGUAGE_ENGLISH, Profile, Station,
                     TYPE_SUFFIX, language_label, type_label)
import i18n
import settings as settings_module
import theme
from i18n import t
from settings import Settings, SettingsDialog

log = logging.getLogger("gui")

# 语音（Mumble）服务器。FSD 是另一台，地址在设置里（fsd.ceruleanavi.net:6809）。
SERVER = "audio.ceruleanavi.net"

# 外观在 theme.py 里，四个客户端共用一份——它们多半并排开着，长得不一样会很跳。
# 这里只把用到的名字取出来，**不要在这个文件里再写死任何 #rrggbb**。
from theme import (ACTIVE_COLOR, IDLE_COLOR, MONO_FONT, MUTED_COLOR as OFF_COLOR,
                   ON_COLOR, THEME_COLOR, WINDOW_BG, apply_theme)


def resource_path(name):
    """找随程序一起分发的资源。

    打包之后当前目录是用户双击时所在的目录，不是程序目录，用相对路径取图标
    会取不到（Qt 不会报错，只是默默用默认图标）。PyInstaller 把 datas 解到
    sys._MEIPASS。
    """
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


icon_path = resource_path("favicon.ico")


class AtisSignals(QObject):
    """后台线程 → 界面线程。"""
    state = pyqtSignal(str, str, str)      # callsign, state, message
    metar = pyqtSignal(str, object, str)   # callsign, Metar 或 None, 错误
    # 开播前的核对结果：callsign, cid, password, 数据源是否可达, 管制席位, 等级
    precheck = pyqtSignal(str, str, str, bool, object, int)
    # 查更新在后台线程里做，结果要过信号回到界面线程
    update_found = pyqtSignal(object)


class StationDialog(QDialog):
    """新建 / 编辑一个通播席位。"""

    def __init__(self, station=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("station.title"))
        self.station = station
        self.setup_ui()
        if station:
            self.identifier.setText(station.identifier)
            self.name.setText(station.name)
            self.frequency.setText(station.frequency)
            self.atis_type.setCurrentIndex(
                list(TYPE_SUFFIX).index(station.atis_type))
            self.range_start.setText(station.code_range[0])
            self.range_end.setText(station.code_range[1])
            if station.latitude or station.longitude:
                self.latitude.setText(f"{station.latitude:.5f}")
                self.longitude.setText(f"{station.longitude:.5f}")
            index = self.language.findData(
                getattr(station, "voice_language", LANGUAGE_ENGLISH))
            self.language.setCurrentIndex(max(0, index))
            self.chinese_name.setText(getattr(station, "chinese_name", ""))
            self.chinese_runway.setText(getattr(station, "chinese_runway", ""))

    def setup_ui(self):
        layout = QVBoxLayout(self)

        self.identifier = LineEdit()
        self.identifier.setPlaceholderText(t("station.icao_hint"))
        self.name = LineEdit()
        self.name.setPlaceholderText(t("station.name_hint"))
        self.frequency = LineEdit()
        self.frequency.setPlaceholderText(t("station.frequency_hint"))

        self.atis_type = ComboBox()
        for key in TYPE_SUFFIX:
            self.atis_type.addItem(f"{type_label(key)}  {TYPE_SUFFIX[key]}", key)

        self.language = ComboBox()
        for key in LANGUAGES:
            self.language.addItem(language_label(key), key)

        self.chinese_name = LineEdit()
        self.chinese_name.setPlaceholderText(t("station.chinese_name_hint"))
        self.chinese_runway = LineEdit()
        self.chinese_runway.setPlaceholderText(t("station.chinese_runway_hint"))

        for label, widget in (('机场:', self.identifier), ('名称:', self.name),
                              ('频率:', self.frequency), ('类型:', self.atis_type),
                              ('语音:', self.language),
                              ('中文名:', self.chinese_name),
                              ('中文跑道:', self.chinese_runway)):
            row = QHBoxLayout()
            row.addWidget(BodyLabel(label))
            row.addWidget(widget)
            layout.addLayout(row)

        position_row = QHBoxLayout()
        self.latitude = LineEdit()
        self.latitude.setPlaceholderText(t("station.latitude_hint"))
        self.longitude = LineEdit()
        self.longitude.setPlaceholderText(t("station.longitude_hint"))
        position_row.addWidget(BodyLabel(t("station.position")))
        position_row.addWidget(self.latitude)
        position_row.addWidget(self.longitude)
        layout.addLayout(position_row)

        # 输入机场代码时自动带出坐标，省得手查
        self.identifier.textChanged.connect(self.fill_position_from_airport)

        position_hint = BodyLabel(t("station.position_hint"))
        position_hint.setStyleSheet(f"color: {IDLE_COLOR};")
        position_hint.setWordWrap(True)
        layout.addWidget(position_hint)

        range_row = QHBoxLayout()
        self.range_start = LineEdit()
        self.range_start.setText('A')
        self.range_start.setFixedWidth(40)
        self.range_end = LineEdit()
        self.range_end.setText('Z')
        self.range_end.setFixedWidth(40)
        range_row.addWidget(BodyLabel(t("station.range")))
        range_row.addWidget(self.range_start)
        range_row.addWidget(BodyLabel(t("common.to")))
        range_row.addWidget(self.range_end)
        range_row.addStretch()
        layout.addLayout(range_row)

        hint = BodyLabel(t("station.range_hint"))
        hint.setStyleSheet(f"color: {IDLE_COLOR};")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        ok = PrimaryPushButton(t("common.ok"))
        ok.clicked.connect(self.validate_and_accept)
        cancel = PushButton(t("common.cancel"))
        cancel.clicked.connect(self.reject)
        buttons.addWidget(ok)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    def fill_position_from_airport(self, text):
        """机场代码填完整了就自动带出坐标；用户已经手填过的不覆盖。"""
        if self.latitude.text().strip() or self.longitude.text().strip():
            return
        found = airports.coordinates(text)
        if found:
            self.latitude.setText(f"{found[0]:.5f}")
            self.longitude.setText(f"{found[1]:.5f}")

    def validate_and_accept(self):
        import re
        if not re.match(r'^[A-Za-z]{4}$', self.identifier.text().strip()):
            QMessageBox.warning(self, t("station.bad_input"), t("station.bad_icao"))
            return
        try:
            khz = int(round(float(self.frequency.text().strip()) * 1000))
            if not 100000 <= khz <= 199999:
                raise ValueError
        except ValueError:
            QMessageBox.warning(self, t("station.bad_input"), t("station.bad_frequency"))
            return
        self.accept()

    def build(self):
        def coordinate(widget):
            try:
                return float(widget.text().strip())
            except ValueError:
                return 0.0

        return Station(
            self.identifier.text().strip(),
            self.name.text().strip(),
            self.frequency.text().strip(),
            self.atis_type.currentData(),
            (self.range_start.text().strip().upper()[:1] or 'A',
             self.range_end.text().strip().upper()[:1] or 'Z'),
            self.station.presets if self.station else None,
            self.station.contractions if self.station else None,
            coordinate(self.latitude),
            coordinate(self.longitude),
            self.language.currentData(),
            self.chinese_name.text().strip(),
            self.chinese_runway.text().strip(),
        )


class AtisWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowIcon(QIcon(icon_path))
        # 产品名和 XPC for CAN / MSFS for CAN 对齐，四个客户端一套命名
        self.setWindowTitle(f'ATIS for CAN {version.full()}')
        self.setMinimumSize(900, 600)

        self.settings = Settings()
        # 界面语言：设置里选过就用选的，没选过跟系统走。必须在建控件之前定下来
        # ——控件的文字是构造时取的，之后再切只会影响新建的那些。
        i18n.set_language(self.settings.language or i18n.system_language())
        # **一定要带路径。** 不带 path 的 Profile 是内存里的一份，不读也不存——
        # 界面拿它当配置用的话，打开就是空席位列表，加了席位也一个都存不下来。
        self.profile = Profile(path=profile_module.DEFAULT_PROFILE_PATH)
        self.broadcasters = {}          # callsign -> Broadcaster（语音）
        self.fsd_clients = {}           # callsign -> FSDClient（网络在线与文字通播）
        self.metars = {}                # callsign -> Metar
        self.raw_metars = {}            # callsign -> 原始电码，用来判断变没变
        self._weather_errors = {}       # callsign -> 还没恢复的取天气错误

        self.signals = AtisSignals()
        self.signals.state.connect(self.on_broadcast_state)
        self.signals.metar.connect(self.on_metar)
        self.signals.precheck.connect(self.on_precheck)
        self.signals.update_found.connect(self.on_update_found)

        self.setup_ui()
        self.refresh_stations()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_all_metars)
        self.apply_refresh_interval()

        # 上次开着置顶 / 精简的话，这次起来就还是那样
        if self.settings.always_on_top:
            self.toggle_always_on_top(True)
        if self.settings.compact:
            self.toggle_compact(True)

        # 起来之后在后台问一次有没有新版。查不到就当没这回事。
        self.check_for_update()

    # ---------- 界面 ----------
    def setup_ui(self):
        # 主题只管 qfluentwidgets 的控件，普通 QWidget 的底色还得自己给，
        # 否则深色控件会浮在一片白底上。和管制端同一套写法。
        self.setStyleSheet(
            f"QMainWindow {{ background-color: {WINDOW_BG}; }}"
            f"QWidget#page {{ background-color: {WINDOW_BG}; }}"
            f"QSplitter {{ background-color: {WINDOW_BG}; }}")

        central = QWidget()
        central.setObjectName("page")
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # 顶栏包一层：精简模式要整条藏起来，而 QHBoxLayout 没有 setVisible。
        # **纵向必须钉成 Fixed。** 裸 QWidget 默认是 Preferred，会跟着窗口一起
        # 长——多出来的高度全被这一条吃掉，账号输入框浮在窗口正中间，底下的
        # 分割器反被挤扁。原来用 addLayout 时没这问题，布局本身不占额外空间。
        self.top_bar = QWidget()
        self.top_bar.setSizePolicy(QSizePolicy.Policy.Preferred,
                                   QSizePolicy.Policy.Fixed)
        top = QHBoxLayout(self.top_bar)
        top.setContentsMargins(0, 0, 0, 0)
        self.cid_input = LineEdit()
        self.cid_input.setText(self.settings.cid)
        self.cid_input.setPlaceholderText(t("main.username"))
        self.cid_input.setFixedWidth(110)
        # 专门的密码框：自带那个"看一眼"的小眼睛，比手设 EchoMode 好用
        self.password_input = PasswordLineEdit()
        self.password_input.setPlaceholderText(t("main.password"))
        self.password_input.setFixedWidth(160)
        top.addWidget(BodyLabel(t("main.account")))
        top.addWidget(self.cid_input)
        top.addWidget(self.password_input)
        top.addStretch()
        update_button = PushButton(t("update.check"))
        update_button.setIcon(FluentIcon.UPDATE)
        update_button.setToolTip(t("update.check_tip"))
        update_button.clicked.connect(lambda: self.check_for_update(manual=True))
        top.addWidget(update_button)
        settings_button = PushButton(t("main.settings"))
        settings_button.setIcon(FluentIcon.SETTING)
        settings_button.clicked.connect(self.open_settings)
        top.addWidget(settings_button)
        layout.addWidget(self.top_bar)

        self.splitter = splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左：席位列表
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)

        # 标题那一行带着精简开关。开关必须留在精简模式下也看得见的地方，
        # 否则收起来之后没有任何路径能切回去。
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(StrongBodyLabel(t("main.stations")))
        header.addStretch()

        # 置顶。和精简是一对：值班时窗口压在 vATIS / 雷达上面用，被盖住就等于
        # 看不见情报字母推进了没有。和精简一样放在这一行——顶栏在精简模式下整条
        # 藏起来，放那儿就没了。
        self.pin_button = TogglePushButton(t("main.pin"))
        self.pin_button.setIcon(FluentIcon.PIN)
        self.pin_button.setToolTip(t("main.pin_tip"))
        self.pin_button.setChecked(self.settings.always_on_top)
        self.pin_button.clicked.connect(self.toggle_always_on_top)
        header.addWidget(self.pin_button)

        self.compact_button = TogglePushButton(t("main.compact"))
        self.compact_button.setIcon(FluentIcon.MINIMIZE)
        self.compact_button.setToolTip(t("main.compact_tip"))
        self.compact_button.setChecked(self.settings.compact)
        self.compact_button.clicked.connect(self.toggle_compact)
        header.addWidget(self.compact_button)
        left_layout.addLayout(header)

        self.station_list = ListWidget()
        self.station_list.currentItemChanged.connect(lambda *_: self.on_station_selected())
        left_layout.addWidget(self.station_list)

        # 新建/编辑/删除/导入包一层，精简模式下整块收起来。同样要钉成 Fixed，
        # 否则它会跟席位列表抢纵向空间
        self.station_buttons = QWidget()
        self.station_buttons.setSizePolicy(QSizePolicy.Policy.Preferred,
                                           QSizePolicy.Policy.Fixed)
        buttons_layout = QVBoxLayout(self.station_buttons)
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        buttons = QHBoxLayout()
        for text, handler in (('新建', self.add_station), ('编辑', self.edit_station),
                              ('删除', self.remove_station)):
            button = PushButton(text)
            button.clicked.connect(handler)
            buttons.addWidget(button)
        buttons_layout.addLayout(buttons)

        # 两个按钮看着像，取的是两样东西：上面那个取的是**配置**（席位、频率、
        # 跑道构型预设、模板、中文用词），下面那个取的是**此刻谁在播**（只有
        # 机场和频率）。文案上必须分得开，不然会有人按错了还以为配置没更新。
        config_button = PushButton(t("main.net_config"))
        config_button.setIcon(FluentIcon.CLOUD_DOWNLOAD)
        config_button.setToolTip(
            '从 can 取全网通播配置：席位、频率、跑道构型预设、模板和中文'
            '播报用词。本地已有的席位默认不动，可以选择用网络版覆盖。')
        config_button.clicked.connect(self.update_from_network)
        buttons_layout.addWidget(config_button)

        network_button = PushButton(t("main.net_stations"))
        network_button.setIcon(FluentIcon.PEOPLE)
        network_button.setToolTip(
            '读 can-fsd 数据源上此刻在线的通播席位，把机场和频率加进来。'
            '这是运行状态不是配置——模板和预设要么自己填，要么用上面那个按钮。')
        network_button.clicked.connect(self.import_from_network)
        buttons_layout.addWidget(network_button)

        import_button = PushButton(t("main.import_vatis"))
        import_button.setToolTip(t("main.import_vatis_tip"))
        import_button.clicked.connect(self.import_vatis)
        buttons_layout.addWidget(import_button)
        left_layout.addWidget(self.station_buttons)
        splitter.addWidget(left)

        # 右：预设与通播内容
        self.right_panel = right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        preset_row = QHBoxLayout()
        self.preset_combo = ComboBox()
        self.preset_combo.currentIndexChanged.connect(lambda *_: self.regenerate())
        preset_row.addWidget(BodyLabel(t("main.preset")))
        preset_row.addWidget(self.preset_combo, 1)
        edit_preset = PushButton(t("main.edit_preset"))
        edit_preset.clicked.connect(self.edit_preset)
        preset_row.addWidget(edit_preset)
        right_layout.addLayout(preset_row)

        letter_row = QHBoxLayout()
        self.letter_label = SubtitleLabel(t("main.letter_none"))
        letter_row.addWidget(self.letter_label)
        letter_row.addStretch()
        advance = PushButton(t("main.advance"))
        advance.setIcon(FluentIcon.CHEVRON_RIGHT)
        advance.clicked.connect(self.advance_letter)
        letter_row.addWidget(advance)
        fetch = PushButton(t("main.refresh_weather"))
        fetch.setIcon(FluentIcon.SYNC)
        fetch.clicked.connect(lambda: self.refresh_metar(self.current_station()))
        letter_row.addWidget(fetch)
        right_layout.addLayout(letter_row)

        self.metar_label = CaptionLabel('METAR: --')
        self.metar_label.setWordWrap(True)
        self.metar_label.setStyleSheet(f"color: {IDLE_COLOR}; font-family: {MONO_FONT}, monospace;")
        right_layout.addWidget(self.metar_label)

        right_layout.addWidget(StrongBodyLabel(t("main.text_atis")))
        self.text_preview = TextEdit()
        self.text_preview.setReadOnly(True)
        self.text_preview.setFixedHeight(90)
        right_layout.addWidget(self.text_preview)

        right_layout.addWidget(StrongBodyLabel(t("main.voice_script")))
        self.voice_preview = TextEdit()
        self.voice_preview.setReadOnly(True)
        right_layout.addWidget(self.voice_preview)

        broadcast_row = QHBoxLayout()
        self.broadcast_button = PrimaryPushButton(t("main.start"))
        self.broadcast_button.clicked.connect(self.toggle_broadcast)
        broadcast_row.addWidget(self.broadcast_button)
        self.status_label = CaptionLabel(t("main.idle"))
        self.status_label.setStyleSheet(f"color: {IDLE_COLOR};")
        broadcast_row.addWidget(self.status_label, 1)
        right_layout.addLayout(broadcast_row)

        splitter.addWidget(right)
        splitter.setSizes([260, 640])
        # 伸缩权重给分割器：窗口拉高时长的应该是席位列表和稿子预览，
        # 不是顶栏那一条
        layout.addWidget(splitter, 1)

    # ---------- 置顶 ----------
    def toggle_always_on_top(self, checked=None):
        """窗口保持在其它程序上面。

        Qt 改窗口标志会把窗口重建一次，show() 之后最大化状态会丢，所以要自己
        记住再还原——管制端那边踩过这个坑。
        """
        if checked is None:
            checked = self.pin_button.isChecked()
        checked = bool(checked)
        self.pin_button.setChecked(checked)
        self.settings.always_on_top = checked
        self.settings.save_settings()

        maximised = self.isMaximized()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, checked)
        if maximised:
            self.showMaximized()
        else:
            self.show()

    # ---------- 精简模式 ----------
    def toggle_compact(self, enabled=None):
        """只留席位列表，其余整块收起来。

        和管制端那个精简是一回事：值班时真正要扫的就是「哪个场、第几份、风、
        修压」那几行，模板、预览、播出控件都是配置用的。收起来之后窗口能小到
        压在雷达或 vATIS 旁边。

        精简开关本身放在列表标题那一行，跟着列表一起留下——顶栏整条藏掉之后，
        它是唯一能切回去的路径。
        """
        if enabled is None:
            enabled = self.compact_button.isChecked()
        enabled = bool(enabled)
        self.compact_button.setChecked(enabled)

        self.top_bar.setVisible(not enabled)
        self.station_buttons.setVisible(not enabled)
        self.right_panel.setVisible(not enabled)

        if enabled:
            self.setMinimumSize(300, 220)
            self.resize(300, 320)
        else:
            self.setMinimumSize(0, 0)
            self.resize(max(self.width(), 900), max(self.height(), 560))
            self.splitter.setSizes([260, 640])

        self.settings.compact = enabled
        self.settings.save_settings()

    # ---------- 席位 ----------
    def current_station(self):
        item = self.station_list.currentItem()
        return self.profile.get(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def station_item_text(self, station):
        return script.summary(station, self.metars.get(station.callsign),
                              station.callsign in self.broadcasters)

    def update_station_labels(self):
        """只改列表文字，不重建、不动选中项。

        情报字母每次天气变化都会推进，而 refresh_stations() 会清空列表再填，
        顺带触发 on_station_selected()——那会把预设下拉框重置回第一项，用户
        选的构型就没了。天气每 5 分钟刷一次，选择被抢一次是不能接受的。
        """
        for row in range(self.station_list.count()):
            item = self.station_list.item(row)
            station = self.profile.get(item.data(Qt.ItemDataRole.UserRole))
            if station:
                item.setText(self.station_item_text(station))

    def refresh_stations(self):
        selected = self.station_list.currentItem()
        wanted = selected.data(Qt.ItemDataRole.UserRole) if selected else None

        self.station_list.clear()
        for station in self.profile:
            item = QListWidgetItem(self.station_item_text(station))
            item.setData(Qt.ItemDataRole.UserRole, station.callsign)
            self.station_list.addItem(item)
            if station.callsign == wanted:
                self.station_list.setCurrentItem(item)

        if self.station_list.currentRow() < 0 and self.station_list.count():
            self.station_list.setCurrentRow(0)
        self.on_station_selected()

    def update_metar_label(self):
        """METAR 那一行永远显示**当前选中席位**的报文。

        原来它只在 on_metar 里、且到货的正好是当前席位时才写一次——切席位不刷，
        于是屏幕上会出现"选中 ZBAA、右边渲染的是 ZBAA、METAR 那行却是 ZSHC"。
        看的人会以为渲染用错了天气，实际只是这一行没跟着走。
        """
        station = self.current_station()
        parsed = self.metars.get(station.callsign) if station else None
        if parsed is not None:
            self.metar_label.setText(f'METAR: {parsed.raw}')
        elif station is not None:
            self.metar_label.setText(t("main.metar_none"))
        else:
            self.metar_label.setText('METAR: -')

    def on_station_selected(self):
        station = self.current_station()
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        if station:
            for preset in station.presets:
                self.preset_combo.addItem(preset.name)
        self.preset_combo.blockSignals(False)

        broadcasting = bool(station and station.callsign in self.broadcasters)
        self.broadcast_button.setText(t("main.stop") if broadcasting else t("main.start"))
        self.broadcast_button.setEnabled(station is not None)
        self.update_metar_label()
        self.regenerate()

        if station and station.callsign not in self.metars:
            self.refresh_metar(station)

    def add_station(self):
        dialog = StationDialog(parent=self)
        if not dialog.exec():
            return
        try:
            station = self.profile.add(dialog.build())
        except ValueError as e:
            QMessageBox.warning(self, t("station.add_failed"), str(e))
            return
        self.profile.save()
        self.refresh_stations()
        self.refresh_metar(station)

    def edit_station(self):
        station = self.current_station()
        if not station:
            return
        dialog = StationDialog(station, parent=self)
        if not dialog.exec():
            return
        updated = dialog.build()
        if updated.callsign != station.callsign and self.profile.get(updated.callsign):
            QMessageBox.warning(self, t("station.edit_failed"),
                                t("station.duplicate", callsign=updated.callsign))
            return
        updated.letter = station.letter
        self.profile.remove(station.callsign)
        self.profile.add(updated)
        self.profile.save()
        self.refresh_stations()

    # ---------- 网络配置 ----------
    def update_from_network(self):
        """从 can-web 取全网通播配置，并进本地这一份。

        取的是**配置**：席位、频率、跑道构型预设、模板、中文播报用词，全套。
        和下面 import_from_network 取的东西不是一回事——那个读的是 can-fsd
        数据源里此刻谁在播，只有机场和频率，是运行状态。

        三件事必须由用户点头，所以这里是"先看差异，再动手"，不是按一下就变：

        - 只补缺是默认动作；覆盖本地已有的席位要单独问一次，因为值班时改过的
          临时构型和 NOTAM 都在预设里，一律盖掉等于把人家的活删了。
        - **正在播出的席位一律不动**，并在结果里说明。播出中的 Station 被
          Broadcaster 和 FSDClient 拿着，换掉它只会让在播内容和界面对不上。
        - 覆盖时保留本地当前的情报字母（netconfig.merge 负责），播了一半把
          字母退回 A，飞行员报的和听到的就对不上了。
        """
        self.status_label.setText(t("netconfig.fetching"))
        QApplication.processEvents()
        try:
            config = netconfig.parse(netconfig.fetch(self.settings.config_url))
        except netconfig.NetConfigError as e:
            self.status_label.setText(t("main.idle"))
            log.warning("could not fetch the network configuration: %s", e)
            QMessageBox.warning(self, t("netconfig.failed"), str(e))
            return
        self.status_label.setText(t("main.idle"))

        missing, differing, same = netconfig.compare(self.profile,
                                                     config.stations)
        header = [t("netconfig.header", label=config.label, count=len(config))]
        if config.notes:
            header.append(config.notes)
        if (self.settings.config_version
                and self.settings.config_version != config.version):
            header.append(t("netconfig.previous", version=self.settings.config_version))
        if config.problems:
            header.append(t("netconfig.problems", count=len(config.problems),
                                 details='；'.join(config.problems[:3])))
        header.append('')

        if not missing and not differing:
            self.settings.config_version = config.version
            self.settings.save_settings()
            QMessageBox.information(
                self, t("netconfig.up_to_date"),
                '\n'.join(header + [t("netconfig.same_note", count=len(same))]))
            return

        add_missing = False
        if missing:
            add_missing = QMessageBox.question(
                self, t("netconfig.ask_add"),
                '\n'.join(header + [
                    t("netconfig.missing", count=len(missing)),
                    '　' + self._station_names(missing),
                    '',
                    t("netconfig.add_note")])) == QMessageBox.StandardButton.Yes

        overwrite = False
        if differing:
            overwrite = QMessageBox.question(
                self, t("netconfig.ask_overwrite"),
                '\n'.join([
                    t("netconfig.differing", count=len(differing)),
                    '　' + self._station_names(differing),
                    '',
                    t("netconfig.overwrite_note")])
            ) == QMessageBox.StandardButton.Yes

        chosen = (missing if add_missing else []) + (differing if overwrite else [])
        if not chosen:
            self.status_label.setText(t("main.idle"))
            QMessageBox.information(self, t("netconfig.no_change"), t("netconfig.unchanged"))
            return

        added, replaced, _kept, skipped = netconfig.merge(
            self.profile, chosen, overwrite=overwrite,
            protected=set(self.broadcasters))

        self.profile.save()
        self.refresh_stations()
        for station in added:
            self.refresh_metar(station)
        # 只有整份都并进来了才算"更新到这一版"，否则下次还得让人再看一遍差异
        if not skipped and (not differing or overwrite):
            self.settings.config_version = config.version
            self.settings.save_settings()

        report = [t("netconfig.result", added=len(added), replaced=len(replaced))]
        if added:
            report.append(t("netconfig.added", names=self._station_names(added)))
        if replaced:
            report.append(t("netconfig.replaced", names=self._station_names(replaced)))
        if skipped:
            report.append('')
            report.append(t("netconfig.skipped_live", count=len(skipped),
                            names=self._station_names(skipped)))
            report.append(t("netconfig.retry_later"))
        QMessageBox.information(self, t("netconfig.updated"), '\n'.join(report))

    @staticmethod
    def _station_names(stations, limit=8):
        names = [station.label for station in stations[:limit]]
        if len(stations) > limit:
            names.append(t("netconfig.and_more", count=len(stations)))
        return '、'.join(names)

    def import_from_network(self):
        """把数据源上此刻在线的通播席位加进配置。

        取的是 can-fsd 数据源里的 `atis[]`——**此刻谁在播**，只有机场和频率，
        是运行状态而不是配置。配置走上面的 update_from_network（can-web 的
        /api/v1/atis/config）。

        所以这个动作省掉的只是查 ICAO 和频率：模板、预设、跑道构型它给不了。
        对话框里会把这点说清楚，别让人以为导完就能播。
        """
        self.status_label.setText(t("datafeed.fetching"))
        QApplication.processEvents()
        found = datafeed.atis_stations(self.settings.datafeed_url)
        self.status_label.setText(t("main.idle"))

        if not found:
            QMessageBox.information(
                self, '没有可导入的席位',
                '数据源上此刻没有在线的通播席位。\n\n'
                '这个功能读的是 can-fsd 数据源里正在播出的通播（atis[]），'
                '不是配置。要取全网配置请用「从网络更新配置」。')
            return

        added, skipped = [], []
        for icao, callsign, frequency, kind in found:
            if self.profile.get(callsign):
                skipped.append(f'{callsign} {frequency}')
                continue
            try:
                station = self.profile.add(Station(icao, frequency=frequency,
                                                   atis_type=kind))
            except ValueError as e:
                log.warning("importing %s failed: %s", callsign, e)
                continue
            added.append(f'{callsign} {frequency}')
            self.refresh_metar(station)

        self.profile.save()
        self.refresh_stations()

        report = [t("datafeed.imported", count=len(added))]
        if added:
            report.append('　' + '、'.join(added))
        if skipped:
            report.append(t("datafeed.skipped", count=len(skipped),
                            names='、'.join(skipped)))
        if added:
            report.append('')
            report.append(t("datafeed.note"))
        QMessageBox.information(self, t("import.done"), '\n'.join(report))

    def import_vatis(self):
        """从 vATIS 的 profile 里导入席位。"""
        path, _ = QFileDialog.getOpenFileName(
            self, '选择 vATIS 配置文件', '', 'vATIS 配置 (*.json);;所有文件 (*)')
        if not path:
            return

        try:
            profile_name, stations, notes = vatis_import.load_profile(path)
        except vatis_import.ImportError_ as e:
            QMessageBox.critical(self, t("import.failed"), str(e))
            return

        added, skipped = [], []
        for station in stations:
            if self.profile.get(station.callsign):
                skipped.append(station.callsign)
                continue
            self.profile.add(station)
            added.append(station.callsign)

        self.profile.save()
        self.refresh_stations()
        for station in stations:
            if station.callsign in added:
                self.refresh_metar(station)

        report = [t("import.result", source=profile_name or path, count=len(added))]
        if added:
            report.append('　' + '、'.join(added))
        if skipped:
            report.append(t("datafeed.skipped", count=len(skipped),
                            names='、'.join(skipped)))
        report.extend(notes)
        QMessageBox.information(self, t("import.done"), '\n'.join(report))

    def remove_station(self):
        station = self.current_station()
        if not station:
            return
        if station.callsign in self.broadcasters:
            QMessageBox.warning(self, t("station.cannot_delete"), t("station.stop_first"))
            return
        self.profile.remove(station.callsign)
        self.profile.save()
        self.refresh_stations()

    # ---------- 预设与生成 ----------
    def current_preset(self):
        station = self.current_station()
        if not station or not station.presets:
            return None
        index = max(0, self.preset_combo.currentIndex())
        return station.presets[min(index, len(station.presets) - 1)]

    def edit_preset(self):
        station = self.current_station()
        preset = self.current_preset()
        if not station or not preset:
            return
        dialog = PresetDialog(preset, parent=self)
        if dialog.exec():
            dialog.apply()
            self.profile.save()
            self.regenerate()

    def regenerate(self):
        """把当前席位 + 预设 + METAR 渲染出来，填进右边的预览。

        渲染本身在 script.py 里——预览和真正推出去的稿子必须是同一份代码算的，
        两边各写一遍的话，改岔了界面上根本看不出来。
        """
        station = self.current_station()
        preset = self.current_preset()
        if not station or not preset:
            self.text_preview.setPlainText('')
            self.voice_preview.setPlainText('')
            self.letter_label.setText(t("main.letter_none"))
            return

        self.letter_label.setText(t("main.letter", letter=station.letter))
        rendered = script.render(station, preset, self.metars.get(station.callsign))
        if rendered is None:
            self.text_preview.setPlainText('（还没有天气数据）')
            self.voice_preview.setPlainText('')
            return

        text, voice = rendered
        self.text_preview.setPlainText(text)
        self.voice_preview.setPlainText(voice)

        unknown = script.unknown_variables(preset)
        if unknown:
            self.status_label.setText(t("preset.unknown_vars", names=', '.join(unknown)))
        return text, voice

    # ---------- 天气 ----------
    def refresh_metar(self, station):
        if not station:
            return
        callsign, icao = station.callsign, station.identifier
        url = self.settings.metar_url
        signals = self.signals
        # 已经连上 FSD 就直接问自己的服务器要（$AX），它自带气象源和缓存
        fsd = self.fsd_clients.get(callsign)

        def worker():
            try:
                raw = None
                if fsd is not None and fsd.connected:
                    report = fsd.request_metar(icao)
                    raw = weather.normalize(report, icao) if report else None
                if raw is None:
                    raw = weather.fetch_metar(icao, url)
                signals.metar.emit(callsign, metar_module.Metar(raw), "")
            except weather.WeatherError as e:
                # 这条以前只进状态栏。打包是 console=False，用户报"取不到天气"
                # 时手里什么都没有，只能截图状态栏——日志里必须留一份
                log.warning("fetching the weather for %s failed: %s", callsign, e)
                signals.metar.emit(callsign, None, str(e))
            except Exception as e:
                log.warning("fetching the weather for %s raised: %s", callsign, e,
                            exc_info=True)
                signals.metar.emit(callsign, None, t("weather.failed", error=e))

        threading.Thread(target=worker, daemon=True).start()

    def apply_refresh_interval(self):
        """按设置重开定时器。设置改完要调一次，否则新间隔下次重启才生效。"""
        seconds = settings_module.clamp_refresh(
            getattr(self.settings, "metar_refresh",
                    settings_module.DEFAULT_METAR_REFRESH))
        self.timer.start(seconds * 1000)
        log.info("weather auto-refresh interval is %d s", seconds)
        return seconds

    def refresh_all_metars(self):
        for station in self.profile:
            self.refresh_metar(station)

    def on_metar(self, callsign, parsed, error):
        station = self.profile.get(callsign)
        if not station:
            return
        if error or parsed is None:
            message = error or '没有取到天气'
            self._weather_errors[callsign] = message
            self.status_label.setText(message)
            return

        # 这个席位取到了：把它之前那条错误撤掉。不撤的话，一次抖动留下的报错
        # 会一直挂在状态栏上——后面每次成功刷新都是静默的，没有谁会去改写它，
        # 于是天气早就正常了，界面还在喊失败。
        recovered = self._weather_errors.pop(callsign, None)

        changed = self.raw_metars.get(callsign) not in (None, parsed.raw)
        first = callsign not in self.raw_metars
        self.raw_metars[callsign] = parsed.raw
        self.metars[callsign] = parsed

        note = ''
        if changed:
            # 报文变了就换一格情报字母，这是 ATIS 的基本约定
            station.advance_letter()
            self.profile.save()
            note = t("weather.updated", callsign=callsign, letter=station.letter)
        elif recovered:
            note = t("weather.recovered", callsign=callsign)

        # 列表上那一行带着风和修压，每收到一份天气都要刷——不能只在 changed 时
        # 刷：**第一份**报文的 changed 是 False（之前没有旧值可比），那正是列表
        # 从空白变成有数据的那一次
        self.update_station_labels()

        # 还没恢复的错误优先于流水账。反过来的话，一个席位取不到天气这件事会
        # 被另一个席位的"天气更新"盖掉，再也没人看得见。
        outstanding = next(iter(self._weather_errors.values()), None)
        if outstanding or note:
            self.status_label.setText(outstanding or note)

        if station is self.current_station():
            self.update_metar_label()
            self.regenerate()

        # 已经在播的席位，换稿
        if (changed or first) and callsign in self.broadcasters:
            self.push_update(station)

    def push_update(self, station):
        """把新的通播稿推给语音和 FSD 两边。"""
        rendered = self.render_for(station)
        if not rendered:
            return
        text, voice = rendered
        broadcaster = self.broadcasters.get(station.callsign)
        if broadcaster:
            broadcaster.update_text(voice)
        fsd = self.fsd_clients.get(station.callsign)
        if fsd:
            fsd.set_atis_lines(fsdclient.wrap_atis_text(text))

    def render_for(self, station):
        """给某个席位渲染稿子。当前选中的那个用界面上选的预设，其余用第一个。"""
        preset = station.presets[0] if station.presets else None
        if station is self.current_station():
            preset = self.current_preset() or preset
        return script.render(station, preset, self.metars.get(station.callsign))

    def advance_letter(self):
        station = self.current_station()
        if not station:
            return
        station.advance_letter()
        self.profile.save()
        self.regenerate()
        self.update_station_labels()
        self.push_update(station)

    # ---------- 播出 ----------
    def toggle_broadcast(self):
        station = self.current_station()
        if not station:
            return
        if station.callsign in self.broadcasters:
            self.stop_broadcast(station.callsign)
            return

        cid = self.cid_input.text().strip()
        password = self.password_input.text()
        refused = rules.blocking_reason(
            station, self.profile, self.broadcasters,
            cid, password, self.render_for(station))
        if refused:
            QMessageBox.warning(self, *refused)
            return

        # 开播前先查一次数据源：确认本人确实在管制，顺便拿到等级。
        # 放后台线程做，界面不卡；结果经信号回来后才真正连接。
        self.broadcast_button.setEnabled(False)
        self.status_label.setText(t("duty.checking"))
        callsign = station.callsign
        url = self.settings.datafeed_url
        signals = self.signals

        def worker():
            data = datafeed.fetch(url)
            controller = datafeed.controller_for(cid, data=data) if data else None
            rating = datafeed.rating_for(cid, data=data) if data else None
            try:
                signals.precheck.emit(callsign, cid, password, data is not None,
                                      controller or {}, rating or 0)
            except RuntimeError:
                pass        # 窗口已经关了

        threading.Thread(target=worker, daemon=True).start()

    def on_precheck(self, callsign, cid, password, reachable, controller, rating):
        """数据源核对结果回来了（已在界面线程）。"""
        self.broadcast_button.setEnabled(True)
        self.status_label.setText(t("main.idle"))

        station = self.profile.get(callsign)
        if not station or callsign in self.broadcasters:
            return

        if not reachable:
            answer = QMessageBox.question(
                self, '无法核对管制席位',
                '连不上网络数据源，无法确认你此刻是否在管制。\n\n仍然开始播出吗？',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        elif not controller:
            # 没在管制就不该挂通播——否则会留下无人值守的通播
            QMessageBox.warning(self, t("duty.not_staffing"),
                                t("duty.not_online", cid=cid))
            return

        if controller:
            log.info("CID %s is staffing %s, letting the broadcast start",
                     cid, controller.get('callsign', '?'))

        self.start_broadcast(station, cid, password, rating)

    def start_broadcast(self, station, cid, password, rating=0):
        """核对通过之后真正建立两条连接。"""
        # 数据源核对后再次检查账号和播出稿，两条路径共用 rules.py。
        rendered = self.render_for(station)
        refused = rules.blocking_reason(
            station, self.profile, self.broadcasters, cid, password, rendered)
        if refused:
            QMessageBox.warning(self, *refused)
            return

        callsign = station.callsign

        # FSD 那条连接：让席位出现在网络上、回答文字通播查询、提供气象
        if self.settings.connect_fsd:
            problem = fsdclient.callsign_problem(callsign)
            if problem:
                answer = QMessageBox.question(
                    self, t("callsign.bad_title"),
                    t("duty.mismatch", problem=problem),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                if answer != QMessageBox.StandardButton.Yes:
                    return
            else:
                fsd = fsdclient.FSDClient(
                    self.settings.fsd_host, callsign, cid, password,
                    station.frequency,
                    real_name=self.settings.real_name or cid,
                    port=self.settings.fsd_port,
                    # 等级：设置里指定的优先，否则用刚才核对时查到的，
                    # 免得通播在雷达图上显示成观察员而管制席位是 C1
                    rating=self.settings.rating or rating,
                    latitude=station.latitude, longitude=station.longitude,
                    atis_lines=fsdclient.wrap_atis_text(rendered[0]),
                    # FSD 的错误单独标一个 state：它只是附加能力，登录失败
                    # 不该把频率上的语音通播一起停掉
                    # FSD 的错误单独标一个 state（见上）。'offline' 是另一回事：
                    # 那是重连三次都没成功，整条链路完了，席位该整个停播。
                    on_status=lambda state, message, _c=callsign:
                        self.signals.state.emit(
                            _c, 'fsd-error' if state == 'error' else state,
                            f"[FSD] {message}"))
                self.fsd_clients[callsign] = fsd
                fsd.start()

        broadcaster = Broadcaster(
            SERVER, cid, password, station,
            datafeed_url=self.settings.datafeed_url,
            atis_range_nm=self.settings.atis_range_nm,
            on_state=lambda state, message, _c=callsign:
                self.signals.state.emit(_c, state, message))
        self.broadcasters[callsign] = broadcaster

        self.settings.cid = cid
        self.settings.save_settings()

        broadcaster.start(rendered[1])
        self.refresh_stations()

    def stop_broadcast(self, callsign):
        broadcaster = self.broadcasters.pop(callsign, None)
        if broadcaster:
            try:
                broadcaster.stop()
            except Exception as e:
                log.warning(f"stopping the broadcast raised: {e}")
        fsd = self.fsd_clients.pop(callsign, None)
        if fsd:
            try:
                fsd.stop()
            except Exception as e:
                log.warning(f"disconnecting from FSD raised: {e}")
        self.status_label.setText(t("main.idle"))
        self.refresh_stations()

    def on_broadcast_state(self, callsign, state, message):
        if callsign not in self.broadcasters:
            return
        station = self.current_station()
        if station and station.callsign == callsign:
            self.status_label.setText(message)
            self.status_label.setStyleSheet(
                f"color: {OFF_COLOR};" if state in ('error', 'offline')
                else f"color: {ON_COLOR};" if state == 'online'
                else f"color: {ACTIVE_COLOR};" if state == 'reconnecting'
                else f"color: {IDLE_COLOR};")

        if state == 'reconnecting':
            # 还在重连：什么都不收。**尤其不能 stop_broadcast**——一次网络抖动
            # 就把值守中的席位停掉，等于把频率上的通播弄没了，而它本来几秒后
            # 就自己好了。
            return

        if state == 'offline':
            # 语音或 FSD 重连三次都没成功：这个席位整个停播，语音和 FSD 一起收。
            # **只停这一个席位**，客户端上其它席位各有自己的连接，不受影响。
            log.warning("%s could not reconnect, taking the whole station off "
                        "air: %s", callsign, message)
            self.stop_broadcast(callsign)
            self.status_label.setText(message)
            self.status_label.setStyleSheet(f"color: {OFF_COLOR};")
            QMessageBox.warning(self, t("broadcast.stopped_title", callsign=callsign), message)
            return

        if state == 'fsd-error':
            # 只收掉 FSD 这一条，语音继续播——席位不在网络上总比频率上没声音好
            fsd = self.fsd_clients.pop(callsign, None)
            if fsd:
                try:
                    fsd.stop()
                except Exception as e:
                    log.warning(f"stopping the FSD connection raised: {e}")
            self.status_label.setText(t("broadcast.voice_continues", message=message))
            self.status_label.setStyleSheet(f"color: {ACTIVE_COLOR};")
            log.warning("the FSD link of %s failed, voice continues: %s", callsign,
                        message)
        elif state == 'error':
            QMessageBox.critical(self, t("broadcast.error_title", callsign=callsign), message)
            self.stop_broadcast(callsign)

    # ---------- 其它 ----------
    # ---------- 更新 ----------
    def check_for_update(self, manual=False):
        """后台问一次有没有新版。

        `manual=True` 是用户自己点的按钮，那种情况下没有新版也要回一句，否则点了
        跟没点一样。启动时那次是静默的——值班的人不需要被"你已经是最新版"打断。
        """
        if not manual and not getattr(self.settings, 'update_check', True):
            return

        def work():
            found = update.check('atis-for-can', version.version(),
                                 getattr(self.settings, 'update_url', None))
            self.signals.update_found.emit(found or (manual and 'none' or None))

        threading.Thread(target=work, daemon=True).start()

    def on_update_found(self, found):
        """已经在界面线程。found 是 Update、'none'（手动查但没有新版）或 None。"""
        if found is None:
            return
        if found == 'none':
            QMessageBox.information(
                self, t("update.check"),
                t("update.current", version=version.version()))
            return
        if found.version == getattr(self.settings, 'skipped_version', ''):
            log.info("update %s was skipped by the user", found.version)
            return

        # **播出中不打断。** 弹一个模态框会挡住正在值班的席位列表，而通播是循环
        # 播出的，用户可能几个小时都不看这个窗口。有席位在播就只记一行日志，
        # 等他停播之后自己点「检查更新」。
        if self.broadcasters:
            log.info("update %s available, not prompting while %d station(s) "
                     "are on air", found.version, len(self.broadcasters))
            self.status_label.setText(t("update.status", version=found.version))
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
        if clicked is download and update.is_safe_url(found.download):
            QDesktopServices.openUrl(QUrl(found.download))
        elif clicked is notes and update.is_safe_url(found.notes):
            QDesktopServices.openUrl(QUrl(found.notes))
        elif clicked is skip:
            self.settings.skipped_version = found.version
            self.settings.save_settings()

    def open_settings(self):
        dialog = SettingsDialog(self.settings, self)
        dialog.exec()
        # 刷新间隔要立刻生效，不然改了得重启才算数
        self.apply_refresh_interval()

    def closeEvent(self, event):
        try:
            # 两个字典取并集：万一 FSD 连上了而语音没起来，也要收干净
            for callsign in set(self.broadcasters) | set(self.fsd_clients):
                self.stop_broadcast(callsign)
        except Exception as e:
            log.warning(f"error while closing: {e}")
        finally:
            event.accept()


class PresetDialog(QDialog):
    """编辑一份预设：模板、机场条件、NOTAM。"""

    def __init__(self, preset, parent=None):
        super().__init__(parent)
        self.preset = preset
        self.setWindowTitle(t("preset.title", name=preset.name))
        self.setMinimumSize(640, 520)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)

        name_row = QHBoxLayout()
        self.name = LineEdit()
        self.name.setText(self.preset.name)
        name_row.addWidget(BodyLabel(t("preset.name")))
        name_row.addWidget(self.name)
        layout.addLayout(name_row)

        layout.addWidget(BodyLabel(t("preset.template")))
        self.template = TextEdit()
        self.template.setPlainText(self.preset.template)
        layout.addWidget(self.template)

        variables = BodyLabel(t("preset.variables", names='  '.join(
            f'[{name}]' for name in sorted(set(template_module.ALIASES)))))
        variables.setWordWrap(True)
        variables.setStyleSheet(f"color: {IDLE_COLOR}; font-size: 11px;")
        layout.addWidget(variables)

        vox = BodyLabel(t("preset.vox_hint"))
        vox.setStyleSheet(f"color: {IDLE_COLOR};")
        vox.setWordWrap(True)
        layout.addWidget(vox)

        layout.addWidget(BodyLabel(t("preset.conditions")))
        self.conditions = TextEdit()
        self.conditions.setPlainText(self.preset.airport_conditions)
        self.conditions.setFixedHeight(60)
        layout.addWidget(self.conditions)

        layout.addWidget(BodyLabel('NOTAM  [NOTAMS]'))
        self.notams = TextEdit()
        self.notams.setPlainText(self.preset.notams)
        self.notams.setFixedHeight(60)
        layout.addWidget(self.notams)

        tl_row = QHBoxLayout()
        self.transition_level = LineEdit()
        self.transition_level.setText(self.preset.transition_level)
        self.transition_level.setPlaceholderText(t("preset.transition_hint"))
        tl_row.addWidget(BodyLabel(t("preset.transition")))
        tl_row.addWidget(self.transition_level)
        layout.addLayout(tl_row)

        buttons = QHBoxLayout()
        ok = PrimaryPushButton(t("common.save"))
        ok.clicked.connect(self.accept)
        cancel = PushButton(t("common.cancel"))
        cancel.clicked.connect(self.reject)
        buttons.addWidget(ok)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    def apply(self):
        self.preset.name = self.name.text().strip() or self.preset.name
        self.preset.template = self.template.toPlainText()
        self.preset.airport_conditions = self.conditions.toPlainText().strip()
        self.preset.notams = self.notams.toPlainText().strip()
        self.preset.transition_level = self.transition_level.text().strip()


def selftest():
    """检查这份构建能不能真的合成语音。

    pyttsx3 走 SAPI5，而 SAPI5 靠 comtypes 在运行时生成绑定代码——打包之后
    很容易因为找不到驱动或者写不了缓存目录而失灵，界面上却只表现为"通播没声
    音"。加个自检开关，装机之后先跑一次就能确认：

        atis-for-can.exe --selftest    退出码 0 表示语音可用
    """
    from broadcast import Synthesizer
    synth = Synthesizer()
    try:
        pcm = synth.synthesize("ZSPD information alpha, wind calm.")
        if not pcm:
            log.warning("self-test failed: speech synthesis produced no audio")
            return 1
        seconds = len(pcm) / (2.0 * 48000)
        log.info(f"self-test passed: synthesised {seconds:.1f} s of audio")
        return 0
    except Exception as e:
        log.warning(f"self-test failed: {e}")
        return 1
    finally:
        synth.cleanup()


if __name__ == '__main__':
    # --debug 会把协议细节也记进日志（FSD 收发的每个包、频道操作）。
    # 设置里的开关是给拿不到命令行的用户准备的，两者任一打开即生效。
    applog.setup(debug='--debug' in sys.argv or Settings().debug)
    log.info("ATIS for CAN starting, %s", version.full())

    if '--selftest' in sys.argv:
        sys.exit(selftest())

    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(icon_path))
    apply_theme()               # 必须在建窗口之前
    window = AtisWindow()
    window.show()
    sys.exit(app.exec())
