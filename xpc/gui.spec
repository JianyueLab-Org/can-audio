# -*- mode: python ; coding: utf-8 -*-

import ctypes.util
import os
import sys

# build 号在这里固化。打包之后程序里没有 .git，运行时再问 git 只会得到"不是
# 仓库"——所以打包时把 git 状态写成 buildinfo.json 一起打进去。
sys.path.insert(0, os.path.dirname(os.path.abspath(SPEC)))
import version

version.freeze(os.path.dirname(os.path.abspath(SPEC)))

# qfluentwidgets 的 QSS、SVG 图标和内置字体是编进 Qt resource 模块（*_rc.py）的，
# 跟着普通 import 就一起打进去了，不用 collect_data_files——实测收出来是 0 个文件。
#
# 但**不要**用 collect_submodules('qfluentwidgets')：它会把整个包里的模块都列成
# 隐式导入，其中几个引用了 scipy / pillow / colorthief（那是它 `full` 附加功能的
# 可选依赖，做亚克力模糊和取图片主色用的，这里一个都没用到），于是 PyInstaller
# 把 scipy 一整套打了进来，包会涨出上百兆。
excludes = ['torch', 'transformers', 'scipy', 'pyttsx3', 'matplotlib',
            'PIL', 'colorthief']


def find_opus():
    """定位 opus 原生库。

    pymumble 的 opuslib 是运行时用 ctypes 去加载 opus 的，PyInstaller 的静态
    分析看不到这条依赖，不显式打进来的话，装机上没有 opus 的用户一启动就会
    报 "Could not find Opus library"。
    """
    found = ctypes.util.find_library('opus')
    if found and os.path.isfile(found):
        return found

    candidates = [os.path.join(os.path.dirname(os.path.abspath(SPEC)), 'opus.dll')]
    try:
        # pyogg 的 wheel 里带了 Windows 版 opus.dll，装了就直接拿来用
        import pyogg
        pyogg_dir = os.path.dirname(pyogg.__file__)
        candidates.append(os.path.join(pyogg_dir, 'opus.dll'))
        candidates.append(os.path.join(pyogg_dir, 'libs', 'opus.dll'))
    except Exception:
        pass

    for path in candidates:
        if os.path.isfile(path):
            return path

    print('警告: 没有找到 opus.dll，打出来的程序在没有 opus 的机器上无法启动。'
          '把 opus.dll 放到 xpc 目录下再打包即可。')
    return None


opus_path = find_opus()
opus_binaries = [(opus_path, '.')] if opus_path else []


def find_rnnoise():
    """rnnoise.dll 是 denoise.py 运行时用 ctypes 加载的，PyInstaller 看不见。

    CI 用 native/rnnoise/build.ps1 构建后放进组件目录。缺了不会打包失败，
    只会让用户拿到一个没有降噪的包，所以这里要警告、发布流程要验。
    """
    path = os.path.join(os.path.dirname(os.path.abspath(SPEC)), 'rnnoise.dll')
    if os.path.exists(path):
        return [(path, '.')]
    print('警告: 没有找到 rnnoise.dll，打出来的程序没有麦克风降噪。'
          '先运行 native/rnnoise/build.ps1 -Out <组件目录>。')
    return []


a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=opus_binaries + find_rnnoise(),
    datas=[
        # 窗口图标要随程序分发，否则打包后运行时取不到
        ('favicon.ico', '.'),
        # X-Plane 插件。它不在这个进程里跑——是给用户复制到
        # <X-Plane>/Resources/plugins/PythonPlugins/ 的，所以只是随包带上。
        ('plugin/PI_XpcTraffic.py', 'plugin'),
        ('buildinfo.json', '.'),
    ],
    hiddenimports=[
        'pymumble_py3',
        'google.protobuf',
        # Fluent 外观
        'qfluentwidgets',
        'qframelesswindow',
        # pynput 的平台后端是运行时按名字 import 的（pynput/_util/__init__.py 的
        # backend()），静态分析看不见。少了它们，打包后的程序照常启动，只是键盘和
        # 鼠标侧键 PTT 一按没反应——而摇杆那条路还是好的，看起来像"就这个键坏了"。
        'pynput.keyboard._win32',
        'pynput.mouse._win32',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='xpc-for-can',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['favicon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='xpc-for-can',
)
