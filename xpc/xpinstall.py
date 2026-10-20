"""把他机插件装进 X-Plane。

只有 XPC 需要这个：MSFS 那边靠 SimConnect 直接建 AI 飞机，一个插件都不用装。

**装的是原生插件，不再是 Python 插件。** 他机由 `native_plugin/` 编出来的
`XPC_for_CAN`（照搬 swift 的 xswiftbus + xplanemp2）来画，X-Plane 直接加载
`.xpl`，不需要 XPPython3。整个插件是一个目录：

    Resources/plugins/XPC_for_CAN/64/win.xpl
    Resources/plugins/XPC_for_CAN/LegacyData/related.txt, Doc8643.txt
    Resources/plugins/XPC_for_CAN/CSL/        （可选，插件启动时会加载）

CSL 模型不用拷进去：客户端把设置里的 CSL 目录随每一帧交给插件，插件自己
加载、自己按机型/航司/涂装匹配。

**旧的 Python 插件装着的话要删掉。** 它和原生插件绑同一个 UDP 端口（49900），
谁先起来谁收得到数据，另一个永远一架飞机也画不出来。

**X-Plane 的安装目录只能另找来源。** UDP 那条链路给不出来——BECN 信标里只有
地址和端口，客户端连模拟器装在哪个盘都不知道。所以这里两条路：先读 X-Plane
安装器自己写的那份记录（`x-plane_install_12.txt`），读不到就让用户自己选目录。
自动探测是省事用的，**选目录那条路必须一直留着**：那份记录的位置在各平台上
不一样，用绿色版或者搬过目录的人根本没有它。

**新旧用文件内容比，不用版本号。** 内容一样就是最新，不一样就该更新。

协议号那件事值得单独说：`bridge.py` 和插件各存一份协议号，对不上时插件
**静默丢掉每一帧**，症状是"他机一架都不出现"而两边日志都干干净净。原生插件
把 `XPC_BRIDGE_PROTOCOL=<n>` 编进了 `.xpl`，`inspect()` 从装好的那份里把它
读出来单独回报，而不是只说一句"版本旧"。
"""

import hashlib
import logging
import os
import re
import shutil
import sys

import bridge

log = logging.getLogger("xpinstall")

PLUGIN_FOLDER = "XPC_for_CAN"
PLUGIN_BINARY = os.path.join("64", "win.xpl")
LEGACY_FILES = (os.path.join("LegacyData", "related.txt"),
                os.path.join("LegacyData", "Doc8643.txt"))
# 旧的 Python 插件，装原生插件时要删掉
LEGACY_PYTHON_PLUGIN = "PI_XpcTraffic.py"

# X-Plane 根目录下的几个位置
PLUGINS_DIR = os.path.join("Resources", "plugins")
PYTHON_PLUGINS_DIR = os.path.join("Resources", "plugins", "PythonPlugins")

# inspect() 的结果
NO_ROOT = "no_root"             # 没找到 X-Plane 目录
NOT_XPLANE = "not_xplane"       # 指的那个目录不像 X-Plane
MISSING = "missing"             # 没装过
OUTDATED = "outdated"           # 装过，但和随包这份不一样
CURRENT = "current"             # 已是最新


def _install_records():
    """X-Plane 安装器写的那份记录可能在哪。

    每行一个安装目录。位置各平台不同，而且是**尽力而为**——这几条要在真机上
    核过才能当准，核不到也没关系，界面上永远有"自己选目录"。12 排在 11 前面：
    两个都装着的时候，插件更可能是给 12 用的。
    """
    names = ("x-plane_install_12.txt", "x-plane_install_11.txt")
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            home, "AppData", "Local")
        roots = [base]
    elif sys.platform == "darwin":
        roots = [os.path.join(home, "Library", "Preferences")]
    else:
        roots = [os.path.join(home, ".x-plane")]
    return [os.path.join(root, name) for root in roots for name in names]


def is_xplane_root(path):
    """这个目录看着像不像 X-Plane 装的地方。

    认 `Resources/plugins`：那是每个 X-Plane 安装都有的，而 `PythonPlugins`
    不一定（没装 XPPython3 就没有），拿后者判断会把好目录判成坏的。
    """
    if not path:
        return False
    return os.path.isdir(os.path.join(path, PLUGINS_DIR))


def find_installs():
    """从安装记录里读出 X-Plane 目录，按记录顺序去重，只留还在的。"""
    found = []
    for record in _install_records():
        try:
            if not os.path.isfile(record):
                continue
            with open(record, "r", encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError as e:
            log.debug("could not read %s: %s", record, e)
            continue
        for line in lines:
            path = line.strip()
            if path and path not in found and is_xplane_root(path):
                found.append(path)
        log.info("read %d X-Plane install path(s) from %s", len(found), record)
    return found


# 别的联飞客户端放 CSL 的地方，按顺序找。没在设置里选过 CSL 目录的人多半已经
# 给 xPilot / IVAO 装过一份，直接用那一份，不用再占几个 GB。
# XPC_for_CAN/CSL 不在里面：插件自己会加载它。
KNOWN_CSL_DIRS = (
    os.path.join("IVAO_CSL", "CSL"),
    os.path.join("xPilot", "Resources", "CSL"),
    os.path.join("LiveTraffic", "Resources", "CSL"),
    os.path.join("XSquawkBox", "Resources", "CSL"),
    "X-CSL",
)


def has_csl_package(path, depth=2):
    """path 自己或往下 depth 层里有没有 xsb_aircraft.txt（一个 CSL 包）。"""
    if not path or not os.path.isdir(path):
        return False
    try:
        entries = list(os.scandir(path))
    except OSError:
        return False
    for entry in entries:
        if entry.is_file() and entry.name.lower() == "xsb_aircraft.txt":
            return True
    if depth <= 0:
        return False
    return any(entry.is_dir() and has_csl_package(entry.path, depth - 1) for entry in entries)


def find_csl(roots):
    """在这些 X-Plane 目录里找一个现成的 CSL 目录，找不到返回空串。"""
    for root in roots:
        for relative in KNOWN_CSL_DIRS:
            path = os.path.join(root, PLUGINS_DIR, relative)
            if has_csl_package(path):
                return path
    return ""


def bundled_package():
    """随包带的那份原生插件目录（里面有 64/win.xpl 和 LegacyData/）。

    打包之后当前目录是用户双击时所在的目录，不是程序目录，用相对路径取不到
    ——PyInstaller 把 datas 解到 sys._MEIPASS，gui.spec 里它落在
    native_plugin/XPC_for_CAN/ 下。从源码跑时用 cmake 编出来的那份。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(getattr(sys, "_MEIPASS", here), "native_plugin", PLUGIN_FOLDER),
        os.path.join(here, "native_plugin", "build", "package", PLUGIN_FOLDER),
    ]
    for candidate in candidates:
        if os.path.isfile(os.path.join(candidate, PLUGIN_BINARY)):
            return candidate
    return candidates[0]


def plugin_dir(root):
    return os.path.join(root, PLUGINS_DIR, PLUGIN_FOLDER)


def plugin_path(root):
    """装好之后 X-Plane 加载的那个文件。"""
    return os.path.join(plugin_dir(root), PLUGIN_BINARY)


def legacy_python_path(root):
    return os.path.join(root, PYTHON_PLUGINS_DIR, LEGACY_PYTHON_PLUGIN)


def _digest(path):
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except OSError as e:
        log.debug("could not read %s: %s", path, e)
        return None


_PROTOCOL_MARKER = re.compile(rb"XPC_BRIDGE_PROTOCOL=(\d+)")


def protocol_version(path):
    """从一份 .xpl 里把编进去的 XPC_BRIDGE_PROTOCOL=<n> 抠出来，抠不到返回 None。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    match = _PROTOCOL_MARKER.search(data)
    return int(match.group(1)) if match else None


def _same_package(source, target):
    """装着的那份和随包这份是不是同一份：插件本体和 LegacyData 都要一样。"""
    for name in (PLUGIN_BINARY,) + LEGACY_FILES:
        mine = _digest(os.path.join(source, name))
        if not mine or mine != _digest(os.path.join(target, name)):
            return False
    return True


class Status:
    """一次探测的结果。界面直接照着它画。"""

    def __init__(self, root="", state=NO_ROOT, installed_protocol=None,
                 path="", bundled=False, legacy_python=False):
        self.root = root
        self.state = state
        self.installed_protocol = installed_protocol
        self.bundled_protocol = bridge.PROTOCOL_VERSION
        self.path = path
        # 随包有没有带原生插件。没带（打包时没编出来）就装不了
        self.bundled = bundled
        # 旧的 Python 插件还在不在。在的话它和原生插件抢端口
        self.legacy_python = legacy_python

    @property
    def can_install(self):
        return self.bundled and self.state in (MISSING, OUTDATED, CURRENT)

    @property
    def protocol_mismatch(self):
        """装好的那份和客户端说的不是同一种话。

        这一条要单独报：协议对不上时插件是**静默**丢帧的，用户看到的是"他机
        一架都不出现"，日志里两边都干干净净。
        """
        return (self.installed_protocol is not None
                and self.installed_protocol != self.bundled_protocol)


def inspect(root=""):
    """看一眼现状。`root` 留空就自动探测。"""
    if not root:
        installs = find_installs()
        root = installs[0] if installs else ""
    if not root:
        return Status(state=NO_ROOT)
    if not is_xplane_root(root):
        return Status(root=root, state=NOT_XPLANE)

    source = bundled_package()
    target = plugin_path(root)
    status = Status(root=root, state=MISSING, path=target,
                    bundled=os.path.isfile(os.path.join(source, PLUGIN_BINARY)),
                    legacy_python=os.path.isfile(legacy_python_path(root)))
    if not os.path.isfile(target):
        return status
    status.installed_protocol = protocol_version(target)
    # 随包那份读不到（打包漏了）时不要报"最新"——那会让用户以为装好了
    status.state = (CURRENT if status.bundled and not status.legacy_python
                    and _same_package(source, plugin_dir(root)) else OUTDATED)
    return status


def install(root):
    """把插件目录复制过去，删掉旧的 Python 插件，返回 .xpl 落地的路径。

    出错一律抛 OSError，让界面把 X-Plane 装在 Program Files 里这类权限问题
    原样说给用户听——自己吞掉的话，界面只能说一句"失败了"。
    """
    source = bundled_package()
    if not os.path.isfile(os.path.join(source, PLUGIN_BINARY)):
        raise OSError("the bundled plugin is missing: %s" % source)
    target_dir = plugin_dir(root)
    for name in (PLUGIN_BINARY,) + LEGACY_FILES:
        destination = os.path.join(target_dir, name)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.copyfile(os.path.join(source, name), destination)
    # CSL/ 是插件启动时自己加载的目录，先建好，用户想放模型就有地方放
    os.makedirs(os.path.join(target_dir, "CSL"), exist_ok=True)

    legacy = legacy_python_path(root)
    if os.path.isfile(legacy):
        os.remove(legacy)
        log.info("removed the old Python traffic plugin %s", legacy)
    target = plugin_path(root)
    log.info("installed the native traffic plugin into %s", target)
    return target
