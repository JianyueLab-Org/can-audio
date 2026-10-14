"""界面文字的多语言。

用字典而不是 Qt 的 .ts/.qm：那一套要引入 pylupdate6 / lrelease 两个构建步骤，
再把二进制的 .qm 塞进每个 spec，而这个仓库从头到尾就是"纯 Python + PyInstaller"，
没有别的构建工具。字典跟着 .py 一起打包，不会漏。

用法：

    from i18n import t
    button.setText(t("main.start"))
    label.setText(t("main.letter", letter="J"))

约定：

- **键名统一在这里定义，源码里不再出现界面字面量**。这样漏翻的地方一眼能看出来
  （界面上会直接显示成 key），而不是混在中文里看不见。test_i18n.py 里的
  NoHardcodedUiStringTest 会扫源码兜底。
- 每个键两种语言都必须有，test_i18n.py 会逐条核对——只翻一半的话，用户看到的是
  中英混排。
- 带占位符的用 str.format 的写法（`{who}`），不要用 % ——两种语言的语序不同，
  位置参数会错位，命名参数不会。

**这里翻的是操作界面，不是通播稿。** 通播内容本身有自己的一套语言设置
（station.voice_language：英文 / 中文 / 中英双语），那是播给飞行员听的，和操作者
看哪种界面完全无关——一个英文界面的操作者照样可能在管一份中文通播。chinese.py、
template.py、metar.py 里的文字一律不进这张表。
"""

import locale
import logging
import os

log = logging.getLogger("i18n")

DEFAULT = "zh"
LANGUAGES = {"zh": "中文", "en": "English"}

TEXT = {
    # ---------- 通用 ----------
    "app.title":            {"zh": "ATIS for CAN", "en": "ATIS for CAN"},
    "common.ok":            {"zh": "确定", "en": "OK"},
    "common.cancel":        {"zh": "取消", "en": "Cancel"},
    "common.save":          {"zh": "保存", "en": "Save"},
    "common.to":            {"zh": "到", "en": "to"},

    # ---------- 顶栏 / 账号 ----------
    "main.account":         {"zh": "账号:", "en": "Account:"},
    "main.username":        {"zh": "用户名", "en": "Username"},
    "main.password":        {"zh": "密码", "en": "Password"},
    "main.settings":        {"zh": "设置", "en": "Settings"},
    "main.stations":        {"zh": "通播席位", "en": "ATIS positions"},
    "main.pin":             {"zh": "置顶", "en": "Pin"},
    "main.pin_tip":         {"zh": "窗口保持在其它程序上面",
                             "en": "Keep this window above all others"},
    "main.compact":         {"zh": "精简", "en": "Compact"},
    "main.compact_tip":     {"zh": "只留席位列表，窗口缩到最小",
                             "en": "Show only the position list and shrink the window"},

    # ---------- 席位操作 ----------
    "main.net_config":      {"zh": "从网络更新配置", "en": "Update from the network"},
    "main.net_stations":    {"zh": "取在线席位", "en": "Fetch online positions"},
    "main.import_vatis":    {"zh": "导入 vATIS 配置…", "en": "Import a vATIS profile…"},
    "main.import_vatis_tip": {"zh": "读取 vATIS 的 profile JSON，把里面的席位和预设导进来",
                              "en": "Read a vATIS profile JSON and bring its stations "
                                    "and presets in"},
    "main.preset":          {"zh": "预设:", "en": "Preset:"},
    "main.edit_preset":     {"zh": "编辑预设", "en": "Edit the preset"},
    "main.letter":          {"zh": "情报字母: {letter}", "en": "Information: {letter}"},
    "main.letter_none":     {"zh": "情报字母: -", "en": "Information: -"},
    "main.advance":         {"zh": "推进字母", "en": "Next letter"},
    "main.refresh_weather": {"zh": "刷新天气", "en": "Refresh the weather"},
    "main.text_atis":       {"zh": "文字通播", "en": "Text ATIS"},
    "main.voice_script":    {"zh": "语音稿", "en": "Spoken script"},
      "main.manual_text":     {"zh": "手动输入文本", "en": "Enter the text manually"},
    "main.start":           {"zh": "开始播出", "en": "Start broadcasting"},
    "main.stop":            {"zh": "停止播出", "en": "Stop broadcasting"},
    "main.idle":            {"zh": "未播出", "en": "Not broadcasting"},
    "main.metar_none":      {"zh": "METAR: （还没取到）", "en": "METAR: (not fetched yet)"},

    # ---------- 席位对话框 ----------
    "station.title":        {"zh": "通播席位", "en": "ATIS position"},
    "station.icao_hint":    {"zh": "机场 ICAO，例如 ZSPD", "en": "Airport ICAO, e.g. ZSPD"},
    "station.name_hint":    {"zh": "机场名称（可留空）", "en": "Airport name (optional)"},
    "station.frequency_hint": {"zh": "频率，例如 127.850", "en": "Frequency, e.g. 127.850"},
    # 中文稿里念的名字：ICAO 代码推不出来，得手填
    "station.chinese_name_hint": {"zh": "中文稿里念的机场名，例如 上海浦东",
                                  "en": "Airport name as spoken in the Chinese script, "
                                        "e.g. 上海浦东"},
    "station.chinese_runway_hint": {"zh": "中文稿里念的跑道，例如 三六左",
                                    "en": "Runway as spoken in the Chinese script, "
                                          "e.g. 三六左"},
    "station.position":     {"zh": "席位位置:", "en": "Position:"},
    "station.latitude_hint": {"zh": "纬度，例如 31.14340", "en": "Latitude, e.g. 31.14340"},
    "station.longitude_hint": {"zh": "经度，例如 121.80500", "en": "Longitude, e.g. 121.80500"},
    "station.position_hint": {"zh": "留空会按机场代码自动填。位置决定席位在雷达图上的位置。",
                              "en": "Leave empty to fill it in from the airport code. "
                                    "This is where the position shows up on the radar."},
    "station.range":        {"zh": "情报字母范围:", "en": "Information letters:"},
    "station.range_hint":   {"zh": "离场和进场分别用不同字母段，飞行员就不会把两份通播搞混。",
                             "en": "Giving departure and arrival separate letter ranges "
                                   "keeps crews from mixing the two ATIS up."},
    "station.bad_input":    {"zh": "输入错误", "en": "Invalid input"},
    "station.bad_icao":     {"zh": "机场代码要是 4 位 ICAO 代码",
                             "en": "The airport code must be a 4-letter ICAO code"},
    "station.bad_frequency": {"zh": "频率格式无效，例如 127.850",
                              "en": "Not a valid frequency — it should look like 127.850"},

    # ---------- 预设对话框 ----------
    "preset.title":         {"zh": "预设 - {name}", "en": "Preset — {name}"},
    "preset.name":          {"zh": "名称:", "en": "Name:"},
    "preset.template":      {"zh": "模板", "en": "Template"},
      "preset.manual_text":   {"zh": "手动输入文本", "en": "Enter the text manually"},
      "preset.manual_text_hint": {"zh": "勾选后隐藏模板、机场条件和 NOTAM，直接在主页面编辑两份稿子。",
                                                 "en": "Hide the template, airport conditions and NOTAM, "
                                                         "then edit both scripts on the main page."},
    "preset.variables":     {"zh": "可用变量: {names}", "en": "Variables: {names}"},
    "preset.vox_hint":      {"zh": "变量后面加 :VOX 表示在文字通播里也用语音说法，"
                                   "例如 [WX:VOX]。",
                             "en": "Adding :VOX to a variable uses its spoken form in the "
                                   "written ATIS too, e.g. [WX:VOX]."},
    "preset.conditions":    {"zh": "机场条件  [ARPT_COND]", "en": "Airport conditions  [ARPT_COND]"},
      "preset.notams":        {"zh": "NOTAM  [NOTAMS]", "en": "NOTAM  [NOTAMS]"},
    "preset.transition":    {"zh": "过渡高度层  [TL]:", "en": "Transition level  [TL]:"},
    "preset.transition_hint": {"zh": "例如 3600 米", "en": "e.g. 3600 m"},
    "preset.unknown_vars":  {"zh": "模板里有认不出的变量: {names}",
                             "en": "The template uses variables that are not known: {names}"},

    # ---------- 席位增删改 ----------
    "station.add_failed":   {"zh": "添加失败", "en": "Could not add"},
    "station.edit_failed":  {"zh": "修改失败", "en": "Could not change"},
    "station.duplicate":    {"zh": "{callsign} 已经存在了", "en": "{callsign} already exists"},
    "station.cannot_delete": {"zh": "无法删除", "en": "Cannot delete"},
    "station.stop_first":   {"zh": "请先停止这个席位的播出",
                             "en": "Stop broadcasting on this position first"},

    # ---------- 从网络取配置 ----------
    "netconfig.fetching":   {"zh": "正在从 can 取通播配置…",
                             "en": "Fetching the ATIS configuration from can…"},
    "netconfig.failed":     {"zh": "取配置失败", "en": "Could not fetch the configuration"},
    "netconfig.header":     {"zh": "网络配置 {label}，共 {count} 个席位",
                             "en": "Network configuration {label}, {count} position(s)"},
    "netconfig.previous":   {"zh": "（上次并入的是 {version}）",
                             "en": "(last merged: {version})"},
    "netconfig.no_change":  {"zh": "没有改动", "en": "Nothing changed"},
    "netconfig.unchanged":  {"zh": "配置保持原样。", "en": "The configuration is unchanged."},
    "netconfig.same":       {"zh": "本地这 {count} 个席位和网络版一致，",
                             "en": "{count} local position(s) already match the network "
                                   "version,"},
    "netconfig.missing":    {"zh": "本地缺少 {count} 个席位：",
                             "en": "{count} position(s) are missing locally:"},
    "netconfig.differing":  {"zh": "本地已有、但和网络版内容不同的 {count} 个席位：",
                             "en": "{count} local position(s) differ from the network "
                                   "version:"},
    "netconfig.updated":    {"zh": "配置已更新", "en": "Configuration updated"},
    "netconfig.result":     {"zh": "新增 {added} 个，覆盖 {replaced} 个",
                             "en": "{added} added, {replaced} replaced"},
    "netconfig.added":      {"zh": "新增：{names}", "en": "Added: {names}"},
    "netconfig.replaced":   {"zh": "覆盖：{names}", "en": "Replaced: {names}"},
    # 正在播出的席位一概不动：它的 Station 对象正被 Broadcaster 和 FSDClient 拿着，
    # 换掉会让在播的音频和屏幕上的稿子对不上，而界面上一切正常。
    "netconfig.skipped_live": {"zh": "正在播出、这次没有动的 {count} 个：{names}",
                               "en": "{count} position(s) are on air and were left alone: "
                                     "{names}"},
    "netconfig.retry_later": {"zh": "停播之后再更新一次即可。",
                              "en": "Update again once they are off the air."},
    "netconfig.and_more":   {"zh": "等 {count} 个", "en": "and {count} more"},
    "netconfig.problems":   {"zh": "有 {count} 项读不进来，已跳过：{details}",
                             "en": "{count} entr(ies) could not be read and were skipped: "
                                   "{details}"},
    "netconfig.up_to_date": {"zh": "配置已是最新", "en": "Already up to date"},
    "netconfig.same_note":  {"zh": "本地这 {count} 个席位和网络版一致，不需要改动。",
                             "en": "All {count} local position(s) match the network "
                                   "version — nothing to change."},
    "netconfig.ask_add":    {"zh": "要加入缺少的席位吗", "en": "Add the missing positions?"},
    "netconfig.add_note":   {"zh": "加进来的是完整配置——频率、跑道构型预设、模板和中文用词"
                                   "都有，不用再自己填。",
                             "en": "What comes in is the complete configuration — "
                                   "frequency, runway-configuration presets, templates and "
                                   "the Chinese wording — so there is nothing left to fill "
                                   "in by hand."},
    "netconfig.ask_overwrite": {"zh": "要用网络版覆盖吗",
                                "en": "Overwrite with the network version?"},
    # 覆盖会丢掉值班时改出来的东西，所以这句必须说清后果——那些修改都藏在预设里，
    # 光看席位列表是看不出来的
    "netconfig.overwrite_note": {"zh": "覆盖会丢掉你对这些席位做的修改——临时的跑道构型、"
                                       "NOTAM、中文附言都在预设里。\n选「否」就只保留本地"
                                       "那份，不影响上面的新增。",
                                 "en": "Overwriting discards the changes you made to these "
                                       "positions — a temporary runway configuration, a "
                                       "NOTAM or extra Chinese wording all live inside the "
                                       "presets.\nChoosing No keeps the local copy and does "
                                       "not affect the additions above."},
    "duty.not_staffing":    {"zh": "你还没有在管制", "en": "You are not staffing a position"},
    "callsign.bad_title":   {"zh": "呼号不合服务端规则",
                             "en": "The callsign does not follow the server's rules"},

    # ---------- 取在线席位 ----------
    "datafeed.fetching":    {"zh": "正在从数据源读在线通播席位…",
                             "en": "Reading the online ATIS positions from the datafeed…"},
    "datafeed.imported":    {"zh": "从数据源导入了 {count} 个席位",
                             "en": "Imported {count} position(s) from the datafeed"},
    "datafeed.skipped":     {"zh": "已存在、跳过的 {count} 个：{names}",
                             "en": "{count} already existed and were skipped: {names}"},
    "datafeed.note":        {"zh": "导进来的只有机场和频率——数据源给不了别的。模板、预设和"
                                   "中文稿的用词还得自己配。",
                             "en": "Only the airport and the frequency come from the "
                                   "datafeed — it has nothing else. Templates, presets and "
                                   "the Chinese wording still have to be set up by hand."},

    # ---------- 导入 vATIS ----------
    "import.done":          {"zh": "导入完成", "en": "Import finished"},
    "import.failed":        {"zh": "导入失败", "en": "Import failed"},
    "import.result":        {"zh": "从「{source}」导入了 {count} 个席位",
                             "en": "Imported {count} position(s) from \u201c{source}\u201d"},

    # ---------- 播出前的席位核对 ----------
    "duty.checking":        {"zh": "正在核对管制席位…", "en": "Checking your ATC position…"},
    "duty.not_online":      {"zh": "网络上没有找到 CID {cid} 的管制席位，因此不能开通播。"
                                   "\n\n请先用管制客户端上线（观察员不算），再回来开始播出。",
                             "en": "No ATC position for CID {cid} was found on the network, "
                                   "so the ATIS cannot go on air.\n\nSign in with an ATC "
                                   "client first (an observer does not count), then come "
                                   "back and start broadcasting."},
    "duty.mismatch":        {"zh": "{problem}\n\n继续的话只播语音，席位不会出现在网络上。"
                                   "是否继续？",
                             "en": "{problem}\n\nIf you continue, only the voice goes out — "
                                   "the position will not appear on the network. Continue?"},

    # ---------- 播出状态 ----------
    "broadcast.stopped_title": {"zh": "{callsign} 已停播", "en": "{callsign} went off air"},
    "broadcast.error_title": {"zh": "{callsign} 播出错误", "en": "{callsign} broadcast error"},
    "broadcast.voice_continues": {"zh": "{message}（语音仍在播出）",
                                  "en": "{message} (the voice is still on air)"},
    "weather.updated":      {"zh": "{callsign} 天气更新，情报字母推进到 {letter}",
                             "en": "{callsign} weather updated — information is now {letter}"},
    "weather.recovered":    {"zh": "{callsign} 天气已恢复", "en": "{callsign} weather is back"},
    "weather.failed":       {"zh": "取天气出错: {error}",
                             "en": "Could not fetch the weather: {error}"},

    # ---------- 更新 ----------
    # 查到新版只是弹一句，装不装是用户的事。措辞要说清下载走的是自己的服务器
    # ——大陆连 GitHub 很不稳，这正是这个功能存在的理由。
    "update.title":         {"zh": "有新版本", "en": "A new version is available"},
    "update.body":          {"zh": "ATIS for CAN {version} 已经发布{size}。\n"
                                   "你现在用的是 {current}。",
                             "en": "ATIS for CAN {version} is out{size}.\n"
                                   "You are running {current}."},
    # 包大小是可有可无的一段，所以单独成键——括号的写法两种语言不一样，
    # 写死在 body 的参数里会让英文界面出现一对中文全角括号。
    "update.size":          {"zh": "（{size}）", "en": " ({size})"},
    "update.detail":        {"zh": "下载走的是 can 自己的服务器，不直接连 GitHub。\n"
                                   "下载完解压覆盖原来那个文件夹即可——设置不在里面。",
                             "en": "The download comes from can's own server, not "
                                   "from GitHub.\nUnzip it over the old folder — your "
                                   "settings are not in there."},
    "update.download":      {"zh": "下载", "en": "Download"},
    "update.notes":         {"zh": "看更新说明", "en": "Release notes"},
    "update.skip":          {"zh": "跳过这个版本", "en": "Skip this version"},
    "update.later":         {"zh": "以后再说", "en": "Later"},
    "update.check":         {"zh": "检查更新", "en": "Check for updates"},
    "update.check_tip":     {"zh": "看看有没有新版本。下载走 can 自己的服务器，"
                                   "不直接连 GitHub",
                             "en": "See whether a newer version is out. The download "
                                   "comes from can's own server, not from GitHub"},
    "update.current":       {"zh": "已经是最新版本（{version}）。",
                             "en": "You are on the latest version ({version})."},
    # 值班时不弹模态框，只在状态栏挂一句，理由见 gui.py
    "update.status":        {"zh": "有新版本 {version}，停播后可在「检查更新」里下载",
                             "en": "Version {version} is available — go off air and use "
                                   "Check for updates to download it"},

    # ---------- 设置对话框 ----------
    "settings.title":       {"zh": "设置", "en": "Settings"},
    "settings.connect_fsd": {"zh": "播出时同时登录 FSD", "en": "Log in to FSD while broadcasting"},
    "settings.fsd_host":    {"zh": "FSD 服务器:", "en": "FSD server:"},
    "settings.fsd_port":    {"zh": "端口:", "en": "Port:"},
    "settings.refresh":     {"zh": "天气自动刷新:", "en": "Weather auto-refresh:"},
    "settings.refresh_hint": {"zh": "（报文变化时自动推进情报字母并换稿）",
                              "en": "(advances the information letter and swaps the script "
                                    "when the report changes)"},
    "settings.rating":      {"zh": "登录等级:", "en": "Login rating:"},
    "settings.rating_hint": {"zh": "（不能高于本人实际等级）",
                             "en": "(must not be above your real rating)"},
    "settings.real_name":   {"zh": "真实姓名:", "en": "Real name:"},
    "settings.real_name_hint": {"zh": "登录 FSD 时显示的姓名",
                                "en": "The name shown when logging in to FSD"},
    "settings.weather_source": {"zh": "备用气象源:", "en": "Fallback weather source:"},
    "settings.weather_hint": {"zh": "登录 FSD 之后天气直接向自己的服务器要（$AX）；这个地址"
                                    "只在没连 FSD 时用。",
                              "en": "Once logged in to FSD the weather comes from that "
                                    "server directly ($AX); this address is only used when "
                                    "FSD is not connected."},
    "settings.language":    {"zh": "界面语言:", "en": "Interface language:"},
    "settings.debug":       {"zh": "记录调试信息（重启后生效）",
                             "en": "Write debug logs (takes effect after restart)"},
    "settings.debug_tip":   {"zh": "打开后连 FSD 收发的每个包都会记下来",
                             "en": "Logs every FSD packet in and out"},
    "settings.open_log":    {"zh": "打开日志", "en": "Open log"},
    "settings.connect_fsd_note": {"zh": "（席位出现在在线列表，并回答文字通播查询）",
                                  "en": "(the position shows up in the online list and "
                                        "answers text-ATIS queries)"},
    # 刷新间隔的说法。分钟/小时在两种语言里的写法不一样，逐条列比拼字符串稳当
    "refresh.1m":           {"zh": "1 分钟", "en": "1 minute"},
    "refresh.2m":           {"zh": "2 分钟", "en": "2 minutes"},
    "refresh.5m":           {"zh": "5 分钟", "en": "5 minutes"},
    "refresh.10m":          {"zh": "10 分钟", "en": "10 minutes"},
    "refresh.15m":          {"zh": "15 分钟", "en": "15 minutes"},
    "refresh.30m":          {"zh": "30 分钟", "en": "30 minutes"},
    "refresh.60m":          {"zh": "1 小时", "en": "1 hour"},
    "rating.auto":          {"zh": "自动（按 CID 从数据源获取）",
                             "en": "Automatic (looked up by CID from the datafeed)"},
    "rating.obs":           {"zh": "OBS 观察员", "en": "OBS (observer)"},

    # ---------- 通播稿的语言（不是界面语言） ----------
    # 这三个是播出去给飞行员听的，和操作者看哪种界面无关——英文界面的操作者
    # 照样可能在管一份中文通播。
    "station_type.combined": {"zh": "综合", "en": "Combined"},
    "station_type.departure": {"zh": "离场", "en": "Departure"},
    "station_type.arrival": {"zh": "进场", "en": "Arrival"},

    "voice_language.english": {"zh": "英文", "en": "English"},
    "voice_language.chinese": {"zh": "中文", "en": "Chinese"},
    "voice_language.both":  {"zh": "中英双语", "en": "Chinese and English"},

    # ---------- 配置（profile）的校验错误 ----------
    # 这些 ValueError 会原样进 QMessageBox
    "profile.name_empty":   {"zh": "名字不能为空", "en": "The name cannot be empty"},
    "profile.exists":       {"zh": "已经有一份叫「{name}」的配置了",
                             "en": "A profile called \u201c{name}\u201d already exists"},
    "profile.missing":      {"zh": "没有叫「{name}」的配置",
                             "en": "There is no profile called \u201c{name}\u201d"},
    "profile.last_one":     {"zh": "至少要留一份配置",
                             "en": "At least one profile has to remain"},

    # ---------- 语音播出（broadcast.py 的 _state 消息） ----------
    "voice.connecting":     {"zh": "正在以 {user} 连接 {server} …",
                             "en": "Connecting to {server} as {user}…"},
    "voice.connect_failed": {"zh": "连接失败: {error}", "en": "Connection failed: {error}"},
    "voice.timeout":        {"zh": "连接 {server} 超时，服务器没有响应",
                             "en": "Timed out connecting to {server} — no response"},
    "voice.timeout_reason": {"zh": "连接 {server} 超时：{reason}",
                             "en": "Timed out connecting to {server}: {reason}"},
    "voice.rejected":       {"zh": "语音服务器拒绝了 {user}：{reason}",
                             "en": "The voice server rejected {user}: {reason}"},
    "voice.rejected_plain": {"zh": "到 {server} 的连接意外中断，服务器没有说明原因（详见日志）",
                             "en": "The connection to {server} ended unexpectedly with no "
                                   "reason given (see the log)"},
    "voice.reconnecting":   {"zh": "语音掉线，正在重连（{attempt}/{limit}）",
                             "en": "Voice connection lost — reconnecting ({attempt}/{limit})"},
    # 被服务端踢下线。通播的每个席位各有一条连接，所以停的是这个席位。
    "voice.kicked":         {"zh": "{callsign} 被语音服务器断开：{reason}。不会自动重连。",
                             "en": "{callsign} was disconnected by the voice server: "
                                   "{reason}. Not reconnecting."},
    "voice.kicked_plain":   {"zh": "账号可能在其他位置登录了",
                             "en": "the account may have signed in elsewhere"},
    "voice.give_up":        {"zh": "语音掉线后重连 {limit} 次都没成功，{callsign} 已停播",
                             "en": "Reconnected {limit} times without success — {callsign} "
                                   "went off air"},
    "voice.online":         {"zh": "已在 {frequency} 播出", "en": "On air on {frequency}"},
    "voice.join_failed":    {"zh": "进入频道失败: {error}",
                             "en": "Could not join the channel: {error}"},
    "voice.channel_denied": {"zh": "服务器不允许建立频道 {name}：{reason}",
                             "en": "The server refused to create channel {name}: {reason}"},
    "voice.channel_timeout": {"zh": "建立频道 {name} 后 {seconds:.0f} 秒内没有出现，"
                                    "服务器没有说明原因",
                              "en": "Channel {name} did not appear within {seconds:.0f}s of "
                                    "being created, and the server gave no reason"},
    "voice.move_timeout":   {"zh": "发出了进入频道 {name} 的请求，但 {seconds:.0f} 秒内没有生效",
                             "en": "Sent the request to join {name} but it did not take "
                                   "effect within {seconds:.0f}s"},
    "voice.waiting":        {"zh": "频率上有通话，等待中…",
                             "en": "Someone is talking on the frequency — waiting…"},
    "voice.interrupted":    {"zh": "有人讲话，中止本轮播报",
                             "en": "Someone keyed up — this round was cut short"},
    "voice.send_failed":    {"zh": "发送音频失败: {error}",
                             "en": "Could not send the audio: {error}"},
    "voice.script_swapped": {"zh": "已换用新的通播稿", "en": "Switched to the new script"},
    "voice.tts_failed":     {"zh": "语音合成失败，请检查系统 TTS 语音",
                             "en": "Speech synthesis failed — check the system TTS voices"},
    "voice.speaking":       {"zh": "正在播报 {letter}", "en": "Broadcasting {letter}"},
    "voice.stopped":        {"zh": "通播已停止", "en": "The ATIS has stopped"},
    "voice.round_failed":   {"zh": "播报异常: {error}", "en": "The broadcast round failed: {error}"},
    "voice.empty_audio":    {"zh": "语音文件为空", "en": "The audio file is empty"},
    "voice.bad_width":      {"zh": "不支持的采样宽度: {bits} bit",
                             "en": "Unsupported sample width: {bits} bit"},

    # 服务器 Reject 的类型。全都笼统说成"用户名或密码"会把人引到错误的方向——
    # 认证器挂了的时候，用户会一直去改密码。
    "reject.WrongUserPW":   {"zh": "密码错误", "en": "Wrong password"},
    "reject.WrongServerPW": {"zh": "服务器密码错误", "en": "Wrong server password"},
    "reject.InvalidUsername": {"zh": "用户名不符合服务器的规则",
                               "en": "The server does not accept this username"},
    "reject.UsernameInUse": {"zh": "这个用户名已经在线了", "en": "That username is already online"},
    "reject.ServerFull":    {"zh": "服务器已满", "en": "The server is full"},
    "reject.NoCertificate": {"zh": "服务器要求证书", "en": "The server requires a certificate"},
    "reject.AuthenticatorFail": {"zh": "服务端认证器故障（服务器上的 login.py 可能没在运行）",
                                 "en": "The server's authenticator failed (login.py may not "
                                       "be running on the server)"},
    "reject.WrongVersion":  {"zh": "客户端版本不被服务器接受",
                             "en": "The server does not accept this client version"},
    "reject.with_note":     {"zh": "{reason}（服务器附言：{note}）",
                             "en": "{reason} (server said: {note})"},

    # 服务器拒绝了某个动作
    "denied.Permission":    {"zh": "没有权限（建立频率频道需要根频道的 MakeTempChannel 权限）",
                             "en": "Not permitted (creating a frequency channel needs "
                                   "MakeTempChannel on the root channel)"},
    "denied.ChannelName":   {"zh": "频道名不合服务器的规矩",
                             "en": "The server does not accept that channel name"},
    "denied.NestingLimit":  {"zh": "频道层级超过了服务器上限",
                             "en": "Too many nested channels for this server"},
    "denied.ChannelCountLimit": {"zh": "服务器上的频道数已达上限",
                                 "en": "The server has reached its channel limit"},
    "denied.other":         {"zh": "服务器拒绝了操作: {kind}",
                             "en": "The server refused the action: {kind}"},
    "denied.with_note":     {"zh": "{reason}（{note}）", "en": "{reason} ({note})"},

    # ---------- FSD（fsdclient.py 的 _status 消息） ----------
    "fsd.connecting":       {"zh": "正在以 {callsign} 登录 FSD（等级 {rating}）…",
                             "en": "Logging in to FSD as {callsign} (rating {rating})…"},
    "fsd.connect_failed":   {"zh": "无法连接 FSD 服务器 {host}:{port}（{error}）",
                             "en": "Could not reach the FSD server {host}:{port} ({error})"},
    "fsd.online":           {"zh": "已作为 {callsign} 登录 FSD",
                             "en": "Logged in to FSD as {callsign}"},
    "fsd.closed":           {"zh": "FSD 服务器关闭了连接",
                             "en": "The FSD server closed the connection"},
    "fsd.login_timeout":    {"zh": "FSD 登录超时，未收到服务器回应",
                             "en": "The FSD login timed out — no reply from the server"},
    "fsd.dropped":          {"zh": "与 FSD 服务器的连接已断开",
                             "en": "Lost the connection to the FSD server"},
    "fsd.exception":        {"zh": "FSD 连接异常: {error}",
                             "en": "The FSD connection failed: {error}"},
    "fsd.send_failed":      {"zh": "发送失败: {error}", "en": "Could not send: {error}"},
    "fsd.rejected":         {"zh": "FSD 拒绝登录（{code}）: {message}",
                             "en": "The FSD server refused the login ({code}): {message}"},
    "fsd.bad_frequency":    {"zh": "频率 {frequency} 无法编码",
                             "en": "The frequency {frequency} cannot be encoded"},
    "fsd.stopped":          {"zh": "已从 FSD 下线", "en": "Signed off from FSD"},

    # 呼号在本地先查一遍，用户得到的是解释而不是一次登录拒绝。规则来自 can-fsd 的
    # IsValidCallsign / IsATISCallsign——ZSPD_D_ATIS 有 11 个字符，长度上限最容易踩。
    "callsign.length":      {"zh": "呼号 {callsign} 有 {count} 个字符，服务端只接受 2-{limit} 个",
                             "en": "The callsign {callsign} is {count} characters long; "
                                   "the server only accepts 2-{limit}"},
    "callsign.charset":     {"zh": "呼号 {callsign} 含有服务端不接受的字符",
                             "en": "The callsign {callsign} contains characters the server "
                                   "does not accept"},
    "callsign.not_atis":    {"zh": "呼号 {callsign} 不是以 _ATIS 结尾，服务端不会把它算作"
                                   "通播席位",
                             "en": "The callsign {callsign} does not end in _ATIS, so the "
                                   "server will not treat it as an ATIS position"},
    "fsd.reconnecting":     {"zh": "与 FSD 的连接断开，正在重连（{attempt}/{limit}）",
                             "en": "Lost the FSD connection — reconnecting "
                                   "({attempt}/{limit})"},
    "fsd.give_up":          {"zh": "与 FSD 断开后重连 {limit} 次都没成功，{callsign} 已下线",
                             "en": "Reconnected to FSD {limit} times without success — "
                                   "{callsign} went offline"},
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
        # Windows 上一般不设那些环境变量，得问系统。优先用 Qt——
        # locale.getdefaultlocale() 在 3.15 会被移除，而且在 Windows 上给出的是
        # "English_United States" 这种名字，不好解析。
        code = ""
        try:
            from PyQt6.QtCore import QLocale
            code = QLocale.system().name().split("_")[0].lower()
        except Exception:
            code = (locale.getdefaultlocale()[0] or "").split("_")[0].lower()
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
