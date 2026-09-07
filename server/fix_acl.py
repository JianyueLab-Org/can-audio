"""检查并补齐根频道 ACL 里这套系统需要的权限。

两个症状，同一个原因——Mumble 的默认 ACL 不一定给全这套系统要用的权限：

    管制端  "没有权限（频道监听需要 Listen 权限）"
    情报台  "Channel FREQ_127800 does not exists"（服务器其实是拒绝了建频道）

需要哪两个：

    Listen           0x800   管制端一个人收多个频率靠 Mumble 1.4 的频道监听
                             （UserState.listening_channel_add），人在主频率的
                             频道里，其他频率靠"监听"收。这个位是 1.4 才加的，
                             从 1.2/1.3 升级上来的服务器数据库里还是老 ACL。
    MakeTempChannel  0x400   频率频道（FREQ_xxxxxx）是客户端按需在根下现建的
                             临时频道。没有这个权限服务器直接拒绝，而客户端只
                             看到"频道不存在"，猜不到是权限问题。

频率频道都是根下的临时频道，ACL 从根继承，所以在根上放开一次就够，不用给每个
FREQ_* 单独配。

**授给哪个组是有安全后果的。** Mumble 的 `all` 组**包含未认证用户**（不含未认证
的那个组叫 `auth`）。历史上这些权限一直授给 `all`，而 `login.py` 当时对任何它认
不出的名字都回 -2（交回 Murmur 自己的账号库）；这个部署又从来没设过
`serverpassword`，于是"用户名随便填"就能落地，并在所有 FREQ_* 频道上收发话音。

那个洞已经在 `login.py` 堵上了（见 `MURMUR_LOCAL_ACCOUNTS`）：认不出的名字现在
一律 AUTH_FAILED，未认证用户根本连不进来。所以授给 `all` **目前不是一个活的漏
洞**，但它是唯一一道门。把这些位迁到 `auth` 就多一道：将来谁再把 -2 放宽，未认
证用户拿到的也只是一个进不了任何频道、说不了话的连接。

    python fix_acl.py                   # 只看，不改
    python fix_acl.py --apply           # 授给 all（历史行为，默认）
    python fix_acl.py --group auth      # 预览迁移到 auth：授给 auth 并从 all 收回
    python fix_acl.py --group auth --apply

**迁移前务必先看 dry-run 的输出。** 判断依据：这台服务器上每一个合法连接都必须
是"已认证"的。本仓库的三类连接都满足——会员用纯数字 CAN 号、通播机器人用
`{cid}_atis{频率}`、两者都由 `login.py` 返回真实 user id；SuperUser 走 Murmur 自
己的账号库，同样是已认证。如果这台服务器上还有别的东西以访客身份连进来（本仓库
里没有），迁移会让它静默失去收发话音的能力。

在 Mumble 主机上跑（Ice 只监听 127.0.0.1）。要先能 import MumbleServer，
生成办法和 login.py 一样：

    slice2py /usr/share/mumble-server/MumbleServer.ice
"""

import sys

import Ice

import serverconf

try:
    import MumbleServer as MumbleIce          # Mumble 1.5+
except ImportError:                            # 1.4 及更早叫 Murmur
    import Murmur as MumbleIce

# 和 login.py 保持一致
ICE_PROXY = "Meta:tcp -h 127.0.0.1 -p 6502"
SERVER_ID = 1
# 口令不写在这里，和 login.py 走同一套（serverconf.py）

ROOT_CHANNEL = 0

# ACL 权限位，取自 mumble 的 src/ACL.h（enum Perm）
PERM_WRITE = 0x1
PERM_TRAVERSE = 0x2
PERM_ENTER = 0x4
PERM_SPEAK = 0x8
PERM_MUTE_DEAFEN = 0x10
PERM_MOVE = 0x20
PERM_MAKE_CHANNEL = 0x40
PERM_LINK_CHANNEL = 0x80
PERM_WHISPER = 0x100
PERM_TEXT_MESSAGE = 0x200
PERM_MAKE_TEMP_CHANNEL = 0x400
PERM_LISTEN = 0x800

PERM_NAMES = [
    (PERM_WRITE, "Write"), (PERM_TRAVERSE, "Traverse"), (PERM_ENTER, "Enter"),
    (PERM_SPEAK, "Speak"), (PERM_MUTE_DEAFEN, "MuteDeafen"), (PERM_MOVE, "Move"),
    (PERM_MAKE_CHANNEL, "MakeChannel"), (PERM_LINK_CHANNEL, "LinkChannel"),
    (PERM_WHISPER, "Whisper"), (PERM_TEXT_MESSAGE, "TextMessage"),
    (PERM_MAKE_TEMP_CHANNEL, "MakeTempChannel"), (PERM_LISTEN, "Listen"),
]

# 这套系统跑起来必须有的，以及缺了会怎样。
#
# 这几条的共同点是：缺了之后服务器**只是默默不照做**，不会给客户端任何看得懂的
# 反馈。表现出来就是"命令发出去了、没有报错、就是不生效"，看着像网络慢或者服务
# 器卡，实际上再等一万年也不会成功。真实日志里是这样的：
#
#     频道 FREQ_127100 不存在，建一个临时的
#     建立频道 FREQ_127100 后 5 秒内没有出现，1 秒后重试   （无限重复）
REQUIRED = [
    (PERM_LISTEN, "Listen", "管制端收不到主频率以外的频率"),
    (PERM_MAKE_TEMP_CHANNEL, "MakeTempChannel", "建不了 FREQ_* 频道，报频道不存在"),
    (PERM_ENTER, "Enter",
     "进不了 FREQ_* 频道：频道明明存在，进频道的命令也发了，人却一直留在根频道，"
     "于是 PTT 发不出去、也收不到任何人的话音"),
    (PERM_WHISPER, "Whisper",
     "管制端发不出话音：管制端不是普通说话，而是用 VoiceTarget 对着一组频道"
     "whisper，缺这一条它说什么都没人听得到"),
    (PERM_SPEAK, "Speak", "谁都发不出话音"),
]


def describe(mask):
    """把权限位翻译成人看得懂的名字。"""
    if not mask:
        return "无"
    names = [name for bit, name in PERM_NAMES if mask & bit]
    extra = mask & ~sum(bit for bit, _ in PERM_NAMES)
    if extra:
        names.append(f"0x{extra:x}")
    return " ".join(names) or f"0x{mask:x}"


def show(acls, groups, inherit):
    print(f"\n根频道当前 ACL（继承父级={inherit}，共 {len(acls)} 条）：")
    for i, acl in enumerate(acls):
        who = acl.group if acl.userid < 0 else f"用户#{acl.userid}"
        scope = []
        if acl.applyHere:
            scope.append("本频道")
        if acl.applySubs:
            scope.append("子频道")
        mark = "（继承，只读）" if acl.inherited else ""
        print(f"  [{i}] {who:12} 作用于 {'+'.join(scope) or '无'}{mark}")
        print(f"       允许: {describe(acl.allow)}")
        if acl.deny:
            print(f"       拒绝: {describe(acl.deny)}")
    if groups:
        print(f"组：{', '.join(g.name for g in groups)}")


def find_group_acl(acls, group):
    """找到给指定组、且作用于子频道的那条——临时频率频道靠它继承。

    只认非继承的：继承来的 ACL 是只读的，改了写不回去。
    """
    for acl in acls:
        if (acl.userid < 0 and acl.group == group
                and acl.applySubs and not acl.inherited):
            return acl
    return None


def new_group_acl(group):
    """给某个组新建一条作用于本频道和子频道的空 ACL。

    逐字段赋值而不是靠构造函数的位置参数：slice 生成的类字段顺序是 Ice 说了算
    的，写错一个位置不会报错，只会写进去一条语义完全不同的 ACL。
    """
    acl = MumbleIce.ACL()
    acl.applyHere = True
    acl.applySubs = True
    acl.inherited = False
    acl.userid = -1
    acl.group = group
    acl.allow = 0
    acl.deny = 0
    return acl


def unauthenticated_exposure(acls):
    """`all` 组当前握着哪些 REQUIRED 位。

    Mumble 的 `all` 包含未认证用户，所以这些位在 `all` 上就等于"任何能落地的
    连接都能收发话音"。返回 (位, 名字, 后果) 的列表，空表示没有暴露面。
    """
    entry = find_group_acl(acls, "all")
    if entry is None:
        return []
    return [(bit, name, why) for bit, name, why in REQUIRED if entry.allow & bit]


def missing_permissions(allow):
    """还缺哪些。返回 (位, 名字, 缺了会怎样) 的列表。"""
    return [(bit, name, why) for bit, name, why in REQUIRED if not allow & bit]


def main():
    apply = "--apply" in sys.argv

    # 默认仍是 all，也就是历史行为——迁移到 auth 要显式要求，且必须先看 dry-run。
    group = "all"
    if "--group" in sys.argv:
        index = sys.argv.index("--group")
        if index + 1 >= len(sys.argv):
            print("--group 后面要跟组名（all 或 auth）")
            return 2
        group = sys.argv[index + 1]
    if group not in ("all", "auth"):
        print(f"不认识的组 {group!r}：只支持 all（含未认证用户）和 auth（不含）")
        return 2

    init_data = Ice.InitializationData()
    init_data.properties = Ice.createProperties()
    # login.py 也这么设：服务端用的是 1.0 编码
    init_data.properties.setProperty("Ice.Default.EncodingVersion", "1.0")
    init_data.properties.setProperty("Ice.MessageSizeMax", "65536")

    try:
        ice_secret = serverconf.ice_secret()
    except serverconf.MissingSecret as e:
        print(f"启动失败: {e}")
        return 1

    with Ice.initialize(init_data) as communicator:
        meta = MumbleIce.MetaPrx.checkedCast(
            communicator.stringToProxy(ICE_PROXY))
        if not meta:
            print("连不上 Mumble 的 Ice 接口，检查 mumble-server.ini 里的 ice= 配置")
            return 1

        # 所有 Ice 调用都要带 secret，否则抛 InvalidSecretException
        context = {"secret": ice_secret}
        server = meta.getServer(SERVER_ID, context)
        if not server:
            print(f"拿不到 {SERVER_ID} 号虚拟服务器")
            return 1
        server = MumbleIce.ServerPrx.checkedCast(server.ice_timeout(5000))

        acls, groups, inherit = server.getACL(ROOT_CHANNEL, context)
        show(acls, groups, inherit)

        # 不管这次要做什么，先把未认证用户当前拿着什么说清楚。这是唯一一个
        # 从客户端完全看不出来的东西，而它决定了"谁能在频率上说话"。
        exposure = unauthenticated_exposure(acls)
        if exposure:
            print("\n注意：以下权限当前授给 `all` 组，而 Mumble 的 `all` "
                  "**包含未认证用户**：")
            for bit, name, _ in exposure:
                print(f"  {name}（0x{bit:x}）")
            if group == "all":
                print("  一个能落地的未认证连接就能收发话音。login.py 现在会拒掉"
                      "认不出的名字，所以目前进不来；要多一道，用 --group auth。")

        target = find_group_acl(acls, group)
        created = target is None
        if created:
            if group == "all":
                print("\n根频道上没有给 all 组、作用于子频道的 ACL。"
                      "这不太正常，建议用 Mumble 客户端手工看一眼再决定怎么改。")
                return 1
            print(f"\n根频道上还没有给 {group} 组的 ACL，打算新建一条"
                  "（作用于本频道和子频道）。")
            target = new_group_acl(group)

        missing = missing_permissions(target.allow)
        # 迁移的第二半：授给 auth 之后必须把同样的位从 all 收回，否则 all 那份
        # 还在，等于什么都没做。
        strip = [] if group == "all" else [
            (bit, name, why) for bit, name, why in REQUIRED
            if (find_group_acl(acls, "all") or new_group_acl("all")).allow & bit]

        if not missing and not strip:
            print("\n需要的权限都有了。客户端还是不正常的话，看看 mumble-server.ini"
                  " 里的 listenersperuser / listenersperchannel 是不是限制了数量。")
            return 0

        if missing:
            print(f"\n{group} 组缺这些权限：")
            for bit, name, why in missing:
                print(f"  {name}（0x{bit:x}）—— 缺了会：{why}")

        wanted = target.allow
        for bit, _, _ in missing:
            wanted |= bit
        print(f"\n打算把 {group} 组的允许位从\n  {describe(target.allow)}\n改成\n  "
              f"{describe(wanted)}")

        all_entry = find_group_acl(acls, "all") if strip else None
        if strip and all_entry is not None:
            stripped = all_entry.allow
            for bit, _, _ in strip:
                stripped &= ~bit
            print(f"\n并把 all 组的允许位从\n  {describe(all_entry.allow)}\n收回成\n  "
                  f"{describe(stripped)}")
            print("\n**这一步会让未认证连接失去收发话音的能力。** 确认这台服务器上"
                  "每一个合法连接都是已认证的再动手——本仓库的会员、通播机器人和"
                  "SuperUser 都是。")

        if not apply:
            print("\n这是预览。确认没问题就加 --apply 真的写进去。")
            return 0

        # getACL 会把继承来的 ACL 一起返回，而那些是只读的，原样写回去会在本
        # 频道复制出一份。根频道没有父级所以本来就不会有，但别依赖这个巧合。
        own = [acl for acl in acls if not acl.inherited]
        if len(own) != len(acls):
            print(f"（略过 {len(acls) - len(own)} 条继承来的 ACL，它们是只读的）")

        target.allow = wanted
        if created:
            # 放在 all 之前：Mumble 按顺序求值 ACL，后面的覆盖前面的，所以
            # 收紧用的条目必须排在被收紧的那条前面才不会被它盖掉。
            own.insert(0, target)
        if strip and all_entry is not None:
            all_entry.allow = stripped
        server.setACL(ROOT_CHANNEL, own, groups, inherit, context)
        print("已写入。")

        # 回读确认，别只信写入没抛异常
        acls, _, _ = server.getACL(ROOT_CHANNEL, context)
        again = find_group_acl(acls, group)
        still = missing_permissions(again.allow if again else 0)
        if still:
            print("回读之后这些还是没有：" +
                  "、".join(name for _, name, _ in still) + "，写入可能没成功。")
            return 1
        left = unauthenticated_exposure(acls)
        if group != "all" and left:
            print("回读之后 all 组还握着：" +
                  "、".join(name for _, name, _ in left) + "，收回可能没成功。")
            return 1
        print("回读确认：都生效了。客户端重连一次即可。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
