# Can 语音服务端：Mumble/Murmur + Ice 认证器（server/login.py）。
#
#   docker build -t can-audio-server .
#   docker run -d --name can-server --restart unless-stopped \
#     -p 64738:64738/tcp -p 64738:64738/udp \
#     -v can-mumble:/var/lib/mumble-server \
#     -e MUMBLE_SUPERUSER_PASSWORD=换成你自己的 \
#     can-audio-server
#
#   docker logs -f can-server
#   docker exec can-server python3 fix_acl.py --apply    # 头一次要跑
#
# 四件和直觉相反的事，都写在这里免得下次再踩：
#
# 1. **不要发布 6502。** Ice 绑在 127.0.0.1，认证器和它在同一个容器里，回环就
#    够了。docker 的端口转发也到不了绑在回环上的监听，映出去根本不生效；真要
#    让它生效，等于把只靠一个口令保护的管理接口摆到公网上。
# 2. **口令不进镜像。** 写进 Dockerfile 的口令会永远留在镜像层和 docker
#    history 里，而且"一个能用的默认口令永远不会被改"。Ice 口令由 start.sh 在
#    容器启动时随机生成、写进 mumble-server.ini —— serverconf.py 的第 3 顺位
#    本来就是读那个文件的 icesecretwrite，所以什么都不用配；SuperUser 密码只
#    从 MUMBLE_SUPERUSER_PASSWORD 来，不给就不设。
# 3. **数据库必须挂出来。** SuperUser 密码、注册用户，以及 fix_acl.py 授的
#    Listen / MakeTempChannel 权限，全在 /var/lib/mumble-server 的 sqlite 里。
#    不挂卷的话每换一次容器就得重授一次权限，而症状是管制端除了主频道以外全
#    是静音 —— 完全看不出和换容器有关。
# 4. **serverconf.py 必须一起复制进来。** login.py 的第一件事就是
#    `import serverconf`，缺了它容器起来就退出；而 login.py 不在的时候
#    Murmur 会回落到自己的空账号库，所有人登录被拒、客户端显示的是"密码错误"。
#
# 基础镜像刻意钉死版本而不是用 latest：整份文件依赖 mumble-server ≥ 1.5
# （频道监听的 Listen 权限是 1.4 才有的，1.3 上管制端听不到非主频道；客户端
# 用的 pymumble-udp 只发 1.5 的 protobuf 音频格式，1.4 收到也不认，所有人
# 互相听不见），而 latest 会漂。换别的基础镜像只有这一条要求，下面的 RUN 会
# 当场校验。
FROM ubuntu:26.04

# 避免交互式提示
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Asia/Shanghai

# 通播机队（server/ATIS/）默认不装依赖：它不在 CMD 里，而 numpy + ffmpeg 会让
# 镜像大一大圈。要跑它就 --build-arg WITH_ATIS=1。
ARG WITH_ATIS=0

# 安装系统依赖。
#   python3-zeroc-ice / zeroc-ice-*  —— login.py 的 Ice 绑定和 slice2py
#   python3-requests                 —— 走 apt 而不是 pip，省掉 --break-system-packages
#   tzdata                           —— 不装的话上面那个 TZ 是空设，日志还是 UTC
#   procps                           —— HEALTHCHECK 里的 pgrep
# 等端口用的是 bash 自带的 /dev/tcp，所以不需要 nc。
RUN apt-get update && apt-get install -y --no-install-recommends \
    mumble-server \
    python3 \
    python3-requests \
    python3-zeroc-ice \
    zeroc-ice-compilers \
    zeroc-ice-slice \
    ca-certificates \
    tzdata \
    procps \
    && rm -rf /var/lib/apt/lists/*

# 通播机队的依赖，只有 WITH_ATIS=1 时才装。
#   ffmpeg   —— mumble.py 用它把 edge-tts 的 mp3 转成 wav（subprocess 直接调）
#   libopus0 —— opuslib 用 ctypes 加载原生 opus，没有就 Could not find Opus library
#   tabulate —— request.py 导入它，缺了整个 ATIS 包都导不进来
RUN if [ "$WITH_ATIS" = "1" ]; then \
        apt-get update && apt-get install -y --no-install-recommends \
            ffmpeg libopus0 python3-pip \
        && rm -rf /var/lib/apt/lists/* \
        && pip3 install --break-system-packages --no-cache-dir \
            pymumble numpy edge-tts tabulate; \
    fi

# 探路径 + 校验版本，结果写进 /etc/can/paths.env 给 start.sh 用。
#
# 三处都不能写死：1.5 的 ini 在 /etc/mumble/mumble-server.ini，1.3 在
# /etc/mumble-server.ini；slice 文件的位置和名字也随版本变（1.5 起 Murmur.ice
# 改名 MumbleServer.ice，login.py 两个都 import 得了，先 MumbleServer 后
# Murmur）。写死的话换一个基础镜像就是构建成功、运行时才莫名其妙。
# 版本从 dpkg 拿而不是 mumble-server -version：murmur 对不认识的参数打的是
# usage，从那里 grep 数字什么都可能抓到。注意 dpkg 版本可能带 epoch（1:1.5.735）
# 和 debian 后缀（-2build1），grep 只取中间的语义版本。
RUN set -eu; \
    mkdir -p /etc/can /app/server; \
    ver="$(dpkg-query -W -f '${Version}' mumble-server 2>/dev/null | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -n1 || true)"; \
    if [ -z "$ver" ]; then \
        echo "读不出 mumble-server 的版本号（不是 dpkg 装的？），跳过版本校验" >&2; \
    else \
        major="${ver%%.*}"; rest="${ver#*.}"; minor="${rest%%.*}"; \
        if [ "$major" -lt 1 ] || { [ "$major" -eq 1 ] && [ "$minor" -lt 5 ]; }; then \
            echo "mumble-server $ver 太老。客户端只发 1.5 的 protobuf 音频格式，" >&2; \
            echo "更老的服务器收到也不认，所有人互相听不见，而且客户端毫无提示。" >&2; \
            echo "换一个带 mumble-server >= 1.5 的基础镜像。" >&2; \
            exit 1; \
        fi; \
    fi; \
    ini=""; \
    for p in /etc/mumble/mumble-server.ini /etc/mumble-server.ini; do \
        if [ -f "$p" ]; then ini="$p"; break; fi; \
    done; \
    [ -n "$ini" ] || { echo "找不到 mumble-server.ini" >&2; exit 1; }; \
    slice=""; \
    for p in /usr/share/mumble-server/MumbleServer.ice /etc/mumble/MumbleServer.ice \
             /usr/share/slice/MumbleServer.ice /usr/share/mumble-server/Murmur.ice \
             /etc/mumble/Murmur.ice /usr/share/slice/Murmur.ice; do \
        if [ -f "$p" ]; then slice="$p"; break; fi; \
    done; \
    [ -n "$slice" ] || { echo "找不到 Mumble 的 Ice slice 文件" >&2; exit 1; }; \
    inc=""; [ -d /usr/share/ice/slice ] && inc="-I/usr/share/ice/slice"; \
    slice2py $inc --output-dir /app/server "$slice"; \
    printf 'MUMBLE_INI=%s\nMUMBLE_VERSION=%s\n' "$ini" "$ver" > /etc/can/paths.env; \
    echo "ini=$ini slice=$slice version=$ver"

# 改 ini：Ice 只听回环，日志走 stdout（logfile 留空 Murmur 就往控制台打，否则
# 日志进文件、docker logs 是空的）。口令不在这里写，见文件开头第 2 条。
#
# **写到文件最前面，不在原地改。** Murmur 用 QSettings 读这个 ini，而
# QSettings 的 ini 是**分节**的：一个 key 归属它上面最近的那个 [section]，
# 顶层的 ice 和某个节里的 ice 是两个不同的东西。原来是就地把注释掉的示例
# 那一行取消注释——只要那一行落在任何一个 [section] 底下，Murmur 就读不到
# 它，而且**一声不吭**：日志里连一行 Ice 都没有，6502 不开，看起来完全像
# 这个包没编进 Ice（它编了，mumble-server 依赖 libzeroc-ice3.7t64）。
# 症状是 start.sh 等 30 秒超时、login.py 根本没起来、健康检查永远 unhealthy。
# 先把所有 ice= 行删干净（重复的 key QSettings 取最后一个），再插到第 1 行，
# 那里一定在任何节之前。
RUN set -eu; \
    . /etc/can/paths.env; \
    sed -i '/^#\?ice=/d; /^#\?logfile=/d' "$MUMBLE_INI"; \
    awk 'BEGIN { print "ice=\"tcp -h 127.0.0.1 -p 6502\""; print "logfile=" } { print }' \
        "$MUMBLE_INI" > "$MUMBLE_INI.new"; \
    cat "$MUMBLE_INI.new" > "$MUMBLE_INI"; \
    rm -f "$MUMBLE_INI.new"; \
    echo "ini 里的节和 Ice 相关行："; \
    grep -nE '^\[|^#?ice|^(logfile|database|uname)=' "$MUMBLE_INI" || true

# 复制服务端代码。
#   serverconf.py —— login.py 直接 import，漏了容器起不来
#   fix_acl.py    —— 授 Listen / MakeTempChannel，Ice 只听回环所以只能在容器里跑
#   whereami.py   —— 查"谁在哪个频道"，排障用
COPY server/serverconf.py server/login.py server/fix_acl.py server/whereami.py /app/server/
COPY server/start.sh /app/server/
COPY server/ATIS/ /app/server/ATIS/

# 设置工作目录：login.py 是平铺 import（serverconf、生成的 MumbleServer），
# 必须从这里跑
WORKDIR /app/server

RUN chmod +x /app/server/start.sh

# Mumble 数据库：SuperUser 密码、注册用户、根 ACL 都在里面
VOLUME /var/lib/mumble-server

# 只暴露语音端口。Ice 的 6502 故意不暴露，见文件开头第 1 条。
EXPOSE 64738/tcp
EXPOSE 64738/udp

# 认证器死了但容器还活着，是最难发现的故障：语音端口照常应答，所有人却都登不上。
# pgrep 的模式必须写成 [l]ogin —— 健康检查自己的 sh -c 命令行里就带着这串字，
# 直接写 login.py 会匹配到自己，于是 login.py 死没死它都报健康。
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD bash -c '(exec 3<>/dev/tcp/127.0.0.1/64738) 2>/dev/null && pgrep -f "[l]ogin\.py" > /dev/null'

# 启动
CMD ["./start.sh"]
