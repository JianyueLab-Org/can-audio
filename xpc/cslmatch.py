"""CSL 模型匹配。

对应 xPilot 的 `src/aircrafts/` 里那套 model matching。CSL 是 X-Plane 多人机
模型的通用格式（Bluebell、X-CSL 等），xPilot、LiveTraffic、swift 都用它，所以
这里跟着它的约定走，用户装哪个包都能认。

一个 CSL 包的目录里有 `xsb_aircraft.txt`，长这样：

    EXPORT_NAME BB_Airbus
    OBJ8_AIRCRAFT A320_CCA
    OBJ8 SOLID YES A320/A320_CCA.obj
    ICAO A320
    AIRLINE A320 CCA

`ICAO` 给机型码，`AIRLINE` 再加航司码。匹配就是拿 FSD 那边问来的
`EQUIPMENT=B738:AIRLINE=CCA` 去这张表里找，找不到就一级级往下退：

    1. 机型 + 航司     波音 738 的国航涂装
    2. 机型            波音 738，随便什么涂装
    3. 同族近似机型    B739 顶替 B738
    4. 同类别通用      双发喷气客机
    5. 兜底            包里的第一个模型

**退化必须一直有结果**。宁可画一架涂装不对的飞机，也不能因为匹配不到就让天上
空着——飞行员看不见的飞机比看错涂装的飞机危险得多。
"""

import logging
import os
import re

log = logging.getLogger("cslmatch")

# 同族机型。匹配不到精确型号时按这里找替身，都是外形接近的。
# 不求全，只覆盖网络上常见的；查不到就落到按类别的通用匹配。
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
# 而宽体顶宽体、支线顶支线至少大小对得上。和 msfs/aimatch.py 保持一致。
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

# 机型码猜类别，用于最后一级通用匹配
GENERIC_BY_PREFIX = (
    ("A3", "A320"), ("A2", "A320"),
    ("B7", "B738"), ("B3", "B738"),
    ("E1", "E190"), ("E7", "E190"),
    ("CRJ", "CRJ7"),
    ("MD", "MD82"),
    ("DH", "DH8D"), ("AT", "AT76"),
    ("C1", "C172"), ("P2", "C172"), ("SR", "C172"),
)

DEFAULT_TYPE = "B738"


class Model:
    """CSL 包里的一个模型。"""

    __slots__ = ("name", "path", "icao", "airline", "livery", "package",
                 "vert_offset")

    def __init__(self, name, path, package="", icao="", airline="", livery="",
                 vert_offset=None):
        self.name = name
        self.path = path
        self.package = package
        self.icao = icao.upper()
        self.airline = airline.upper()
        self.livery = livery.upper()
        # xsb_aircraft.txt 里写明的垂直偏移（米）。None = 没写，从 .obj 算
        self.vert_offset = vert_offset

    def __repr__(self):
        return f"<Model {self.name} {self.icao}/{self.airline or '-'}>"


def parse_package(directory):
    """读一个 CSL 包的 xsb_aircraft.txt，返回 Model 列表。

    这个格式有几十年的历史，各家包写得并不一致：路径分隔符可能是 `/` 也可能
    是 `\\`，OBJ8 行的字段数不固定，注释用 `#`。宽松地读，认不出的行跳过就好
    ——一个包里一行有问题不该让整包用不了。
    """
    manifest = os.path.join(directory, "xsb_aircraft.txt")
    if not os.path.isfile(manifest):
        return []

    models = []
    package = ""
    current = None
    try:
        with open(manifest, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        log.warning("cannot read %s: %s", manifest, e)
        return []

    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        keyword = parts[0].upper()

        if keyword == "EXPORT_NAME" and len(parts) > 1:
            package = parts[1]
        elif keyword in ("OBJ8_AIRCRAFT", "AIRCRAFT") and len(parts) > 1:
            current = Model(parts[1], "", package=package)
            models.append(current)
        elif keyword == "OBJ8" and current is not None and len(parts) >= 4:
            # OBJ8 <类型> <是否有动画> <路径>；只要 SOLID 的那条
            if parts[1].upper() == "SOLID" and not current.path:
                relative = " ".join(parts[3:]).replace("\\", "/")
                current.path = os.path.normpath(os.path.join(directory, relative))
        elif keyword == "VERT_OFFSET" and current is not None and len(parts) > 1:
            current.vert_offset = _float_or_none(parts[1])
        elif keyword == "OFFSET" and current is not None and len(parts) > 3:
            # PilotEdge 的写法，只有第三个参数是垂直偏移（XPMP2 同样只认它）
            current.vert_offset = _float_or_none(parts[3])
        elif keyword == "ICAO" and current is not None and len(parts) > 1:
            current.icao = parts[1].upper()
        elif keyword == "AIRLINE" and current is not None and len(parts) > 2:
            current.icao = current.icao or parts[1].upper()
            current.airline = parts[2].upper()
        elif keyword == "LIVERY" and current is not None and len(parts) > 3:
            current.icao = current.icao or parts[1].upper()
            current.airline = current.airline or parts[2].upper()
            current.livery = parts[3].upper()

    usable = [m for m in models if m.path and m.icao]
    log.info("%s: %d usable models (%d total, %d missing a path or type code)",
             os.path.basename(directory), len(usable), len(models),
             len(models) - len(usable))
    return usable


def _float_or_none(text):
    try:
        return float(text)
    except ValueError:
        return None


# .obj 路径 -> 算出来的垂直偏移。一个模型只读一次文件。
_obj_offsets = {}


def obj_vert_offset(path):
    """从 OBJ8 文件的顶点算垂直偏移（米），XPMP2 的 FetchVertOfsFromObjFile。

    CSL 模型的原点通常在机身中间，不在轮子底下，按真高画轮子就陷在地里。
    最低的顶点（VT/VLINE 的 y）取反就是要抬的高度。min 和 max 都从 0 起算、
    min > 0 时取 -max，这些都照抄 XPMP2——它自己的注释也说不明白为什么，但
    所有 CSL 包是对着它调的。读不了、不是 OBJ8 就返回 0。
    """
    if path in _obj_offsets:
        return _obj_offsets[path]
    low = high = 0.0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for number, line in enumerate(f, 1):
                if number == 2:
                    try:
                        if int(line.split()[0]) < 800:
                            break
                    except (ValueError, IndexError):
                        break
                if len(line) < 20 or line[0] != "V" or line[1] not in "TL":
                    continue
                parts = line.split()
                if len(parts) < 7 or parts[0] not in ("VT", "VLINE"):
                    continue
                try:
                    y = float(parts[2])
                except ValueError:
                    continue
                if y < low:
                    low = y
                elif y > high:
                    high = y
    except OSError as e:
        log.debug("could not read %s for its vertical offset: %s", path, e)
    offset = -low if low < 0.0 else -high
    _obj_offsets[path] = offset
    return offset


# Bluebell 等从 World Traffic 转来的 CSL 用 cjs/world_traffic/* 驱动灯光和舵面，
# 插件只注册 libxplanemp/controls/*，不换名的话这些灯永远不跟我们的值走。
# 表抄自 XPMP2 的 Resources/Obj8DataRefs.txt，只留插件注册了的目标。
# wolrd 的拼写错误是模型里真实存在的，XPMP2 同样照抄。
OBJ_DATAREF_REPLACEMENTS = (
    ("cjs/world_traffic/hstab_ratio", "libxplanemp/controls/yoke_pitch_ratio"),
    ("cjs/world_traffic/rudder_ratio", "libxplanemp/controls/yoke_heading_ratio"),
    ("cjs/world_traffic/aileron_ratio", "libxplanemp/controls/yoke_roll_ratio"),
    ("cjs/world_traffic/roll_spoiler_ratio_L", "libxplanemp/controls/spoiler_ratio"),
    ("cjs/world_traffic/roll_spoiler_ratio_R", "libxplanemp/controls/spoiler_ratio"),
    ("cjs/world_traffic/flaperon_ratio_L", "libxplanemp/controls/flap_ratio"),
    ("cjs/world_traffic/flaperon_ratio_R", "libxplanemp/controls/flap_ratio"),
    ("cjs/world_traffic/tef_ratio", "libxplanemp/controls/flap_ratio"),
    ("cjs/world_traffic/speed_brake_ratio", "libxplanemp/controls/speed_brake_ratio"),
    ("cjs/world_traffic/main_gear_retraction_ratio", "libxplanemp/controls/gear_ratio"),
    ("cjs/world_traffic/nose_gear_retraction_ratio", "libxplanemp/controls/gear_ratio"),
    ("cjs/world_traffic/nose_gear_steering_angle", "libxplanemp/controls/nws_ratio"),
    ("cjs/wolrd_traffic/landing_lights_on", "libxplanemp/controls/landing_lites_on"),
    ("cjs/world_traffic/landing_lights_on", "libxplanemp/controls/landing_lites_on"),
    ("cjs/world_traffic/wing_landing_lights_on", "libxplanemp/controls/landing_lites_on"),
    ("cjs/world_traffic/taxi_lights_on", "libxplanemp/controls/taxi_lites_on"),
    ("cjs/world_traffic/nav_lights_on", "libxplanemp/controls/nav_lites_on"),
    ("cjs/world_traffic/beacon_lights_on", "libxplanemp/controls/beacon_lites_on"),
    ("cjs/world_traffic/strobe_lights_on", "libxplanemp/controls/strobe_lites_on"),
)
# 换过名的副本和原件放在同一个目录（OBJ 里的贴图是相对路径），文件名加这个后缀
REWRITTEN_SUFFIX = ".xpc.obj"
# 副本第 4 行的标记。表改了就改这个号，旧副本会重写。
REWRITE_MARK = "# XPC for CAN dataref rewrite v1"

# 原件路径 -> 实际交给插件的路径。一个模型只处理一次。
_drawn_paths = {}


def _rewrite_line(line):
    """换一行里的 dataref。和 XPMP2 一样：每行最多换一处，先匹配的算。"""
    if "/" not in line:
        return line
    for old, new in OBJ_DATAREF_REPLACEMENTS:
        if old in line:
            return line.replace(old, new, 1)
    return line


def obj_for_drawing(path):
    """插件该加载的 .obj：需要换 dataref 的给换过名的副本，否则原样。

    XPMP2 的 CSLObj::CopyAndReplace。副本写不出来（目录只读等）就用原件，
    模型照样画，只是灯和舵面不动。
    """
    if not path:
        return path
    if path in _drawn_paths:
        return _drawn_paths[path]
    result = path
    try:
        with open(path, "r", encoding="utf-8", errors="surrogateescape",
                  newline="") as f:
            lines = f.readlines()
        rewritten = [_rewrite_line(line) for line in lines]
        if rewritten != lines:
            copy = os.path.splitext(path)[0] + REWRITTEN_SUFFIX
            if not _copy_is_current(copy, path):
                rewritten.insert(min(3, len(rewritten)), REWRITE_MARK + "\n")
                with open(copy, "w", encoding="utf-8", errors="surrogateescape",
                          newline="") as f:
                    f.writelines(rewritten)
                log.info("rewrote the animation datarefs of %s into %s",
                         os.path.basename(path), os.path.basename(copy))
            result = copy
    except OSError as e:
        log.warning("could not rewrite the datarefs of %s, its lights and "
                    "surfaces will not animate: %s", path, e)
    _drawn_paths[path] = result
    return result


def _copy_is_current(copy, original):
    try:
        if os.path.getmtime(copy) < os.path.getmtime(original):
            return False
        with open(copy, "r", encoding="utf-8", errors="replace") as f:
            head = [f.readline() for _ in range(4)]
    except OSError:
        return False
    return head[-1].rstrip("\r\n") == REWRITE_MARK


def vert_offset(model):
    """画这个模型时要往上抬多少米：xsb_aircraft.txt 写了就用，没写就从 .obj 算。"""
    if model.vert_offset is not None:
        return model.vert_offset
    if not model.path:
        return 0.0
    return obj_vert_offset(model.path)


def find_packages(root):
    """在一个目录树里找所有 CSL 包（含 xsb_aircraft.txt 的目录）。

    **必须 followlinks=True**，和 msfs/aimatch.py 的 find_aircraft_cfgs 同一个
    理由：`os.walk` 默认不进符号链接，而 Windows 的目录联接（junction）从
    Python 3.8 起就被 `os.path.islink()` 认成符号链接。CSL 包动辄几个 GB，
    "放在另一块盘、在 Resources/plugins 下留个链接"是 X-Plane 这边最常见的
    安置方式——跳过它，整套 CSL 就一个包都扫不到，而现象只是他机不显示。

    代价是要自己防环：跟着链接走可能绕回上层目录。按 realpath 记账，进过的
    目录不再进。
    """
    packages = []
    if not os.path.isdir(root):
        return packages
    seen = set()
    for directory, subdirs, files in os.walk(root, followlinks=True):
        real = os.path.realpath(directory)
        if real in seen:
            subdirs[:] = []          # 绕回来了，这一枝不用再往下走
            continue
        seen.add(real)
        if "xsb_aircraft.txt" in files:
            packages.append(directory)
            subdirs[:] = []          # 包里面不会再套包，别往下走
    return packages


def family_of(icao):
    """机型所属的同族列表，不在表里就返回空。"""
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
    """猜一个同类别的通用机型码。"""
    icao = (icao or "").upper()
    for prefix, generic in GENERIC_BY_PREFIX:
        if icao.startswith(prefix):
            return generic
    return DEFAULT_TYPE


class ModelSet:
    """所有装好的 CSL 模型，以及匹配逻辑。"""

    def __init__(self, models=None):
        self.models = list(models or [])
        self._by_icao = {}
        self._by_icao_airline = {}
        self._reindex()

    def _reindex(self):
        self._by_icao.clear()
        self._by_icao_airline.clear()
        for model in self.models:
            self._by_icao.setdefault(model.icao, []).append(model)
            if model.airline:
                key = (model.icao, model.airline)
                self._by_icao_airline.setdefault(key, []).append(model)

    def __len__(self):
        return len(self.models)

    @property
    def types(self):
        return set(self._by_icao)

    @classmethod
    def load(cls, root):
        """把一个目录下所有 CSL 包读进来。"""
        models = []
        for package in find_packages(root):
            models.extend(parse_package(package))
        log.info("loaded %d models from %s", len(models), root)
        return cls(models)

    def by_name(self, name):
        """对方直接指定了 CSL 名字时按名字找。"""
        if not name:
            return None
        name = name.upper()
        for model in self.models:
            if model.name.upper() == name:
                return model
        return None

    def match(self, equipment="", airline="", csl=""):
        """挑一个模型。返回 (Model, 匹配层级说明) 或 (None, 原因)。

        层级说明会写进日志，用户报"我看到的飞机长得不对"时能直接看出来是精确
        匹配还是退化了几级。
        """
        if not self.models:
            return None, "没有装任何 CSL 模型"

        # 0. 对方直接给了 CSL 名字
        model = self.by_name(csl)
        if model:
            return model, "CSL 名字精确匹配"

        equipment = (equipment or "").upper()
        airline = (airline or "").upper()

        # 1. 机型 + 航司
        if equipment and airline:
            found = self._by_icao_airline.get((equipment, airline))
            if found:
                return found[0], "机型和航司都匹配"

        # 2. 机型，任意涂装
        if equipment:
            found = self._by_icao.get(equipment)
            if found:
                return found[0], "机型匹配，涂装不对"

        # 3. 同族近似机型；优先仍带正确航司的
        for relative in family_of(equipment):
            if relative == equipment:
                continue
            if airline:
                found = self._by_icao_airline.get((relative, airline))
                if found:
                    return found[0], f"用同族 {relative} 顶替，航司正确"
            found = self._by_icao.get(relative)
            if found:
                return found[0], f"用同族 {relative} 顶替"

        # 4. 同类机身。宽体顶宽体、支线顶支线，至少大小对得上。
        #
        #    **这一级必须排在「通用机型」前面。** GENERIC_BY_PREFIX 是按两位前缀
        #    猜的，而 A3 / B7 这样的前缀同时盖住窄体和宽体：B77W 会被猜成 B738、
        #    A359 会被猜成 A320。把通用那级放前面的话，只要装了 B738 或 A320
        #    （最普及的两个模型），**所有宽体都会退成窄体**，这一级永远轮不到
        #    ——一架 777 在别人屏幕上变成 737，正是它本来要挡的那种情况。
        category = category_of(equipment)
        if category:
            for candidate in CATEGORIES[category]:
                found = self._by_icao.get(candidate)
                if found:
                    return found[0], f"同为{category}，用 {candidate} 顶替"

        # 5. 同类别通用。走到这里说明机型码不在 CATEGORIES 里（新机型、打错的
        #    代码），只能按前缀猜。猜出来的通用机型本身也可能没装（比如猜出
        #    A320 但包里只有 A20N），所以这一级同样要走一遍同族。
        generic = generic_for(equipment)
        for candidate in (generic,) + tuple(family_of(generic)):
            found = self._by_icao.get(candidate)
            if found:
                return found[0], f"退到通用机型 {candidate}"

        # 6. 兜底。看不见的飞机比涂装错的飞机危险得多。
        return self.models[0], "没有近似机型，用了包里的第一个"
