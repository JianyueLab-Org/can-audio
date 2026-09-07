"""配置，存成当前目录下的 xpc_settings.json。

**密码默认不再落盘。** 这里的 password 不是"这个客户端的密码"，它就是成员的
网站密码：can-api 的 `VerifyNetworkCredential` 对两个列都认，注册和改密写进去
的是同一个秘密。所以这个文件泄露一次，泄露的是整个账号——网站、FSD、语音一起。

而这个文件躺的地方偏偏最容易被端走：它写在**当前工作目录**，也就是用户双击
exe 的地方。X-Plane 的 Community 文件夹、会被云同步的游戏目录、报障时打包发过来
的那个 zip，都是常见的落点。

所以 `remember_password` 默认 False，`save()` 只在它为 True 时写 password。
老配置里已经有明文密码的，`load()` 会把它读进内存（这次运行照常能连，不会
突然登不上），然后立刻把文件重写掉、并把 `password_migrated` 置上让界面说一
声——形状照着下面 OLD_MUMBLE_HOSTS 那个迁移来：**认得出老样子，就地改掉，
并且说出来**。
"""

import json
import logging
import os

import ptt

log = logging.getLogger("settings")

SETTINGS_FILE = "xpc_settings.json"

MUMBLE_HOST = "audio.ceruleanavi.net"
FSD_HOST = "fsd.ceruleanavi.net"
FSD_PORT = 6809

# 语音服务器的旧域名。mumble_host 是存进配置文件的，所以光换上面那个默认值
# 只对全新安装有效——老用户的 json 里还是旧域名，等旧域名停掉那天他们看到的
# 是"连不上语音服务器"，而设置界面上那一行看着完全正常。读配置时换掉。
# 只认这几个旧值：用户自己填的别的地址是有意为之，不能动。
OLD_MUMBLE_HOSTS = {"hjdczy.top", "audio.airwaysn.org"}

# FSD 服务端同理，而且这一条现在就在发生：airwaysn.org 整个域已经不解析了，
# 老配置里的 fsd_host 不换掉就是连不上，报出来还是一个超时，看不出是域名的事。
OLD_FSD_HOSTS = {"fsd.airwaysn.org"}

DEFAULTS = {
    "cid": "",
    "password": "",
    # 把密码存进这个文件。**默认关**，理由见模块开头：这是成员的网站密码，
    # 而这个文件写在双击 exe 的那个目录里。想省事的人自己勾。
    "remember_password": False,
    "real_name": "",
    "callsign": "",
    "aircraft": "",
    "rating": 1,
    "mumble_host": MUMBLE_HOST,
    "fsd_host": FSD_HOST,
    "fsd_port": FSD_PORT,
    # PTT 现在是一串绑定（键盘 / 鼠标侧键 / 摇杆），任意一个按住即发话。
    # 老配置里的 ptt_key + joystick_ptt 两个字段读得进来，见 load()。
    "ptt_bindings": None,
    "ptt_key": "`",
    "joystick_ptt": None,
    # 界面语言。空字符串表示"还没选过"，第一次启动跟系统走
    "language": "",
    "debug": False,
    "input_device_index": None,
    "output_device_index": None,
    "mic_volume": 100,
    "speaker_volume": 100,
    # 收到管制消息时响一声。默认开：飞行员盯着的是窗外，消息区多一行没人看得见。
    # message_sound_all 打开的话频率上每条消息都响；默认只有私聊、以及正文里
    # 点到自己呼号的那种才响（见 chime.wants_alert）。
    "message_sound": True,
    "message_sound_all": False,
    "message_sound_volume": 100,
    "connect_fsd": True,
    "connect_voice": True,
    # 观察员模式（双人机组的右座）：只连语音，不连 FSD，网络上不会多一架
    # 飞机。observer_frequency 是手输的频率，留空就跟着 COM1 走。见 observer.py。
    "observer_mode": False,
    "observer_frequency": "",
    "flight_plan": {},
    # 他机渲染。csl_path 指向装好的 CSL 模型包所在目录（Bluebell 等）；
    # 留空就只送 TCAS，不画模型。
    "render_traffic": True,
    "csl_path": "",
    # 用户指定的 X-Plane 安装目录。留空就每次自动探测——只有探测不出来（绿色版、
    # 搬过目录）时才需要手工指，所以这里不预填。
    "xplane_path": "",
    "traffic_range_nm": 60,
    # 更新检查。启动时问一次 can 有没有新版；查到了也只是弹一句，
    # 装不装由用户决定。skipped_version 记住"这一版我不要"，免得每次
    # 启动再问一遍——那和自动更新一样烦人，只是烦得更频繁。
    "update_check": True,
    "skipped_version": "",
    "update_url": "",
}


def _clamp_volume(value, default=100):
    """夹到 0-200。0 是合法的（静音），None 和坏值才回默认。"""
    try:
        return max(0, min(200, int(value)))
    except (TypeError, ValueError):
        return default


class Settings:
    def __init__(self, path=SETTINGS_FILE):
        self.path = path
        # 这一次启动是不是刚把老配置里的明文密码清掉了。不进 DEFAULTS，
        # 所以不落盘——它描述的是本次启动，不是配置。界面拿它提示一次。
        self.password_migrated = False
        for key, value in DEFAULTS.items():
            setattr(self, key, value)
        self.load()
        if self.ptt_bindings is None:
            # 全新安装，连配置文件都还没有：用默认的 PTT 键起一条绑定
            self.ptt_bindings = ptt.load(None, legacy_key=self.ptt_key,
                                         legacy_joystick=self.joystick_ptt)

    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            log.warning("could not read the settings, using the defaults: %s", e)
            return
        for key in DEFAULTS:
            if key in data:
                setattr(self, key, data[key])
        if (self.mumble_host or "").strip().lower() in OLD_MUMBLE_HOSTS:
            log.info("the voice server was renamed, using %s instead of %s",
                     MUMBLE_HOST, self.mumble_host)
            self.mumble_host = MUMBLE_HOST
        if (self.fsd_host or "").strip().lower() in OLD_FSD_HOSTS:
            log.info("the FSD server was renamed, using %s instead of %s",
                     FSD_HOST, self.fsd_host)
            self.fsd_host = FSD_HOST
        # 音量存坏了会让整条音频链路失灵，夹一下
        # `or 100` 会把用户特意拉到 0 的静音在重启后悄悄变回 100（麦克风
        # 开着而用户不知道）；0 是滑条上真实可取的值，只有 None/坏值才回默认
        self.mic_volume = _clamp_volume(self.mic_volume)
        self.speaker_volume = _clamp_volume(self.speaker_volume)
        self.message_sound_volume = _clamp_volume(self.message_sound_volume)
        # 绑定表从 JSON 变成 ptt.Binding。老配置里没有 ptt_bindings，就拿
        # ptt_key + joystick_ptt 升上来——升不上来的话，用户原来设的 PTT 会在
        # 升级之后悄悄失效，而界面上一切正常，只是没人听得见。
        self.ptt_bindings = ptt.load(data.get("ptt_bindings"),
                                     legacy_key=self.ptt_key,
                                     legacy_joystick=self.joystick_ptt)
        log.info("read the settings from %s", os.path.abspath(self.path))
        self._migrate_stored_password(data)

    def _migrate_stored_password(self, data):
        """老版本存下来的明文密码：本次还能用，但从文件里清掉。

        认得出"老样子"的判据是 `remember_password` 这个键**不存在**——那只
        可能是加上这个开关之前的版本写的。用户自己关掉记住密码的文件里这个
        键是有的（值为 False），不会被反复当成待迁移。

        故意不做成"读到就删、下次自己重打"这么干脆：那等于悄悄把人锁在外面。
        密码留在内存里，这一次连接照常；文件当场重写；`password_migrated` 让
        界面说一句，用户可以当场再勾上"记住密码"。
        """
        if "remember_password" in data or not (self.password or "").strip():
            return
        self.remember_password = False
        self.password_migrated = True
        log.info("an older version had stored the network password in cleartext "
                 "in %s; it is being removed from the file. This session still "
                 "has it, and 'remember password' is off unless the user turns "
                 "it back on.", os.path.abspath(self.path))
        self.save()

    def save(self):
        data = {key: getattr(self, key, DEFAULTS[key]) for key in DEFAULTS}
        data["ptt_bindings"] = ptt.dump(self.ptt_bindings)
        # 没勾"记住密码"就不写。写空串而不是把键删掉：老版本读到空串是"没存
        # 过密码"，读不到这个键也一样，但留着键能让上面那个迁移判据只认
        # remember_password，不至于把用户自己清空的密码又当成待迁移。
        if not self.remember_password:
            data["password"] = ""
        # ptt_key / joystick_ptt 不再写回去：留着就有两个说了算的地方，而且它们
        # 加起来也表达不了鼠标侧键这一种。老版本读到没有这两个键会用自己的默认，
        # 那是降级时能接受的行为。
        data.pop("ptt_key", None)
        data.pop("joystick_ptt", None)
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            log.warning("could not save the settings: %s", e)
