"""语音引擎回报给 C# 客户端的状态文字（中 / 英）。

只收 voice.py 经 on_state 送出的连接、拒绝、权限文字；界面文字全部在 C# 里。

    from i18n import t
    t("voice.online", cid="1000")

约定：每个键两种语言都必须有（test_i18n.py 逐条核对）；占位符用命名的
`{name}`，两种语言语序不同，位置参数会错位。
"""

import locale
import logging
import os

log = logging.getLogger("i18n")

DEFAULT = "zh"
LANGUAGES = {"zh": "中文", "en": "English"}

TEXT = {
    # ---------- 语音连接状态 ----------
    # voice.py 经 on_state 送到状态栏和登录页的文字
    "voice.connecting":     {"zh": "正在以 {cid} 连接 {server} …",
                             "en": "Connecting to {server} as {cid}…"},
    "voice.connect_failed": {"zh": "连接失败: {error}", "en": "Connection failed: {error}"},
    "voice.timeout":        {"zh": "连接 {server} 超时", "en": "Timed out connecting to {server}"},
    "voice.timeout_reason": {"zh": "连接 {server} 超时：{reason}",
                             "en": "Timed out connecting to {server}: {reason}"},
    "voice.no_response":    {"zh": "连接 {server} 超时，服务器没有响应",
                             "en": "Timed out connecting to {server} — no response"},
    "voice.audio_failed":   {"zh": "音频设备打开失败: {error}",
                             "en": "Could not open the audio device: {error}"},
    "voice.online":         {"zh": "已连接，账号 {cid}", "en": "Connected as {cid}"},
    "voice.dropped":        {"zh": "与语音服务器的连接已断开",
                             "en": "Lost the connection to the voice server"},
    # 掉线之后：先重连，最多 RECONNECT_LIMIT 次，都失败才真的下线。
    # 这三条要说清"还在试"和"不再试了"的区别——原来一律是上面那句 dropped，
    # 一次抖动看上去就和彻底断了一样。
    "voice.dropped_retrying": {"zh": "与语音服务器的连接已断开，正在重连（最多 {limit} 次）",
                               "en": "Lost the connection to the voice server — "
                                     "reconnecting (up to {limit} attempts)"},
    "voice.reconnecting":   {"zh": "正在重连语音服务器（{attempt}/{limit}）",
                             "en": "Reconnecting to the voice server ({attempt}/{limit})"},
    "voice.reconnected":    {"zh": "语音服务器已重连",
                             "en": "Reconnected to the voice server"},
    # 被服务端踢下线。和 reconnect_failed 分开：它不是"连不上"，是账号在别处
    # 登录了，连回去只会把那一端顶掉，所以要说明不再重连。
    "voice.kicked":         {"zh": "被语音服务器断开：{reason}。不会自动重连。",
                             "en": "Disconnected by the voice server: {reason}. "
                                   "Not reconnecting."},
    "voice.kicked_plain":   {"zh": "账号可能在其他位置登录了",
                             "en": "the account may have signed in elsewhere"},
    "voice.reconnect_failed": {"zh": "掉线后重连 {limit} 次都没成功，已下线",
                               "en": "Reconnected {limit} times without success — "
                                     "went offline"},
    "voice.stopped":        {"zh": "已断开", "en": "Disconnected"},
    "voice.rejected":       {"zh": "语音服务器拒绝了 {cid}：{reason}",
                             "en": "The voice server rejected {cid}: {reason}"},
    "voice.rejected_plain": {"zh": "到 {server} 的连接意外中断，服务器没有说明原因（详见日志）",
                             "en": "The connection to {server} ended unexpectedly with no "
                                   "reason given (see the log)"},

    # 服务器 Reject 的类型。全都笼统说成"用户名或密码"会把人引到错误的方向——
    # 认证器挂了的时候，用户会一直去改密码。
    "reject.WrongUserPW":     {"zh": "密码错误", "en": "Wrong password"},
    "reject.WrongServerPW":   {"zh": "服务器密码错误", "en": "Wrong server password"},
    "reject.InvalidUsername": {"zh": "用户名不符合服务器的规则",
                               "en": "The server does not accept this username"},
    "reject.UsernameInUse":   {"zh": "这个用户名已经在线了",
                               "en": "That username is already online"},
    "reject.ServerFull":      {"zh": "服务器已满", "en": "The server is full"},
    "reject.NoCertificate":   {"zh": "服务器要求证书",
                               "en": "The server requires a certificate"},
    # **这一条恰恰说明 login.py 在跑。** Murmur 只在 Ice 认证器返回 -3
    # （AUTH_TEMPORARY_FAILURE）时报 AuthenticatorFail，而 server/login.py 只在
    # 上游 /api/v1/public/auth 没响应时返回 -3。认证器要是真的没注册，Murmur 会
    # 回落到它自己的账号库，拒绝理由是 WrongUserPW 而不是这个。原来这句写的是
    # "login.py 可能没在运行"，把排查方向直接引到服务器进程上，而该看的是那个
    # 接口——一次实测故障就是这么被找错方向的。
    "reject.AuthenticatorFail": {"zh": "服务端暂时验不了账号（认证器的上游 "
                                       "/api/v1/public/auth 没有响应）",
                                 "en": "The server could not verify the account right now "
                                       "(the authenticator's upstream "
                                       "/api/v1/public/auth did not respond)"},
    "reject.WrongVersion":    {"zh": "客户端版本不被服务器接受",
                               "en": "The server does not accept this client version"},
    "reject.with_note":       {"zh": "{reason}（服务器附言：{note}）",
                               "en": "{reason} (server said: {note})"},

    # 服务器拒绝了某个动作
    "denied.UserListenerLimit":    {"zh": "服务器限制了每个用户能监听的频道数，部分频率收不到",
                                    "en": "The server caps how many channels one user may "
                                          "listen to — some frequencies will be silent"},
    "denied.ChannelListenerLimit": {"zh": "服务器限制了单个频道的监听人数，这个频率收不到",
                                    "en": "The server caps how many people may listen to one "
                                          "channel — this frequency will be silent"},
    "denied.Permission":           {"zh": "没有权限（频道监听需要 Listen 权限）",
                                    "en": "Not permitted (channel listening needs the Listen "
                                          "permission)"},
    "denied.other":                {"zh": "服务器拒绝了操作: {kind}",
                                    "en": "The server refused the action: {kind}"},
    "denied.with_note":            {"zh": "{reason}（{note}）", "en": "{reason} ({note})"},
}

_current = DEFAULT


def available():
    """能选的语言：代码 → 显示名。"""
    return dict(LANGUAGES)


def current():
    return _current


def set_language(code):
    """切换语言。不认识的代码退回默认，不抛异常——语言坏了不该让程序起不来。"""
    global _current
    code = (code or "").strip().lower()
    if code not in LANGUAGES:
        if code:
            log.warning("unknown language %r, falling back to %s", code, DEFAULT)
        code = DEFAULT
    _current = code
    return _current


def system_language():
    """猜系统语言，第一次启动时用。猜不出来就用默认。"""
    try:
        # 环境变量优先，方便测试和命令行覆盖
        for name in ("CAN_LANG", "LANGUAGE", "LC_ALL", "LANG"):
            value = os.environ.get(name)
            if value:
                code = value.split(".")[0].split("_")[0].lower()
                if code in LANGUAGES:
                    return code
        # Windows 上一般不设那些环境变量，退回系统区域设置。
        code = (locale.getlocale()[0] or "").split("_")[0].lower()
        if code == "chinese":
            code = "zh"
        elif code == "english":
            code = "en"
        if code in LANGUAGES:
            return code
    except Exception as e:
        log.debug("could not determine the system language: %s", e)
    return DEFAULT


def t(key, /, **kwargs):
    """取一条界面文字。

    找不到键就返回键本身——在界面上很扎眼，正好当成"这里漏翻了"的提示，比默默
    显示一个空字符串强得多。某个语言缺这一条时退回默认语言，用户至少看得懂。

    第一个参数是**位置限定**的（那个 `/`）。不加的话，文案里只要有一个叫
    `{key}` 的占位符，`t("ptt.keyboard", key="V")` 就会撞上形参名，报的还是
    "got multiple values for argument 'key'" 这种和 i18n 毫无关系的错。
    """
    entry = TEXT.get(key)
    if entry is None:
        log.warning("no such string: %s", key)
        return key
    text = entry.get(_current) or entry.get(DEFAULT) or key
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError) as e:
            log.warning("the placeholders of string %s do not match: %s", key, e)
            return text
    return text
