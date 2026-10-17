import logging
import json
import os

from PyQt6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout
from qfluentwidgets import (BodyLabel, CaptionLabel, CheckBox, ComboBox, FluentIcon,
                            LineEdit, PrimaryPushButton, PushButton)

import applog
import atisrouting
import datafeed
import i18n
import netconfig
import theme
import weather
from i18n import t

log = logging.getLogger("settings")

DEFAULT_FSD_HOST = "fsd.ceruleanavi.net"
DEFAULT_FSD_PORT = 6809

# 早期版本把 FSD 主机错填成了语音服务器的地址。那台机器上没有 FSD，
# 留着只会一直连不上，所以读配置时直接换掉。语音服务器历来的三个域名都要认：
# 前两个还留在老配置里，最后一个是同一个人下次还会填错的那个。
WRONG_FSD_HOSTS = {"hjdczy.top", "audio.airwaysn.org", "audio.ceruleanavi.net"}

# 停用域名上的旧地址。这些是**存进配置文件**的，所以光换上面和 datafeed.py /
# netconfig.py 里的默认值只对全新安装有效——老用户的 json 里还是 airwaysn.org，
# 而那个域已经整个不解析了，通播于是取不到席位配置也取不到在线数据，界面上那
# 几行却看着完全正常。读配置时按原值换掉；只认这几个旧值，用户自己指到别处
# （内网镜像、测试服）是有意为之，不能动。
OLD_FSD_HOSTS = {"fsd.airwaysn.org"}
OLD_DATAFEED_URLS = {"https://data.airwaysn.org/v1/data.json"}
OLD_CONFIG_URLS = {"https://airwaysn.org/api/v1/atis/config"}

# 自动刷新天气的间隔（秒）
DEFAULT_METAR_REFRESH = 300
MIN_METAR_REFRESH = 60
MAX_METAR_REFRESH = 3600


def clamp_refresh(value):
    """刷新间隔夹到合理范围。

    配置被改成 0 或负数会让定时器疯转，把气象源打死然后被封；改成一天一次又
    等于没有自动更新。两头都夹住。
    """
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return DEFAULT_METAR_REFRESH
    return max(MIN_METAR_REFRESH, min(MAX_METAR_REFRESH, seconds))


class Settings:
    def __init__(self):
        self.config_file = "atis_settings.json"
        self.cid = ""
        self.metar_url = weather.DEFAULT_METAR_URL
        # FSD 服务端：席位靠它出现在网络上，气象也从它那里要。
        # 和语音服务器（audio.ceruleanavi.net:64738）不是同一台。
        self.fsd_host = DEFAULT_FSD_HOST
        self.fsd_port = DEFAULT_FSD_PORT
        self.real_name = ""
        self.connect_fsd = True
        # FSD 登录用的等级。0 表示自动：登录前从数据源查本人此刻的等级，
        # 这样通播和管制席位显示的等级一致；查不到就退回 OBS。
        self.rating = 0
        self.datafeed_url = datafeed.DEFAULT_DATAFEED_URL
        self.atis_range_nm = atisrouting.DEFAULT_RANGE_NM
        # 全网通播配置的地址（can-web 的 /api/v1/atis/config），和上面那个
        # 数据源不是一回事：数据源说的是此刻谁在播，这个给的是配置本身。
        self.config_url = netconfig.DEFAULT_CONFIG_URL
        # 上次并进来的那份网络配置的版本（服务端算的内容哈希）。记下来是为了
        # 能回答"已经是最新的了"，而不是每次都让人再看一遍完整的差异。
        self.config_version = ""
        # 自动刷新天气的间隔（秒）。METAR 半小时一发，5 分钟查一次足够及时，
        # 又不至于把气象源打太狠。夹在 1 分钟到 1 小时之间。
        self.metar_refresh = DEFAULT_METAR_REFRESH
        # 精简模式：只留席位列表。值班时窗口压在别的东西旁边用，要记住
        self.compact = False
        # 窗口置顶。和精简是一对，同样要记住
        self.always_on_top = False
        # 界面语言。空字符串表示"还没选过"，第一次启动跟系统走。
        # 注意这是**操作界面**的语言，和每个席位的 voice_language（通播稿播出去
        # 用哪种语言）没有关系——英文界面的操作者照样可能在管一份中文通播。
        self.language = ""
        # 更新检查：启动时问一次 can 有没有新版，装不装由用户决定。
        # skipped_version 记住"这一版我不要"，免得每次启动再问一遍。
        self.update_check = True
        self.skipped_version = ""
        self.update_url = ""
        self.debug = False
        self.load_settings()

    def load_settings(self):
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.cid = data.get("cid", "")
                self.metar_url = data.get("metar_url") or weather.DEFAULT_METAR_URL
                self.fsd_host = data.get("fsd_host") or DEFAULT_FSD_HOST
                if (self.fsd_host or "").strip().lower() in WRONG_FSD_HOSTS:
                    log.warning("the FSD host %s in the settings is the voice server, "
                                "using %s instead", self.fsd_host,
                                DEFAULT_FSD_HOST)
                    self.fsd_host = DEFAULT_FSD_HOST
                elif (self.fsd_host or "").strip().lower() in OLD_FSD_HOSTS:
                    log.info("the FSD server was renamed, using %s instead of %s",
                             DEFAULT_FSD_HOST, self.fsd_host)
                    self.fsd_host = DEFAULT_FSD_HOST
                self.fsd_port = int(data.get("fsd_port") or DEFAULT_FSD_PORT)
                self.real_name = data.get("real_name", "")
                self.connect_fsd = bool(data.get("connect_fsd", True))
                self.rating = int(data.get("rating") or 0)
                self.datafeed_url = (data.get("datafeed_url")
                                     or datafeed.DEFAULT_DATAFEED_URL)
                if self.datafeed_url.strip() in OLD_DATAFEED_URLS:
                    log.info("the datafeed moved, using %s instead of %s",
                             datafeed.DEFAULT_DATAFEED_URL, self.datafeed_url)
                    self.datafeed_url = datafeed.DEFAULT_DATAFEED_URL
                self.config_url = (data.get("config_url")
                                   or netconfig.DEFAULT_CONFIG_URL)
                if self.config_url.strip() in OLD_CONFIG_URLS:
                    log.info("the configuration endpoint moved, using %s instead of %s",
                             netconfig.DEFAULT_CONFIG_URL, self.config_url)
                    self.config_url = netconfig.DEFAULT_CONFIG_URL
                self.config_version = str(data.get("config_version") or "")
                self.atis_range_nm = data.get("atis_range_nm", atisrouting.DEFAULT_RANGE_NM)
                self.metar_refresh = clamp_refresh(
                    data.get("metar_refresh", DEFAULT_METAR_REFRESH))
                self.compact = bool(data.get("compact", False))
                self.always_on_top = bool(data.get("always_on_top", False))
                self.language = data.get("language", "") or ""
                self.update_check = bool(data.get("update_check", True))
                self.skipped_version = str(data.get("skipped_version") or "")
                self.update_url = str(data.get("update_url") or "")
                self.debug = bool(data.get("debug", False))
        except Exception as e:
            log.warning(f"could not load the settings: {e}")

    def save_settings(self):
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump({
                    "cid": self.cid,
                    "metar_url": self.metar_url,
                    "fsd_host": self.fsd_host,
                    "fsd_port": self.fsd_port,
                    "real_name": self.real_name,
                    "connect_fsd": self.connect_fsd,
                    "rating": self.rating,
                    "datafeed_url": self.datafeed_url,
                    "atis_range_nm": self.atis_range_nm,
                    "config_url": self.config_url,
                    "config_version": self.config_version,
                    "metar_refresh": self.metar_refresh,
                    "compact": self.compact,
                    "always_on_top": self.always_on_top,
                    "language": self.language,
                    "update_check": self.update_check,
                    "skipped_version": self.skipped_version,
                    "update_url": self.update_url,
                    "debug": self.debug,
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning(f"could not save the settings: {e}")


def _find_data(combo, value):
    """qfluentwidgets 的 ComboBox 没有 findData()。"""
    for i in range(combo.count()):
        if combo.itemData(i) == value:
            return i
    return -1


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle(t("settings.title"))
        # QDialog 不吃 Fluent 主题，不铺底色的话，深色界面上会弹出一个纯白的框
        self.setStyleSheet(theme.dialog_qss())
        self.setMinimumWidth(460)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)

        self.connect_fsd = CheckBox(t("settings.connect_fsd")
                                    + t("settings.connect_fsd_note"))
        self.connect_fsd.setChecked(self.settings.connect_fsd)
        layout.addWidget(self.connect_fsd)

        fsd_row = QHBoxLayout()
        self.fsd_host_input = LineEdit()
        self.fsd_host_input.setText(self.settings.fsd_host)
        self.fsd_port_input = LineEdit()
        self.fsd_port_input.setText(str(self.settings.fsd_port))
        self.fsd_port_input.setFixedWidth(70)
        fsd_row.addWidget(BodyLabel(t("settings.fsd_host")))
        fsd_row.addWidget(self.fsd_host_input)
        fsd_row.addWidget(BodyLabel(t("settings.fsd_port")))
        fsd_row.addWidget(self.fsd_port_input)
        layout.addLayout(fsd_row)

        refresh_row = QHBoxLayout()
        self.refresh_input = ComboBox()
        for seconds, key in ((60, "refresh.1m"), (120, "refresh.2m"),
                             (300, "refresh.5m"), (600, "refresh.10m"),
                             (900, "refresh.15m"), (1800, "refresh.30m"),
                             (3600, "refresh.60m")):
            self.refresh_input.addItem(t(key), userData=seconds)
        index = _find_data(self.refresh_input,
                           clamp_refresh(getattr(self.settings, "metar_refresh",
                                                 DEFAULT_METAR_REFRESH)))
        self.refresh_input.setCurrentIndex(index if index >= 0 else 2)
        refresh_row.addWidget(BodyLabel(t("settings.refresh")))
        refresh_row.addWidget(self.refresh_input)
        refresh_row.addWidget(CaptionLabel(t("settings.refresh_hint")))
        refresh_row.addStretch()
        layout.addLayout(refresh_row)

        rating_row = QHBoxLayout()
        self.rating_input = ComboBox()
        # 等级名（S1/C1/I3 …）是 FSD 的术语，两种语言里都是这么写的，不进 i18n
        for value, label in ((0, t("rating.auto")), (1, t("rating.obs")),
                             (2, "S1"), (3, "S2"), (4, "S3"),
                             (5, "C1"), (7, "C3"), (8, "I1"), (10, "I3")):
            self.rating_input.addItem(label if value == 0 else f"{value}  {label}",
                                      userData=value)
        index = _find_data(self.rating_input, self.settings.rating)
        self.rating_input.setCurrentIndex(index if index >= 0 else 0)
        rating_row.addWidget(BodyLabel(t("settings.rating")))
        rating_row.addWidget(self.rating_input)
        rating_row.addWidget(CaptionLabel(t("settings.rating_hint")))
        layout.addLayout(rating_row)

        name_row = QHBoxLayout()
        self.real_name_input = LineEdit()
        self.real_name_input.setText(self.settings.real_name)
        self.real_name_input.setPlaceholderText(t("settings.real_name_hint"))
        name_row.addWidget(BodyLabel(t("settings.real_name")))
        name_row.addWidget(self.real_name_input)
        layout.addLayout(name_row)

        row = QHBoxLayout()
        self.metar_input = LineEdit()
        self.metar_input.setText(self.settings.metar_url)
        self.metar_input.setPlaceholderText(weather.DEFAULT_METAR_URL)
        row.addWidget(BodyLabel(t("settings.weather_source")))
        row.addWidget(self.metar_input)
        layout.addLayout(row)

        hint = CaptionLabel(t("settings.weather_hint"))
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.IDLE_COLOR};")
        layout.addWidget(hint)

        language_row = QHBoxLayout()
        self.language_input = ComboBox()
        for code, name in i18n.available().items():
            self.language_input.addItem(name, userData=code)
        index = _find_data(self.language_input, i18n.current())
        if index >= 0:
            self.language_input.setCurrentIndex(index)
        language_row.addWidget(BodyLabel(t("settings.language")))
        language_row.addWidget(self.language_input)
        language_row.addStretch()
        layout.addLayout(language_row)

        # 日志：出问题时让用户能一键找到文件，而不是去解释路径
        log_row = QHBoxLayout()
        self.debug_checkbox = CheckBox(t("settings.debug"))
        self.debug_checkbox.setChecked(self.settings.debug)
        self.debug_checkbox.setToolTip(t("settings.debug_tip"))
        open_log = PushButton(FluentIcon.FOLDER, t("settings.open_log"))
        open_log.clicked.connect(lambda: applog.open_log_folder())
        log_row.addWidget(self.debug_checkbox)
        log_row.addStretch()
        log_row.addWidget(open_log)
        layout.addLayout(log_row)

        buttons = QHBoxLayout()
        save = PrimaryPushButton(t("common.save"))
        save.clicked.connect(self.save_and_close)
        cancel = PushButton(t("common.cancel"))
        cancel.clicked.connect(self.reject)
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def save_and_close(self):
        self.settings.metar_url = (self.metar_input.text().strip()
                                   or weather.DEFAULT_METAR_URL)
        self.settings.metar_refresh = clamp_refresh(
            self.refresh_input.currentData())
        self.settings.connect_fsd = self.connect_fsd.isChecked()
        self.settings.fsd_host = self.fsd_host_input.text().strip() or DEFAULT_FSD_HOST
        try:
            self.settings.fsd_port = int(self.fsd_port_input.text().strip())
        except ValueError:
            self.settings.fsd_port = DEFAULT_FSD_PORT
        self.settings.real_name = self.real_name_input.text().strip()
        self.settings.rating = self.rating_input.currentData()
        self.settings.debug = self.debug_checkbox.isChecked()
        self.settings.language = self.language_input.currentData()
        i18n.set_language(self.settings.language)
        self.settings.save_settings()
        self.accept()
