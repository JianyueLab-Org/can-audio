# XPC for CAN 原生 X-Plane 插件

他机由这个插件画进 X-Plane。它是 X-Plane 直接加载的 `.xpl`，**不需要 XPPython3**，
也不需要 Python。渲染部分照搬 [swift](https://github.com/swift-project/pilotclient)
的 xswiftbus：模型的加载、匹配、绘制、TCAS 都交给 swift 用的 xplanemp2，客户端
（`xpc-for-can.exe`）只负责把网络上的飞机经本机 UDP 交给它。

支持 X-Plane 11.50 以上和 X-Plane 12，Windows。

## 1. 装插件

**一般用客户端装**：设置 → 他机 → 「安装插件」。它会找到 X-Plane 目录，把插件
拷过去，并删掉旧的 Python 插件。装完重启 X-Plane。

手工装的话，把整个 `XPC_for_CAN` 文件夹拷进 X-Plane 的插件目录，结果应当是：

```text
X-Plane 12/
└── Resources/
    └── plugins/
        └── XPC_for_CAN/
            ├── 64/
            │   └── win.xpl            ← X-Plane 加载的就是它
            ├── LegacyData/
            │   ├── related.txt        ← 机型族（A319/A320/A321 互相顶替）
            │   └── Doc8643.txt        ← ICAO 机型表
            └── CSL/                   ← 可选：放模型的地方之一（见下文）
```

`LegacyData` 不能少：没有它插件照样启动，但按机型族找替代模型的那一档全部失效，
大部分飞机会掉到默认的 A320。

**旧的 Python 插件必须删掉**：
`Resources/plugins/PythonPlugins/PI_XpcTraffic.py`。它和原生插件抢同一个端口
（127.0.0.1:49900），谁先起来谁收数据，另一个一架飞机也画不出来。

## 2. 放 CSL 模型（以 X-CSL 为例）

插件本身不带机模，机模来自 CSL 模型包。下面用 [X-CSL](https://x-csl.ru/downloads)
举例，Bluebell 等其他 CSL 包放法完全一样。

### 2.1 下载并安装 X-CSL

1. 从 <https://x-csl.ru/downloads> 下载 X-CSL 的安装程序（X-CSL Package / Updater）。
2. 安装时它会问装到哪个文件夹。**两种选法都可以，任选其一**：

   | 选法 | 装到哪里 | 之后要做什么 |
   |---|---|---|
   | A（推荐） | `X-Plane 12\Resources\plugins\XPC_for_CAN\CSL` | 什么都不用做，插件启动时自己加载这个目录 |
   | B | 任意目录，例如 `D:\CSL`，或者 xPilot / swift 已经在用的那个 CSL 目录 | 在客户端 设置 → 他机 → 「CSL 模型目录」里选这个目录 |

   选法 B 适合已经给 xPilot、swift 装过 X-CSL 的人——同一份模型几个客户端共用，
   不用再占一份硬盘。

装好之后目录大致是这样（X-CSL 的子文件夹名以安装程序实际生成的为准）：

```text
CSL/                               ← 选法 A 就是 XPC_for_CAN\CSL；选法 B 是你选的目录
└── X-CSL/
    ├── A320/
    │   ├── xsb_aircraft.txt       ← 每个模型包都有一份，这是插件认包的唯一依据
    │   ├── A320.obj
    │   ├── CES.png  CCA.png …     ← 各家航司的涂装贴图
    │   └── …
    ├── B738/
    │   ├── xsb_aircraft.txt
    │   └── …
    └── …
```

### 2.2 插件是怎么读到的

记住一条：**含有 `xsb_aircraft.txt` 的文件夹就是一个模型包**。

- 插件从你给的目录往下找（最多 4 层文件夹）所有含 `xsb_aircraft.txt` 的文件夹，把它们的
  **上一级**交给 xplanemp2，xplanemp2 再把那一级下面的每个包都读进来。所以
  「CSL 模型目录」选 `D:\CSL`、`D:\CSL\X-CSL`，甚至直接选某一个包的文件夹，都能读到。
- 客户端连上之后，每一帧都会把「CSL 模型目录」告诉插件；改了目录不用重启
  X-Plane，下一帧就会加载新目录，已有的飞机会重新匹配一次。
- `XPC_for_CAN\CSL` 在插件启动时就会加载，不用等客户端。
- 两处可以同时用。**但同一个模型包不要放两份**：包名（`EXPORT_NAME`）重复时第二份
  会被跳过，Log.txt 里会有 `Package name … already in use`。
- 路径里有中文没关系（`D:\游戏\X-Plane 12\…` 可以）。

几条容易踩的坑：

- **不要把 X-CSL 拆开放。** 很多包用 `DEPENDENCY` 引用别的包里的文件，只拷其中几个
  文件夹会让依赖它们的包整包加载失败。整个 `X-CSL` 文件夹原样放。
- **模型目录要可写。** X-CSL 的涂装是「一个 `.obj` + 每家航司一张贴图」
  （`OBJ8 SOLID YES X-CSL_A320:A320.obj CES.png`）。X-Plane 的 `.obj` 只能写死一张
  贴图，所以插件第一次加载时会在原 `.obj` 旁边生成 `A320__CES.obj` 这样的副本，
  把贴图换成 `CES.png`（有 `CES_LIT.png` 的话夜间贴图一起换）。目录写不进去
  （比如放在 `C:\Program Files` 下）时，飞机还会出现，但所有航司都显示成同一套涂装，
  Log.txt 里会有 `could not write …`。这些 `__xxx.obj` 是自动生成的，删了下次会再生成。
- 不要放进 `Resources\plugins\XPC_for_CAN\64`，那里只放 `win.xpl`。
- 只用 ACF（`.acf`）或 OBJ7 的老 CSL 包不支持，Log.txt 里会说 `legacy`。

### 2.3 模型是怎么匹配的

1. 客户端先按它自己的规则挑一个模型名（`xsb_aircraft.txt` 里 `OBJ8_AIRCRAFT` 后面
   那个名字），插件在已加载的包里找到同名的就用它。
2. 找不到时，交给 xplanemp2 按 机型 + 航司 + 涂装 → 机型 + 航司 → 机型 →
   同族机型（`related.txt`）… 依次降级。
3. 什么都找不到时用默认机型 A320。

所以涂装能不能对上取决于你装的 CSL 包里有没有那家航司：X-CSL 带的是
`AIRLINE A320 CES` 这样的记录，对方 FSD 报的航司代码是 `CES` 就能对上。

## 3. 怎么确认读到了

装好后开一次 X-Plane，打开 X-Plane 根目录下的 `Log.txt`，搜 `XPC`：

```text
XPC for CAN: plugin starting (XPC_BRIDGE_PROTOCOL=3)
XPC for CAN: listening for the XPC client on 127.0.0.1:49900
XPC_for_CAN: Loading package: D:/CSL/X-CSL/A320/xsb_aircraft.txt      ← 每个包一行
XPC for CAN: loaded CSL packages from D:/CSL/X-CSL
XPC for CAN: 1532 CSL models available; re-matching every aircraft
XPC for CAN: added CES2345 (A320/CES) as A320 A320_CES                  ← 每架飞机用了哪个模型
```

| Log.txt 里看到的 | 原因 | 怎么办 |
|---|---|---|
| 没有任何 `XPC for CAN` | 插件没被加载 | 检查是不是 `plugins\XPC_for_CAN\64\win.xpl` 这个路径；在 插件 → 插件管理 里看是否被禁用 |
| `could not bind 127.0.0.1:49900` | 旧的 Python 插件还在 | 删掉 `PythonPlugins\PI_XpcTraffic.py`，重启 X-Plane |
| `could not initialise the renderer` | 缺 `LegacyData` | 把 `LegacyData` 文件夹放回 `XPC_for_CAN` 下 |
| `no CSL package (xsb_aircraft.txt) found below …` | 选的目录下面没有模型包 | 换成装 X-CSL 时选的那个目录 |
| `required package … not found` / `problems loading CSL packages` | 包的依赖缺了，或包本身有错 | 整个 X-CSL 原样放，不要拆 |
| `could not write …__xxx.obj` | 模型目录不可写 | 把 CSL 挪到可写的位置，或用管理员身份给目录加写权限 |
| 飞机都是 A320 | 模型目录没读到，或者那种机型没装 | 看上面几行有没有 `loaded CSL packages` |
| `AI aircraft are held by '…'` | LiveTraffic 之类的插件占着 AI 机位 | 只开一个，否则 TCAS 上可能看不到 |

插件菜单 插件 → XPC for CAN → Show callsign labels 可以开关飞机上方的呼号。

## 4. 编译（只有 Windows）

需要 X-Plane SDK 4.x、CMake 3.20+、Visual Studio 2022：

```powershell
cmake -S native_plugin -B native_plugin/build -DXPLANE_SDK="C:\SDK\XPSDK430" `
  -G "Visual Studio 17 2022" -A x64
cmake --build native_plugin/build --config Release
```

产物是一个可以直接拷进 X-Plane 的文件夹：
`native_plugin/build/package/XPC_for_CAN/`（`64/win.xpl` + `LegacyData/`）。
`gui.spec` 把它打进客户端，设置里的「安装插件」就是从那里拷的，所以要先编插件、
再跑 `pyinstaller gui.spec`。

## 5. 代码从哪来

| 目录 | 来源 | 许可 |
|---|---|---|
| `xplanemp2/` | swift 的 [xplanemp2](https://github.com/swift-project/xplanemp2)（commit `0f81fd1`，xswiftbus 用的那一份） | MIT，见 `xplanemp2/LICENSE.md` |
| `LegacyData/` | swift `src/xswiftbus/LegacyData` | 同 swift |
| `src/traffic.*`、`src/plugin.cpp`、`src/utils.*` | 照 swift `src/xswiftbus/traffic.cpp` 等改写：去掉 D-Bus，换成本机 UDP | GPL-3.0-or-later |
| `src/bridge.*` | 新写：`xpc/bridge.py` 的接收端 | GPL-3.0-or-later |
| `third_party/nlohmann/json.hpp` | nlohmann/json 3.11.3 | MIT |

对 xplanemp2 的改动只有三处，都标了 `XPC for CAN`：

- `OBJ8` 行带贴图参数时生成换贴图的 `.obj` 副本（X-CSL 的涂装靠这个）；
- 放在 X-Plane 目录以外的 CSL 不再被截错路径；
- Windows 上按 UTF-8 打开文件，路径里有中文也能读。
