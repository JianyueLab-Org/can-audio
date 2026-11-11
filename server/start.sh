#!/usr/bin/env bash
# 起 Mumble 服务端 + Ice 认证器（login.py）。
#
#   ./start.sh            两个都起
#   ./start.sh --login    只起 login.py（服务端已经在别处跑着）
#
# 任一进程退出就整组退出；Docker 用 --restart unless-stopped 重启。
# 公网准入不依赖监督器响应速度：Murmur 启动前强制 forceExternalAuth=true，
# 认证器尚未注册、注册失败或连接丢失时，Murmur 原生拒绝认证，不回落访客。
set -euo pipefail

cd "$(dirname "$0")"

ICE_PORT=6502
ICE_WAIT=30                 # 等 Ice 端口开的上限，秒

only_login=0
case "${1:-}" in
    --login) only_login=1 ;;
    "")      ;;
    *)       echo "用法: $0 [--login]" >&2; exit 2 ;;
esac

# ini 的位置由构建时探出来写在这里：1.5 是 /etc/mumble/mumble-server.ini，
# 1.3 是 /etc/mumble-server.ini。不在容器里跑就退回默认值。
if [ -f /etc/can/paths.env ]; then
    # shellcheck disable=SC1091
    . /etc/can/paths.env
fi
MUMBLE_INI="${MUMBLE_INI:-/etc/mumble/mumble-server.ini}"

mumble_pid=""
login_pid=""
stopping=0

shutdown() {
    trap - TERM INT EXIT
    [ -n "$login_pid" ] && kill "$login_pid" 2>/dev/null || true
    [ -n "$mumble_pid" ] && kill "$mumble_pid" 2>/dev/null || true
    wait 2>/dev/null || true
}

on_signal() {
    stopping=1
    shutdown
}
# EXIT 也要挂：set -e 中途退出（比如 wait_for_ice 超时）不走 TERM 的 trap，
# 不挂的话已经起来的 mumble-server 就被留在后台了——容器里 PID 1 一死会连坐
# 收掉，裸机上那就是个孤儿进程，还占着 64738。
trap on_signal TERM INT
trap shutdown EXIT

# Ice 口令：环境变量优先，其次沿用 ini 里已有的，都没有就随机生成一个。
#
# Murmur 只认 ini 里的那份，所以环境变量也要落进文件。刻意不放一个固定的默认
# 值：写死的口令会留在镜像层和 docker history 里，而且一个能用的默认口令永远
# 不会被改。login.py 那边什么都不用配——serverconf.py 的第 3 顺位就是读这个
# 文件的 icesecretwrite。
ensure_ice_secret() {
    local current secret
    current="$(sed -n 's/^icesecretwrite=\(.\+\)$/\1/p' "$MUMBLE_INI" | tail -n1)"
    secret="${MUMBLE_ICE_SECRET:-$current}"
    if [ -z "$secret" ]; then
        secret="$(head -c 24 /dev/urandom | base64 | tr -d '/+=')"
        echo "生成了一个随机的 Ice 口令（只在这个容器里有效）"
    fi
    # 先删后加，不用 sed 替换：口令里可能有 sed 的分隔符或 & ，替换会被吃掉
    sed -i '/^#\?icesecretwrite=/d' "$MUMBLE_INI"
    # 空的 icesecretread 是"拒绝一切读"，不是"不设口令"——官方文档写得很明白：
    # 取消注释但留空就是拒绝访问，要关掉得整行注释掉。留着它 login.py 连
    # getServer 都拿不到。只删空的：非空的是运维自己设的，不能动。
    sed -i '/^icesecretread=[[:space:]]*$/d' "$MUMBLE_INI"
    # **插到第 1 行，不是追加到末尾。** 和 Dockerfile 里写 ice= 是同一个道理：
    # Murmur 用 QSettings 读 ini，key 归属它上面最近的那个 [section]，追加到
    # 文件末尾就落进了最后一个节里，Murmur 读不到、也不报错——Ice 会以"没设
    # 口令"启动，也就是谁连上 6502 谁就是管理员。
    #
    # 口令走 ENVIRON 而不是 awk -v：-v 的值会被 awk 当转义序列展开，口令里
    # 一个反斜杠就能让写进文件的和 login.py 读出来的不是同一个东西（\t 变成
    # 制表符）。随机生成的那个是纯字母数字碰不到，但 MUMBLE_ICE_SECRET 是人
    # 填的。sed 的 1i 有同样的毛病，所以两处都不用它。
    ICE_SECRET_LINE="icesecretwrite=$secret" awk \
        'BEGIN { print ENVIRON["ICE_SECRET_LINE"] } { print }' \
        "$MUMBLE_INI" > "$MUMBLE_INI.new"
    # 原地换内容而不是 mv：mv 会换掉 inode 和属主/权限，而这个文件 Debian 是
    # 按 640 root:mumble-server 装的
    cat "$MUMBLE_INI.new" > "$MUMBLE_INI"
    rm -f "$MUMBLE_INI.new"
}

# SuperUser 密码只从环境变量来，不给就不动。必须在服务端起来之前设：
# -supw 是直接开数据库改的。
set_superuser_password() {
    [ -n "${MUMBLE_SUPERUSER_PASSWORD:-}" ] || return 0
    mumble-server -ini "$MUMBLE_INI" -supw "$MUMBLE_SUPERUSER_PASSWORD" > /dev/null
    echo "已设置 SuperUser 密码"
    # 上面这条是用 root 跑的，别把数据库的属主留成 root——服务端会 setuid 到
    # mumble-server（ini 里的 uname=），然后打不开自己的库
    if id -u mumble-server > /dev/null 2>&1; then
        chown -R mumble-server: /var/lib/mumble-server 2>/dev/null || true
    fi
}

# 回环上的端口探测用 bash 自带的 /dev/tcp，连不上立刻失败，不依赖镜像里有 nc
ice_open() {
    (exec 3<>"/dev/tcp/127.0.0.1/${ICE_PORT}") 2>/dev/null
}

# login.py 连的是 Meta:tcp -h 127.0.0.1 -p 6502，端口没开就直接"无法连接到
# Mumble 服务器"退出。固定 sleep 赌不起，轮询。
wait_for_ice() {
    local waited=0
    while ! ice_open; do
        if [ -n "$mumble_pid" ] && ! kill -0 "$mumble_pid" 2>/dev/null; then
            echo "mumble-server 没起来（Ice 端口 $ICE_PORT 一直没开）" >&2
            return 1
        fi
        if [ "$waited" -ge "$ICE_WAIT" ]; then
            echo "等了 ${ICE_WAIT} 秒，Ice 端口 $ICE_PORT 还没开——" \
                 "${MUMBLE_INI} 里的 ice= 配了吗？" >&2
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
}

if [ "$only_login" -eq 0 ]; then
    # 必须先于 -supw 和 -fg：-fg 一绑定语音端口就能处理客户端。
    version="$(dpkg-query -W -f '${Version}' mumble-server)"
    python3 admission.py "$MUMBLE_INI" "$version"
    ensure_ice_secret
    set_superuser_password
    echo "启动 mumble-server（${MUMBLE_INI}）"
    mumble-server -ini "$MUMBLE_INI" -fg &
    mumble_pid=$!
    wait_for_ice
fi

# -u 不缓冲：login.py 全是 print，缓冲住的话 docker logs 里长时间什么都没有
echo "启动 login.py"
python3 -u login.py &
login_pid=$!

set +e
wait -n
code=$?
set -e

# docker stop 发的 TERM 也会把 wait -n 打断（返回 143），别把正常停机说成
# 是哪个进程出了事。
# 中文标点紧跟在变量后面时必须写 ${code}：bash 5.3 起会把多字节字符也当成
# 变量名的一部分，$code）读成一个不存在的变量，set -u 之下当场退出。
if [ "$stopping" -eq 1 ]; then
    echo "收到停止信号，退出"
elif [ -n "$mumble_pid" ] && ! kill -0 "$mumble_pid" 2>/dev/null; then
    echo "mumble-server 退出了（${code}），整个停下来" >&2
else
    echo "login.py exited (${code}); stopping Murmur" >&2
fi

exit "$code"
