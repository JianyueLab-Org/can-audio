"""配置模型：Profile → Station → Preset，层级照搬 vATIS。

    Profile   一套配置，含若干席位
    Station   一个机场的通播席位：ICAO、频率、类型、情报字母范围、若干预设
    Preset    一份模板（含机场条件、NOTAM 自由文本），随天气/跑道构型切换

情报字母（ATIS letter）在 METAR 变化时前进一格，可以限定取值范围——离场和进场
分别用不同字母段是 vATIS 的 Code Range，避免飞行员把两份通播搞混。

只有数据和规则，没有 I/O 之外的东西，所以这一层可以直接测。
"""

import logging
import json
import os

# 这些 ValueError 会原样进 QMessageBox，是界面文字
from i18n import t

log = logging.getLogger("profile")

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# 通播类型决定网络上的呼号后缀，和 vATIS 一致
TYPE_COMBINED = "combined"
TYPE_DEPARTURE = "departure"
TYPE_ARRIVAL = "arrival"

TYPE_SUFFIX = {
    TYPE_COMBINED: "_ATIS",
    TYPE_DEPARTURE: "_D_ATIS",
    TYPE_ARRIVAL: "_A_ATIS",
}

# 席位类型在界面上的说法。和 LANGUAGES 一样只留键，文案在 i18n 里——
# 写成 值 → 文案的字典的话，文案会在模块导入时定死，切语言不跟着变。
TYPE_KEYS = {
    TYPE_COMBINED: "station_type.combined",
    TYPE_DEPARTURE: "station_type.departure",
    TYPE_ARRIVAL: "station_type.arrival",
}


def type_label(value):
    """席位类型在界面上的说法。"""
    return t(TYPE_KEYS.get(value, TYPE_KEYS[TYPE_COMBINED]))

# 语音语言。中文稿由 chinese.py 单独渲染——中文通播不是英文的逐词翻译，
# 语序和数字读法都是民航自己的一套。
LANGUAGE_ENGLISH = "en"
LANGUAGE_CHINESE = "zh"
LANGUAGE_BOTH = "both"

# 通播稿的语言，**不是界面语言**：这是播给飞行员听的，一个英文界面的操作者照样
# 可能在管一份中文通播。说法在 i18n 里，这里只留下认得的取值——写成 值 → 文案的
# 字典的话，文案会在模块导入时定死，用户之后切界面语言不会跟着变。
LANGUAGES = (LANGUAGE_ENGLISH, LANGUAGE_CHINESE, LANGUAGE_BOTH)

LANGUAGE_KEYS = {
    LANGUAGE_ENGLISH: "voice_language.english",
    LANGUAGE_CHINESE: "voice_language.chinese",
    LANGUAGE_BOTH: "voice_language.both",
}


def language_label(value):
    """通播稿语言在界面上的说法。"""
    return t(LANGUAGE_KEYS.get(value, LANGUAGE_KEYS[LANGUAGE_ENGLISH]))


class Preset:
    def __init__(self, name="默认", template=None, airport_conditions="",
                 notams="", transition_level="", chinese_runway="",
                 closing="", chinese_extra="", manual_text=False,
                 manual_atis="", manual_voice=""):
        from template import DEFAULT_TEMPLATE
        self.name = name
        self.template = DEFAULT_TEMPLATE if template is None else template
        self.airport_conditions = airport_conditions
        self.notams = notams
        self.transition_level = transition_level
        # 中文稿念的跑道。跟着预设走而不是跟着席位——切到"北向"时英文稿的
        # ARR RWY 会变，中文稿要是还念着南向的跑道，两份稿子就自相矛盾了。
        # 留空则回退到席位上的 chinese_runway。
        self.chinese_runway = chinese_runway
        # 收尾语跟着预设走。不同构型要交代的事不一样，比如「并确认能否执行
        # RNAV 程序」只该出现在 RNAV 离场可用的那份稿子里。留空用内置那句。
        self.closing = closing
        # 中文稿的附加文本。中文通播不是英文的逐词翻译（chinese.py 是从 METAR
        # 独立渲染的），跑道构型、放行频率、应答机模式这些注意事项在中文侧没有
        # 对应字段，整段写在这里，接在气象之后念。
        self.chinese_extra = chinese_extra
        # 手动模式下，主页面的两份稿子就是预设内容，不再经过模板和天气渲染。
        self.manual_text = bool(manual_text)
        self.manual_atis = manual_atis
        self.manual_voice = manual_voice

    def to_dict(self):
        return {
            "name": self.name,
            "template": self.template,
            "airport_conditions": self.airport_conditions,
            "notams": self.notams,
            "transition_level": self.transition_level,
            "chinese_runway": self.chinese_runway,
            "closing": self.closing,
            "chinese_extra": self.chinese_extra,
            "manual_text": self.manual_text,
            "manual_atis": self.manual_atis,
            "manual_voice": self.manual_voice,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            data.get("name", "默认"),
            data.get("template"),
            data.get("airport_conditions", ""),
            data.get("notams", ""),
            data.get("transition_level", ""),
            # 老配置没有这一项，缺省空字符串会回退到席位上的那个
            data.get("chinese_runway", ""),
            # 老配置没有这两项：空串 = 用内置收尾语、中文稿不加附言
            data.get("closing", ""),
            data.get("chinese_extra", ""),
            data.get("manual_text", False),
            data.get("manual_atis", ""),
            data.get("manual_voice", ""),
        )


class Station:
    def __init__(self, identifier, name="", frequency="118.000",
                 atis_type=TYPE_COMBINED, code_range=None, presets=None,
                 contractions=None, latitude=0.0, longitude=0.0,
                 voice_language=LANGUAGE_ENGLISH, chinese_name="",
                 chinese_runway=""):
        self.identifier = identifier.strip().upper()
        self.name = name
        self.frequency = str(frequency).strip()
        # 语音用哪种语言播。中文稿由 chinese.py 单独渲染，不是英文的翻译。
        self.voice_language = (voice_language if voice_language in LANGUAGES
                               else LANGUAGE_ENGLISH)
        # 中文稿里念的机场名和跑道，比如"上海浦东"和"三六左"。留空就用识别码。
        self.chinese_name = chinese_name
        self.chinese_runway = chinese_runway
        self.atis_type = atis_type if atis_type in TYPE_SUFFIX else TYPE_COMBINED
        # 情报字母的可用范围，(起, 止) 闭区间
        self.code_range = tuple(code_range) if code_range else ("A", "Z")
        self.presets = presets if presets is not None else [Preset()]
        # 缩略语：模板里写 @变量名，渲染时替换。文字和语音两种形态，
        # 和气象要素一样（vATIS 的 Contractions）
        self.contractions = contractions or {}
        # 席位在 FSD 上报的位置。没填就按 ICAO 查机场坐标——留成 0/0 的话
        # 席位会显示在几内亚湾外海
        self.latitude = float(latitude or 0.0)
        self.longitude = float(longitude or 0.0)
        if not self.latitude and not self.longitude:
            import airports
            found = airports.coordinates(self.identifier)
            if found:
                self.latitude, self.longitude = found

        self.letter = self.code_range[0]

    # ---------- 派生 ----------
    @property
    def callsign(self):
        """网络上的呼号：ZSPD + _ATIS / _D_ATIS / _A_ATIS。"""
        return self.identifier + TYPE_SUFFIX[self.atis_type]

    @property
    def frequency_khz(self):
        return int(round(float(self.frequency) * 1000))

    @property
    def channel(self):
        """语音频道名，全网统一的约定。"""
        return f"FREQ_{str(self.frequency_khz).zfill(6)}"

    @property
    def label(self):
        return f"{self.callsign}  {self.frequency}"

    def preset(self, name):
        for preset in self.presets:
            if preset.name == name:
                return preset
        return self.presets[0] if self.presets else None

    # ---------- 情报字母 ----------
    def letters_in_range(self):
        start = LETTERS.find(self.code_range[0])
        end = LETTERS.find(self.code_range[1])
        if start < 0 or end < 0:
            return list(LETTERS)
        if start <= end:
            return list(LETTERS[start:end + 1])
        # 允许跨 Z 回绕，例如 X..C
        return list(LETTERS[start:]) + list(LETTERS[:end + 1])

    def advance_letter(self):
        """推进一格情报字母，到范围末尾就绕回开头。"""
        available = self.letters_in_range()
        try:
            index = available.index(self.letter)
        except ValueError:
            self.letter = available[0]
            return self.letter
        self.letter = available[(index + 1) % len(available)]
        return self.letter

    def set_letter(self, letter):
        letter = (letter or "").strip().upper()[:1]
        if letter in self.letters_in_range():
            self.letter = letter
            return True
        return False

    # ---------- 持久化 ----------
    def to_dict(self):
        return {
            "identifier": self.identifier,
            "name": self.name,
            "frequency": self.frequency,
            "atis_type": self.atis_type,
            "code_range": list(self.code_range),
            "letter": self.letter,
            "presets": [p.to_dict() for p in self.presets],
            "contractions": {k: list(v) for k, v in self.contractions.items()},
            "latitude": self.latitude,
            "longitude": self.longitude,
            "voice_language": self.voice_language,
            "chinese_name": self.chinese_name,
            "chinese_runway": self.chinese_runway,
        }

    @classmethod
    def from_dict(cls, data):
        station = cls(
            data["identifier"],
            data.get("name", ""),
            data.get("frequency", "118.000"),
            data.get("atis_type", TYPE_COMBINED),
            data.get("code_range"),
            [Preset.from_dict(p) for p in data.get("presets", [])] or None,
            {k: tuple(v) for k, v in (data.get("contractions") or {}).items()},
            data.get("latitude", 0.0),
            data.get("longitude", 0.0),
            # 老配置里没有这几项，缺省按英文——升级不该改变已有席位的行为
            data.get("voice_language", LANGUAGE_ENGLISH),
            data.get("chinese_name", ""),
            data.get("chinese_runway", ""),
        )
        station.set_letter(data.get("letter", station.code_range[0]))
        return station


DEFAULT_PROFILE_NAME = "默认"

# 配置文件名。相对当前目录——所以客户端必须在自己那个目录里跑（见 CLAUDE.md）。
# 有名字的常量是为了别再写第二遍字面量：`Profile()` 不带 path 是**内存里的一份**，
# 不读不存，界面拿它当配置用的话打开就是空的，而且改了什么都存不下来。
DEFAULT_PROFILE_PATH = "atis_profile.json"


class Profile:
    """一套席位配置。vATIS 的说法：一份 profile 装一组席位。

    自己不再管文件——存盘交给 ProfileSet，它才知道整个文件里有几份 profile。
    `path` 还留着是为了兼容直接 `Profile(path=…)` 的老调用（测试里有）：那种
    用法下它自己读自己存，行为和以前一样。
    """

    def __init__(self, path=None, name=DEFAULT_PROFILE_NAME, stations=None):
        self.path = path
        self.name = name
        self.stations = list(stations or [])
        if path is not None:
            self.load()

    def add(self, station):
        if self.get(station.callsign):
            raise ValueError(t("station.duplicate", callsign=station.callsign))
        self.stations.append(station)
        self.stations.sort(key=lambda s: s.callsign)
        return station

    def remove(self, callsign):
        station = self.get(callsign)
        if station:
            self.stations.remove(station)
            return True
        return False

    def get(self, callsign):
        for station in self.stations:
            if station.callsign == callsign:
                return station
        return None

    def __iter__(self):
        return iter(self.stations)

    def __len__(self):
        return len(self.stations)

    # ---------- 序列化 ----------
    @staticmethod
    def stations_from(entries):
        """一份 profile 里的席位列表。认不出的单个跳过，不连累整份。"""
        stations = []
        for entry in entries or []:
            try:
                stations.append(Station.from_dict(entry))
            except (KeyError, TypeError, ValueError) as e:
                log.warning(f"skipping an unrecognisable station: {e}")
        stations.sort(key=lambda s: s.callsign)
        return stations

    def to_dict(self):
        return {"name": self.name,
                "stations": [s.to_dict() for s in self.stations]}

    @classmethod
    def from_dict(cls, data):
        return cls(name=str(data.get("name") or DEFAULT_PROFILE_NAME).strip()
                   or DEFAULT_PROFILE_NAME,
                   stations=cls.stations_from(data.get("stations")))

    # ---------- 单份直读直写（兼容老调用） ----------
    def load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # 新格式是 {"profiles": [...]}，这里只取当前那一份
                if "profiles" in data:
                    active = data.get("active")
                    chosen = next(
                        (p for p in data["profiles"] if p.get("name") == active),
                        (data["profiles"] or [{}])[0])
                    self.name = str(chosen.get("name") or self.name)
                    self.stations = self.stations_from(chosen.get("stations"))
                else:
                    self.stations = self.stations_from(data.get("stations"))
        except Exception as e:
            log.warning(f"could not read the profile file: {e}")

    def save(self):
        """写回去。多 profile 的文件只改自己那一份，别的原样保留。

        load() 认得 {"profiles": [...]} 并只取当前那份，save() 原来却整个
        文件重写成 {"stations": [...]}——改一次跑道构型，其余 profile 全部
        无声消失。
        """
        payload = {"stations": [s.to_dict() for s in self.stations]}
        try:
            existing = None
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                except Exception:
                    existing = None
            if isinstance(existing, dict) and "profiles" in existing:
                active = existing.get("active")
                profiles = existing.get("profiles") or []
                chosen = next(
                    (p for p in profiles if p.get("name") == active),
                    profiles[0] if profiles else None)
                if chosen is None:
                    profiles.append({"name": self.name, **payload})
                else:
                    chosen["stations"] = payload["stations"]
                payload = existing
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning(f"could not save the profile file: {e}")


class ProfileSet:
    """整个文件：几份 profile，加上当前用哪一份。

    为什么要多份：同一个人可能同时管华东和华北，两边的席位、模板、跑道构型
    完全不同；混在一张列表里，值班时要在十几个不相关的席位里找自己那两个。
    vATIS 也是这个模型。

    **老文件必须读得进来。** 现有的 atis_profile.json 是 `{"stations": [...]}`，
    没有 profile 这一层。读到那种形状就当成一份名叫"默认"的 profile——不能因为
    加了这个功能，让所有人打开就是空配置。
    """

    def __init__(self, path=DEFAULT_PROFILE_PATH):
        self.path = path
        self.profiles = []
        self.active_name = DEFAULT_PROFILE_NAME
        self.load()

    # ---------- 查询 ----------
    def __iter__(self):
        return iter(self.profiles)

    def __len__(self):
        return len(self.profiles)

    @property
    def names(self):
        return [p.name for p in self.profiles]

    def get(self, name):
        for profile in self.profiles:
            if profile.name == name:
                return profile
        return None

    def active(self):
        """当前那一份。文件是空的也保证有一份，界面不用到处判 None。"""
        found = self.get(self.active_name)
        if found is None:
            if not self.profiles:
                self.profiles.append(Profile(name=DEFAULT_PROFILE_NAME))
            found = self.profiles[0]
            self.active_name = found.name
        return found

    # ---------- 增删改 ----------
    def add(self, name):
        """新建一份空的。名字重复时抛 ValueError。"""
        name = (name or "").strip()
        if not name:
            raise ValueError(t("profile.name_empty"))
        # **必须写 is not None。** Profile 定义了 __len__，所以一份还没有席位的
        # 配置是**假值**——`if self.get(name):` 会认为它不存在，于是允许重名建
        # 第二份，两份同名之后选中哪一份全看顺序。和 xpc 的 TrafficTable 是同
        # 一个坑。
        if self.get(name) is not None:
            raise ValueError(t("profile.exists", name=name))
        profile = Profile(name=name)
        self.profiles.append(profile)
        return profile

    def rename(self, old, new):
        new = (new or "").strip()
        if not new:
            raise ValueError(t("profile.name_empty"))
        profile = self.get(old)
        if profile is None:
            raise ValueError(t("profile.missing", name=old))
        # 同样是 is not None——空 profile 是假值，见 add() 里那段
        if new != old and self.get(new) is not None:
            raise ValueError(t("profile.exists", name=new))
        profile.name = new
        if self.active_name == old:
            self.active_name = new
        return profile

    def remove(self, name):
        """删一份。**最后一份不许删**——删光了界面就没有可操作的对象了。"""
        if len(self.profiles) <= 1:
            raise ValueError(t("profile.last_one"))
        profile = self.get(name)
        if profile is None:
            return False
        self.profiles.remove(profile)
        if self.active_name == name:
            self.active_name = self.profiles[0].name
        return True

    def select(self, name):
        if self.get(name) is None:
            return False
        self.active_name = name
        return True

    # ---------- 文件 ----------
    def load(self):
        self.profiles = []
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if "profiles" in data:
                    for entry in data.get("profiles") or []:
                        self.profiles.append(Profile.from_dict(entry))
                    self.active_name = str(
                        data.get("active") or DEFAULT_PROFILE_NAME)
                else:
                    # 老格式：整个文件就是一份配置
                    self.profiles.append(Profile(
                        name=DEFAULT_PROFILE_NAME,
                        stations=Profile.stations_from(data.get("stations"))))
                    self.active_name = DEFAULT_PROFILE_NAME
                    log.info("read a profile file in the old shape, treating it as one "
                             "profile named %r", DEFAULT_PROFILE_NAME)
        except Exception as e:
            log.warning(f"could not read the profile file: {e}")
        self.active()        # 保证至少有一份，并把 active_name 校正到存在的名字

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"active": self.active_name,
                           "profiles": [p.to_dict() for p in self.profiles]},
                          f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning(f"could not save the profile file: {e}")
