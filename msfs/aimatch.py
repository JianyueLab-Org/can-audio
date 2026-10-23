"""MSFS 机型匹配。

对应 xpc/cslmatch.py。退化链是同一套（那部分是通用的，也是被测试钉住的部分），
换掉的只有"模型从哪来"：

    X-Plane   CSL 包，一个 xsb_aircraft.txt 里列出 .obj 和 ICAO/AIRLINE
    MSFS      装好的飞机，每个 aircraft.cfg 里有若干涂装

MSFS 的 aircraft.cfg 长这样：

    [GENERAL]
    icao_type_designator = "A20N"

    [FLTSIM.0]
    title = "Airbus A320neo Asobo"
    icao_airline = ""

    [FLTSIM.1]
    title = "Airbus A320neo Air China"
    icao_airline = "CCA"

一个文件里 `[GENERAL]` 给机型码，每个 `[FLTSIM.n]` 是一个涂装，各有自己的 title
和航司码。**title 就是交给 AICreateNonATCAircraft 的东西**，必须逐字一致，所以
不能自己拼，只能从配置文件里读出来。

**匹配必须一直有结果。** 宁可放一架涂装不对的飞机，也不能因为匹配不到就让天上
空着——飞行员看不见的飞机比看错涂装的飞机危险得多。
"""

import configparser
import logging
import os

log = logging.getLogger("aimatch")

# 同族机型。匹配不到精确型号时按这里找替身，都是外形接近的。
# 和 xpc/cslmatch.py 保持一致，改一边要记得改另一边。
FAMILIES = [
    ("A318", "A319", "A320", "A321", "A19N", "A20N", "A21N"),
    ("A332", "A333", "A338", "A339"),
    ("A343", "A345", "A346"),
    ("A359", "A35K"),
    ("A388",),
    ("B731", "B732", "B733", "B734", "B735", "B736", "B737", "B738", "B739",
     "B37M", "B38M", "B39M"),
    ("B741", "B742", "B743", "B744", "B748"),
    ("B752", "B753"),
    ("B762", "B763", "B764"),
    ("B772", "B773", "B77L", "B77W"),
    ("B788", "B789", "B78X"),
    ("E170", "E75L", "E75S", "E190", "E195"),
    ("CRJ2", "CRJ7", "CRJ9", "CRJX"),
    ("C919", "AR21"),
    ("MD82", "MD83", "MD88", "MD90"),
    ("C172", "C182", "C152", "P28A", "SR22"),
]

# 机身类别。同族找不到时按这个找替身——拿一架 A319 去顶 B777 视觉上差得离谱，
# 而宽体顶宽体、支线顶支线至少大小对得上。
# 顺序有意义：类别里排在前面的优先被选中当替身。
CATEGORIES = {
    "宽体": ("B77W", "B77L", "B772", "B773", "B788", "B789", "B78X",
             "A332", "A333", "A338", "A339", "A359", "A35K",
             "B742", "B743", "B744", "B748", "A388", "B762", "B763", "B764",
             "MD11", "A306", "A310"),
    "窄体": ("A319", "A320", "A321", "A318", "A19N", "A20N", "A21N",
             "B737", "B738", "B739", "B736", "B735", "B734", "B733",
             "B37M", "B38M", "B39M", "B752", "B753", "C919",
             "MD82", "MD83", "MD88", "MD90", "B712"),
    "支线": ("E170", "E75L", "E75S", "E190", "E195", "E145", "E135",
             "CRJ2", "CRJ7", "CRJ9", "CRJX", "AR21", "AT45", "AT72", "AT76",
             "DH8A", "DH8B", "DH8C", "DH8D", "SF34", "J328"),
    "通航": ("C172", "C182", "C152", "C208", "C25C", "C700", "P28A", "SR22",
             "S22T", "DA40", "DA62", "BE36", "BE58", "B350", "TBM9", "PC12",
             "PC6", "DHC2", "DV20", "DR40", "MXS", "VL3", "A5"),
}

GENERIC_BY_PREFIX = (
    ("A3", "A320"), ("A2", "A320"), ("A1", "A320"),
    ("B7", "B738"), ("B3", "B738"),
    ("E1", "E190"), ("E7", "E190"),
    ("CRJ", "CRJ7"),
    ("MD", "MD82"),
    ("DH", "DH8D"), ("AT", "AT76"),
    ("C1", "C172"), ("P2", "C172"), ("SR", "C172"),
)

DEFAULT_TYPE = "B738"

# MSFS 2024 的被动 AI 机（PassiveAircraft）。2024 的默认机是流式下载的，包目录里
# 扫不到它们的 aircraft.cfg，可 AICreateNonATCAircraft 拿这些 title 一定建得出来。
# 只装着默认机的 2024 用户扫出来的模型是零或者只有附加件，不补上这张表的话天上
# 一架他机都没有。表照 can-pilot-client 的 BuiltInRules.Msfs2024（vPilot 也是这么
# 写死的），改一边要记得改另一边。
MSFS2024_PASSIVE = (
    ("BCS1", "Asobo PassiveAircraft A220-100"),
    ("BCS3", "Asobo PassiveAircraft A220-300"),
    ("A318", "Asobo PassiveAircraft A318"),
    ("A319", "Asobo PassiveAircraft A319 CEO"),
    ("A320", "Asobo PassiveAircraft A320 NEO"),
    ("A321", "Asobo PassiveAircraft A321 CEO"),
    ("A321", "Asobo PassiveAircraft A321 NEO"),
    ("AT43", "Asobo PassiveAircraft ATR42-320"),
    ("AT45", "Asobo PassiveAircraft ATR42-500"),
    ("AT76", "Asobo PassiveAircraft ATR72-600"),
    ("B712", "Asobo PassiveAircraft B717-200"),
    ("B733", "Asobo PassiveAircraft B737-300"),
    ("B734", "Asobo PassiveAircraft B737-400"),
    ("B739", "Asobo PassiveAircraft B737-900"),
    ("B752", "Asobo PassiveAircraft B757-200"),
    ("B763", "Asobo PassiveAircraft B767-300BCF"),
    ("B772", "Asobo PassiveAircraft B777-200LR"),
    ("BE23", "Asobo PassiveAircraft Beechcraft 23 Musketeer"),
    ("C152", "Asobo PassiveAircraft C152 Aerobat"),
    ("C402", "Asobo PassiveAircraft C402"),
    ("C25A", "Asobo PassiveAircraft Citation CJ2"),
    ("C25A", "Asobo PassiveAircraft Citation CJ2 Plus"),
    ("C25C", "Asobo PassiveAircraft Citation CJ4"),
    ("C700", "Asobo PassiveAircraft Citation Longitude"),
    ("C680", "Asobo PassiveAircraft Citation Sovereign Plus"),
    ("C650", "Asobo PassiveAircraft Citation VII"),
    ("DH8D", "Asobo PassiveAircraft DHC-8 400"),
    ("E145", "Asobo PassiveAircraft E145"),
    ("E170", "Asobo PassiveAircraft E170"),
    ("E190", "Asobo PassiveAircraft E190"),
    ("E195", "Asobo PassiveAircraft E195"),
    ("E195", "Asobo PassiveAircraft E195-E2"),
    ("SF34", "Microsoft PassiveAircraft S340B Passenger"),
)

# 2024 上什么都对不上时的兜底，和 can-pilot-client 的 DefaultModel 一致
MSFS2024_DEFAULT = "Asobo PassiveAircraft E190"


class Model:
    """一个可用的涂装。"""

    __slots__ = ("title", "icao", "airline", "package")

    def __init__(self, title, icao="", airline="", package=""):
        self.title = title
        self.icao = (icao or "").upper()
        self.airline = (airline or "").upper()
        self.package = package

    @property
    def name(self):
        return self.title

    def __repr__(self):
        return f"<Model {self.title!r} {self.icao}/{self.airline or '-'}>"


# 兜底要先试 MSFS2024_DEFAULT，所以把它排到最前
_PASSIVE_MODELS = tuple(sorted(
    (Model(title, icao=icao, package="MSFS 2024 PassiveAircraft")
     for icao, title in MSFS2024_PASSIVE),
    key=lambda model: model.title != MSFS2024_DEFAULT))

_PASSIVE_BY_ICAO = {}
for _model in _PASSIVE_MODELS:
    _PASSIVE_BY_ICAO.setdefault(_model.icao, []).append(_model)
del _model


def _clean(value):
    """配置里的值常带引号和行尾注释。"""
    value = (value or "").split(";")[0].split("//")[0].strip()
    return value.strip('"').strip("'").strip()


def _clean_icao(value):
    """规整机型码。

    真实安装里这个字段并不干净——见过 `"A359 ULR"` 这种带后缀的写法。ICAO 机型
    码是 2-4 位字母数字，取第一段再校验，对不上就当没有：宁可退到同族匹配，也
    不要让一个假的机型码进索引，那会让别的飞机永远匹配不到它。
    """
    token = _clean(value).split()[0] if _clean(value) else ""
    token = token.upper()
    return token if 2 <= len(token) <= 4 and token.isalnum() else ""


def parse_aircraft_cfg(path):
    """读一个 aircraft.cfg，返回它里面所有涂装。

    这些文件是人手写出来的，格式相当随意：重复的键、没有值的行、BOM、各种编码
    都见得到。configparser 默认遇到重复键会抛，这里放宽，读不了的文件跳过就好
    ——一个装得不干净的飞机不该让整个匹配表建不起来。
    """
    parser = configparser.ConfigParser(
        strict=False, interpolation=None, allow_no_value=True)
    parser.optionxform = str.lower
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            parser.read_file(f)
    except (OSError, configparser.Error) as e:
        log.debug("cannot read %s: %s", path, e)
        return []

    icao = ""
    for section in parser.sections():
        if section.strip().lower() == "general":
            icao = _clean_icao(parser[section].get("icao_type_designator", ""))
            break

    package = os.path.basename(os.path.dirname(os.path.dirname(path)))
    models = []
    for section in parser.sections():
        if not section.strip().lower().startswith("fltsim."):
            continue
        entry = parser[section]
        title = _clean(entry.get("title", ""))
        if not title:
            continue
        airline = _clean(entry.get("icao_airline", ""))
        models.append(Model(title, icao=icao, airline=airline, package=package))
    return models


def find_aircraft_cfgs(root):
    """在一个包目录树里找所有 aircraft.cfg。

    MSFS 的布局是 <包>/SimObjects/Airplanes/<飞机>/aircraft.cfg。不写死这个路径
    ——用户装的第三方包目录层级五花八门，直接走一遍更省事。

    **必须 followlinks=True。** Windows 的目录联接（junction）在 Python 3.8 以后
    被 `os.path.islink()` 认成符号链接，而 `os.walk` 默认不进符号链接——于是整个
    `Official` 会被安静地跳过。这不是边角情况：商店版就常把 `Official` 做成
    junction，而"把包目录搬到另一块盘、原地留个 junction"是社区里最普遍的做法
    之一。实飞日志里那次只扫到 4 个涂装、0 种机型，形态正是"只剩 Community 里
    几个零星附加件"。

    代价是要自己防环：跟着链接走可能绕回上层目录。按 realpath 记账，进过的
    目录不再进。
    """
    found = []
    if not os.path.isdir(root):
        return found
    seen = set()
    for directory, subdirs, files in os.walk(root, followlinks=True):
        real = os.path.realpath(directory)
        if real in seen:
            # 绕回来了，这一枝不用再往下走
            subdirs[:] = []
            continue
        seen.add(real)
        for name in files:
            if name.lower() == "aircraft.cfg":
                found.append(os.path.join(directory, name))
        # 跳过这些目录能省掉大量磁盘遍历，也避免读进不是飞机的东西。
        # attachments 尤其重要：Fenix 之类的插件机在那底下放了几十个部件配置，
        # 每个都有 [GENERAL] 和 title，但没有 icao_type_designator——当成飞机
        # 会污染匹配表，还可能被当成兜底模型顶到别人头上。
        subdirs[:] = [
            d for d in subdirs
            if d.lower() not in ("texture", "sound", "panel", "model",
                                 "attachments", "contentinfo")
            and not d.lower().startswith("texture.")]
    return found


def family_of(icao):
    icao = (icao or "").upper()
    for family in FAMILIES:
        if icao in family:
            return family
    return ()


def category_of(icao):
    """机型属于哪一类机身。认不出返回空。"""
    icao = (icao or "").upper()
    for name, types in CATEGORIES.items():
        if icao in types:
            return name
    return ""


def generic_for(icao):
    icao = (icao or "").upper()
    for prefix, generic in GENERIC_BY_PREFIX:
        if icao.startswith(prefix):
            return generic
    return DEFAULT_TYPE


class ModelSet:
    """装好的所有涂装，以及匹配逻辑。"""

    def __init__(self, models=None):
        self.models = list(models or [])
        self._by_icao = {}
        self._by_icao_airline = {}
        self._reindex()

    def _reindex(self):
        self._by_icao.clear()
        self._by_icao_airline.clear()
        for model in self.models:
            if not model.icao:
                continue
            self._by_icao.setdefault(model.icao, []).append(model)
            if model.airline:
                self._by_icao_airline.setdefault(
                    (model.icao, model.airline), []).append(model)

    def __len__(self):
        return len(self.models)

    @property
    def types(self):
        return set(self._by_icao)

    @classmethod
    def load(cls, *roots):
        """扫若干个目录（Community、Official 等），把飞机都读进来。

        每个目录单独记一行。扫出来的数目不对时，光有一个总数是查不动的——
        「目录根本没找对」和「目录对、但里面的 aircraft.cfg 都没有机型码」写进
        日志长得一模一样，而两者要做的事完全相反。带上机型数尤其重要：涂装有
        几百个而机型是 0，说明读到的全是附加件那类没有机型码的配置。
        """
        models = []
        for root in roots:
            if not root:
                continue
            paths = find_aircraft_cfgs(root)
            found = []
            for path in paths:
                found.extend(parse_aircraft_cfg(path))
            models.extend(found)
            log.info("scanned %s: %d aircraft.cfg, %d liveries, %d types",
                     root, len(paths), len(found),
                     len({m.icao for m in found if m.icao}))
        return cls(models)

    def by_title(self, title):
        if not title:
            return None
        title = title.strip().lower()
        for model in self.models:
            if model.title.strip().lower() == title:
                return model
        return None

    def match(self, equipment="", airline="", csl="", exclude=None,
              msfs2024=False):
        """挑一个涂装。返回 (Model, 匹配层级说明) 或 (None, 原因)。

        层级说明会写进日志，用户报"这飞机长得不对"时能直接看出来退化了几级。
        csl 参数是对方在 #SB 里直接指定的模型名——MSFS 上通常对不上（那是
        X-Plane 的 CSL 名），但如果刚好是个装着的 title 就直接用。

        msfs2024：连着的是 MSFS 2024。每一级在扫到的涂装之后再试 2024 的被动
        AI 机（MSFS2024_PASSIVE），兜底先用 MSFS2024_DEFAULT。2024 的默认机
        扫不到，不这样的话只装默认机的 2024 用户一架他机都看不见。
        """
        passive = _PASSIVE_MODELS if msfs2024 else ()
        if not self.models and not passive:
            return None, "没有找到任何可用的飞机"

        model = self.by_title(csl)
        if model:
            return model, "对方指定的模型正好装着"
        wanted = (csl or "").strip().lower()
        for model in passive:
            if wanted and model.title.lower() == wanted:
                return model, "对方指定的模型是 MSFS 2024 的被动 AI 机"

        equipment = (equipment or "").upper()
        airline = (airline or "").upper()
        # 模拟器拒绝生成过的模型要跳过，否则会一直挑中同一个建不出来的，飞机
        # 永远出不来。调用方把 TrafficInjector 的黑名单传进来。
        excluded = {t.strip().lower() for t in (exclude or ())}

        def of_type(icao):
            # 扫到的在前：用户自己装的涂装比被动 AI 机好看
            found = self._by_icao.get(icao) or []
            if passive:
                found = found + _PASSIVE_BY_ICAO.get(icao, [])
            return found

        def pick(models):
            for model in models or ():
                if model.title.strip().lower() not in excluded:
                    return model
            return None

        if equipment and airline:
            model = pick(self._by_icao_airline.get((equipment, airline)))
            if model:
                return model, "机型和航司都匹配"

        if equipment:
            model = pick(of_type(equipment))
            if model:
                return model, "机型匹配，涂装不对"

        for relative in family_of(equipment):
            if relative == equipment:
                continue
            if airline:
                model = pick(self._by_icao_airline.get((relative, airline)))
                if model:
                    return model, f"用同族 {relative} 顶替，航司正确"
            model = pick(of_type(relative))
            if model:
                return model, f"用同族 {relative} 顶替"

        # 同类机身。宽体顶宽体、支线顶支线，至少大小对得上。
        #
        # **这一级必须排在「通用机型」前面。** GENERIC_BY_PREFIX 是按两位前缀猜
        # 的，而 A3 / B7 这样的前缀同时盖住窄体和宽体：B77W 会被猜成 B738、
        # A359 会被猜成 A320。把通用那级放前面的话，只要装了 B738 或 A320
        # （最普及的两个），**所有宽体都会退成窄体**，这一级永远轮不到——一架
        # 777 在别人屏幕上变成 737，正是它本来要挡的那种情况。
        category = category_of(equipment)
        if category:
            for candidate in CATEGORIES[category]:
                model = pick(of_type(candidate))
                if model:
                    return model, f"同为{category}，用 {candidate} 顶替"

        # 通用机型。走到这里说明机型码不在 CATEGORIES 里（新机型、打错的代码），
        # 只能按前缀猜。猜出来的通用机型也可能没装（比如猜出 A320 但用户只有
        # A20N），所以这一级同样要走一遍同族，否则会白白掉到兜底。
        generic = generic_for(equipment)
        for candidate in (generic,) + tuple(family_of(generic)):
            model = pick(of_type(candidate))
            if model:
                return model, f"退到通用机型 {candidate}"

        # 兜底。看不见的飞机比涂装错的飞机危险得多。
        # 2024 上先用被动 AI 机：扫到的涂装可能来自 2020 的包目录，2024 不一定
        # 建得出来，而被动 AI 机一定建得出来。
        model = pick(passive)
        if model:
            return model, "没有近似机型，用了 MSFS 2024 的默认 AI 机"
        # 优先挑有机型码的：没有机型码的多半是装得不规范的附加件，拿它当所有
        # 飞机的替身最难看。
        model = pick([m for m in self.models if m.icao])
        if model:
            return model, "没有近似机型，用了第一个装着的飞机"
        model = pick(self.models)
        if model:
            return model, "没有近似机型，也没有带机型码的飞机可用"
        return None, "装着的模型全都被模拟器拒绝过了"


def _packages_from_usercfg(path):
    """从 UserCfg.opt 里读 InstalledPackagesPath。

    这是**唯一靠谱的来源**。包目录默认在 AppData 下，但安装时可以改到任何地方，
    改了之后 AppData 下那个目录要么不存在要么只剩零星几个包。开发机上实测：
    UserCfg.opt 写的是 D:\\MSFS2022（257 个飞机），而 AppData 下那个一个都没有
    ——只猜路径会安静地漏掉整个安装。

    格式是一行 `InstalledPackagesPath "D:\\MSFS2022"`。

    键和值之间按空白切，不能只按单个空格切：见过用制表符分隔的写法，那样
    `partition(" ")` 什么都切不出来，整个安装就被安静地漏掉了。路径本身带空格
    没问题——只切第一段。
    """
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                if not line.strip().lower().startswith("installedpackagespath"):
                    continue
                parts = line.strip().split(None, 1)
                if len(parts) < 2:
                    continue
                value = parts[1].strip().strip('"').strip()
                if value and os.path.isdir(value):
                    return value
                if value:
                    log.warning("UserCfg.opt points at %s, which does not exist",
                                value)
    except OSError:
        pass
    return None


def default_roots():
    """MSFS 的包目录。

    先读各个版本的 UserCfg.opt（权威来源），读不到再退回猜默认位置。
    """
    appdata = os.environ.get("APPDATA", "")
    local = os.environ.get("LOCALAPPDATA", "")

    roots = []
    for folder in ("Microsoft Flight Simulator",
                   "Microsoft Flight Simulator 2024"):
        base = os.path.join(appdata, folder)
        configured = _packages_from_usercfg(os.path.join(base, "UserCfg.opt"))
        if configured:
            roots.append(configured)
        fallback = os.path.join(base, "Packages")
        if os.path.isdir(fallback) and fallback not in roots:
            roots.append(fallback)

    # Microsoft Store 版把 UserCfg.opt 放在 LocalCache 下。2024 版在商店里的
    # 包名不是 FlightSimulator 而是 Limitless，只认前者会整个漏掉商店版 2024。
    for family in ("Microsoft.FlightSimulator_8wekyb3d8bbwe",
                   "Microsoft.Limitless_8wekyb3d8bbwe"):
        store = os.path.join(local, "Packages", family, "LocalCache")
        configured = _packages_from_usercfg(os.path.join(store, "UserCfg.opt"))
        if configured and configured not in roots:
            roots.append(configured)

    return roots
