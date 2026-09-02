"""界面文字的多语言。

用字典而不是 Qt 的 .ts/.qm：那一套要引入 pylupdate6 / lrelease 两个构建步骤，
再把二进制的 .qm 塞进每个 spec，而这个仓库从头到尾就是"纯 Python + PyInstaller"，
没有别的构建工具。字典跟着 .py 一起打包，不会漏。

用法：

    from i18n import t
    button.setText(t("connect.connect"))
    label.setText(t("radio.traffic", count=12))

约定：

- **键名统一在这里定义，源码里不再出现界面字面量**。这样漏翻的地方一眼能看出来
  （界面上会直接显示成 key），而不是混在中文里看不见。test_i18n.py 里的
  NoHardcodedUiStringTest 会扫源码兜底。
- 每个键两种语言都必须有，test_i18n.py 会逐条核对——只翻一半的话，用户看到的是
  中英混排。
- 带占位符的用 str.format 的写法（`{who}`），不要用 % ——两种语言的语序不同，
  位置参数会错位，命名参数不会。
- **voice.py 和 fsdpilot.py 也从这里取词**。那两个模块的 `_status()` 消息会直接
  进消息区，它们是界面文字，不是日志——日志那一半仍然是英文，见 CLAUDE.md。
  这也是 msfs/i18n.py 必须带上同样这批 `voice.*` / `fsd.*` 键的原因：那两个模块
  在 xpc 和 msfs 之间是逐字节共享的。

这份和 msfs/i18n.py 是**姊妹文件**，不是副本：结构和键名一样，差别只在提到模拟器
的那几条（X-Plane ↔ MSFS）和 XPC 独有的 CSL / 插件设置。
"""

import locale
import logging
import os

log = logging.getLogger("i18n")

DEFAULT = "zh"
LANGUAGES = {"zh": "中文", "en": "English"}

TEXT = {
    # ---------- 通用 ----------
    "app.title":            {"zh": "XPC for CAN", "en": "XPC for CAN"},
    "app.name":             {"zh": "XPC for CAN", "en": "XPC for CAN"},

    # ---------- 菜单 ----------
    # & 后面那个字母是快捷键。中英文各挑各的，别照抄——中文菜单里的 (&F) 是
    # 附在括号里的，英文里是直接下划线标在词上的。
    "menu.file":            {"zh": "文件(&F)", "en": "&File"},
    "menu.flight_plan":     {"zh": "飞行计划(&P)…", "en": "Flight &plan…"},
    "menu.settings":        {"zh": "设置(&S)…", "en": "&Settings…"},
    "menu.quit":            {"zh": "退出(&Q)", "en": "&Quit"},
    "menu.help":            {"zh": "帮助(&H)", "en": "&Help"},
    "menu.open_log":        {"zh": "打开日志目录(&L)", "en": "Open the &log folder"},
    "menu.about":           {"zh": "关于(&A)", "en": "&About"},

    # ---------- 连接栏 ----------
    "connect.title":        {"zh": "连接", "en": "Connection"},
    "connect.callsign":     {"zh": "呼号", "en": "Callsign"},
    "connect.callsign_hint": {"zh": "如 CCA1501", "en": "e.g. CCA1501"},
    "connect.cid":          {"zh": "CAN ID", "en": "CAN ID"},
    "connect.password":     {"zh": "密码", "en": "Password"},
    "connect.aircraft":     {"zh": "机型", "en": "Aircraft"},
    "connect.aircraft_hint": {"zh": "如 B738", "en": "e.g. B738"},
    "connect.observer":     {"zh": "观察员模式（双人机组）",
                             "en": "Observer mode (two-crew)"},
    "connect.observer_hint": {
        "zh": "右座用：只连语音，不在网络上产生第二架飞机。两个人要用各自的账号。",
        "en": "For the right seat: voice only, no second aircraft on the network. "
              "Each person signs in with their own account."},
    "connect.observer_tip": {
        "zh": "双人机组时给副驾用。这个模式下不连 FSD：网络上只有机长那一架飞机，"
              "你和他在同一个频率上，听得见也说得出。代价是收不到管制的文字消息"
              "——那是发给机长呼号的。频率跟着你自己的 COM1 走，没开模拟器就在"
              "无线电栏里手输一个。",
        "en": "For the second crew member. This mode never connects to FSD: only the "
              "flying pilot's aircraft is on the network, and you sit on the same "
              "frequency, hearing and transmitting normally. The cost is that ATC text "
              "messages do not reach you — they are addressed to the other callsign. "
              "The frequency follows your own COM1, or you type one into the radio bar "
              "when no simulator is running."},
    "connect.connect":      {"zh": "连接", "en": "Connect"},
    "connect.connecting":   {"zh": "连接中…", "en": "Connecting…"},
    "connect.disconnect":   {"zh": "断开", "en": "Disconnect"},

    # ---------- 三条链路的状态 ----------
    # 模拟器、FSD、语音各自独立，一条断了不影响另外两条，所以状态也分三行显示。
    "sim.waiting":          {"zh": "X-Plane：等待中", "en": "X-Plane: waiting"},
    "sim.connected":        {"zh": "X-Plane：已连接", "en": "X-Plane: connected"},
    "sim.link_up":          {"zh": "已连接 X-Plane @ {address}",
                             "en": "Connected to X-Plane @ {address}"},
    "sim.no_data":          {"zh": "X-Plane 没有数据（是否已进入飞行？）",
                             "en": "No data from X-Plane (are you in a flight?)"},
    "sim.port_error":       {"zh": "打不开 UDP 端口: {error}",
                             "en": "Could not open the UDP port: {error}"},
    "net.disconnected":     {"zh": "网络：未连接", "en": "Network: not connected"},
    "net.observer":         {"zh": "网络：观察员模式（不上网络）",
                             "en": "Network: observer mode (not connected)"},
    "net.online":           {"zh": "网络：已上线", "en": "Network: online"},
    "net.reconnecting":     {"zh": "网络：重连中", "en": "Network: reconnecting"},
    "net.offline":          {"zh": "网络：已下线", "en": "Network: offline"},
    "net.other":            {"zh": "网络：{state}", "en": "Network: {state}"},
    "voicebar.disconnected": {"zh": "语音：未连接", "en": "Voice: not connected"},
    "voicebar.online":      {"zh": "语音：已连接", "en": "Voice: connected"},
    "voicebar.reconnecting": {"zh": "语音：重连中", "en": "Voice: reconnecting"},
    "voicebar.offline":     {"zh": "语音：已下线", "en": "Voice: offline"},
    "voicebar.other":       {"zh": "语音：{state}", "en": "Voice: {state}"},

    # ---------- 消息区 ----------
    "messages.title":       {"zh": "消息", "en": "Messages"},
    "messages.recipient_hint": {"zh": "收件人（呼号，留空发到当前频率）",
                                "en": "Recipient (callsign; blank sends to the "
                                      "current frequency)"},
    "messages.body_hint":   {"zh": "输入消息后回车发送",
                             "en": "Type a message and press Enter"},
    "messages.send":        {"zh": "发送", "en": "Send"},

    # ---------- 附近管制 ----------
    "controllers.title":    {"zh": "附近管制", "en": "Nearby ATC"},
    "controllers.hint":     {"zh": "双击把呼号填进收件人",
                             "en": "Double-click to put the callsign in the "
                                   "recipient box"},

    # ---------- 无线电条 ----------
    "radio.title":          {"zh": "无线电", "en": "Radio"},
    "radio.com1_none":      {"zh": "COM1  ---.---", "en": "COM1  ---.---"},
    "radio.com1":           {"zh": "COM1  {frequency}", "en": "COM1  {frequency}"},
    "radio.manual_hint":    {"zh": "频率，留空跟随 COM1",
                             "en": "Frequency — empty follows COM1"},
    "radio.manual_tip":     {"zh": "观察员模式专用：手输一个频率，语音就待在那里。"
                                   "清空则回到跟随座舱 COM1。",
                             "en": "Observer mode only: type a frequency and voice stays "
                                   "there. Clear it to follow the cockpit's COM1 again."},
    "radio.traffic":        {"zh": "他机 {count}", "en": "Traffic {count}"},
    "radio.position_none":  {"zh": "位置 --", "en": "Position --"},
    "radio.ident":          {"zh": "IDENT", "en": "IDENT"},
    "radio.ptt":            {"zh": "按住通话", "en": "Hold to talk"},
    "radio.ptt_tip":        {"zh": "按住这里说话。也可以在设置里绑按键、鼠标侧键或摇杆按钮",
                             "en": "Hold to transmit. You can also bind a key, a mouse "
                                   "side button or a joystick button in the settings"},
    "status.ready":         {"zh": "就绪", "en": "Ready"},

    # ---------- 消息区里的每一行 ----------
    "msg.sim":              {"zh": "[X-Plane] {message}", "en": "[X-Plane] {message}"},
    "msg.net":              {"zh": "[网络] {message}", "en": "[Network] {message}"},
    "msg.voice":            {"zh": "[语音] {message}", "en": "[Voice] {message}"},
    "msg.text":             {"zh": "{sender}: {body}", "en": "{sender}: {body}"},
    "msg.text_private":     {"zh": "{sender} → {recipient}: {body}",
                             "en": "{sender} → {recipient}: {body}"},
    "msg.sent":             {"zh": "我 → {recipient}: {body}",
                             "en": "me → {recipient}: {body}"},
    "msg.wallop_sent":      {"zh": "我 → 督导: {body}",
                             "en": "me → supervisors: {body}"},
    "msg.wallop_empty":     {"zh": ".wallop 后面要写内容，例如 .wallop 请求协助",
                             "en": "Write something after .wallop, e.g. "
                                   ".wallop request assistance"},
    "msg.observer_on":      {"zh": "观察员模式：只连语音，网络上不会出现这架飞机",
                             "en": "Observer mode: voice only, this aircraft is not on "
                                   "the network"},
    "msg.observer_off":     {"zh": "已退出观察员模式：下次连接会正常上网络",
                             "en": "Observer mode off: the next connection joins the "
                                   "network normally"},
    "msg.observer_no_frequency": {
        "zh": "还没有频率：在无线电栏里输入一个，或者开着模拟器跟 COM1 走",
        "en": "No frequency yet: type one into the radio bar, or start the simulator "
              "and follow COM1"},
    "msg.manual_frequency": {"zh": "手动频率 {frequency}",
                             "en": "Manual frequency {frequency}"},
    "msg.follow_com1":      {"zh": "频率改回跟随 COM1", "en": "Following COM1 again"},
    "msg.bad_frequency":    {"zh": "频率 {value} 读不出来，写成 121.800 这样",
                             "en": "Cannot read the frequency {value} — write it like "
                                   "121.800"},
    "msg.disconnected":     {"zh": "已断开", "en": "Disconnected"},
    "msg.not_connected":    {"zh": "尚未连接到网络，消息没有发出去",
                             "en": "Not connected to the network — the message was "
                                   "not sent"},
    "msg.no_recipient":     {"zh": "没有收件人，也读不到 COM1 频率",
                             "en": "No recipient, and COM1 cannot be read"},
    "msg.ident":            {"zh": "IDENT", "en": "IDENT"},
    "msg.plan_filed":       {"zh": "飞行计划已提交", "en": "Flight plan filed"},
    "msg.plan_local":       {"zh": "尚未连接到网络，飞行计划只保存在本地",
                             "en": "Not connected — the flight plan was only saved "
                                   "locally"},

    # ---------- 弹框 ----------
    "dialog.callsign_bad":  {"zh": "呼号不可用", "en": "Callsign not usable"},
    "dialog.missing":       {"zh": "缺少信息", "en": "Missing information"},
    "dialog.missing_body":  {"zh": "请填写 CAN ID 和密码。",
                             "en": "Enter your CAN ID and password."},
    "dialog.no_sim":        {"zh": "X-Plane 未连接", "en": "X-Plane not connected"},
    "dialog.no_sim_body":   {"zh": "还没有从 X-Plane 收到数据。没有位置就无法把飞机"
                                   "报到网络上。\n\n仍然继续连接吗？",
                             "en": "No data from X-Plane yet. Without a position your "
                                   "aircraft cannot be reported to the network.\n\n"
                                   "Connect anyway?"},
    "dialog.observer":      {"zh": "观察员模式", "en": "Observer mode"},
    "dialog.observer_busy": {"zh": "连接期间不能切换观察员模式。先断开，再改。",
                             "en": "Observer mode cannot be switched while connected. "
                                   "Disconnect first."},
    "dialog.audio":         {"zh": "音频设备", "en": "Audio device"},
    "dialog.audio_failed":  {"zh": "重开音频设备失败：{error}",
                             "en": "Could not reopen the audio device: {error}"},
    "dialog.about":         {"zh": "关于", "en": "About"},
    "dialog.about_body":    {"zh": "{name} {version}\n\n"
                                   "Cerulean Aviation Network 的 X-Plane 飞行员客户端。\n"
                                   "语音走 Mumble，网络走 FSD，飞行数据从 X-Plane 的 "
                                   "UDP 取。\n\n日志：{log}",
                             "en": "{name} {version}\n\n"
                                   "The X-Plane pilot client for the Cerulean Aviation "
                                   "Network.\nVoice over Mumble, network over FSD, "
                                   "flight data over X-Plane's UDP link.\n\nLog: {log}"},
    "dialog.no_log":        {"zh": "（未写入文件）", "en": "(not written to a file)"},

    "menu.update":          {"zh": "检查更新(&U)", "en": "Check for &updates"},

    # ---------- 他机插件的安装 ----------
    # X-Plane 的安装目录 UDP 那条链路给不出来，所以这里既要报探测结果，也要能
    # 让用户自己指。文案的重点是把"下一步该干什么"说清楚。
    "plugin.title":         {"zh": "他机插件", "en": "Traffic plugin"},
    "plugin.install":       {"zh": "安装插件", "en": "Install the plugin"},
    "plugin.update":        {"zh": "更新插件", "en": "Update the plugin"},
    "plugin.reinstall":     {"zh": "重新安装", "en": "Reinstall"},
    "plugin.pick_root":     {"zh": "选择 X-Plane 目录…", "en": "Choose the X-Plane folder…"},
    "plugin.pick_title":    {"zh": "选择 X-Plane 安装目录",
                             "en": "Choose the X-Plane installation folder"},
    "plugin.no_root":       {"zh": "没有自动找到 X-Plane 的安装目录，请手工指定",
                             "en": "Could not find the X-Plane folder — please choose it"},
    "plugin.not_xplane":    {"zh": "{path} 里没有 Resources/plugins，不像是 X-Plane 的安装目录",
                             "en": "{path} has no Resources/plugins — that does not look "
                                   "like an X-Plane installation"},
    "plugin.missing":       {"zh": "X-Plane：{path}\n还没有装他机插件",
                             "en": "X-Plane: {path}\nThe traffic plugin is not installed"},
    "plugin.outdated":      {"zh": "X-Plane：{path}\n装着的插件和这个版本的客户端不一致，"
                                   "建议更新",
                             "en": "X-Plane: {path}\nThe installed plugin does not match "
                                   "this build of the client — updating is recommended"},
    "plugin.current":       {"zh": "X-Plane：{path}\n插件已是最新",
                             "en": "X-Plane: {path}\nThe plugin is up to date"},
    # 协议对不上时插件是静默丢帧的，症状只有"他机一架都不出现"，所以这句要
    # 明说因果，不能只说"版本不一致"
    "plugin.protocol_mismatch": {"zh": "装着的插件用的是第 {installed} 版桥接协议，客户端是"
                                       "第 {bundled} 版——不更新的话，他机会一架都不出现，"
                                       "而且不会有任何报错。",
                                 "en": "The installed plugin speaks bridge protocol "
                                       "version {installed}, this client speaks "
                                       "{bundled} — until it is updated no traffic will "
                                       "appear at all, and nothing will report an error."},
    "plugin.no_xppython3":  {"zh": "还没装 XPPython3，装了插件也不会被加载。"
                                   "X-Plane 12 装 v4.x，X-Plane 11.52 装 v3.1.5"
                                   "（v4 不兼容 XP11）：https://xppython3.readthedocs.io",
                             "en": "XPPython3 is not installed, so the plugin will not be "
                                   "loaded. X-Plane 12 needs v4.x, X-Plane 11.52 needs "
                                   "v3.1.5 (v4 does not work on XP11): "
                                   "https://xppython3.readthedocs.io"},
    "plugin.installed":     {"zh": "已装好", "en": "Installed"},
    "plugin.installed_body": {"zh": "插件已装到\n{path}\n\n重启 X-Plane 后生效。",
                              "en": "The plugin was installed to\n{path}\n\nRestart "
                                    "X-Plane for it to take effect."},
    "plugin.failed":        {"zh": "安装失败", "en": "Installation failed"},
    "plugin.failed_body":   {"zh": "写不进去：{error}\n\nX-Plane 装在 Program Files 之类"
                                   "的位置时，需要用管理员身份运行本程序。",
                             "en": "Could not write the file: {error}\n\nIf X-Plane is "
                                   "installed somewhere like Program Files, run this "
                                   "program as an administrator."},

    # ---------- 更新 ----------
    # 查到新版只是弹一句，装不装是用户的事。措辞要说清下载走的是自己的服务器
    # ——大陆连 GitHub 很不稳，这正是这个功能存在的理由。
    "update.title":         {"zh": "有新版本", "en": "A new version is available"},
    "update.body":          {"zh": "XPC for CAN {version} 已经发布{size}。\n"
                                   "你现在用的是 {current}。",
                             "en": "XPC for CAN {version} is out{size}.\n"
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
    "msg.update_available": {"zh": "[更新] 有新版 {version}（已跳过）",
                             "en": "[Update] {version} is available (skipped)"},
    "msg.update_downloading": {"zh": "[更新] 正在浏览器里下载 {version}",
                               "en": "[Update] downloading {version} in your browser"},
    "msg.update_skipped":   {"zh": "[更新] 已跳过 {version}",
                             "en": "[Update] skipped {version}"},

    # ---------- 设置 ----------
    "settings.title":       {"zh": "设置", "en": "Settings"},
    "settings.tab_audio":   {"zh": "音频", "en": "Audio"},
    "settings.tab_network": {"zh": "网络", "en": "Network"},
    "settings.tab_traffic": {"zh": "他机", "en": "Traffic"},
    "settings.mic":         {"zh": "麦克风", "en": "Microphone"},
    "settings.speaker":     {"zh": "扬声器", "en": "Speaker"},
    "settings.mic_volume":  {"zh": "麦克风音量", "en": "Microphone volume"},
    "settings.speaker_volume": {"zh": "扬声器音量", "en": "Speaker volume"},
    "settings.system_default": {"zh": "系统默认", "en": "System default"},
    "settings.message_sound": {"zh": "收到管制消息时播放提示音",
                               "en": "Play a sound for incoming ATC messages"},
    "settings.message_sound_tip": {
        "zh": "私聊给你的消息一定会响；频率上的消息默认只有点到你呼号的才响。"
              "提示音走上面选的那块扬声器，不是系统默认设备。",
        "en": "A message addressed to you always chimes; a message on the "
              "frequency chimes only when it names your callsign. The chime "
              "plays on the speaker selected above, not the system default."},
    "settings.message_sound_all": {"zh": "频率上的每条消息都提示",
                                   "en": "Chime for every message on the frequency"},
    "settings.message_sound_volume": {"zh": "提示音音量", "en": "Alert volume"},
    "settings.message_sound_test": {"zh": "试听", "en": "Test"},
    "settings.language":    {"zh": "语言", "en": "Language"},
    "settings.language_note": {"zh": "切换语言后重开窗口生效",
                               "en": "Reopen the window for a language change to apply"},
    "settings.mumble_host": {"zh": "语音服务器", "en": "Voice server"},
    "settings.fsd_host":    {"zh": "FSD 服务器", "en": "FSD server"},
    "settings.fsd_port":    {"zh": "FSD 端口", "en": "FSD port"},
    "settings.real_name":   {"zh": "真实姓名", "en": "Real name"},
    "settings.connect_voice": {"zh": "连接语音服务器", "en": "Connect to the voice server"},
    "settings.connect_fsd": {"zh": "连接 FSD 网络", "en": "Connect to the FSD network"},
    "settings.render_traffic": {"zh": "把其他飞机画进 X-Plane",
                                "en": "Draw other aircraft in X-Plane"},
    "settings.csl_path":    {"zh": "CSL 模型目录", "en": "CSL model folder"},
    "settings.csl_hint":    {"zh": "装好的 CSL 模型包所在目录",
                             "en": "Where the installed CSL packages live"},
    "settings.browse":      {"zh": "浏览…", "en": "Browse…"},
    "settings.csl_pick":    {"zh": "选择 CSL 模型目录", "en": "Choose the CSL model folder"},
    "settings.range":       {"zh": "显示范围", "en": "Range"},
    "settings.range_suffix": {"zh": " 海里", "en": " NM"},
    "settings.traffic_note": {"zh": "他机要画进 X-Plane，得先装 XPPython3——装哪个版本"
                                    "取决于模拟器：\n"
                                    "    X-Plane 12    → XPPython3 v4.x\n"
                                    "    X-Plane 11.52 → XPPython3 v3.1.5（v4 不兼容 XP11）\n"
                                    "插件本身用下面那个按钮装，不用自己拷文件。\n\n"
                                    "没装模型也能用——他机仍然会出现在 TCAS 和 ND 上，"
                                    "只是看不到机身。\n"
                                    "TCAS 需要 X-Plane 11.50 以上；更老的版本只会把飞机"
                                    "画出来。\n"
                                    "同时开着 LiveTraffic 之类的插件会互相抢 AI 机位，"
                                    "建议只开一个。",
                              "en": "To draw other aircraft in X-Plane you first need "
                                    "XPPython3 — which version depends on the "
                                    "simulator:\n"
                                    "    X-Plane 12    → XPPython3 v4.x\n"
                                    "    X-Plane 11.52 → XPPython3 v3.1.5 (v4 does not "
                                    "work on XP11)\n"
                                    "The plugin itself installs with the button below — "
                                    "no need to copy files by hand.\n\n"
                                    "It works without models too — other aircraft still "
                                    "show up on TCAS and the ND,\nyou just cannot see the "
                                    "airframe.\n"
                                    "TCAS needs X-Plane 11.50 or newer; older versions "
                                    "only draw the aircraft.\n"
                                    "Running LiveTraffic or a similar plugin at the same "
                                    "time fights over the AI slots —\nbetter to run only "
                                    "one."},
    "settings.debug":       {"zh": "记录调试信息（重启后生效）",
                             "en": "Write debug logs (takes effect after restart)"},
    "settings.debug_tip":   {"zh": "打开后会把协议细节也写进日志，排查连不上、听不到时用",
                             "en": "Logs protocol detail — use it when you cannot connect "
                                   "or hear anything"},
    "settings.open_log":    {"zh": "打开日志", "en": "Open log"},

    # ---------- PTT 绑定 ----------
    # 三种输入源共用一份列表，任意一个按住就发话。文案要说清"还能再加一个"，
    # 否则用户会以为设了摇杆就没有键盘了。
    "settings.ptt_title":   {"zh": "PTT 绑定", "en": "PTT bindings"},
    "settings.ptt_hint":    {"zh": "键盘、鼠标侧键、摇杆按钮都行，按住其中任意一个即发话",
                             "en": "A key, a mouse side button or a joystick button — "
                                   "hold any one of them to transmit"},
    "settings.ptt_none":    {"zh": "还没有绑定，PTT 用不了",
                             "en": "No bindings yet — PTT will not work"},
    "settings.ptt_add":     {"zh": "添加绑定", "en": "Add a binding"},
    "settings.ptt_capturing": {"zh": "请按下按键、鼠标侧键或摇杆按钮…（点此取消）",
                               "en": "Press a key, a mouse side button or a joystick "
                                     "button… (click to cancel)"},
    "settings.ptt_remove":  {"zh": "移除这条绑定", "en": "Remove this binding"},
    "settings.ptt_duplicate": {"zh": "这条绑定已经有了", "en": "That binding is already there"},

    # 一条绑定在界面上怎么念。ptt.py 只给出键名（"V" / "X1" / "3"），说法在这里拼——
    # 那个模块是三个客户端逐字节共享的，不产生界面文字。
    "ptt.keyboard":         {"zh": "键盘 {key}", "en": "Key {key}"},
    "ptt.mouse":            {"zh": "鼠标侧键 {button}", "en": "Mouse {button}"},
    "ptt.joystick":         {"zh": "摇杆 {device} 的按钮 {button}",
                             "en": "Button {button} on {device}"},
    "ptt.joystick_plain":   {"zh": "摇杆按钮 {button}", "en": "Joystick button {button}"},

    # ---------- 飞行计划 ----------
    "plan.title":           {"zh": "飞行计划", "en": "Flight plan"},
    "plan.rules":           {"zh": "飞行规则", "en": "Flight rules"},
    "plan.ifr":             {"zh": "IFR 仪表", "en": "IFR"},
    "plan.vfr":             {"zh": "VFR 目视", "en": "VFR"},
    "plan.aircraft":        {"zh": "机型", "en": "Aircraft"},
    "plan.cruise_speed":    {"zh": "巡航速度", "en": "Cruise speed"},
    "plan.departure":       {"zh": "起飞地", "en": "Departure"},
    "plan.arrival":         {"zh": "目的地", "en": "Destination"},
    "plan.alternate":       {"zh": "备降场", "en": "Alternate"},
    "plan.cruise_altitude": {"zh": "巡航高度", "en": "Cruise altitude"},
    "plan.departure_time":  {"zh": "预计起飞", "en": "Off-block time"},
    "plan.time_hint":       {"zh": "UTC，如 1230", "en": "UTC, e.g. 1230"},
    "plan.enroute":         {"zh": "航路时间", "en": "Time en route"},
    "plan.fuel":            {"zh": "续航时间", "en": "Fuel on board"},
    "plan.hours":           {"zh": "小时", "en": "h"},
    "plan.minutes":         {"zh": "分钟", "en": "min"},
    "plan.route":           {"zh": "航路", "en": "Route"},
    "plan.remarks":         {"zh": "备注", "en": "Remarks"},

    # ---------- 语音（voice.py 的 _status 消息） ----------
    # 这些会直接进消息区，是界面文字。同一个模块里的 log.* 仍然是英文。
    "voice.connecting":     {"zh": "正在连接语音服务器 {server} …",
                             "en": "Connecting to the voice server {server}…"},
    "voice.audio_failed":   {"zh": "打不开音频设备: {error}",
                             "en": "Could not open the audio device: {error}"},
    "voice.connect_failed": {"zh": "语音服务器连接失败: {error}",
                             "en": "Could not connect to the voice server: {error}"},
    "voice.rejected":       {"zh": "语音服务器拒绝了 {username}（{reason}）",
                             "en": "The voice server rejected {username} ({reason})"},
    "voice.rejected_guess": {"zh": "用户名或密码不对？", "en": "Wrong username or password?"},
    "voice.rejected_plain": {"zh": "服务器拒绝了连接",
                             "en": "The server refused the connection"},
    "voice.online":         {"zh": "语音已连接（{username}）", "en": "Voice connected ({username})"},
    "voice.reconnected":    {"zh": "语音已重连（{username}）",
                             "en": "Voice reconnected ({username})"},
    "voice.stopped":        {"zh": "语音已断开", "en": "Voice disconnected"},
    # 掉线之后：先重连，最多 RECONNECT_LIMIT 次，都失败才真的下线。这三条要说清
    # "还在试"和"不再试了"的区别——一次抖动看上去不该和彻底断了一样。
    "voice.reconnecting":   {"zh": "语音连接断开，正在重连（{attempt}/{limit}）",
                             "en": "Voice connection lost — reconnecting "
                                   "({attempt}/{limit})"},
    "voice.dropped_retrying": {"zh": "语音连接断开，正在重连（最多 {limit} 次）",
                               "en": "Voice connection lost — reconnecting (up to "
                                     "{limit} attempts)"},
    "voice.give_up":        {"zh": "掉线后重连 {limit} 次都没成功，语音已下线",
                             "en": "Reconnected {limit} times without success — voice "
                                   "went offline"},
    # 被服务端踢下线。和 give_up 分开，因为它不是"连不上"：账号在别处登录了，
    # 连回去只会把那一端顶掉，所以文案要说清楚不再重连。
    "voice.kicked":         {"zh": "被服务器断开：{reason}。不会自动重连。",
                             "en": "Disconnected by the server: {reason}. "
                                   "Not reconnecting."},
    "voice.kicked_plain":   {"zh": "账号可能在其他位置登录了",
                             "en": "the account may have signed in elsewhere"},

    # 服务器拒绝了某个动作。笼统说成"操作失败"会把人引到错误的方向——
    # 缺 MakeTempChannel 时用户会一直去查频率填得对不对。
    "denied.Permission":    {"zh": "没有权限（建频率频道要根频道的 MakeTempChannel，"
                                   "进频道要 Enter）",
                             "en": "Not permitted (creating a frequency channel needs "
                                   "MakeTempChannel on the root channel, and joining "
                                   "one needs Enter)"},
    "denied.ChannelName":   {"zh": "频道名不合服务器的规矩",
                             "en": "The server does not accept that channel name"},
    "denied.NestingLimit":  {"zh": "频道层级超过了服务器上限",
                             "en": "Too many nested channels for this server"},
    "denied.ChannelCountLimit": {"zh": "服务器上的频道数已达上限",
                                 "en": "The server has reached its channel limit"},
    "denied.UserListenerLimit": {"zh": "服务器限制了每个用户能监听的频道数",
                                 "en": "The server caps how many channels one user may "
                                       "listen to"},
    "denied.other":         {"zh": "服务器拒绝了操作: {kind}",
                             "en": "The server refused the action: {kind}"},
    # 服务器有时还会附一句自己的说明，原样带上——它常常是唯一能定位问题的东西
    "denied.with_note":     {"zh": "{reason}（{note}）", "en": "{reason} ({note})"},

    # ---------- FSD（fsdpilot.py 的 _status 消息） ----------
    "fsd.connecting":       {"zh": "正在以 {callsign} 登录 {server} …",
                             "en": "Logging in to {server} as {callsign}…"},
    "fsd.connect_failed":   {"zh": "无法连接 FSD 服务器 {host}:{port}（{error}）",
                             "en": "Could not reach the FSD server {host}:{port} ({error})"},
    "fsd.online":           {"zh": "已作为 {callsign} 上线", "en": "Online as {callsign}"},
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
    "fsd.reconnecting":     {"zh": "与 FSD 的连接断开，正在重连（{attempt}/{limit}）",
                             "en": "Lost the FSD connection — reconnecting "
                                   "({attempt}/{limit})"},
    "fsd.give_up":          {"zh": "与 FSD 断开后重连 {limit} 次都没成功，已下线",
                             "en": "Reconnected to FSD {limit} times without success — "
                                   "went offline"},
    "fsd.stopped":          {"zh": "已从 FSD 下线", "en": "Signed off from FSD"},

    # 呼号在本地先查一遍，用户得到的是解释而不是一次登录拒绝。规则来自 can-fsd
    # 的 IsValidCallsign。
    "callsign.length":      {"zh": "呼号 {callsign} 有 {count} 个字符，服务端只接受 "
                                   "2-{limit} 个",
                             "en": "The callsign {callsign} is {count} characters long; "
                                   "the server only accepts 2-{limit}"},
    "callsign.charset":     {"zh": "呼号 {callsign} 含有服务端不接受的字符"
                                   "（只能是字母、数字、_ 和 -）",
                             "en": "The callsign {callsign} contains characters the "
                                   "server does not accept (only letters, digits, _ "
                                   "and - are allowed)"},
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


def binding_label(binding):
    """一条 PTT 绑定在界面上怎么念。

    放在这里而不是 ptt.py 里：那个模块在三个客户端之间逐字节共享，一旦让它产生
    界面文字，同一套中文就会躺在三份副本里，翻译时必然漏掉其中两份。它只给键名，
    说法在这里拼。
    """
    if binding.kind == "keyboard":
        return t("ptt.keyboard", key=binding.token())
    if binding.kind == "mouse":
        return t("ptt.mouse", button=binding.token())
    if binding.device_name:
        return t("ptt.joystick", device=binding.device_name, button=binding.token())
    return t("ptt.joystick_plain", button=binding.token())
