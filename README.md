# airwaysn_audio

Cerulean Aviation Network（原 Cerulean Aviation Network）的**语音层**。基于 Mumble：飞行员的模拟器
COM1 频率决定他坐在哪个 Mumble 频道，管制员用一个电台栈同时守听/发话多个频率，
情报通播由服务端的机器人把合成语音播到各自的频率上。

管制客户端的使用说明在 [can-audio-client](https://github.com/JianyueLab-Org/can-audio-client)；给 AI 助手看的工程约定在
[`CLAUDE.md`](CLAUDE.md)。这份 README 是给要维护语音引擎、部署服务端的人看的。

网络的另外两块在隔壁仓库：**can-fsd**（Go 写的 FSD 服务端 + 数据源）和
**can-web**（网站、账号、雷达）。这个仓库只做语音，不参与飞行数据。

---

## 组件

| 目录 | 入口 | 是什么 |
|---|---|---|
| `controller/` | `bridge.py` | 无界面语音引擎：Mumble 连接、音频收发、混音、降噪、增益。由 C# 管制客户端启动，经 stdin/stdout 的 JSON Lines 通信 |
| `server/` | `login.py`、`ATIS/mumble.py` | 跑在 Mumble 主机上：Ice 认证器 + 服务端通播机队 |
| `native/rnnoise/` | `build.ps1` | 可选的 RNNoise 降噪原生库构建 |

这是 `new-client-sys` 分支：只放**客户端与 Python 开发共同维护的语音部分**和服务端。
界面、设置、PTT 输入、电台栈规则都在 C# 客户端
[can-audio-client](https://github.com/JianyueLab-Org/can-audio-client) 里；
飞行员客户端由 C# 的 can-pilot-client 维护。旧的 Python 管制 GUI、桌面通播客户端
（`atis/`）和飞行员客户端仍在 `main` 分支的历史里。

C# ↔ 语音引擎的协议、文件分工、测试和打包见 [`controller/README.md`](controller/README.md)。
改协议要两个仓库一起改。

`mumblecompat.py` 在 `controller/` 和 `server/ATIS/` 各有一份，必须逐字节相同，
`server/ATIS/test_mumble.py` 会核对。

---

## 跑起来

需要 **Python 3.12**（3.13+ 没有 PyAudio 的 wheel）。Windows 上 requirements 文件里有中文注释，
pip 默认按 GBK 读会失败，先设 `PYTHONUTF8=1`：

```powershell
py -3.12 -m venv .venv
$env:PYTHONUTF8 = 1
.\.venv\Scripts\python.exe -m pip install -r requirements-voice.txt
```

客户端使用仓库的 `pymumble_udp` 分支，而不是 PyPI 上只有 TCP tunnel 的旧版
`pymumble`。它对外仍导入为 `pymumble_py3`，并会在 Mumble 1.5+ 服务端上使用
加密 UDP 音频；UDP 不可用时自动回退到 TCP tunnel。两个包装进同一个
`pymumble_py3` 目录，以前装过 PyPI 版的先 `pip uninstall pymumble`。

pymumble 通过 ctypes 加载原生的 **opus** 库；`controller/opus.dll` 已提交，
`bridge.py` 启动时会把自己的目录加进 `PATH`。

语音引擎本身没有界面，平时由 C# 客户端启动。手动调试可以直接跑，往 stdin 里一行一条命令：

```powershell
.\.venv\Scripts\python.exe -u controllerridge.py
{"id": 1, "command": "devices"}
```

---

## 测试

都**不需要服务器、不需要音频设备、不需要网络**：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s controller -v
cd server;        python -m unittest test_login test_serverconf test_fix_acl -v
cd server\ATIS;   python -m unittest test_mumble test_atisrouting -v
```

**写测试请断言真实行为**——耗时、状态、真的收发了什么——而不是"有没有调用某个
函数"。这条不是洁癖：仓库里已经有好几个 bug 是因为测试只对源码做字符串匹配而
漏掉的，其中一个让飞行员在掉线重连后对着根频道说话，而帧数计数一路正常。

---

## 打包

语音引擎打成单文件 `can-voice.exe`，命令见 [`controller/README.md`](controller/README.md#开发与测试)。
C# 客户端的发行脚本会把它放进发行包的 `controller/` 目录。

---

## 服务端

跑在 Mumble/Murmur 主机上。

```bash
cd server
./start.sh            # 起 mumble-server + login.py
./start.sh --login    # 只起 login.py
cd ATIS && python3 mumble.py          # 通播机队，需要 PATH 里有 ffmpeg
```

`start.sh` 里**任何一个进程退出，整个退出**（容器上配 `--restart
unless-stopped` 让外面重新拉起）。两个方向都会出事：`login.py` 不在，Murmur
回落到自己的空账号库，所有人登录被拒而客户端显示"密码错误"；`mumble-server`
重启之后认证器不会被自动重新注册，`login.py` 的 `watch_connection()` 察觉链路
断了会主动退出——"退出比假装还活着好"。

### Docker

这个分支暂时没有 GitHub CI，镜像需要手动构建并推到 GHCR，服务器上用 compose 部署：

```bash
cp .env.example .env      # 改里面的密码；全都可以留空
docker compose up -d
docker compose logs -f
docker compose exec mumble python3 fix_acl.py --apply   # 头一次要跑，见下面"权限"

docker compose pull && docker compose up -d             # 升级
```

包是私有的话，服务器上先 `docker login ghcr.io`（密码用一个带 `read:packages`
的 PAT），或者把这个包在 GitHub 上改成 public。想回滚就在 `.env` 里把
`SERVER_IMAGE` 指到某个 `sha-xxxxxxx` 标签——每次构建都会推一个。

本地不用 compose 也行：

```bash
docker build -t can-audio-server .
docker run -d --name can-server --restart unless-stopped \
  -p 64738:64738/tcp -p 64738:64738/udp \
  -v can-mumble:/var/lib/mumble-server \
  -e MUMBLE_SUPERUSER_PASSWORD=换成你自己的 \
  can-audio-server
```

**构建完一定要冒烟**：这个镜像的失败模式全是"构建成功、运行时才发现"
（漏了 `serverconf.py`、`start.sh` 没有执行位、ini 或 slice 路径探错），在
`docker build` 的输出里一律是绿色的。所以构建完要真的把容器跑起来，等它自己的
HEALTHCHECK 变 healthy，再确认日志里出现了「认证器已设置」和「服务器回调已注册」。

四条别绕过去的：

- **不要发布 6502。** Ice 绑在 127.0.0.1，认证器和它同容器，回环就够；映出去
  等于把只靠一个口令保护的管理接口摆到公网上（何况绑在回环上时那个映射根本
  不生效）。
- **口令不进镜像**，写进 `Dockerfile` 的会永远留在镜像层和 `docker history`
  里。Ice 口令由 `start.sh` 启动时随机生成写进 ini，`serverconf.py` 自己会去
  读；SuperUser 密码只从 `MUMBLE_SUPERUSER_PASSWORD` 来。
- **`/var/lib/mumble-server` 必须挂卷**：SuperUser 密码、注册用户和 `fix_acl.py`
  授的权限都在那个 sqlite 里。不挂的话每换一次容器就得重授一次，而症状是管制端
  除了主频道以外全是静音，看不出和换容器有关。
- **基础镜像钉死版本**，因为 Listen 权限是 Mumble 1.4 才有的，构建时会校验
  `mumble-server >= 1.4` 并当场失败。

通播机队默认不装依赖（numpy + ffmpeg 太大，而且不在 `CMD` 里），GHCR 上那个
镜像也不带。要跑它用 compose 的 `atis` profile，那是本地构建的：

```bash
docker compose --profile atis up -d --build
```

`.env` 里要有 `ATIS_PASSWORD`。注意 `login.py` 里的保留账号旁路已经去掉了，
那个 cid（默认 900）得在 can-web 那边真实存在。

`login.py` 是 Ice 认证器：网络没有本地账号，登录一律转给 can-web 的
`/api/v1/public/auth` 校验。它没跑起来的话，谁都连不上语音。Ice 绑定要在主机上
自己生成（已 gitignore）：

```bash
slice2py /usr/share/mumble-server/MumbleServer.ice
```

### 口令

**仓库里没有口令，也不要往里放。** `server/serverconf.py` 按顺序找：

1. 环境变量 `MUMBLE_ICE_SECRET` / `ATIS_PASSWORD` / `ATIS_CID`
2. `server/server_secrets.json`（已 gitignore，格式见 `server_secrets.example.json`）
3. 仅 Ice 口令：`/etc/mumble/mumble-server.ini` 里的 `icesecretwrite`

第 3 条意味着**正常的主机通常不用配任何东西**——那个口令本来就在 ini 里。

刻意没有兜底默认值：源码里一旦有一个能用的默认口令，它就永远不会被改，而且会
跟着仓库到处走。缺失时：Ice 口令缺了 `login.py` / `fix_acl.py` 非零退出并告诉你
三个可放的位置；ATIS 口令缺了 `login.py` 照常服务普通用户，只是通播保留账号那条
捷径关掉，而 `server/ATIS/mumble.py` 拒绝启动。

### 权限

Mumble 默认的根 ACL 不一定够。两个看起来毫不相干的故障其实是同一个缺口：

| 症状 | 缺的权限 |
|---|---|
| 管制端「没有权限（频道监听需要 Listen 权限）」 | `Listen` `0x800` |
| 情报台「Channel FREQ_127800 does not exists」 | `MakeTempChannel` `0x400` |

`Listen` 是 Mumble 1.4 才加的，**从 1.2/1.3 升级上来的服务器会留着一份没有它的
旧 ACL**。频率频道都是根的临时子频道、继承根的 ACL，所以在根上授一次就够：

```bash
python3 server/fix_acl.py            # 只看不改
python3 server/fix_acl.py --apply    # 真的写进去
```

---

## 几条踩过的坑

- **pymumble 1.6.1 在 Python 3.12 上开箱即坏。** 它用 `ssl.wrap_socket()` 建 TLS，
  而这个函数 3.12 已经删除，它的 `except AttributeError` 兜底又调回同一个函数。
  异常从 pymumble 自己的线程里抛出去，外面只看到"服务器拒绝连接"，于是你会去查
  密码，而 TLS 握手根本没开始。`mumblecompat.install()` 补上这个函数，**所有说
  Mumble 的组件都在 import pymumble 之前调用它**——包括 `server/ATIS/mumble.py`，
  它一直漏着，而 Debian 13 / 新一点的 Ubuntu 上 python 都 ≥3.12，通播机队装上就
  连不上。
- **pymumble 的阻塞命令永远不会超时。** `channels.new_channel()` 和
  `users.myself.move_in()` 都走 `execute_command(blocking=True)`，那个
  `lock.acquire()` 没有任何超时（pymumble 源码里就写着 TODO）。命令没被处理就
  永久卡死，症状是日志的*缺失*——停在"建一个临时的"之后既没成功也没报错。
  **任何组件都不许调这两个包装**，自己发 `CreateChannel` / `MoveCmd`，再轮询确认
  结果。`controller/voice.py` 是参考实现。
- **进没进频道是服务器说了算的，不能靠本地记账。** 连接都是 `reconnect=True`，
  重连之后服务器会把人放回根频道，而本地记的频率还是旧的。只比对本地值的话会
  以为"已经到位"而再也不切。另外根频道的 `channel_id` 就是 `0`，判断"有没有进
  频道"必须用 `is None`。
- **`favicon.ico` 其实是 PNG**，扩展名不对。Qt 认内容所以窗口图标没问题，但
  PyInstaller 6 会拒绝它当 exe 图标，除非装了 `pillow` 帮它转换。

更多背景和逐条出处见 `CLAUDE.md`。
