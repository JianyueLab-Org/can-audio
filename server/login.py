# 处理mumble的登录逻辑
# https://ceruleanavi.net/api/v1/public/auth
# json:{ "cid":"1000", "password": "1234"}
# 正确返回200，错误返回400
#
# Mumble 1.5 起 Ice 的 slice 模块由 Murmur 改名为 MumbleServer，接口本身没变
# （ServerAuthenticator 的方法和签名逐条对过 v1.5.735 的 MumbleServer.ice）。
# 生成绑定：
#     slice2py /usr/share/mumble-server/MumbleServer.ice     # Debian 13
import sys
import os
import signal
import time
import Ice
import requests
import json
import traceback
import re

import serverconf

try:
    import MumbleServer as MumbleIce          # Mumble 1.5+
except ImportError:                            # 1.4 及更早叫 Murmur
    import Murmur as MumbleIce

ICE_PROXY = "Meta:tcp -h 127.0.0.1 -p 6502"
SERVER_ID = 1

# 口令不写在这里，见 serverconf.py。环境变量 / server_secrets.json /
# mumble-server.ini 三选一，都没有就在启动时大声失败。

# requests 没有默认超时。authenticate 跑在 Ice 的服务端分发线程上，上游接口
# 一旦变成黑洞（不是拒连，是不回包），这个线程就永远回不来——线程池被占满之后
# 整台服务器的语音认证全部瘫痪，而进程看起来完全正常。
HTTP_TIMEOUT = 10
# 网络层面的失败重试一次。注意只重试连不上/超时，不重试接口明确回的 4xx：
# can-web 那边按 CAN 对认证失败限流，把明确的拒绝再打一遍等于自己锁自己。
HTTP_RETRIES = 1
HTTP_RETRY_DELAY = 0.5

# 多久确认一次和 Murmur 的链路还在
HEALTH_INTERVAL = 15.0

# authenticate 的返回约定（Murmur 的 ServerAuthenticator.ice）：
#   >=0 认证通过并给出 user id
#   -1  认证失败
#   -2  不认识这个用户，交回服务端自己的账号库
#   -3  暂时验证不了（上游没响应）——服务端不会把它当成密码错误
AUTH_FAILED = -1
AUTH_FALLTHROUGH = -2
AUTH_TEMPORARY_FAILURE = -3

# 允许交回 Murmur 内部账号库的名字，**精确匹配，只有这些**。
#
# -2 不是"先放过去、后面还有一道关"。它的意思是"这个用户不归我管"，而 Murmur
# 只在 serverpassword 非空时才拦未注册的访客。这个部署里 serverpassword 从来
# 没设过：Dockerfile 只改 ice= 和 logfile=，start.sh 只写 icesecretwrite。
# 与此同时 fix_acl.py 按设计把 Enter/Speak/Whisper/Listen/MakeTempChannel 授给
# `all` 组，而 Mumble 的 `all` **包含未认证用户**（不含未认证的是 `auth`）。
#
# 所以对任意名字回 -2，等于让任何人填个用户名就落地并在所有 FREQ_* 频道上收发
# 话音。这里原来是"凡是 user_id_for 认不出的就 -2"，理由是 SuperUser 要能登进
# 来——那个需求是真的（回 -1 会把服务器自己的管理入口锁死），但它只需要这一个
# 名字，不需要整个补集。
#
# 名单要精确匹配：大小写变体、带空格、带前后缀的都不是 SuperUser。Mumble 的
# SuperUser 就是这个拼写且不可改名，所以不必也不该放宽。
MURMUR_LOCAL_ACCOUNTS = frozenset({"SuperUser"})

# 全局关闭标志，Ctrl+C 时立即设置，阻止清理时再做 Ice 调用
_shutting_down = False

# 通播账号的名字形状：{cid}_atis{频率6位}，由 atis/broadcast.py 和
# server/ATIS/mumble.py 拼出来。**结尾要卡死**——原来是 r"^.*_atis\d{6}"，
# 不卡结尾的话 1000_atis1180001 也算匹配，而取 id 时 split 拿到的是 7 位的
# 1180001，等于凭空造出一个谁也不认识的用户 id。
ATIS_NAME = re.compile(r"^(?P<cid>.*)_atis(?P<freq>\d{6})$")


def is_atis_name(name):
    return ATIS_NAME.match(name) is not None


def user_id_for(name):
    """名字 → Murmur 用户 id。名字不合法时抛 ValueError。

    **authenticate 和 nameToId 必须共用这一个函数。** 它们原来各写各的，对
    通播账号给出的答案不一样（一个 118000，一个 -2），而这种不一致不会有任何
    报错，只会让按名字配的权限静默落空。

    普通用户：id 就是 CAN 号，所以 Mumble 用户名必须是纯数字。
    通播账号：id 是频率那六位，这样同一个人在不同频率上开的多个通播不会互相
    顶掉（同名踢人是按名字判的，名字里带着频率）。代价是不同 cid 在**同一个**
    频率上开通播会拿到同一个 id——这个取舍是全网约定的一部分，见 CLAUDE.md。
    """
    matched = ATIS_NAME.match(name)
    if matched:
        return int(matched.group("freq"))
    # 不能直接 int(name)：int() 还认下划线、正负号、前后空白、甚至全角数字
    # （int("１０００") == 1000）。名字"１０００"和"1000"是两个不同的账号，
    # 同名踢人踢不到对方，却会拿到同一个用户 id——继承对方的全部 ACL。
    if not (name.isascii() and name.isdigit()):
        raise ValueError(f"not a plain numeric name: {name!r}")
    return int(name)


class AuthenticatorI(MumbleIce.ServerAuthenticator):
    def __init__(self, server, adapter, context=None):
        self.server = server
        self.adapter = adapter
        self.context = context or {}
        self.online_users = {}  # 用户名 -> session，由 ServerCallbackI 维护

    def kick_previous_session(self, name):
        """同名账号已经在线就把旧会话踢掉。

        踢人要带上 secret context，否则服务端会抛 InvalidSecretException。

        **不能同步调**，而且是真的会死锁，不是理论风险：authenticate 跑在 Ice
        的服务端分发线程上，Murmur 这时正卡在那次调用上等我们回话。同步的
        kickUser 要 Murmur 处理完才返回，而 Murmur 要等我们返回才能去处理它。

        **也不能走 oneway**，虽然这里原来就是这么写的。kickUser 的 slice 声明
        带 throws 子句：

            void kickUser(int session, string reason)
                throws ServerBootedException, InvalidSessionException,
                       InvalidSecretException;

        而 Ice 只允许无返回值、无出参、**无用户异常**的操作单向调用，所以用
        oneway 代理调它每一次都抛 TwowayOnlyException —— 这个「优化」对这个操
        作从来没成立过，踢人功能一次都没工作过。日志里那行

            踢出用户失败: exception ::Ice::TwowayOnlyException

        就是它，每次登录一条。

        异步调用两个性质都占：立刻返回，不占分发线程；同时是 twoway，所以用户
        异常有地方可去。结果不 join —— 等它就等于把死锁又装回来了 —— 但要挂个
        回调，否则失败会变成一个没人看的 future，我们就又回到了「踢人静默失效」
        的状态，只是这次连日志都没有。
        """
        if _shutting_down:
            return
        old_session = self.online_users.get(name)
        if old_session is None:
            return
        try:
            future = self.server.kickUserAsync(
                old_session, "您的账号在其他位置登录", self.context)
        except Ice.ConnectionTimeoutException:
            return  # 关闭时连接的 Ice 已断，忽略
        except Exception as e:
            print(f"踢出用户失败: {e}")
            return

        future.add_done_callback(self._kick_finished)

    @staticmethod
    def _kick_finished(future):
        """异步踢人的结局。跑在 Ice 的线程上，所以只许打日志。

        InvalidSessionException 是正常的：Murmur 自己也认同名会话（日志里的
        Disconnecting ghost），旧会话可能在我们的踢人到达之前就已经没了。
        """
        try:
            future.result()
        except Exception as e:
            if type(e).__name__ == "InvalidSessionException":
                return
            print(f"踢出用户失败: {e}")

    def authenticate(self, name, pw, certificates, certhash, certstrong, current=None):
        try:
            print(f"认证用户: {name}")
            # 名字既不是纯数字也不是通播格式的，只有 MURMUR_LOCAL_ACCOUNTS
            # 里那几个才**放行**给 Murmur 自己的账号库（SuperUser 要能登进来，
            # 否则服务器自己的管理入口被这只认证器锁死）。其余一律拒绝：
            # 这个部署没有 serverpassword，而 `all` 组带着收发话音的权限，
            # 所以放行给未注册的名字就是直接放人进来。见常量处的说明。
            try:
                user_id = user_id_for(name)
            except ValueError:
                if name in MURMUR_LOCAL_ACCOUNTS:
                    return (AUTH_FALLTHROUGH, "", [])
                return (AUTH_FAILED, "", [])
            if is_atis_name(name):
                print(f"匹配到ATIS登录: {name}")
                result = login_ATIS(name, pw)
            else:
                result = login(name, pw)

            if result == VERIFY_OK:
                self.kick_previous_session(name)
                return (user_id, name, [])
            if result == VERIFY_UNREACHABLE:
                # 上游没响应时不能报"密码错误"——那会让用户一直去改密码，
                # 而问题根本不在他那边
                return (AUTH_TEMPORARY_FAILURE, "", [])
            return (AUTH_FAILED, "", [])
        except Exception as e:
            print(f"认证异常: {e}")
            traceback.print_exc()
            return (AUTH_FAILED, "", [])

    def nameToId(self, name, current=None):
        """名字 → 用户 id。**必须和 authenticate 给出同一个答案。**

        原来这里只有 int(name)，通播账号（1000_atis118000）在这里抛异常、
        回落到 -2，而 authenticate 对同一个名字给的是 118000。两个方法各说
        各话的后果是按名字把通播账号写进 ACL 不生效——setACL 收下了，权限却
        落在一个不存在的用户上，看着完全成功。
        """
        try:
            return user_id_for(name)
        except ValueError:
            return AUTH_FALLTHROUGH

    def idToName(self, id, current=None):
        """用户 id → 名字。

        **通播账号这条路是还不回去的**，不是没写：id 取的是频率六位，cid 那
        一截在 authenticate 里就丢了，118000 既可能是 CAN 118000 也可能是
        118.000 上的某个通播。这里只能给出数字本身。Murmur 拿它做的是显示和
        ACL 反查，当前部署（根 ACL 只用 all 组）碰不到这条路。真要能反查，
        得先改 id 的算法，而那个算法是全网约定，见 CLAUDE.md。
        """
        return str(id)

    def getInfo(self, id, current=None):
        return (False, {})

    def idToTexture(self, id, current=None):
        return []


class ServerCallbackI(MumbleIce.ServerCallback):
    """在线用户的进出通知。

    userConnected / userDisconnected 属于 ServerCallback，不属于
    ServerAuthenticator——以前把它们写在认证器里，但从没调用 addCallback，
    所以那两个方法从来没被触发过，online_users 一直是空的，"同一账号在别处
    登录就踢掉旧会话"其实从未生效。这里单独注册回调把这条链路补上。

    ServerCallback 的七个方法都要实现：Ice 会按接口分发，缺一个就会在服务端
    触发那个事件时报错。
    """

    def __init__(self, authenticator):
        self.authenticator = authenticator

    def userConnected(self, user, current=None):
        self.authenticator.online_users[user.name] = user.session
        print(f"用户 {user.name} 已连接，session: {user.session}")

    def userDisconnected(self, user, current=None):
        self.authenticator.online_users.pop(user.name, None)
        print(f"用户 {user.name} 已断开连接")

    def userStateChanged(self, user, current=None):
        pass

    def userTextMessage(self, user, message, current=None):
        pass

    def channelCreated(self, state, current=None):
        pass

    def channelRemoved(self, state, current=None):
        pass

    def channelStateChanged(self, state, current=None):
        pass


url = "https://ceruleanavi.net/api/v1/public/auth"

# 上游接口的三种结果。"验证不了"必须和"密码不对"分开：前者再试一次就好，
# 后者再试多少遍都一样，而且会被 can-web 按 CAN 计进限流。
VERIFY_OK = "ok"
VERIFY_REJECTED = "rejected"
VERIFY_UNREACHABLE = "unreachable"


def verify(cid, password):
    """拿账号去问 can-web。返回上面那三个结果之一。

    **不要把 password 打进日志。** FSD 密码就是会员的网站密码，而这个进程是
    前台跑的，输出直接进终端和 journal——原来这里把整个请求体（含明文密码）
    print 出来，等于服务器日志里躺着全网成员的网站密码。仓库在
    xpc/fsdpilot.py 里对 #AP 做了 _redact()，服务端这边同样得守。
    """
    headers = {"Content-Type": "application/json"}
    data = {"cid": str(cid), "password": str(password)}
    print(f"登录请求: cid={cid}")           # 密码不进日志

    last_error = None
    for attempt in range(HTTP_RETRIES + 1):
        try:
            response = requests.post(url, headers=headers,
                                     data=json.dumps(data),
                                     timeout=HTTP_TIMEOUT)
        except requests.exceptions.RequestException as e:
            last_error = e
            print(f"请求错误（第 {attempt + 1}/{HTTP_RETRIES + 1} 次）: {e}")
            if attempt < HTTP_RETRIES:
                time.sleep(HTTP_RETRY_DELAY)
            continue

        if response.status_code == 200:
            print(f"登录成功: cid={cid}")
            return VERIFY_OK
        if response.status_code >= 500:
            # 5xx 是 can-web 那边坏了（部署、网关重启），不是密码错。报
            # REJECTED 的话全网用户在那两分钟里都看到"密码错误"，改密码、
            # 反复重连，每次都计进按账号的限流——等上游恢复了人也被锁死了。
            last_error = f"HTTP {response.status_code}"
            print(f"上游出错（第 {attempt + 1}/{HTTP_RETRIES + 1} 次）: "
                  f"{response.status_code}")
            if attempt < HTTP_RETRIES:
                time.sleep(HTTP_RETRY_DELAY)
            continue
        # 接口明确说了不行（4xx）就不再试——重试只会把这个账号推向限流
        print(f"登录失败: cid={cid}, {response.status_code}, {response.text}")
        return VERIFY_REJECTED

    print(f"验证不了 cid={cid}（上游没有响应）: {last_error}")
    return VERIFY_UNREACHABLE


def login(cid, password):
    return verify(cid, password)


def login_ATIS(name, password):
    """ATIS 登录，用户名形如 DDDD_atisDDDDDD。

    **这条路径不能去掉**，哪怕服务端一台通播机都不跑：管制员手里的桌面通播
    客户端（atis/，打包成 can-atis）登录用的就是 `{自己的cid}_atis{频率}`，
    走的正是这里。cid 部分照常拿去上游接口验，和普通用户一样。

    以前这里还有一条保留账号的旁路，给 server/ATIS/ 那队服务端通播机免验证用。
    默认部署只起 login.py、那队机器人不跑，所以那条旁路是死代码，已经去掉。
    **要重新启用服务端通播机的话，光把 server/ATIS/mumble.py 拉起来是不够的**
    ——它那个保留账号（默认 900）上游接口并不认识，要么把旁路加回来，要么在
    can-web 那边给它建一个真账号。
    """
    matched = ATIS_NAME.match(name)
    cid = matched.group("cid") if matched else name.split("_atis")[0]
    print(f"ATIS登录: cid={cid}")           # 密码不进日志
    return verify(cid, password)



def signal_handler(signum, frame):
    """收到 Ctrl+C 或 SIGTERM 时立即标记关闭，触发 Ice shutdown。"""
    global _shutting_down
    if _shutting_down:
        return  # 重复信号不做处理
    _shutting_down = True
    print(f"\n收到信号 {signum}，正在关闭 ...")
    # communicator 是全局的，在 main 里赋值
    if _communicator:
        try:
            _communicator.shutdown()
        except Exception:
            pass


_communicator = None


def watch_connection(server, context, interval=None):
    """守着和 Murmur 的链路，断了就返回 False 让进程退出。

    原来这里是 communicator.waitForShutdown()，纯粹干等。mumble-server 一旦
    重启（崩溃、systemd 拉起、或者再跑一次 start.sh），认证器就再也不会被重新
    注册，而这个进程还好好地活着——外面看一切正常，实际上所有人登录都被拒。
    start.sh 的 cleanup_ports 只杀 mumble-server、不杀 login.py，所以这种
    "认证器还在跑但已经没用了"的状态非常容易出现。

    退出比假装还活着好：进程一停，谁在看着它都能发现。
    """
    if interval is None:
        interval = HEALTH_INTERVAL
    while not _shutting_down:
        try:
            server.isRunning(context)
        except Exception as e:
            print(f"和 Murmur 的链路断了，退出让外面重新拉起: {e}")
            return False
        # sleep 会被信号打断，Ctrl+C 不用等满一个周期
        time.sleep(interval)
    return True


def main():
    global _shutting_down, _communicator
    exit_code = 0

    # 注册信号处理（确保主线程处理）
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # 显式设置 Ice 编码版本为 1.0，并缩短 Ice 超时
    init_data = Ice.InitializationData()
    init_data.properties = Ice.createProperties()
    init_data.properties.setProperty("Ice.Default.EncodingVersion", "1.0")
    init_data.properties.setProperty("Ice.ACM.Close", "3")          # 空闲超时主动关闭
    init_data.properties.setProperty("Ice.Connection.ConnectTimeout", "5")
    init_data.properties.setProperty("Ice.Connection.CloseTimeout", "1")
    # 加大线程池避免分发线程中做同步 Ice 调用时死锁
    init_data.properties.setProperty("Ice.ThreadPool.Server.Size", "4")
    init_data.properties.setProperty("Ice.ThreadPool.Server.SizeMax", "8")
    init_data.properties.setProperty("Ice.ThreadPool.Client.Size", "4")
    init_data.properties.setProperty("Ice.ThreadPool.Client.SizeMax", "8")

    # 口令要在连之前拿到，缺了就当场退出，别等到第一个人登录才发现
    try:
        ice_secret = serverconf.ice_secret()
    except serverconf.MissingSecret as e:
        print(f"启动失败: {e}")
        return 1
    with Ice.initialize(init_data) as communicator:
        _communicator = communicator

        # 设置Ice连接，强制代理使用 1.0 编码
        base = communicator.stringToProxy(ICE_PROXY)
        meta = MumbleIce.MetaPrx.checkedCast(base)
        if not meta:
            # 必须是非零退出码。原来这里直接 return，进程以 0 退出，start.sh
            # 的 set -e 抓不到，supervisor 也会以为启动成功——而实际上认证器
            # 根本没注册，Murmur 回落到自己的账号库（那里一个账号都没有），
            # 所有人登录被拒，客户端显示的是"密码错误"。
            print("无法连接到 Mumble 服务器")
            return 1

        # 使用正确的ice secret
        context = {"secret": ice_secret}
        server = meta.getServer(SERVER_ID, context)
        if not server:
            print("无法获取服务器实例")
            return 1

        # 尽早给服务器代理设超时，避免关闭时卡住
        server = MumbleIce.ServerPrx.checkedCast(server.ice_timeout(3000))

        # 创建Ice适配器
        adapter = communicator.createObjectAdapterWithEndpoints(
            "Authenticator", "tcp -h 127.0.0.1")
        adapter.activate()

        # 创建并注册认证器
        auth = AuthenticatorI(server, adapter, context)
        auth_prx = MumbleIce.ServerAuthenticatorPrx.checkedCast(adapter.addWithUUID(auth))

        # 在线用户的进出通知，认证时靠它判断要不要踢掉旧会话
        callback = ServerCallbackI(auth)
        callback_prx = MumbleIce.ServerCallbackPrx.checkedCast(adapter.addWithUUID(callback))

        try:
            server.setAuthenticator(auth_prx, context)
            print("认证器已设置")

            server.addCallback(callback_prx, context)
            print("服务器回调已注册")

            # 补上注册回调之前就已经在线的用户
            try:
                for user in server.getUsers(context).values():
                    auth.online_users[user.name] = user.session
                print(f"当前在线 {len(auth.online_users)} 人")
            except Exception as e:
                print(f"获取在线用户失败: {e}")

            # 守着这条链路，别只是干等
            alive = watch_connection(server, context)
            if not alive:
                exit_code = 1
        except Ice.Exception as e:
            if not _shutting_down:
                print(f"Ice异常: {e}")
                traceback.print_exc()
                exit_code = 1
        finally:
            # 反注册要尽力做一次。留着一个指向已死进程的认证器代理，Murmur
            # 每次登录都会去调它。server 代理上已经设了 3 秒超时（见上面的
            # ice_timeout），所以这里最坏也就多等几秒，不会卡住退出。
            for label, call in (("回调", lambda: server.removeCallback(callback_prx, context)),
                                ("认证器", lambda: server.setAuthenticator(None, context))):
                try:
                    call()
                except Exception as e:
                    print(f"注销{label}失败（忽略）: {e}")

    return exit_code

if __name__ == "__main__":
    # 退出码要给出去：start.sh 里是 set -e，前台跑的这一步失败必须让脚本知道
    sys.exit(main())
