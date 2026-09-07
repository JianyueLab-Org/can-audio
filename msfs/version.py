"""版本号和 build 号。

`VERSION` 是手工维护的语义版本；`build()` 不能手工维护——手写的 build 号第一天
就会忘了改，然后所有用户报上来的都是同一个号，等于没有。

难点在于**打包之后程序里没有 git**：PyInstaller 不会把 `.git` 带上，运行时再去
问 git 只会得到"不是仓库"。所以分两条路：

- 打包时：`gui.spec` 调 `freeze()`，把当时的 git 状态写成 `buildinfo.json` 一起
  打进包里；运行时优先读它。
- 从源码跑：现问 git，顺带标出工作区脏没脏（`+dirty`）——开发机上跑的多半是改了
  一半的代码，报上来的号必须能看出这一点，否则会拿它去比对某个提交，白查半天。

两条都不行就返回 "dev"，不抛异常：显示不出 build 号是小事，为它起不来是大事。

build 号取 `提交数.短哈希`（如 `71.667d9b4`）。提交数是单调递增的，用户说"我这
个比他那个新"时能直接比；短哈希则能精确定位到是哪一次提交。
"""

import json
import logging
import os
import subprocess
import sys

log = logging.getLogger("version")

# 手工维护的**回退**值。真正发出去的版本号由 CI 决定：推到 main 之后，
# 工作流数一遍已有的 v2.2.* 标签，取最大的那个 +1，写进 buildinfo.json。
# 从源码跑时没有 buildinfo.json，就显示这个。
#
# 主次版本（2.1）是人的决定，改这里的同时**必须**把 release.yml 的 SERIES
# 一起改成同一个系列：两边不一致的话，CI 会照旧在老系列里递增，而从源码跑
# 的人看到的是新系列——同一份代码报两个版本号，还没有任何地方会报错。
VERSION = "2.2.0"

# ---- Dev 测试版 ----
# 测试版把 DEV 置 True：版本号显示成**下一个补丁号**加 (Dev Build N)。
# 最新正式版是 v2.1.1 时，第一个 Dev 包就是 v2.1.2 (Dev Build 1)；同一个
# 版本再出一个测试包，把 DEV_BUILD 加一（v2.1.2 (Dev Build 2)）。
#
# 取"下一个补丁号"而不是照抄最新版，是为了让测试包在排序上永远新于它基于
# 的正式版——用户拿版本号对话时（"我这个比他新"）不会把测试包错认成旧版。
#
# **这个常量只管从源码跑的情况，管不着 CI 打的包**，而且是故意的。以前它管：
# release.yml 每次推到 main 都打一个包，却从来不去改 version.py（工作流里没有
# 任何一条 sed 碰它），于是 `DEV = True` 一进 main，之后每个正式包都把自己报成
# 下一个补丁号——打了 v2.2.3 的包报 2.2.4，标题栏写 "2.2.4 (Dev Build 1)"。
# 后果不止是显示难看：查更新是数值比较，装了 v2.2.3 的人自报 2.2.4，等
# v2.2.4 真的发出来时 CompareVersions("2.2.4","2.2.4") 不大于 0，服务端说
# "已经是最新"——**每个版本都会永久跳过它的下一个版本**，而且四个客户端
# 一起，没有任何地方会报错。
#
# 所以判断改成看事实而不是看常量：CI 打包时 `freeze()` 会把它算出来的正式
# 版本号写进 buildinfo.json 并标上 RELEASE_KEY，`is_dev()` 认这个标记。忘了
# 翻常量不再有后果，因为没有什么要翻的。
DEV = True
DEV_BUILD = 1

# CI 用它把算出来的版本号传进打包过程（gui.spec 会调 freeze()）。
VERSION_ENV = "CAN_VERSION"

BUILDINFO_NAME = "buildinfo.json"

# buildinfo.json 里那个"这是 CI 打的正式包"的标记。
#
# 为什么不是"有 buildinfo.json 就算正式包"：在组件目录里手工跑一次
# `pyinstaller gui.spec` 也会写出一个（`freeze()` 写在 SPEC 所在目录），而且
# 它就留在源码树里。那之后从源码跑就会被当成正式包，报一个根本没发过的号。
# CI 和手工打包的区别是环境变量 CAN_VERSION——那个号是工作流数 tag 算出来、
# 真的会被打成 tag 发出去的——所以 `freeze()` 把"有没有拿到它"记下来。
RELEASE_KEY = "release"

_cached = None
_cached_version = None


def _here():
    return os.path.dirname(os.path.abspath(__file__))


def _resource_dir():
    """打包之后资源在 sys._MEIPASS，从源码跑就是本目录。"""
    return getattr(sys, "_MEIPASS", _here())


def _git(*args, cwd=None):
    # Windows 上必须 shell=False + 显式超时：git 不在 PATH 时 subprocess 会直接
    # 抛 FileNotFoundError，卡住比抛异常更糟
    result = subprocess.run(("git",) + args, cwd=cwd or _here(),
                            capture_output=True, text=True, timeout=5)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git 返回非零")
    return result.stdout.strip()


def from_git(cwd=None):
    """问 git 要 build 号。不是仓库、或者没装 git 就返回 None。"""
    try:
        count = _git("rev-list", "--count", "HEAD", cwd=cwd)
        short = _git("rev-parse", "--short", "HEAD", cwd=cwd)
    except Exception as e:
        log.debug("could not read the git info: %s", e)
        return None
    build = f"{count}.{short}"
    try:
        if _git("status", "--porcelain", cwd=cwd):
            build += "+dirty"
    except Exception:
        pass
    return build


def _buildinfo():
    path = os.path.join(_resource_dir(), BUILDINFO_NAME)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.debug("could not read %s: %s", path, e)
        return {}


def _from_file():
    return (_buildinfo().get("build") or "").strip() or None


def release_version():
    """最新正式版的版本号。打包的读固化值，从源码跑就用上面那个回退值。

    固化值优先是关键：CI 每次推到 main 都会算一个新的补丁号，而 VERSION 这个
    常量不会跟着动——只信常量的话，所有自动发出去的包都会显示同一个版本号。
    """
    global _cached_version
    if _cached_version is None:
        _cached_version = (_buildinfo().get("version") or "").strip() or VERSION
    return _cached_version


def is_release_build():
    """这一份是不是 CI 打出来的正式包。

    看的是 buildinfo.json 里的标记，不是 DEV 常量：常量要人记得翻，而这个
    标记是打包时按事实写下的。故意不缓存——缓存了就得再多一个测试要记得
    重置的全局量，而这只是读一个几十字节的文件。
    """
    return bool(_buildinfo().get(RELEASE_KEY))


def is_dev():
    """对外要不要按测试版报告版本号。

    **正式包永远说不是**，哪怕 DEV 还留在 True——见上面 DEV 那段。DEV 于是
    只影响从源码跑和手工打的包，那两种本来就是测试版。
    """
    return bool(DEV) and not is_release_build()


def _bump_patch(number):
    """2.1.1 → 2.1.2。从后往前找到第一段纯数字加一。"""
    pieces = str(number).split(".")
    for index in range(len(pieces) - 1, -1, -1):
        if pieces[index].isdigit():
            pieces[index] = str(int(pieces[index]) + 1)
            break
    return ".".join(pieces)


def version():
    """对外报告的版本号。Dev 包报下一个补丁号，正式包就是正式号。

    **查更新拿的就是这个号**，所以正式包报错了不只是难看：服务端按数值比，
    自报大一号的包会被判成"已经是最新"，然后永久收不到下一个版本。
    """
    base = release_version()
    return _bump_patch(base) if is_dev() else base


def display():
    """标题栏用的版本串（不带 v 前缀）。

    正式版就是 "2.1.1"；Dev 版是 "2.1.2 (Dev Build 1)"——用户一眼能看出
    自己跑的是测试包，报问题时也能说清是第几个测试包。
    """
    if is_dev():
        return f"{version()} (Dev Build {DEV_BUILD})"
    return version()


def build():
    """build 号。打包的读固化值，源码的现问 git，都没有就是 dev。"""
    global _cached
    if _cached is None:
        _cached = _from_file() or from_git() or "dev"
    return _cached


def full():
    """给界面和日志用的完整版本串。Dev 包把两种号都带上：Dev Build 号给
    用户对话用，git build 号给排查定位用。"""
    if is_dev():
        return f"v{version()} (Dev Build {DEV_BUILD}; build {build()})"
    return f"v{version()} (build {build()})"


def freeze(target_dir, source_dir=None):
    """打包时把版本号和当前 git 状态写进 target_dir/buildinfo.json。

    给 gui.spec 调用。版本号优先取环境变量 CAN_VERSION——CI 推到 main
    之后会算出下一个补丁号（v2.1.xxx 里的 xxx）并从那里传进来；本地打包没设
    这个变量，就用 VERSION。

    **拿没拿到 CAN_VERSION 也一起写下来**（RELEASE_KEY）：拿到了就是 CI 在打
    一个真的会被打成 tag 发出去的包，`is_dev()` 认这个标记而不认 DEV 常量。
    CAN_VERSION 没设时标记是 False，于是手工打的包照旧显示成测试版——它本来
    就是。万一哪天 CI 忘了传这个变量，包会显示成 Dev 而不是悄悄报一个从来
    没发过的号，这也是有意的：错得看得见比错得安静好。

    写不出来也不让打包失败——没有 build 号的包仍然是能用的，为了一行版本号让
    CI 挂掉不值得，只是会退回显示 "dev"。
    """
    value = from_git(cwd=source_dir or target_dir) or "dev"
    supplied = (os.environ.get(VERSION_ENV) or "").strip()
    number = supplied or VERSION
    path = os.path.join(target_dir, BUILDINFO_NAME)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"version": number, "build": value,
                       RELEASE_KEY: bool(supplied)}, f,
                      ensure_ascii=False)
    except Exception as e:
        print(f"警告: 写不了 {path}（build 号会显示成 dev）: {e}")
        return None
    print(f"版本 {number}，build 号 {value}"
          + ("" if supplied else f"（没有 {VERSION_ENV}，按测试包处理）"))
    return path


if __name__ == "__main__":
    print(full())
