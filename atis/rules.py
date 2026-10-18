"""开播前的拦截规则。

纯逻辑：不碰 Qt、不碰网络。给它席位、配置和当前在播的集合，它回答"能不能
开"，以及不能开的话该跟用户说什么。

**为什么单独拎出来。** 这几条原来在 `gui.py` 里各写了两遍——点按钮时查一遍
（`toggle_broadcast`），数据源核对回来真正开播前再查一遍（`start_broadcast`）。
席位按呼号区分。不同机场可以使用同一频率，语音账号包含席位呼号。
"""


def blocking_reason(station, profile, broadcasting,
                    cid="", password="", rendered=None):
    """开播前的拦截理由。

    返回 (标题, 正文) 给界面弹窗用；None 表示可以开。检查账号和播出稿。
    """
    if station is None:
        return ("错误", "没有选中席位")

    if not (cid or "").strip() or not password:
        return ("错误", "请先填写用户名和密码")

    # rendered 是 script.render() 的结果：(文字通播, 语音稿)
    if not rendered or not (rendered[1] or "").strip():
        return ("错误", "还没有可播的内容，先刷新天气")

    return None
