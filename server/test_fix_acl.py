"""fix_acl.py 的测试。

    python -m unittest test_fix_acl -v      （在 server 目录下运行）

不连 Murmur：Ice、MumbleServer 和 serverconf 都在导入前换成替身。

钉的是选 ACL 和造 ACL 的那几个纯函数。这里写错一条 ACL 不会抛异常，只会把一条
语义完全不同的规则写进服务器——要么全网说不了话，要么该收紧的没收紧——而两种
都要等有人抱怨才会被发现。
"""

import sys
import types
import unittest


class _ACL:
    """替身要和 slice 生成的类一样：字段全部可写，构造不要求参数。"""

    def __init__(self):
        self.applyHere = False
        self.applySubs = False
        self.inherited = False
        self.userid = 0
        self.group = ""
        self.allow = 0
        self.deny = 0


def _install_stubs():
    """装上 fix_acl 导入时要的替身。

    **补齐已有模块，不是「有就跳过」。** unittest discover 在同一个进程里按
    字母序导入所有 test_*，所以这里和 test_login 的替身会撞在一起：谁先跑，
    后一个就看到 sys.modules 里已经有了。两份替身各自只装自己要的东西——
    test_login 装 ServerAuthenticator/ServerCallback，这里装 ACL——于是
    「有就跳过」的写法必然让后来者拿到一个缺字段的模块。两个方向都会炸，
    而分文件单独跑（各自新进程）永远看不到。
    """
    ice = sys.modules.setdefault("Ice", types.ModuleType("Ice"))
    if not hasattr(ice, "Exception"):
        ice.Exception = type("Exception", (Exception,), {})
    if not hasattr(ice, "ConnectionTimeoutException"):
        ice.ConnectionTimeoutException = type(
            "ConnectionTimeoutException", (ice.Exception,), {})
    if not hasattr(ice, "InitializationData"):
        ice.InitializationData = lambda: types.SimpleNamespace(properties=None)
        ice.createProperties = lambda: types.SimpleNamespace(
            setProperty=lambda *a: None)
        ice.initialize = lambda *a, **k: None

    mumble = sys.modules.setdefault("MumbleServer", types.ModuleType("MumbleServer"))
    if not hasattr(mumble, "ACL"):
        mumble.ACL = _ACL
    # login.py 继承这两个，所以它们必须是真的类。
    for name in ("ServerAuthenticator", "ServerCallback"):
        if not hasattr(mumble, name):
            setattr(mumble, name, type(name, (), {}))
    for name in ("ServerPrx", "MetaPrx", "ServerAuthenticatorPrx",
                 "ServerCallbackPrx"):
        if not hasattr(mumble, name):
            setattr(mumble, name, types.SimpleNamespace(
                checkedCast=lambda p: p, uncheckedCast=lambda p: p))

    # **serverconf 不能替身。** fix_acl 只在 main() 里用它，导入期不需要，
    # 而 unittest discover 是在同一个进程里按字母序导入所有 test_*：往
    # sys.modules 里塞一个假 serverconf，后面导入的 test_serverconf 拿到的
    # 就是这一份，于是它的每个 setUp 都在 serverconf.SECRETS_FILE 上炸掉。
    # 分文件单独跑看不到（各自新进程），CI 跑 discover 才会红。


_install_stubs()

import fix_acl


def acl(group, allow=0, *, apply_subs=True, inherited=False, userid=-1):
    entry = fix_acl.MumbleIce.ACL()
    entry.group = group
    entry.allow = allow
    entry.applyHere = True
    entry.applySubs = apply_subs
    entry.inherited = inherited
    entry.userid = userid
    return entry


VOICE = (fix_acl.PERM_ENTER | fix_acl.PERM_SPEAK | fix_acl.PERM_WHISPER
         | fix_acl.PERM_LISTEN | fix_acl.PERM_MAKE_TEMP_CHANNEL)


class FindGroupAclTest(unittest.TestCase):

    def test_it_finds_the_entry_for_the_named_group(self):
        entries = [acl("all", VOICE), acl("auth", fix_acl.PERM_ENTER)]
        self.assertIs(fix_acl.find_group_acl(entries, "auth"), entries[1])

    def test_an_inherited_entry_is_never_returned(self):
        """继承来的 ACL 是只读的，改了写不回去——挑中它等于静默什么都没做。"""
        entries = [acl("auth", VOICE, inherited=True)]
        self.assertIsNone(fix_acl.find_group_acl(entries, "auth"))

    def test_an_entry_that_does_not_apply_to_subchannels_is_not_returned(self):
        """FREQ_* 是根下的临时子频道，只作用于本频道的那条它们继承不到。"""
        entries = [acl("auth", VOICE, apply_subs=False)]
        self.assertIsNone(fix_acl.find_group_acl(entries, "auth"))

    def test_a_user_entry_is_not_a_group_entry(self):
        entries = [acl("auth", VOICE, userid=7)]
        self.assertIsNone(fix_acl.find_group_acl(entries, "auth"))

    def test_a_missing_group_is_none_rather_than_an_error(self):
        self.assertIsNone(fix_acl.find_group_acl([acl("all", VOICE)], "auth"))


class NewGroupAclTest(unittest.TestCase):
    """新建的那条必须每个字段都显式赋值。

    slice 生成的类字段顺序由 Ice 决定，靠位置参数构造写错一个位置不会报错，只会
    写进去一条语义不同的 ACL——比如 userid 落在 group 的位置上。
    """

    def test_every_field_is_set_explicitly(self):
        entry = fix_acl.new_group_acl("auth")
        self.assertEqual(entry.group, "auth")
        self.assertEqual(entry.userid, -1, "组条目的 userid 必须是负的")
        self.assertTrue(entry.applyHere)
        self.assertTrue(entry.applySubs, "FREQ_* 是子频道，不作用于子频道就白建")
        self.assertFalse(entry.inherited, "新建的不能标成继承，否则写不回去")
        self.assertEqual(entry.allow, 0)
        self.assertEqual(entry.deny, 0)


class UnauthenticatedExposureTest(unittest.TestCase):
    """`all` 组包含未认证用户，所以它握着的每一个 REQUIRED 位都是暴露面。"""

    def test_it_reports_the_required_bits_all_holds(self):
        found = fix_acl.unauthenticated_exposure([acl("all", VOICE)])
        self.assertEqual({name for _, name, _ in found},
                         {name for _, name, _ in fix_acl.REQUIRED})

    def test_it_is_empty_once_the_bits_have_moved_to_auth(self):
        entries = [acl("all", fix_acl.PERM_TRAVERSE), acl("auth", VOICE)]
        self.assertEqual(fix_acl.unauthenticated_exposure(entries), [])

    def test_no_all_entry_is_no_exposure_rather_than_a_crash(self):
        self.assertEqual(fix_acl.unauthenticated_exposure([acl("auth", VOICE)]), [])

    def test_it_reports_only_the_bits_actually_held(self):
        found = fix_acl.unauthenticated_exposure([acl("all", fix_acl.PERM_SPEAK)])
        self.assertEqual([name for _, name, _ in found], ["Speak"])


class MissingPermissionsTest(unittest.TestCase):

    def test_a_full_grant_is_missing_nothing(self):
        self.assertEqual(fix_acl.missing_permissions(VOICE), [])

    def test_it_names_what_is_absent(self):
        missing = fix_acl.missing_permissions(VOICE & ~fix_acl.PERM_LISTEN)
        self.assertEqual([name for _, name, _ in missing], ["Listen"])

    def test_an_empty_grant_is_missing_everything(self):
        self.assertEqual(len(fix_acl.missing_permissions(0)),
                         len(fix_acl.REQUIRED))


if __name__ == "__main__":
    unittest.main(verbosity=2)
