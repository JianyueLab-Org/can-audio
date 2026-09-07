# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Mumble-based voice radio system for the Can online flight-sim network: pilots' sim COM1 frequency drives which Mumble channel they sit in, controllers run a stack of frequencies they monitor and transmit on, and server-side ATIS bots broadcast synthesized speech onto their own frequencies. Code comments and all user-facing UI strings are Chinese — keep new strings Chinese to match. **Log messages and logger names are the exception: those are English** (see the Logging section).

There is no dependency manifest, test suite, or build script in the repo. Everything is run directly with `python` or packaged with PyInstaller.

## Components

| Directory | Entry point | Role |
|---|---|---|
| `client/` | `gui.py` | Pilot client for MSFS — reads COM1 via **SimConnect** (`AircraftRequests.get("COM_ACTIVE_FREQUENCY:1")`) |
| `xplane_client/` | `gui.py` | Pilot client for X-Plane — reads COM1 via **X-Plane UDP** (`XPlaneRadio` in `radio.py`) |
| `controller/` | `gui.py` | ATC client — a **stack of frequencies** modelled on [TrackAudio](https://github.com/pierr3/TrackAudio), each with RX/TX/XC |
| `atis/` | `gui.py` | ATIS client modelled on [vATIS](https://github.com/vatis-project/vatis) — stations, presets, templates; broadcasts over Mumble |
| `xpc/` | `gui.py` | **XPC for CAN** — X-Plane pilot client modelled on [xPilot](https://github.com/xpilot-project/xpilot): Mumble voice **and** an FSD connection |
| `msfs/` | `gui.py` | **MSFS for CAN** — the same client for Microsoft Flight Simulator, over SimConnect |
| `server/` | `login.py`, `ATIS/mumble.py` | Runs on the Mumble/Murmur host: Ice authenticator + server-side ATIS bot fleet |

`client/` and `xplane_client/` are **near-duplicate forks** of each other — same `radio.py`/`settings.py`/`gui.py` layout, identical apart from where COM1 comes from, so a fix to audio or PTT logic has to be applied to both, every time. `controller/`, `atis/` and `xpc/` no longer share that shape: they were rebuilt around their own models (`radiostack.py` + `voice.py`, `profile.py` + `broadcast.py`, and `xplane.py` + `fsdpilot.py` + `voice.py`) and have no `radio.py`.

**`client/` and `xplane_client/` are legacy and are no longer shipped.** `xpc/` supersedes `xplane_client/` and `msfs/` supersedes `client/` — same simulators, but those two also log in to FSD so the aircraft appears on the network, where the old pair is voice-only. The release workflow (`.github/workflows/release.yml`) builds only `controller`, `atis`, `xpc` and `msfs`; the legacy pair is deliberately absent from the build matrix, and the release notes say so.

Legacy does not mean dead: both still run, still have `test_radio.py` and `smoke_gui.py`, and the CI **does** run their tests and smoke checks — a regression in them still has to be known about, because they share the `FREQ_*` channel contract and the Mumble handling that everything else depends on. What it means is that no packaged build is produced, so a fix landing there reaches nobody until someone builds it by hand. Prefer fixing `xpc/`/`msfs/` when the same bug exists in both.

There is no shared package — each component is imported flat from its own directory, which is why `mumblecompat.py` and `applog.py` exist as per-component copies rather than one import. Keep that pattern when adding cross-cutting helpers; a shared parent module would need path juggling in every PyInstaller spec.

The pilot clients now carry `applog.py` too, so all five GUI components log rather than `print` — which matters because the packaged builds are `console=False` and a bare `print` goes nowhere. They also have tests: `test_radio.py` (channel switching, PTT guards) and `smoke_gui.py`, which is the only thing that touches their `gui.py` at all.

## Commands

Run each app **from inside its own directory** — icon paths (`.\favicon.ico`) and config files are resolved relative to the CWD:

```powershell
cd client;        python gui.py     # MSFS pilot client (needs MSFS running)
cd xplane_client; python gui.py     # X-Plane pilot client (needs X-Plane in a flight)
cd controller;    python gui.py     # ATC client
cd atis;          python gui.py     # ATIS client
cd xpc;           python gui.py     # XPC for CAN — X-Plane pilot client (voice + FSD)
cd msfs;          python gui.py     # MSFS for CAN — same, for Microsoft Flight Simulator
cd server\ATIS;   python mumble.py  # server-side ATIS manager (needs ffmpeg on PATH)
python server\login.py              # Murmur Ice authenticator (run on the Mumble host)
cd server; ./start.sh               # both of the above two, supervised (see below)
```

**The server ships as a container**: the root `Dockerfile` builds mumble-server plus `login.py`, and `server/start.sh` is its entrypoint (also the documented way to run it on a bare host — `--login` starts only the authenticator). Four things it does that the obvious Dockerfile does not, each of which produced a real failure mode:

- **Either process exiting takes the whole thing down**, because both halves fail *silently* otherwise. Without `login.py` Murmur falls back to its own empty account database and every login is refused — as *"密码错误"* on the client, while the voice port still answers and the container looks healthy. And after a `mumble-server` restart the authenticator is never re-registered, which is why `login.py`'s `watch_connection()` deliberately exits rather than staying up useless. Pair it with `--restart unless-stopped`.
- **No secret in the image.** Anything written into a `Dockerfile` stays in the image layers and in `docker history` forever, and the repo's own rule is that a working default is never changed. `start.sh` generates a random Ice secret into `mumble-server.ini` at startup — which is exactly where `serverconf.py` looks third — so nothing needs configuring; the SuperUser password comes only from `MUMBLE_SUPERUSER_PASSWORD` and is set before the server starts (`-supw` opens the database directly). `test_serverconf.py`'s repo scan now covers `Dockerfile`, `*.sh`, `*.yml` and `*.ini` as well as `.py`, since deployment files are where a secret comes back.
- **Ice stays on loopback and 6502 is not published.** The authenticator shares the container, so loopback is enough; publishing it would put an interface guarded by one secret on the public net (and, bound to `127.0.0.1`, the mapping does not even work). The same applies to `fix_acl.py` / `whereami.py` — they must be run with `docker exec` from inside.
- **`/var/lib/mumble-server` must be a volume.** The SuperUser password, registered users and the root ACL that `fix_acl.py` grants all live in that sqlite file. Losing it presents as the `Listen`-permission symptom — controllers hear nothing but their primary frequency — with no visible connection to having replaced the container.

**The image is built by CI and deployed with compose.** `.github/workflows/server-image.yml` is path-filtered to `server/**` and `Dockerfile` (so client changes never trigger it), runs the `server/` and `server/ATIS/` tests, builds `linux/amd64` only — arm64 would mean QEMU-emulating `apt` and `slice2py` for a fleet that runs on x86 — and pushes `latest` plus a `sha-…` tag to GHCR, which is what `SERVER_IMAGE` in `.env` pins for a rollback. `docker-compose.yml` sets `pull_policy: always` deliberately: a service that declares both `image:` and `build:` will *silently build from source* when the image is missing locally, so without it a server would quietly deploy something other than what CI produced. The ATIS profile is the exception — it has no published image and always builds locally with `WITH_ATIS=1`. Compose interpolates the whole file regardless of active profiles, so the ATIS variables must not use `${VAR:?}` or a machine with no ATIS password could not start the plain server either.

**The CI smoke step is the point of the whole workflow.** Every failure mode this image has is invisible at build time — a missing `serverconf.py` is an `ImportError`, a non-executable `start.sh` is an exec failure, a mis-detected ini or slice path is a dead Ice port — and `docker build` reports all of them as success. So the job runs the container, waits for its own `HEALTHCHECK` to go `healthy`, and then greps the log for `认证器已设置` and `服务器回调已注册`, because `healthy` alone only proves the port is open and the process is alive. The SuperUser password it passes is `openssl rand`, never a literal: `test_serverconf.py` scans `*.yml` too now.

The image detects the ini and slice paths rather than hardcoding them (1.5 uses `/etc/mumble/mumble-server.ini` and `MumbleServer.ice`, 1.3 uses `/etc/mumble-server.ini` and `Murmur.ice`) and **fails the build on mumble-server < 1.4**, because channel listeners are a 1.4 feature and on 1.3 the controller client goes quiet with no error anywhere. The base image tag is pinned for the same reason. The ATIS bot fleet is copied in but its dependencies (`pymumble`, `numpy`, `edge-tts`, `tabulate`, `ffmpeg`, `libopus0`) are only installed under `--build-arg WITH_ATIS=1`, since it is not in the `CMD`.

Build a Windows bundle (from the component directory):

```powershell
pyinstaller gui.spec
```

**Run the one in `dist/`, never the one in `build/`.** PyInstaller leaves an identically-named exe in its work directory (`build/gui/`, named after the spec file), but that one is only the bootloader plus the archive — `python312.dll`, `opus.dll` and the Qt libraries all live in `dist/<name>/_internal/`. Launching the `build/` copy fails with *"Failed to load Python DLL … LoadLibrary: The specified module could not be found"*, which reads like a broken build but is just the wrong exe. The shippable output is `dist/audio-for-can/`, `dist/atis-for-can/` and `dist/xpc-for-can/`.

Tests — every component except `server/login.py` has some; each runs from its own directory and needs no server, no audio device and no network:

```powershell
cd controller
python -m unittest test_radiostack -v    # radio stack model + RX/TX/XC coupling rules
python -m unittest test_radiostack.CouplingRulesTest.test_tx_forces_rx_on   # one test
python -m unittest test_applog           # logging, including uncaught-exception capture
python -m unittest test_i18n             # both languages present, no hardcoded UI strings
python -m unittest test_ptt              # keyboard / joystick / mouse PTT, with fake devices
python smoke_gui.py                      # builds every window/dialog offscreen

cd atis
python -m unittest test_atis -v          # METAR, templates, stations, vATIS import, network config, FSD
python -m unittest test_applog
python -m unittest test_i18n             # 两种语言都齐，且源码里没有写死的界面文字
python smoke_gui.py

cd xpc
python -m unittest test_xpc -v           # PBH, position packets, RREF, traffic, model matching
python -m unittest test_i18n             # 两种语言都齐；ptt.py 里不能有界面文字
python -m unittest test_ptt
python -m unittest test_xpc.PbhTest      # the one that must match can-fsd exactly
python -m unittest test_xpc.WantsAlertTest      # 消息提示音：哪条该响
python -m unittest test_xpc.ChimePlayTest       # 设备回退、连响抑制、开不出来也不炸
python -m unittest test_xpc.ObserverFrequencyTest  # 观察员：手输频率和 COM1 谁说了算
python -m unittest test_xpc.ModelMatchingTest   # CSL fallback chain
python -m unittest test_xpc.PluginInstallTest   # 插件安装：目录识别、新旧判定、协议号
python smoke_gui.py

cd msfs
python -m unittest test_msfs -v          # BCD squawk, SimVar units, aircraft.cfg
python -m unittest test_i18n
python -m unittest test_ptt
python -m unittest test_msfs.RealWorldLayoutTest   # the ones a live install found
python smoke_gui.py

cd client            # 以及 xplane_client，两份基本一样
python -m unittest test_radio -v         # 频道切换（超时/重试）、PTT 的进频道判据
python smoke_gui.py                      # 登录/主界面/设置，以及切换不能卡 Qt 线程

cd server\ATIS
python -m unittest test_mumble -v        # 通播的建频道 / 进频道，含服务器不回话
```

`test_xpc.py`'s `VoiceChannelTest` is mostly `inspect.getsource()` string-matching — it can only show the code *looks* right. `VoiceRuntimeTest` and `VoiceStartupFailureTest` actually run `_channel_loop`/`_run`/`start()` against a fake server, which is how the reconnect bugs above were found while every source-match assertion still passed. Put new voice coverage there, not in the string-matching class. Because `msfs/voice.py` is a byte-identical copy and is only covered through those xpc tests, `msfs/test_msfs.py`'s `SharedCopyTest` fails if the two ever drift.

`test_xpc.py` loads `plugin/PI_XpcTraffic.py` directly (the plugin guards its `import xp` so it imports outside X-Plane) to check the bridge reassembler and the animation-value ordering. The rest of the plugin needs a running simulator and is not covered.

**Logging.** Both clients ship `applog.py` (a per-component copy, matching how the rest of the repo duplicates rather than shares). `applog.setup(debug)` installs a rotating file handler writing `audio-for-can.log` / `atis-for-can.log` to the CWD, plus a console handler when one exists. This is not optional polish: the packaged builds are `console=False`, so a bare `print` goes nowhere and a user reporting "it won't connect" has nothing to send you. Use `log = logging.getLogger("voice")` per module rather than `print`.

**What goes at INFO is "what changed", not "what happened".** A log a user actually sends you has to fit in a chat window, so the per-event chatter lives at DEBUG and INFO carries state transitions plus one line per *conclusion*. The rules that produced the current shape, from measuring a real 11-minute session: one line per received transmission, not two (`heard 1003 for 2.5 s (127 frames)` — start-of-burst is DEBUG); the provisional model match and the provisional AI spawn are DEBUG, because the `#SB` reply overwrites both within a second and only the second line is a conclusion; `new aircraft X` is DEBUG since the match line that follows names it anyway, while `went offline` stays INFO; and the 120-character audio-device line repeats at DEBUG when the devices have not changed. That cut a real log by 29 %, and by ~60 % of the traffic lines in a session with many aircraft — so the 1 MB rotation (`MAX_BYTES`, down from 2 MB) now covers *more* wall-clock than before, and the four files a user sends add up to 4 MB instead of 8.

**A drop must explain itself.** `Voice._health()` appends ping RTT, the send-buffer congestion summary from `mumblecompat.send_stats()`, and frames sent/received since the last report to the "connection dropped" line, and the same congestion summary is printed when PTT is released if anything blocked. This is what distinguishes "the uplink genuinely cannot keep up" from "one momentary hiccup" — the two need completely different answers, and before this the log said only `voice connection dropped`. `applog.setup()` also writes one environment line (OS, packaged or from source, CWD, Python version); the CWD alone has already explained one report, where the app was being run from inside a 360zip temp folder and its settings vanished on every launch.

**Log text is English; UI text stays Chinese.** This is the one place the repo's "everything user-facing is Chinese" rule does not apply — log messages *and* logger names are English (`getLogger("voice")`, `getLogger("startup")`, `getLogger("uncaught")`), so a log reads:

```
16:39:37 INFO    voice        reconnecting to the voice server (attempt 1/3)
16:39:37 WARNING voice        3 reconnect attempts after the drop all failed, giving up
```

The dividing line is **where the string ends up**, not which module it lives in: anything that can reach a widget, a `QMessageBox`, a status label or an `i18n` key stays Chinese, including the `message` argument of the `_status()`/`_state()` callbacks — those are UI strings that happen to be echoed into the log, and that echo is deliberate (it puts what the user saw next to what the code was doing). A string that only ever goes to the log is English. So the `reject.*` / `denied.*` entries in each `i18n.py` are still Chinese (in the `zh` half) while every `log.*` call around the code that reads them is not — and `voice.py`'s `_skip()` reasons, which only ever reach the log, are English despite sitting between two translated `_status()` calls.

`controller/`, `atis/`, `xpc/` and `msfs/` are converted. **`client/` and `xplane_client/` are not** — they are legacy and unshipped, so their log text is still Chinese; the exception is `version.py`, which `test_version.CopiesAgreeTest` requires to be byte-identical across all six components, so that one file was synced everywhere. New code follows the English rule regardless of which directory it lands in.

`setup()` also replaces `sys.excepthook` **and** `threading.excepthook` — an uncaught exception in a Qt slot or a worker thread otherwise vanishes silently, leaving a frozen window and an empty log. `--debug` (or the settings checkbox, persisted as `debug`) drops the level to DEBUG, which is where the protocol traffic lives: every FSD packet in and out (with the `#AA` password redacted), channel-listener changes, voice-target changes, and the radio-stack sync.

`smoke_gui.py` replaces `QMessageBox.warning`/`critical` with a recorder before touching the GUI — a modal dialog blocks forever even under the offscreen platform, so any new code path that can pop one needs that stub to stay in place.

Three environment facts that will cost an afternoon otherwise:

- **Use Python 3.12.** PyAudio has no wheel for 3.13+, and building it from source needs PortAudio headers plus MSVC.
- **pymumble 1.6.1 does not work on Python 3.12 out of the box.** Its `connect()` builds the TLS socket with `ssl.wrap_socket()`, which was **removed in 3.12**, and its `except AttributeError` fallback calls *the same removed function* — so the exception escapes, from inside pymumble's own thread. The caller only sees `is_alive() == False` and naturally reports "server rejected the connection", which sends you hunting for a password problem while the TLS handshake never even started (the Murmur log shows a connection opening and closing milliseconds later). `mumblecompat.install()` reinstates a `wrap_socket` built on `SSLContext`. **Every component that speaks Mumble carries a copy and calls it at import** — without it the voice path is dead in every packaged build, pilot clients included. `server/ATIS/mumble.py` was the one that did not, and it is a server-side daemon on Debian 13 / Ubuntu 24.04+ where Python is already ≥ 3.12, so the bot fleet simply could not connect and said `连接错误`; it now carries the same byte-identical copy as the clients and installs it before `import pymumble_py3`, pinned by `CompatPatchTest` in `server/ATIS/test_mumble.py` (which asserts the patch took effect, not that the line is present).

  **That pin has to name every component, and for a while it named one.** It compared `server/ATIS` against `xpc` only, so the two legacy pilot clients quietly stayed on an older copy that had `wrap_socket` but no `patch_send()` — meaning the "voice drops whenever I transmit" bug below was still live in them, with nothing red anywhere. `CompatPatchTest.COMPONENTS` now lists all six client directories and compares each; a directory that is absent (the container ships only `server/`) is skipped, but a present one that differs fails.
- **A full send buffer is not a lost connection, but pymumble treats it as one.** This is the "voice drops whenever I transmit" bug, and none of it is the network. pymumble has **no UDP socket at all** — audio is wrapped in a `UDPTUNNEL` message and pushed through the *same* TCP socket as the control channel (`soundoutput.py`, `# encapsulate in tcp tunnel`), so keying the PTT puts ~50 packets/s through the connection that also carries the pings. That socket is set non-blocking in `connect()`, while both send loops guard the C way: `sent = control_socket.send(...)` then `if sent < 0: raise socket.error(...)` — which in Python can never fire, because a full kernel buffer *raises* `BlockingIOError` (`SSLWantWriteError` over TLS). It propagates to `Mumble.run()`'s `except socket.error`, the connection is marked dead, `DISCONNECTED` fires. The real log reads exactly as you would expect from that: every drop 2–3 s **after** pressing PTT, never on a 0.2 s transmission, and every reconnect succeeding on the first attempt — because nothing was ever broken. `mumblecompat.patch_send()` wraps `Mumble.connect` so every connection (**including each reconnect**, which builds a new socket) gets a `_RetryingSocket`: a full buffer waits for the socket to become writable and resumes, and only a stall longer than `SEND_TIMEOUT` (2 s) is reported as a failure. Partial writes are left alone — both pymumble loops already re-send the remainder. Pinned by `SendBufferTest`; sabotaging either half turns it red.
- **pymumble's blocking commands never time out.** `channels.new_channel()` and `users.myself.move_in()` both go through `execute_command(blocking=True)`, whose `lock.acquire()` has no timeout — pymumble's own source says so (`mumble.py:587`, *"TODO: manage a timeout for blocking commands"*). If the command is never processed the calling thread dies there permanently, and the symptom is the *absence* of a log line: the log stops after "建一个临时的" with neither success nor error, because the thread never returned from that line. Confirmed to leave pilot clients stuck in the root channel (nobody hears them, they hear nobody) and is the likely real cause of the ATIS `Channel FREQ_xxxxxx does not exists` report. **No component may call those two wrappers.** Build the message yourself — `messages.CreateChannel(0, name, True)` / `messages.MoveCmd(mumble.users.myself_session, channel_id)` — send it with `execute_command(cmd, blocking=False)`, and poll for the result against `CHANNEL_TIMEOUT` (5 s): the channel appearing in the table, and `myself["channel_id"]` actually changing. Never book a move as done just because the command was sent, or the retry loop thinks it succeeded while the user is still where they were. `xpc/voice.py` is the reference; the same shape is now in `client/`, `xplane_client/`, `controller/voice.py`, `atis/broadcast.py` and `server/ATIS/mumble.py`. The tests fake it with a Mumble stub whose *blocking* entry points never return, so anything that reaches for them hangs and is caught by a `join(timeout=…)`.
- **pymumble's whole loop dies with the thread that constructed it.** `Mumble.__init__` records `self.parent_thread = threading.current_thread()` (`mumble.py:59`, commented *"main thread of the calling application"*), and the main loop runs `while … and self.parent_thread.is_alive() and not self.exit:` — with the command-queue drain (`while self.commands.is_cmd(): self.treat_command(…)`) **inside** it. `xpc/gui.py` and `msfs/gui.py` start voice with `threading.Thread(target=voice.start).start()`; `Voice.start()` built the `Mumble` object on that throwaway thread and returned as soon as its worker threads were up, so the loop exited seconds after connecting. Everything then *looks* fine — the connection stays open, the channel table stays populated, `mumble.connected` stays at `CONNECTED` (the loop exiting never resets it) — but no queued command is ever sent again. `MoveCmd` sits in the queue forever, the server never receives it, and so it neither moves you nor answers with `PermissionDenied`; the log fills with `发出了进入 FREQ_124550 的请求，但 5 秒内没有生效` against a channel that provably exists with the right id. **The one MSFS/XPC bug that looked like a server problem for days was this.** `Voice.start()` now pins `mumble.parent_thread = threading.main_thread()` right after construction — fixed in `voice.py` rather than at the call sites, so a future `Thread(target=voice.start)` can't reintroduce it silently. Shutdown is unaffected: `_release()` goes through `mumble.stop()`, which clears `reconnect` and sets `exit` itself. The rest of the connection lifecycle was then brought over from the old pilot clients wholesale, because that shape is the one proven against this server: **`Voice.start()` runs `mumble.run()` on a thread it owns (`_mumble_loop`) rather than calling `mumble.start()`**, and connection state lives in a `_connection_established` `threading.Event` flipped by the `PYMUMBLE_CLBK_CONNECTED` / `PYMUMBLE_CLBK_DISCONNECTED` callbacks (`client/radio.py` calls it the same thing). Two consequences worth knowing. Running the loop yourself means a rejected login arrives as a real `ConnectionRejectedError` out of `run()` — with `start()` it dies inside pymumble's own thread and all you can do is infer "probably the password" from a status code; `_reject_reason` now carries the server's own words into the error message. And **never test liveness with `mumble.is_alive()` here**: `Mumble` subclasses `Thread`, but since we call `run()` rather than `start()` that thread is never started, so `is_alive()` is permanently `False` — wiring it into the `connected` property makes voice look permanently disconnected. `stop()` joins `_mumble_loop` *after* `_release()`, since it is `mumble.stop()` clearing `reconnect` that lets `run()` return at all. Pinned by `VoiceParentThreadTest`, whose fake copies pymumble's `parent_thread = current_thread()` line and whose `run()` blocks until stopped, so it tests real behaviour rather than matching source text; `VoiceChannelTest.test_a_dead_main_loop_is_not_reported_as_connected` covers the other half, where the status code still says `CONNECTED` after the loop is gone.
- **`msfs/gui.spec` must also bundle `SimConnect.dll`.** Same trap as opus, worse symptom. Python-SimConnect loads it with `os.path.splitext(os.path.abspath(__file__))[0] + '.dll'`, so PyInstaller never sees it, and it has to land in `_internal/SimConnect/` because the path is derived from the module's own location. Leave it out and the packaged app still starts, still scans liveries, and simply never reads the simulator — the UI just says *"连不上 MSFS（模拟器是否已启动？）"* forever, sending the user off to check MSFS instead of the installer. `--debug` distinguishes the two: a working DLL logs `Did not find Flight Simulator running`, a missing one logs a DLL load error.
- **Every spec must bundle `opus.dll`.** `opuslib` loads it through `ctypes` at runtime, so PyInstaller's static analysis never sees it and a packaged build dies at startup on any machine that does not already have Opus. All four specs now run the same `find_opus()` (`find_library` → the component's own `opus.dll` → `pyogg`'s copy).
- **pymumble needs the native `opus` library** (`opuslib` loads it through `ctypes` at runtime). Without it every import of `radio.py`/`voice.py` dies with `Could not find Opus library`. `controller/gui.spec` now locates `opus.dll` at build time and bundles it — via `find_library`, then `pyogg`'s copy, then `controller/opus.dll` — so a packaged build works on a machine that has never seen Opus. The other three specs do not do this yet.
- **`favicon.ico` is actually a PNG** with the wrong extension. Qt sniffs the content so the window icon is fine, but PyInstaller 6 refuses it as an exe icon unless `pillow` is installed to convert it.

X-Plane dataref diagnostics — useful when frequency reads come back wrong:

```powershell
cd xplane_client
python xplane.py            # RREF, 0.01 MHz precision
python xplane.py --833      # com1_frequency_hz_833 dataref, 0.001 MHz precision
python xplane.py --probe    # try every candidate dataref and print raw + MHz
python xplane.py --data     # DATA output row 3 (must be enabled in X-Plane settings)
```

Third-party packages used (install with pip — note the PyPI name for pymumble_py3 is **`pymumble`**): `PyQt6` + `PyQt6-Fluent-Widgets` (all four shipped clients), `pymumble`, `pyaudio`, `numpy`, `pynput` (keyboard and mouse PTT), `pygame` (joystick PTT), `keyboard` (only the two legacy pilot clients), `SimConnect` (`msfs/`), `edge-tts` + `requests` + `pyttsx3` + `scipy` (server ATIS), `zeroc-ice` + generated `MumbleServer` slice modules (server login — gitignored, must be generated from the host's `MumbleServer.ice`). `requirements-build.txt` is the authoritative list for the four packaged clients.

## Cross-cutting conventions

**All four shipped clients use qfluentwidgets on PyQt6, and the palette lives in `theme.py`.** `controller/` and `atis/` were already Fluent; `xpc/` and `msfs/` were plain Qt widgets until they were converted. The colours used to sit at the top of each `gui.py`, which meant the same "green" had three different values across the repo (`#28a745` in controller/atis, `#2ecc71` in xpc/msfs) and — more practically — that `settings.py` could not reach them at all, because it cannot import `gui.py` without a cycle. `theme.py` is another byte-identical copy across all four components (like `voice.py` and `ptt.py`); **no `gui.py` may contain a literal `#rrggbb` any more.**

Two things Fluent does *not* cover, both of which look like "the theme only got applied halfway":

- `QMenuBar` / `QStatusBar` / `QDialog` are Qt's own widgets, not qfluentwidgets', so they stay light on a dark window. `theme.window_qss()` and `theme.dialog_qss()` exist for exactly those; a new `QDialog` that does not apply `dialog_qss()` pops up as a white box.
- `QGroupBox` and `QTabWidget` have no Fluent styling either. Use `HeaderCardWidget` instead of the first and `SegmentedWidget` + `QStackedWidget` instead of the second (`xpc/gui.py` is the reference for both).

`qfluentwidgets`'s `ComboBox` has **no `findData()`** — every component carries a three-line `_find_data()` helper, and `addItem()` needs `userData=` as a keyword rather than a positional second argument.

**UI text goes through `i18n.t()`; there are no interface string literals in the source.** All four components now carry an `i18n.py` (a per-component copy, like `applog.py` — the *keys* are shared conventions, the *strings* are not). Rules that the tests enforce, in `test_i18n.py`:

- Every key must have both `zh` and `en`. Half a translation is worse than none: the sentence still renders, just in the other language, and only someone actually running the English UI would notice.
- **Source is scanned for hardcoded Chinese** in anything that reaches a widget (`NoHardcodedUiStringTest`). The scan covers `gui.py`, `settings.py` and the modules whose `_status()`/`_state()` messages land in the UI (`voice.py`, `fsdpilot.py`, `fsdclient.py`, `broadcast.py`, `profile.py`, `radiostack.py`). It skips comments and docstrings — several docstrings describe a `_status(state, message)` signature and would otherwise be reported as untranslated.
- Placeholders must match across languages, and they are **named** (`{who}`), never positional — the two languages order their clauses differently.

Four traps, each of which produced a real bug during the conversion:

- **`def t(key, /, **kwargs)` — the `/` is load-bearing.** Without it, any string containing a `{key}` placeholder blows up with `t() got multiple values for argument 'key'`, an error that says nothing about i18n.
- **A `kind → 文案` dict is evaluated at import time**, so it freezes whatever language was current when the module loaded and never follows a language switch. `broadcast.py`'s `REJECT_REASONS`, `voice.py`'s denial table, and `profile.py`'s `TYPE_LABELS`/`LANGUAGES` were all this shape; they are now tuples of *known keys* plus a `t("prefix." + kind)` lookup at call time.
- **Default arguments have the same problem.** `Voice.stop(message="语音已断开")` freezes the string at import; it is now `message=None` with the default filled in inside the function.
- **Log text stays English.** The dividing line is where the string ends up, not which module it lives in — `voice.py`'s `_skip()` reasons only ever reach the log and are deliberately not translated, while the `_status()` messages three lines away are.

ATIS has a second, unrelated notion of "language": `station.voice_language` picks what gets **broadcast** (英文 / 中文 / 中英双语). That is aimed at the crews listening, not at the operator, and an English-interface operator can perfectly well be running a Chinese ATIS. `chinese.py`, `template.py` and `metar.py` therefore stay out of the string table entirely, and `VoiceLanguageTest` pins that switching the interface language does not touch a station's script language.

**PTT comes from `ptt.py`: keyboard, joystick button, or mouse side button — any one held transmits.** Another byte-identical copy across `controller/`, `xpc/` and `msfs/`. It replaces two older shapes: controller had keyboard only (a `pynput` listener compared against `settings.ptt_key`), while xpc/msfs polled `keyboard.is_pressed()` plus `pygame` in a hand-rolled loop with the button index typed into a bare `QSpinBox`. Settings now store `ptt_bindings`, a list; `ptt.load()` upgrades an old `ptt_key` + `joystick_ptt` pair into it, because silently losing someone's PTT key on upgrade looks exactly like a broken microphone.

- **Mouse: side buttons X1/X2 only.** The listener is global, so binding the left button means transmitting every time the user clicks anything in any window — with the TX light hidden behind whatever they clicked on. `mouse_name()` returns `""` for left/right/middle, and it matches on the button's *name* because X11 calls the same physical keys `button8`/`button9` while Windows and macOS call them `x1`/`x2`.
- **Never `suppress=True`.** Swallowing the event means the PTT key stops working in every other program, and it usually has another job inside the simulator.
- **Keyboard and mouse are event-driven (`pynput`), the joystick is polled (`pygame`, 20 ms).** SDL has no global hook — its event queue has to be pumped from a thread you own — so the two halves cannot be unified.
- **Only bound sources are started.** On macOS, creating a global keyboard listener triggers the accessibility-permission prompt; asking for it when the user only bound a joystick reads as malware.
- **`PttCapture` (the "press the key you want" picker) needs the watcher stopped first.** Two threads pumping SDL's event queue is not thread-safe, and the keypress being recorded would otherwise go out over the air. Every `open_settings()` stops the watcher, and `is_running()` exists so it is only restarted if it had been running.
- **`pygame` is imported lazily**, inside the function that needs it, because it does not build on every Python version and a missing joystick must not take keyboard PTT down with it. `SDL_VIDEODRIVER`/`SDL_AUDIODRIVER` are set to `dummy` right before that import as well as in `gui.py`.
- **Which thread first initialises SDL is a permanent decision, and getting it wrong kills joystick PTT for the rest of the process.** `SDL_Init(SDL_INIT_JOYSTICK)` creates a hidden helper window for DirectInput (`SDL.c` → `SDL_HelperWindowCreate()`) and every `SDL_JoystickOpen` passes it to `IDirectInputDevice8::SetCooperativeLevel`. Win32 destroys a thread's windows when that thread exits, and SDL creates that window exactly once — `SDL_HelperWindow != NULL` returns early, and only `SDL_Quit` clears it. So letting the capture thread or the polling thread be the first to touch pygame works for that one round and then leaves a dangling HWND: every subsequent open fails with `IDirectInputDevice8::SetCooperativeLevel() DirectX error 0x80070006` (E_HANDLE, invalid window handle) until the client is restarted. A real log shows exactly that — one joystick button captured at 01:28:11, every open after 01:28:13 refused, then `could not open joystick 0` every 3 s forever. `prime_joystick()` is therefore called from `PttWatcher._start_joystick()` and `PttCapture.start()`, both of which run on the Qt thread, and `_import_pygame()` logs a warning if it is nonetheless reached from somewhere else first. **This has nothing to do with `SDL_VIDEODRIVER=dummy`** — the helper window belongs to the joystick subsystem, not the video driver. Pinned by `SdlThreadTest`.
- **A device that will not open keeps not opening, so it is reported once.** The retry is every `JOYSTICK_RETRY` (3 s) and never stops; the E_HANDLE log above filled a whole rotation with one repeated line. `_open_stick` warns the first time per device and drops to DEBUG after, clearing on a successful open.
- `ptt.py` produces **no UI text** — `Binding.token()` returns `"V"` / `"X1"` / `"3"` and `i18n.binding_label()` does the wording. A shared file that carried Chinese would put the same strings in three places, two of which would be missed at translation time. `test_i18n.py` walks its AST to enforce this.

**Frequency → channel name.** The one contract every component shares:

```python
freq_value = int(round(float(frequency_mhz) * 1000))     # 125.400 -> 125400
channel_name = f"FREQ_{str(freq_value).zfill(6)}"        # -> "FREQ_125400"
```

Channels are created at the Mumble root as `temporary=True` if missing, then joined — via the non-blocking `CreateChannel` / `MoveCmd` path described above, never `new_channel()` / `move_in()`. Changing this formula breaks pilot/controller/ATIS interop simultaneously.

**Joining a channel is not a fact you can remember.** Every voice client is built with `reconnect=True`, and after pymumble reconnects the server puts the user back in **root** — while `self.frequency`/`self.channel` still hold the pre-drop values. Two rules follow, and `xpc/voice.py` violated both until they were found by actually running `_channel_loop`:

- The convergence loop must compare against the server (`myself["channel_id"]` vs the channel it thinks it is in — `_in_expected_channel()`), not against its own bookkeeping. Comparing `target == self.frequency` alone means it concludes "already there" and never rejoins; the UI stays green and the user sits in root indefinitely.
- The PTT root guard must be **unconditional** (`myself["channel_id"] == ROOT_CHANNEL` → refuse). Qualifying it with `and self.channel is None` looks like a safety valve but is exactly wrong after a reconnect: the stale channel name makes the condition false, so audio is really transmitted into root — inaudible on the intended frequency, audible to everyone whose own switch failed, and the sent-frame counter climbs normally the whole time.

**A channel *id* is not a fact you can remember either, and this one doesn't need a reconnect.** `FREQ_*` channels are `temporary=True`, so the server destroys one the moment its last occupant leaves — which happens every time a controller moves their primary frequency off a channel nobody else is on. `controller/voice.py` was the only component caching frequency→`channel_id`, and it only invalidated that cache on reconnect, so the id outlived the channel and **both** uses of it failed silently: the `MoveCmd` pointed at a channel that no longer existed (the server neither obeys nor complains, so the log just fills with `发出了进入频道 N 的请求，但 5 秒内没有生效`), and the `VoiceTarget` carried the same dead id, so transmitted audio was dropped on the floor. Listeners on the still-live channels kept working, so the ear worked and the mouth didn't. **This is a real bug but it is not the cause of the open "can't enter the frequency channel" report** — `msfs-for-can`'s log shows the same `没有生效` loop against a channel that demonstrably *does* exist server-side with the right id, so the two only look alike. `_resolve_channel` now re-checks the server's channel table on every call — a local dict lookup, no round trip — and `_forget_channel` tears the dead id out of `_channel_to_khz` (stale entries there misroute incoming audio to the wrong frequency) and `_listening`. Pinned by `TemporaryChannelRemovedTest`, whose fake server grows a `strict_moves` flag because the old fake accepted moves into channels it had never heard of and so could not reproduce this at all. `atis/broadcast.py` and both pilot clients re-resolve by name each time and never had the bug.

**Reconnect is bounded: three attempts, then the client goes offline.** Every connection in the four shipped clients follows the same policy — after a session has been **established**, a drop gets at most `RECONNECT_LIMIT = 3` reconnect attempts; if all three fail the link tears itself down and reports `offline`, and the GUI takes the whole client offline with it. A *first* connection that fails is not retried at all (that is a wrong password or a typo'd host, and retrying just prints the same error three times). The states are `connecting → online → reconnecting → offline`, and a GUI must treat `reconnecting` and `offline` as opposites: `reconnecting` means the link is still alive, so **never drop the object reference** (the pilot clients' `tick()` checks `if self.voice` / `if self.fsd`, so nulling it means the client stops following COM1 or stops sending position even after voice comes back); `offline` means it is gone for good.

Unbounded retrying is not a "try harder" setting — `server/login.py` rate-limits auth failures **per CAN ID**, so one zombie reconnect loop locks that account out of voice entirely and fixing the password changes nothing until the app restarts. For ATIS it is worse: every station logs in under the same reserved account, so one station's zombie can lock out the others.

Three implementation facts, all of which the obvious approach gets wrong:

- **Count in `connect()`, not in the `DISCONNECTED` callback.** pymumble's `run()` (`mumble.py:120-143`) fires `DISCONNECTED` once per *lost* connection and then retries silently — the `connect() >= FAILED` branch does `sleep + continue` with no callback at all. Counting callbacks therefore counts drops, not attempts: a server that stays down fires exactly one callback and then retries until the process exits. The `BoundedReconnect` mixin hooks `connect()` and decides **before** dialling, so both failure modes (socket failure and auth rejection) pass through one gate and no fourth attempt happens.
- **`connect()` returning successfully does not mean you are connected.** It returns `AUTHENTICATING`, not `CONNECTED` — it only builds the TLS socket and sends `Authenticate`. A wrong password returns the same value and dies later in `loop()`. Resetting the counter on that return value re-creates the infinite loop, aimed straight at the per-account rate limit. The counter is reset **only** from the `CONNECTED` callback (ServerSync) via `_session_established()`.
- **The mixin is composed at call time (`bounded_mumble()`), not written as `class X(pymumble.Mumble)`.** The tests swap `voice.pymumble.Mumble` for a fake; a module-level subclass welds the real base into the MRO at import and the fake can never get in.

Where each component notices the give-up differs because each owns its loop differently: `xpc`/`msfs` run `mumble.run()` themselves and see it in `_mumble_loop`; `controller` uses `mumble.start()` and catches it in `_connection_monitor` (checked **first**, before the ping-timeout logic, because "dropped" and "never coming back" are different things to the user); `atis` also uses `start()` but a broadcast round can sit in `_wait_for_quiet()` for up to 60 s, so it gets an `on_give_up` callback that also sets `stop_event`. The FSD links (`xpc/msfs/fsdpilot.py`, `atis/fsdclient.py`) had no reconnect at all and now carry the same policy in `_run()`, with `_status()` translating a mid-retry `error` into `reconnecting` at one choke point rather than at each of the five error sites.

**"整个下线" means the whole client, except in ATIS.** A pilot client with a permanently dead voice link should not sit on FSD looking contactable, so either link giving up calls `disconnect_all()`. ATIS runs several stations in one window, each with its own connection, so there give-up stops **that station only** (voice *and* its FSD session) and leaves the others broadcasting. `server/ATIS/mumble.py` is deliberately excluded: it is a server-side daemon, and a bot fleet that permanently gives up after three attempts would silently take network ATIS off the air until someone noticed.

Pinned by `ReconnectLimitTest` in `xpc/test_xpc.py` (whose fake reproduces pymumble's `run()` loop, including the silent-retry branch), `controller/test_voice.py` and `atis/test_atis.py`, plus `FsdReconnectLimitTest` / `AtisFsdReconnectTest` for the FSD half.

**Update checks go through can, never straight to GitHub.** All four shipped clients carry `update.py` (a byte-identical copy, pinned by `msfs/test_msfs.py`'s `SharedCopyTest`) which asks `https://ceruleanavi.net/api/v1/clients/latest?client=…&version=…` at startup and from a menu item. The reason is connectivity, not preference: from the mainland a 60 MB GitHub asset routinely stalls, while ceruleanavi.net is already reachable (the ATIS configuration comes from it), so can-web relays the asset — see its `/api/v1/clients/download/<client>`. Four rules, each of which the obvious implementation gets wrong:

- **A failed check must be silent and harmless.** Every error path returns `None` and logs at INFO. Failing to reach the update server is not worth a dialog, and it must never delay or break startup.
- **Never update by itself.** The client reports "there is a newer version" and offers 下载 / 看更新说明 / 跳过这个版本 / 以后再说. A controller mid-session does not want an application deciding to restart itself.
- **Don't interrupt someone working.** `controller` suppresses the prompt while connected and `atis` while any station is on air — a modal box over the radio stack is worse than a late update, and PTT is a global hotkey. Both leave a line in the status bar instead.
- **Remember a skipped version.** `settings.skipped_version` exists because asking again every launch is exactly as annoying as auto-updating, only more frequent.

Version comparison is numeric on both sides (`update.is_newer`, and `compareVersions` in can-web): as strings `2.0.10` sorts before `2.0.9`, which would either nag forever or never offer the update. The clients report `2.0.1` while the tag is `v2.0.1`, so both sides strip anything that is not a digit before comparing. `smoke_gui.py` in all four stubs `update.check` — the prompt uses `QMessageBox.exec()`, which blocks forever under the offscreen platform, and a startup check would otherwise make the smoke test hit the network.

**Releasing the voice resources on a failed connect is not optional.** `Voice.start()` must run its `_release()` on every failure path. Leaving PyAudio open keeps the microphone, so the *next* attempt fails with 打不开音频设备 and points the user at their sound card; leaving the `reconnect=True` Mumble object alive leaves a zombie retrying forever, and `server/login.py` rate-limits auth failures per CAN ID — one zombie can lock the account out of voice entirely, so fixing the password changes nothing until the app is restarted.

**ATIS username encoding.** ATIS bots log in as `{cid}_atis{freq6}` (e.g. `1005_atis118000`). `server/login.py` matches `ATIS_NAME` (`^(?P<cid>.*)_atis(?P<freq>\d{6})$`), authenticates the `cid` part against `https://ceruleanavi.net/api/v1/public/auth`, and returns the 6-digit frequency as the Murmur user id so multiple ATIS sessions per user don't collide. Frequency `199.998` is deliberately skipped (placeholder for "no frequency").

Three things that derivation gets wrong if you rewrite it by hand:

- **`authenticate` and `nameToId` must give the same answer**, so both go through `user_id_for()`. They used to be written separately, and `nameToId`'s bare `int(name)` threw on every ATIS name and fell through to `-2` while `authenticate` said `118000`. Nothing reports that: `setACL` accepts a name-based entry and the permission lands on a user that does not exist. Pinned by `UserIdAgreementTest`.
- **The regex must be anchored at the end.** The old `^.*_atis\d{6}` matched `1000_atis1180001` too, and the id came from `split("_atis")[1]` — seven digits, a user id belonging to no frequency and no member. Such a name now falls through to the plain-account path, `int()` raises, and the login is refused.
- **`idToName` cannot be made consistent and does not pretend to be.** The cid is discarded when the id is built, so `118000` is both CAN 118000 and any ATIS on 118.000. The same lossiness means two *different* cids broadcasting on one frequency share a Murmur user id. Changing that means changing the id algorithm, which is a network-wide contract.

**The server-side bot fleet has no shortcut, and that is enforced by a test.** `server/ATIS/mumble.py` logs in as `{ATIS_CID}_atis{freq6}` with `ATIS_CID` defaulting to `900`, and `login_ATIS` sends that cid to the same upstream endpoint as everyone else — the reserved-account bypass was deliberately deleted and `test_there_is_no_shortcut_for_any_account` exists to stop it coming back. So the fleet only works if that cid really exists on can-web, with `ATIS_PASSWORD` as its website password and `rating >= 1`. All three failures look identical from here, so `_wait_until_connected()` gates on the connection actually reaching `CONNECTED` (`start()` returning proves nothing — a rejected login dies inside pymumble's own thread) and `_report_login_failure()` names the account and lists all three conditions. Without that gate a refused login surfaced as `频道 FREQ_xxxxxx 不存在`, sending you to the ACL instead of the account.

**Authentication.** There are no local accounts — the Mumble server delegates to the Can web API via the Ice authenticator in `server/login.py`, which also kicks a prior session when the same name reconnects.

**`-2` (fallthrough) is not "let it through, something downstream will check" — it is the whole check, and returning it for an unknown name admits that name.** It means *"this user is not mine, use your own account database"*, and Murmur only refuses an unregistered guest when `serverpassword` is non-empty. This deployment has never set one: the `Dockerfile` rewrites only `ice=` and `logfile=`, and `start.sh` writes only `icesecretwrite`. Meanwhile `fix_acl.py` grants Enter/Speak/Whisper/Listen/MakeTempChannel to the **`all`** group by design — and in Mumble `all` *includes unauthenticated users* (the group that excludes them is `auth`). So `authenticate()` returning `-2` for every name `user_id_for()` could not parse meant **username `guest` plus any password reached every `FREQ_*` channel with full transmit and receive, without `verify()` ever being called.** The reason the fallthrough existed is real — `SuperUser` must be able to log in, or the server's own admin entry is locked out by this authenticator — but that needs one name, not the entire complement. `MURMUR_LOCAL_ACCOUNTS` is that list, matched **exactly**: case variants, padding and affixes are not `SuperUser`, and Mumble's `SuperUser` cannot be renamed, so there is no reason to loosen it. Everything else is `AUTH_FAILED`, and is refused without spending an upstream request. Pinned by `test_a_name_this_authenticator_does_not_own_is_refused` and `test_only_the_exact_superuser_name_falls_through`.

`nameToId` still answers `-2` for a name it does not recognise, and that is correct — it is a lookup, not an admission decision, and `-2` there only means "I don't know this name".

That kick needs **two** Ice objects registered, which is easy to get wrong: `userConnected`/`userDisconnected` belong to `ServerCallback`, not `ServerAuthenticator`, so `setAuthenticator()` alone leaves `online_users` permanently empty and nothing is ever kicked. `main()` registers a `ServerCallbackI` via `addCallback()` as well, and seeds `online_users` from `getUsers()` for anyone already connected. All Ice calls carry the `{"secret": …}` context — `kickUser` without it raises `InvalidSecretException`.

**Mumble 1.5 renamed the Ice slice module** from `Murmur` to `MumbleServer`; the interfaces themselves are unchanged (signatures checked against `v1.5.735/src/murmur/MumbleServer.ice`). `login.py` imports `MumbleServer` and falls back to `Murmur`, so it runs on either. Regenerate the bindings on the host with `slice2py /usr/share/mumble-server/MumbleServer.ice` — the generated package is gitignored. On Debian 13 the config lives at `/etc/mumble/mumble-server.ini`. The Mumble host is `audio.ceruleanavi.net`, hardcoded in `client/radio.py`, `xplane_client/radio.py`, `controller/gui.py`, `atis/gui.py` and `server/ATIS/mumble.py`, and a *settable default* in `xpc/settings.py` and `msfs/settings.py`.

**Renaming that host is not a one-line change, because two clients persist it.** `xpc`/`msfs` write `mumble_host` into their settings JSON, so changing `MUMBLE_HOST` alone only reaches fresh installs — everyone who has ever launched the app keeps the old name, and the day it stops resolving they get 连不上语音服务器 with a settings dialog that looks entirely correct. Both `settings.py`s carry `OLD_MUMBLE_HOSTS` and rewrite that exact value on load (a deliberate override to anything else is left alone), pinned by `VoiceHostTest` in each suite. The previous host was `hjdczy.top`; it stays in `OLD_MUMBLE_HOSTS`, and also in `atis/settings.py`'s `WRONG_FSD_HOSTS` alongside the new name — that set catches the FSD host being filled in with the *voice* address, which is a mistake the new name invites just as much as the old one did.

**Audio path.** Mono `paInt16`, 20 ms frames (`CHUNK = int(RATE * 0.02)`). Each client runs `_find_best_sample_rate()`, probing `[48000, 44100, 32000, 24000, 16000]` against the selected devices at startup and again on every device change. Note that pymumble's `sound_output.add_sound()` expects 48 kHz PCM and no resampling is done — a fallback rate produces pitch-shifted audio, so 48 kHz is the intended path and lower rates are a last resort.

**PTT and indicators.** The three shipped clients that transmit share `ptt.py` (see above); the two legacy pilot clients still poll `keyboard.is_pressed()` plus a pygame joystick button in a background thread. RX indicators are lit from the `PYMUMBLE_CLBK_SOUNDRECEIVED` callback and cleared by a 0.5 s timeout loop.

**pygame on Windows.** `os.environ['SDL_VIDEODRIVER'] = 'dummy'` and `SDL_AUDIODRIVER = 'dummy'` must be set *before* `import pygame` — that is why these lines sit above the imports in `gui.py`, `radio.py`, and `client/settings.py`. pygame is only used for joystick input; SDL video/audio must stay disabled so it does not fight PyQt6 and PyAudio.

**Diagnosing a refused connection.** pymumble raises `ConnectionRejectedError(mess.reason)` on a server `Reject` — dropping the `type` field, which is the one that actually says *why*, and which Murmur often fills in while leaving `reason` empty. Both clients therefore connect through a `RejectAwareMumble` subclass that parses the `Reject` message in `dispatch_control_message` before handing it on, keeping `reject_type` for the UI. The types are used directly as i18n keys (`reject.WrongUserPW` and friends, with a `REJECT_TYPES` tuple listing the ones that have wording); the distinction that matters most is `WrongUserPW` ("密码错误") versus `AuthenticatorFail` ("服务端认证器故障"), because the second one means `server/login.py` is down and no amount of password fiddling will help.

**Qt threading.** pymumble callbacks fire on the library thread, so every GUI marshals them into the Qt thread through `pyqtSignal` wrappers: `ErrorSignal`/`ConnectSignal` in `client/gui.py` and `xplane_client/gui.py`, `VoiceSignals` in `controller/gui.py`. Follow that pattern for anything that touches widgets from a Mumble or audio callback.

**Settings files.** Written to the CWD as JSON: `radio_settings.json` for `client/` and `controller/` (gitignored), `xplane_radio_settings.json` for `xplane_client/`. The pilot clients persist the Mumble username and password in plaintext; the controller persists `last_username` plus the whole radio stack under `radios`, so a session comes back with the same frequencies and their RX/TX/XC state.

**ATIS text processing.** `server/ATIS/process.py` expands digits into radio readback (Chinese `幺两拐洞`, English `niner`/`zero`) and turns a lone uppercase letter into its NATO word. Server ATIS splits the upstream `text_atis` on `|` into English|Chinese. Broadcasters check the channel for other speakers and pause rather than talk over traffic. ATIS lives **only** on the server side now — the controller client's ATIS was removed.

## The controller's radio stack

`controller/` is modelled on TrackAudio (`C:\Docs\Dev\TrackAudio`): a controller adds several frequencies and each is a "radio" with three switches. The **coupling rules are copied from TrackAudio's `radio.tsx`** and are pinned by `test_radiostack.py`:

- turning **RX off** also clears TX and XC — not receiving makes transmitting and coupling meaningless
- turning **TX on** forces RX on — there is no transmit-only radio
- turning **XC on** forces both RX and TX on

`radiostack.py` holds that model and nothing else — no I/O, no Qt, no Mumble — which is why it is the one part with real unit tests. `voice.py` owns the Mumble side, `gui.py` draws one `RadioRow` per radio.

**How one Mumble connection covers several frequencies.** A Mumble user can only *be* in one channel, so `voice.py` uses two mechanisms and both need raw protobuf because pymumble does not wrap them:

- **RX** → `UserState.listening_channel_add/remove` (Mumble 1.4 channel listeners), sent via `send_message(PYMUMBLE_MSG_TYPES_USERSTATE, …)`. pymumble's `ModUserState` only forwards a fixed set of fields and drops these.
- **TX** → a `VoiceTarget` carrying **all** TX channels at once. `sound_output.set_whisper()` cannot do this: for `id == 1` pymumble copies only `targets[0]`, so multi-channel transmit has to build the message directly.

**Two client-visible errors are really the same server ACL gap.** Mumble's default root ACL does not necessarily carry everything this system needs, and in both cases the client's symptom points somewhere unhelpful:

| Symptom | Missing permission | What the user sees |
|---|---|---|
| 管制端 "没有权限（频道监听需要 Listen 权限）" | `Listen` `0x800` | Silence on every frequency except the joined one |
| ATIS "Channel FREQ_127800 does not exists" | `MakeTempChannel` `0x400` | Looks like a missing channel; the server actually refused to create it |

`Listen` is the one that bites hardest: **Mumble 1.4 added it, so a server upgraded from 1.2/1.3 keeps an old root ACL without it**. Frequency channels are temporary children of root and inherit its ACL, so granting once on root covers all of them:

```powershell
python server\fix_acl.py                  # 只看，不改
python server\fix_acl.py --apply          # 授给 all（历史行为，默认）
python server\fix_acl.py --group auth     # 预览迁移：授给 auth 并从 all 收回
```

Run it on the Mumble host (Ice only listens on `127.0.0.1`). It prints the root ACL with every permission bit spelled out, reports which of `REQUIRED` are missing and what each one breaks, ORs them into the `all` group entry that applies to subchannels, and reads back to confirm rather than trusting that `setACL` didn't throw. Bit values come from mumble's `src/ACL.h`.

If the permissions are all present and radios are still silent, the cause is the `listenersperuser` / `listenersperchannel` caps in `mumble-server.ini` instead — the `PERMISSIONDENIED` callbacks in `controller/voice.py` and `atis/broadcast.py` distinguish those cases by denial type.

`getACL` returns inherited ACLs too and those are read-only; the script filters them before `setACL`. Root has none, but don't rely on that if you adapt it for another channel.

**Which group holds those bits is a security decision, and the default is the historical one.** Mumble's `all` group **includes unauthenticated users** — the group that excludes them is `auth`. Granting the voice permissions to `all` is only safe because `login.py` refuses every name it does not own (see `MURMUR_LOCAL_ACCOUNTS` above); it is one gate, not two. `--group auth` performs the migration, and it is genuinely two steps — grant to `auth` **and** strip the same bits from `all`, since leaving `all`'s copy in place achieves nothing. The script creates the `auth` entry if none exists and inserts it ahead of `all`, because Mumble evaluates ACLs in order and a later entry overrides an earlier one. Run the dry run on the host and read it before applying: the migration is correct only if every legitimate connection is authenticated, which for this repo means members (numeric CAN ID), ATIS bots (`{cid}_atis{freq6}`) and SuperUser — all of them are. Anything connecting as a guest would silently lose the ability to hear or speak. `test_fix_acl.py` covers the selection and construction logic; the Ice round trip is not testable off-host.

**`sync()` is expensive, so a burst of stack changes must collapse — not queue.** This is the shape of a real 13-hour log (`can-controller.log`): 202 channel creations, 180 "the channel is gone", 40 move requests that never took effect, **93 of the creations in the same second as the previous one**, up to 34 in a single minute, and the server eventually answering `ChannelName` (duplicate) — while the controller sat in root all evening hearing nothing, with a green UI. Four things made that storm, all fixed and pinned by `SyncStormTest`:

- **`RadioStack(on_change=…)` fires on everything** — add/remove, RX/TX/XC, volume, mute, selecting the primary — plus the datafeed timer's `set_transmit_allowed` / `set_locked` / auto-add every 60 s. Each fire spawns a thread into `sync()`, and each `sync()` can burn two `CHANNEL_TIMEOUT`s. Queueing them all (the old comment argued "each round re-reads the stack, so the later one wins") is exactly wrong: *because* each round re-reads the stack, the rounds in the middle do identical work. At most one round now waits at the door and it reads the newest stack; the rest return immediately. The one risk this adds — a reconnect's re-push being swallowed — is safe because `_resync_after_reconnect` invalidates the caches *before* calling `sync`, so whichever round runs next is a full re-push; `test_a_reconnect_resync_is_not_swallowed` pins it.
- **One frequency, one resolve per round.** TX ⊆ RX and XC ⊆ TX, so resolving inside each of the three loops meant the same frequency was resolved two or three times; when the first `CreateChannel` had not echoed back within `CHANNEL_TIMEOUT`, the later passes each sent *another* one — hence the duplicate-name denials.
- **A refused create must not burn the timeout.** `_wait_for_channel` now returns as soon as `_denial` is set (`atis/broadcast.py` always did this). That wait happens inside `_sync_lock`, so every wasted 5 s held up every queued round behind it.
- **A move into a channel that died gets one re-resolve.** `FREQ_*` channels are temporary, so between resolving an id and the server processing the `MoveCmd` the channel can be gone — and Murmur **neither obeys nor complains** about a move into a dead id. `_join_frequency` re-resolves by name and tries once more instead of losing the whole round.

The client still **joins** the selected radio's channel (the one marked `▸`) rather than sitting in root. That is deliberate: if the server is Mumble 1.3, the listener message is silently ignored and the controller would otherwise hear nothing at all — this way the primary frequency always works and `listeners_working` drives a warning in the status bar. Incoming audio is routed to a radio by `user["channel_id"]`, which is what makes per-frequency RX indication and per-frequency volume possible.

## The ATIS client

`atis/` is modelled on vATIS, so the vocabulary is theirs: a **profile** holds **stations**, a station holds **presets**, and a preset holds a **template**. The template is free text with `[WIND]`-style variables; the variable names and the `:VOX` suffix are copied from vATIS's documented set (`template.py: ALIASES`).

The idea worth preserving is that **every weather element has two forms**: `text` reproduces the METAR group (`09004MPS`) for the written ATIS, `voice` is what gets spoken (`wind zero niner zero at four meters per second`). `metar.py` produces both, and `template.render()` returns both — the same template rendered twice. In the voice pass every variable takes its spoken form automatically; `:VOX` only exists to force the spoken form into the *written* ATIS. Altitudes are spoken as `three thousand`, not digit-by-digit, which is `spell_altitude`'s whole reason to exist.

`vatis_import.py` reads real vATIS profile JSON. Field names are camelCase and enums are strings because vATIS sets `PropertyNamingPolicy = CamelCase` and `UseStringEnumConverter` in its `SourceGenerationContext`; older profiles call the station list `composites` instead of `stations`, and older builds may write `atisType` as a number, so all of those are accepted. **`frequency` is a uint in hertz** (`133800000` = 133.800) — the single most damaging field to misread, so `parse_frequency` also tolerates kHz/MHz and rejects anything outside the VHF band. Anything vATIS supports that this client does not (`atisFormat` fine-grained readback options, IDS endpoint, static definition libraries, recorded voice) is skipped and *reported in the import dialog* rather than silently dropped.

Contractions come across too: vATIS references them as `@NAME` in a template, and each has a text and a voice form, which maps exactly onto the two-pass rendering — `render()` takes the station's contraction table and expands `@NAME` differently in each pass.

Station callsigns follow vATIS: `ZSPD_ATIS`, `ZSPD_D_ATIS`, `ZSPD_A_ATIS` for combined/departure/arrival. Each station has a **code range** limiting which information letters it uses, so a departure and an arrival ATIS at one field can't be confused; the letter advances by one whenever the raw METAR text changes, and wraps within the range.

**The Chinese script is rendered separately, not translated.** `chinese.py` re-renders straight from the parsed METAR because a Chinese ATIS is not a word-for-word translation of the English one — different order, and digits use the radio readback (`幺两拐洞`) that `server/ATIS/process.py` already establishes network-wide. Two conventions that make it sound right: cloud bases are spoken in **metres at 100 ft = 30 m** (the exact 30.48 gives "九百一十米" where real broadcasts say "九百米"), and temperatures/heights are counted (`二十五`) while headings and QNH are spelled digit by digit (`洞九洞`). A station picks 英文 / 中文 / 中英双语 via `voice_language`, with `chinese_name` and `chinese_runway` for the words that can't be derived from the ICAO code. Old profiles without those keys default to English so an upgrade doesn't change what an existing station broadcasts. `broadcast.py` already switches the SAPI voice per text via `_pick_voice`, so nothing else was needed for the audio side.

**The wording follows a real ZBAA broadcast, element by element, and that is deliberate — do not "simplify" it back.** Each of these was wrong before and sounds wrong to a Chinese crew: the information letter is spoken as the Chinese phonetic word (`spell_letter`: `J` → 朱丽叶) because the raw Latin letter made the TTS drop an English character into the middle of a Chinese sentence; wind names both halves (`风向 三洞洞 度 风速 拐 米每秒`) because `风 三洞洞 度 拐 米每秒` gives no clue which number is which; temperature and dew point carry their unit and put the sign after the label (`气温 二十四 摄氏度`, `露点负 八 摄氏度`, not `露点 零下 八`); the observation time says `世界协调时` because a bare `时` reads as local; and the script closes by asking the crew to report the letter (`首次与管制员联络时报告你已收到通播 朱丽叶`) rather than saying `完毕`. The runway slot takes **either** a bare designator (`三六左`, which gets the `使用跑道` prefix) **or** a whole configuration paragraph already beginning with `跑道` (which does not) — real broadcasts put the configuration before the weather, which is exactly where that slot renders, while `chinese_extra` renders after it. Pinned by `ChineseVoiceTest`.

**Weather auto-refresh** was already there — a `QTimer` calls `refresh_all_metars`, and `on_metar` advances the information letter and pushes a new script to voice and FSD whenever the raw METAR text changes. The interval is now `settings.metar_refresh` rather than a constant, clamped to 60–3600 s by `clamp_refresh`: a 0 spins the timer and gets you rate-limited by the weather source, and a whole day is not auto-refresh. `apply_refresh_interval()` is called after the settings dialog closes so a change takes effect without a restart.

**Three different things are called "取网络配置", and only one of them is the configuration.** This bit people (and one earlier pass of this file) repeatedly, so it is worth naming all three:

| Source | What it actually gives you | Who reads it |
|---|---|---|
| can-web `GET /api/v1/atis/config` | **The configuration** — stations, frequencies, runway-configuration presets, templates, Chinese wording | `atis/netconfig.py`, button 从网络更新配置 |
| can-fsd datafeed `atis[]` | Who is broadcasting **right now**: ICAO and frequency only | `atis/datafeed.py:atis_stations`, button 取在线席位 |
| can-web `GET /api/v1/atis` | A plain-text ATIS generated from a METAR, for EuroScope | nothing in this repo |

The first one did not exist until it was built (`can-web/src/data/atis/config.json` + `src/server/atisConfig.ts`), which is why every operator used to re-type the same templates and Chinese runway wording by hand and a wording fix reached nobody. The document is **this client's own JSON shape** (`Station.to_dict`, snake_case) so each entry goes straight to `Station.from_dict`; an unknown key is ignored, so the server may add fields, but a *renamed* one is silent data loss on every client. `letter` is deliberately absent — the information letter is session state, not configuration. The version is a **content hash computed server-side**, so "已经是最新" cannot go stale through a forgotten manual bump.

Merging it into a local profile is where the care goes, and all three rules are pinned by `NetworkConfigTest` plus the offscreen checks in `smoke_gui.py`:

- **Only missing stations are added by default.** A local station may have been edited on duty — a temporary runway configuration, a NOTAM — and those live inside the presets, so overwriting wholesale deletes someone's work. Overwriting is a separate question with its own dialog.
- **Overwriting keeps the local information letter.** Resetting it to `A` mid-broadcast means the letter the crew reports is not the one they heard.
- **A station that is currently broadcasting is never touched**, and the result dialog says so. Its `Station` object is held by a `Broadcaster` and an `FSDClient`; swapping it makes the audio on air disagree with the script on screen while everything still looks fine.

**Creating the frequency channel is a round trip, not a local operation.** `new_channel()` only sends a message; the channel appears once the server echoes a `ChannelState`. `_join_channel` used to `sleep(0.2)` and then look — long enough on localhost, not on a remote server, and the failure surfaced as `Channel FREQ_127800 does not exists`, which reads like the channel *can't* be created. It now polls until `CHANNEL_TIMEOUT` and bails early if a `PermissionDenied` arrives, so a refused creation is reported as a permission problem and a slow server just waits. Pinned by `JoinChannelTest`.

Audio goes out over Mumble, not AFV: `broadcast.py` opens its own connection per station as `{cid}_atis{freq6}` (the shape `server/login.py` authenticates) and opens **no** local audio device. Transmission is paced against `sound_output.get_buffer_size()` and yields the frequency if anyone else keys up. `update_text()` swaps the script for the *next* cycle rather than cutting off the current one.

**One connection, one output queue — the rule that keeps audio on the right frequency.** `sound_output.target` is a single mutable field shared by PTT and cross-couple, and a `VoiceTarget` id is a server-side registration that gets *overwritten* when reprogrammed. Both of those bit us: reusing one id meant a cross-couple forward silently retargeted the controller's next PTT. The invariants now, pinned by `test_voice.py`:

- PTT owns id `1`; each cross-coupled frequency gets its own id from `XC_TARGET_BASE`, programmed once in `sync()`. Forwarding switches the id, it never reprograms one.
- `_audio_lock` guards every `target =` / `add_sound()` pair.
- Cross-couple **does not forward while transmitting** — two streams into one queue interleave into garbage, and the controller's own voice is what matters.
- `start_transmit()` joins the previous transmit thread first; otherwise the old thread's exit resets `target` to 0 *after* the new one started talking.

**XC (cross-couple)** is implemented client-side in `_forward_cross_couple`: audio received on one XC frequency is re-sent to the other XC frequencies by temporarily swapping the voice target. It restores the normal TX target afterwards — if you add an early return in there, make sure the target still gets restored or the controller's next PTT goes to the wrong frequencies.

## XPC for CAN (`xpc/`)

The X-Plane pilot client, laid out like xPilot: three independent links, none of which can take the others down.

| Module | xPilot equivalent | Role |
|---|---|---|
| `xplane.py` | `src/simulator/` | UDP link to X-Plane — position, attitude, transponder, COM1/2 |
| `fsdpilot.py` | `src/fsd/` | Pilot-side FSD connection: login, position reports, text, flight plans |
| `voice.py` | `src/audio/` + `afv-native/` | Mumble voice; the channel follows COM1 |
| `traffic.py` | `src/aircrafts/` | Other aircraft: sample history, interpolation, model-match state |
| `cslmatch.py` | `src/aircrafts/` (model matching) | CSL package parsing and the type→model fallback chain |
| `bridge.py` | — | UDP transport to the X-Plane plugin |
| `chime.py` | `src/audio/AFV` notification | The message chime: synthesised tones, played on the client's own output device |
| `observer.py` | — | Observer mode: whether a frequency is typed or follows COM1, and the parsing behind it |
| `plugin/PI_XpcTraffic.py` | `xpilot` XPL plugin | Runs *inside* X-Plane (XPPython3): draws traffic, feeds TCAS |
| `gui.py` | `Resources/Views/` | Connect bar, messages, nearby-ATC list, radio bar |

**The X-Plane link subscribes, it does not poll.** `xplane_client/radio.py` sends one RREF and waits for the reply every time it wants COM1. That round-trip is fine for a channel switch but not for position reports 5× a second, so `XPlaneLink` sends one RREF per dataref *once* with a rate, then just keeps reading whatever X-Plane pushes and holds the latest value. `snapshot()` returns the whole set already converted to FSD's units (feet, knots, MHz) — X-Plane reports metres and m/s.

**`ConnectionResetError` is normal here, not an error.** On Windows, sending UDP to a port nobody is listening on comes back as an ICMP port-unreachable, which surfaces as `ConnectionResetError` on the *next* `recvfrom`. Before X-Plane is running that is every single read. Treating it as a fatal socket error re-subscribes once a second forever (visible as a wall of "已订阅 14 个 dataref" in the log); it has to be treated like a timeout. `_still_waiting()` owns that decision and is pinned by `WaitingTest`: warn at `STALE_AFTER` (3 s), rediscover only at `REDISCOVER_AFTER` (15 s).

**PBH packing must be the exact inverse of can-fsd's decoder.** Pitch, bank and heading ride in one 32-bit integer at 10 bits each (`360/1024` ≈ 0.35° per step), with bit 1 as the on-ground flag. `pack_pbh()` normalises to 0–360 *before* quantising — a raw negative angle overflows its 10-bit field and puts the aircraft at a nonsense attitude on everyone else's screen. `test_xpc.py` carries a copy of can-fsd's `PitchBankHeading` decode (`internal/fsd/packet.go`) and round-trips against it; if that Go function ever changes, that test is what catches it.

Other things worth knowing:

- **Callsigns are pre-checked client-side** against can-fsd's `IsValidCallsign` (2–10 characters, `A-Z0-9_-`) so the user gets an explanation instead of a login rejection.
- **`$ID`'s ninth field (challenge) is deliberately omitted** so the server never starts a VATSIM `$ZC` challenge that only official clients hold keys for. Same reasoning as the removed `controller/fsdclient.py`.
- **Packets are colon-delimited**, so every free-text field (real name, remarks, route, chat) goes through `sanitize()`. A colon in a remarks field otherwise shifts every following field by one.
- **`#AP` carries the password**, so `_redact()` masks it before anything reaches the log — users paste logs into chat.
- Position reports drop to one every 5 s when parked on the ground, and the transponder mode character (`S`/`N`/`Y`) is driven by X-Plane's `transponder_mode` dataref, with `Y` held for 8 s after IDENT.
- FSD and voice failures are isolated in `gui.py`: `on_fsd_status('error')` clears only `self.fsd`, so losing the network connection does not drop the frequency you are listening to.

**An incoming ATC message rings, and the chime synthesises itself.** `chime.py` (a byte-identical copy in `msfs/`, pinned by `SharedCopyTest`) plays two short tones from `on_text_message`, because a pilot is looking out of the window and one more line in the message pane is invisible. Four decisions, each of which the obvious version gets wrong:

- **It plays on the output device the client is configured with** (`settings.output_device_index`), not the system default. A pilot's headset and their system default output are routinely different devices, and a chime that comes out of the desk speakers is a chime nobody hears. That is also why it is not `QSoundEffect` — the mechanism `can-atc/src/can_atc/audio.py` uses — which follows the system default and would pull Qt's multimedia plugins into both specs.
- **The waveform is generated at runtime, so there is no wav asset to bundle and no spec to change.** `opus.dll` and `SimConnect.dll` are both "the file did not travel with the build, the app still starts, the feature is silently dead"; a notification sound is not worth a third one. `pyaudio` is imported lazily (`_pyaudio()`), so the module imports on a machine with no PortAudio and the tests hand it a fake.
- **Not every message rings, and the rule is a pure function.** `wants_alert()` holds all of it so it can be tested without Qt: a private message (the recipient is your own callsign) always rings; a frequency message (`@xxxxx`) only when the body names your callsign — with a boundary check, so `CCA150` is *not* rung by an instruction addressed to `CCA1501`; a broadcast rings; your own message never does. `message_sound_all` turns the frequency half into every message for people who want that. A busy frequency that rings on every line is a feature people switch off on the first day.
- **Failure is one INFO line and nothing else.** The FSD receive thread calls `play()` directly, so an exception escaping it would be a disconnect. A burst of messages collapses to one chime (nothing overlaps, and `MIN_INTERVAL` is 1 s), a device that will not open at the chosen rate falls back through 44.1/22.05 kHz and then to the default device, and volume 0 opens nothing at all.

Settings are `message_sound` / `message_sound_all` / `message_sound_volume`, on the audio tab next to the two existing volume sliders, with a 试听 button that plays through **the device currently selected in the dialog** rather than the saved one — the person clicking it has just changed headsets.

**Observer mode is for the second seat of a two-person crew, and it is defined by what it does *not* connect to.** An aircraft can only have one position on the network, so the right-seat member's client (`observer_mode`, the checkbox in the connect bar) **never opens the FSD connection at all** — voice only, in the same `FREQ_*` channel as the flying pilot. It does not consult `connect_fsd`; that switch governs an ordinary connection. Conversely it connects voice even when `connect_voice` is off, because otherwise pressing 连接 would do nothing while the UI claimed success. The rules live in `observer.py` as pure functions so they can be tested without Qt.

Four consequences, all deliberate:

- **No second aircraft and no duplicate-callsign kick**, which is the whole point. The callsign box is disabled, because a callsign is an FSD concept and this mode has no FSD.
- **ATC text messages do not arrive.** They are addressed to the flying pilot's callsign and can-fsd delivers them to that one connection (`sendDirect` in `internal/fsd/handler.go`). The observer hears and transmits normally; they just cannot read. This is the cost of the design and is written down in `observer.py` so it is not re-investigated as a bug later. 发送消息, IDENT and 飞行计划 are disabled for the same reason — leaving them clickable reads as broken.
- **The frequency can be typed.** The second crew member often runs no simulator at all (they are the radio operator), so there is no COM1 to follow; the radio bar grows a frequency box whose rule is *empty = follow COM1, anything else wins*. One control instead of a manual/auto switch, and no question about which one is in charge. `parse_frequency()` accepts `121.8`, `121.800` and the six-digit `121800` that people copy out of the Mumble channel name, quantises to kHz and only then range-checks 118.000–136.975.
- **That box exists only in observer mode.** A pilot who could point voice at a frequency other than the one in the cockpit would eventually be on a different channel from the one ATC thinks they are on, which is worse than hearing nothing. `frequency_for()` takes `observer` as a parameter and ignores `manual` entirely when it is false; `ObserverFrequencyTest` pins that.

Two more things it gets right because the obvious version does not: the frequency is re-evaluated **every tick regardless of whether the simulator produced a snapshot** (the old `if not snapshot: return` meant an observer with no simulator never had their typed frequency applied), and the checkbox refuses to move while connected — flipping it mid-session would leave the UI describing a connection that is not the one that is open. Both crew members must sign in with **their own accounts**: `server/login.py` kicks the previous session when the same name logs in again, so a shared account just takes turns dropping each other.

### Rendering other aircraft

xPilot draws traffic from a C++ plugin built on XPMP2. This does it in Python instead, split across two processes, because **X-Plane's drawing API is only reachable from a plugin, and the plugin runs inside X-Plane's own Python (XPPython3)** — PyQt6, pymumble and pyaudio can't go there.

    client (xpc/)                          plugin (inside X-Plane)
    FSD → traffic.py → cslmatch.py  ──UDP──→  PI_XpcTraffic.py → draw + TCAS

Everything hard lives client-side where it has unit tests; the plugin is deliberately thin because changing it means restarting the simulator.

**`TrafficTable` is falsy when empty** — it defines `__len__`, so `if not self.traffic` is true for a table with no aircraft in it. `fsdpilot.py` must test `if self.traffic is None`. Getting this wrong silently drops *every* traffic packet, because the table is empty exactly when the first aircraft arrives.

**Aircraft type does not come from the position packet.** `@` carries no model information, so the type is fetched over `#SB`: on first sight the client sends `#SB{me}:{them}:PIR`, and the reply is `PI:GEN:EQUIPMENT=B738:AIRLINE=CCA`. can-fsd relays `#SB` verbatim (`handleSquawkbox`, `internal/fsd/handler.go:515`), so no server change is needed. **We must also answer other clients' `PIR`** or everyone else renders us as a generic model. Until the reply arrives the aircraft is drawn with a fallback model and `model_dirty` tells the renderer to re-match once it does.

**Model matching must always return something.** `ModelSet.match()` degrades type+airline → type → same family → generic-by-category → first model in the package, and returns the reason so a "that aircraft looks wrong" report is diagnosable from the log. An aircraft you cannot see is far more dangerous than one with the wrong livery.

**The plugin installs itself from the settings dialog** (`xpinstall.py`, XPC only — MSFS needs no plugin at all). X-Plane's install directory cannot come from the UDP link: the BECN beacon carries an address and a port, nothing else. So `find_installs()` reads the record X-Plane's own installer writes (`x-plane_install_12.txt`, one directory per line; `%LOCALAPPDATA%` on Windows, `~/Library/Preferences/` on macOS, `~/.x-plane/` on Linux), and the traffic tab always keeps a **choose-the-folder** button as well — those paths are best-effort, and anyone running a portable copy or who moved the install has no such record. A folder counts as X-Plane if it has `Resources/plugins`; deliberately not `PythonPlugins`, which does not exist until XPPython3 has run once, so testing for it would reject exactly the installs that most need the plugin.

Three deliberate limits:

- **XPPython3 is detected, never installed.** It is a compiled binary whose version is tied to the simulator (v4.x for X-Plane 12, v3.1.5 for 11.52), and downloading and unpacking someone else's binary is a different risk category. `has_xppython3()` just checks for `Resources/plugins/XPPython3/` so the dialog can say what is missing.
- **New vs. old is decided by file content, not a version string.** The plugin is a flat source file running inside X-Plane's own Python; it cannot import `version.py`, so a version constant would have to be maintained by hand and would eventually be forgotten. Same bytes → current.
- **`inspect()` reports a protocol mismatch separately from "outdated".** `bridge.py` and the plugin each hold a `PROTOCOL_VERSION`, and when they disagree the plugin **silently drops every frame** (`header.get("v") != PROTOCOL_VERSION` → return, no log line). The symptom is "no traffic at all" with clean logs on both sides, so the UI says so in as many words rather than filing it under a generic version warning. `test_the_bundled_plugin_and_the_bridge_agree` fails if the two constants ever drift.

Installing requires restarting X-Plane — the plugin is loaded at simulator startup — and the dialog says so after a successful copy. `install()` lets `OSError` out so the UI can tell an X-Plane-in-Program-Files permission failure apart from anything else.

**The release zip carries the plugin twice, on purpose.** `_internal/plugin/PI_XpcTraffic.py` comes from the spec's `datas` and is the file the in-app installer actually copies (`bundled_plugin()` resolves it through `sys._MEIPASS`); the release workflow additionally drops a copy at `plugin/PI_XpcTraffic.py`, next to the exe, for anyone the automatic install cannot serve — X-Plane in a location needing administrator rights, a portable copy the detection misses, or a simulator on another machine. The build verifies **both** paths for the same reason `opus.dll` is verified: losing either one still produces a package that starts up fine and simply cannot render traffic.

**Two X-Plane facts that shape the plugin**, both confirmed against `Resources/plugins/DataRefs.txt`:

- **`XPLMInstance`-drawn aircraft do not appear on TCAS.** The panel reads `sim/cockpit2/tcas/targets/*` (64 slots, index 0 is the user), which is a separate system that has to be filled by hand. Those datarefs are "writeable only when `override_TCAS` is set", and `override_TCAS` itself is "only writeable by the plugin that has the AI planes acquired" — hence `xp.acquirePlanes()` first. The arrays carry `flight_id` and `icao_type`, so callsign and type show correctly on the ND. Traffic is capped at 63 and sorted by range client-side, because when there are more aircraft than slots the far ones are the right ones to drop.
- **The animation datarefs must be registered before any CSL model loads.** CSL OBJ8 files reference `libxplanemp/controls/*` by name; X-Plane resolves those while parsing the `.obj`, so registering afterwards gives you a model that renders but never moves. `XPluginStart` registers them, and the values themselves travel through `instanceSetPosition`'s `data` list — whose order must match the `createInstance` dataref list exactly.

**X-Plane 11 and 12 are both supported, by probing rather than version checks.** Two things differ, and neither needs a version branch:

- **8.33 kHz COM frequency.** `sim/cockpit2/radios/actuators/com1_frequency_hz_833` (kHz, so `/1000`) only exists from X-Plane 11.30; the older `sim/cockpit/radios/com1_freq_hz` is in units of 10 kHz (`/100`) and can't represent 132.005. `xplane.py` subscribes to **both** and prefers the precise one — a dataref that doesn't exist is simply never pushed, no error.
- **TCAS override needs X-Plane 11.50.** The plugin calls `findDataRef` on `override_TCAS` and the target arrays; if any is missing it sets `tcas_available = False`, still draws traffic, and says so in the log. It also **skips `acquirePlanes()` entirely in that case** — holding the AI planes without being able to use them would block LiveTraffic and friends for nothing.

**XPPython3 version depends on the simulator**: v4.x is built against SDK 420 and is X-Plane 12 only; X-Plane 11.52 needs v3.1.5. The settings tab says so, because installing the wrong one is a silent no-op.

Also worth knowing: `instanceSetPosition` takes `(x, y, z, pitch, heading, roll)` — heading before roll. Writing the TCAS targets makes X-Plane mirror the nearest 19 aircraft back into the legacy `sim/multiplayer/position/plane#_*` datarefs automatically, so plugins still reading those keep working. `xp.worldToLocal()` does the coordinate conversion, so unlike the legacy 19-slot multiplayer datarefs there is no hand-rolled tangent-plane maths. If LiveTraffic or another XPMP2-based plugin is loaded it will have registered the same `libxplanemp` datarefs and acquired the AI planes; the plugin detects this and logs a warning rather than fighting over them.

The bridge is UDP with one JSON object per datagram, fragmented over `seq`/`part`/`total`. UDP rather than TCP because this is a pure position stream — a dropped frame is replaced 200 ms later, whereas a reliable queue would build up latency. The plugin keeps only the newest complete frame; late frames make aircraft jump backwards. `bridge.Reassembler` and the plugin's copy are separate implementations, and `test_xpc.py` loads the plugin file to check they agree.

## MSFS for CAN (`msfs/`)

The same client as `xpc/`, for Microsoft Flight Simulator. `voice.py`, `traffic.py`, `mumblecompat.py`, `ptt.py`, `theme.py`, `update.py`, `chime.py` and `observer.py` are **byte-identical copies** of the `xpc/` versions (`SharedCopyTest` fails if they drift); `fsdpilot.py`, `applog.py` and `i18n.py` are deliberate forks — different simulator id, different log file name, and the handful of strings that name the simulator — everything that is not the simulator is shared by duplication, matching how the rest of the repo works. Only two modules differ:

| Module | Replaces | Role |
|---|---|---|
| `simlink.py` | `xpc/xplane.py` | SimConnect instead of X-Plane UDP |
| `inject.py` + `aimatch.py` | `xpc/bridge.py` + `plugin/` + `cslmatch.py` | AI aircraft instead of an XPPython3 plugin |

**`snapshot()` must stay field-for-field identical to `xpc/xplane.py`'s**, because `fsdpilot.py` and `voice.py` are shared copies that consume it. `SnapshotTest.test_field_names_match_the_xplane_client` pins that.

**MSFS needs no plugin, and TCAS is free.** SimConnect lets an outside process create AI aircraft (`AICreateNonATCAircraft`), so this is one process rather than XPC's client-plus-plugin split. Because the injected aircraft are real SimObjects, the cockpit's traffic display sees them without the hand-filled TCAS arrays that X-Plane requires.

**The object ID comes back asynchronously and Python-SimConnect loses it.** `AICreateNonATCAircraft` only queues the request; the real object ID arrives in a `SIMCONNECT_RECV_ID_ASSIGNED_OBJECT_ID` message correlated by `dwRequestID`. The wrapper *does* handle that message — but it stores the result in `os.environ["SIMCONNECT_OBJECT_ID"]` with **no request correlation**, so creating twenty aircraft overwrites one global and you cannot tell which ID belongs to which. `inject.py` therefore wraps `my_dispatch_proc`, records `requestID → objectID` in its own table, and hands the message on.

**Wrapping that method is not enough, and getting it wrong is invisible.** Python-SimConnect turns the method into a ctypes trampoline *at construction* (`my_dispatch_proc_rd = dll.DispatchProc(self.my_dispatch_proc)`, `SimConnect.py:140`) and its receive loop calls **the trampoline**, not the attribute (`SimConnect.py:181`). Replacing only `sc.my_dispatch_proc` therefore installs a hook that is never called — while everything upstream looks healthy. The symptom in a real v2.0.3 log was **10 creation requests, 0 object IDs, 0 removals and 0 warnings**: with `record["object_id"]` stuck at `None`, `_sync_one` returns at "still waiting for the objectID" every round, so injected aircraft **freeze at their spawn point**, cannot be removed when the pilot leaves (no object id means no `AIRemoveObject`), and get created a second time when the `#SB` reply refines the model — so traffic silently doubles. `EXCEPTION` messages are lost the same way, so a model the simulator refuses never reaches `bad_titles` and is retried forever. `_install_dispatch` now rebinds `my_dispatch_proc_rd` as well; the loop re-reads it every iteration, so swapping it at runtime takes effect immediately. Pinned by `test_the_dispatch_hook_replaces_the_trampoline_not_just_the_attribute` and `test_an_assigned_object_id_reaches_our_table`, whose fake copies the construction-time trampoline — without that detail the test passes either way.

**Two SimVar traps.** `TRANSPONDER CODE:1` is **BCD** — reading `0x1200` as decimal gives 4608 and the wire squawk is garbage. And `PLANE_PITCH_DEGREES` / `PLANE_BANK_DEGREES` are in **radians despite the name**, with the opposite sign convention to FSD (nose-up is negative in the sim).

**The altitude field is *true* altitude, and the altimeter is not.** `PLANE_ALTITUDE` (and X-Plane's `sim/flightmodel/position/elevation`) is geometric MSL altitude; the cockpit gauge shows *indicated* altitude, baro-corrected by whatever is in the Kollsman window. At cruise on STD those differ by roughly 1000 ft per inch of QNH deviation, plus the ISA temperature error — which is the "座舱高度表 35000，服务器上 34000" report. It is not a units bug and there is nothing to "fix" in the conversion.

The FSD position packet already has the right shape for this: field 7 is true altitude and the **tenth field is the pressure correction**, so a client rendering other aircraft uses the true altitude to place them while a controller adds the correction to get the Mode C. Both `fsdpilot.py`s hardcoded that field to `0`, so the network saw raw true altitude. `pressure_delta()` in `msfs/simlink.py` and `xpc/xplane.py` now computes it as `indicated + (29.92 - baro_setting) * 1000 - true`; the temperature error needs no separate term because indicated altitude already carries it and true altitude does not. A reading outside `BARO_RANGE` (25–32 inHg) returns `0` rather than a correction — an unread SimVar is `0`, and `(29.92-0)*1000` would move the aircraft thirty thousand feet on someone's radar.

**Do not switch to `PRESSURE_ALTITUDE` to shortcut this.** Python-SimConnect's `RequestList.py` requests it in **`b'Meters'`** — alone among the altitudes, all of which are `b'Feet'` — so using it like its neighbours is off by 3.28×. Deriving from `INDICATED_ALTITUDE` + `KOHLSMAN_SETTING_HG` (both `Feet`/`inHg`) avoids that entirely.

**Sending the correction is only half of it: can-fsd currently ignores the field.** `internal/fsd/handler.go` stores field 6 verbatim as `client.altitude`, which is what `internal/api/datafeed.go` publishes and what can-web's radar draws, and it serves standard weather (`buildTemperaturePacket` hardcodes 2992) so it has no QNH of its own. Until it adds field 9, the displayed altitude stays true altitude. Note the raw packet is relayed to other pilots verbatim (`broadcastRanged`), so that change affects only the radar/datafeed and not traffic rendering — which is exactly the split the protocol intends.

**Model matching reads the sim's installed aircraft, not CSL packages.** `aimatch.py` walks each package tree for `aircraft.cfg`, taking `icao_type_designator` from `[GENERAL]` and one `title` + `icao_airline` per `[FLTSIM.n]` section. The title string is what `AICreateNonATCAircraft` takes, so it must be reproduced exactly. The fallback chain is the same as `cslmatch.py`'s and the two `FAMILIES` tables are meant to stay in sync.

Three things only a real install revealed — the synthetic `aircraft.cfg` tests all passed while a live scan found 10 liveries instead of 375:

- **The package directory must come from `UserCfg.opt`.** `InstalledPackagesPath` can point anywhere; on the dev machine it is `D:\MSFS2022` (257 aircraft) while the guessed `%APPDATA%\Microsoft Flight Simulator\Packages` holds none. Guessing paths silently misses the whole install.
- **`attachments/` is not aircraft.** Fenix-style addons put dozens of component configs under it, each with a `[GENERAL]` section and a `title` but no type designator. Treated as aircraft they pollute the table and one of them ends up standing in for everyone.
- **`icao_type_designator` is not clean.** Values like `"A359 ULR"` and `"A-319 CFM SL"` occur; `_clean_icao` takes the first token and requires 2–4 alphanumerics, because a bogus code in the index means real aircraft of that type never match. The ultimate fallback also prefers a model that *has* a type code.

**How good the match actually is depends on what the user has installed**, and the honest answer measured against a stock-ish install (375 liveries, 38 types) with 24 typical Chinese-network flights was: 11 got the right type, 11 got a sensible stand-in, 2 got something unrelated, and **0 got the right airline livery**. Two facts behind that:

- **Livery matching needs livery packs.** Only 40 of 375 liveries carried an `icao_airline` at all, and those were developer house liveries (`AIB`, `FENIX`, `IBE`). Default MSFS aircraft ship without airline codes, so `EQUIPMENT+AIRLINE` almost never hits until the user installs real liveries. This is a user-side fact, not a bug — it just means the realistic ceiling is "right aircraft type, wrong paint".
- **`CATEGORIES` exists because family fallback isn't enough.** With no 777 installed, a `B77W` used to fall through every tier and land on the arbitrary first model — an A319 standing in for a 777. The tier between "same family" and "first model" substitutes within 宽体/窄体/支线/通航, which turned that A319 into a 787 and cut the unrelated-aircraft cases from 8/24 to 2/24. `cslmatch.py` carries the same table and tier; the two are meant to stay in sync.

  **The category tier must sit *before* the generic-by-prefix tier, and for a long time it did not.** `GENERIC_BY_PREFIX` guesses from a two-character prefix, and `A3` / `B7` span both narrow and wide bodies — `B77W` guesses `B738`, `A359` guesses `A320`. With the generic tier first, installing either of the two most common models (a 737-800 or an A320) meant **every widebody degraded to a narrowbody and the category tier never ran at all**: a 777 rendered as a 737 on everyone else's screen, which is precisely what the category tier exists to prevent. Both `cslmatch.py` and `aimatch.py` had the tiers in the wrong order. The existing tests missed it because they installed only an `A319` and a `B78X` — with no `B738` present the generic guess found nothing, so the category tier ran anyway and the assertion passed. `test_category_beats_the_generic_guess` (in both suites) installs a `B738` specifically to close that hole. Generic-by-prefix is now the *last* resort before the fallback, for type codes that are not in `CATEGORIES` at all.

## Related repositories

This repo is the **voice layer** of a three-part network. The other two live alongside it and own the contracts this one consumes:

| Path | Repo | Role |
|---|---|---|
| `C:\Docs\Dev\can-fsd` | can-fsd | Go FSD daemon — pilot/controller connections, flight plans, and the live datafeed. Docs: `Readme.md` |
| `C:\Docs\Dev\can-web` | can-web | Astro + Vue website — accounts, roster, radar, docs. Owns the MySQL schema (Prisma). Docs: `CLAUDE.md` |

Three integration points, all hardcoded here:

**Authentication → can-web.** `server/login.py` POSTs `{cid, password}` to `https://ceruleanavi.net/api/v1/public/auth`, implemented at `can-web/src/pages/api/v1/public/auth.ts`. That route compares against the cleartext `user.password` column (the FSD network password, which equals the member's website password) and additionally **rejects `user.rating < 1`** — an unrated member cannot use voice even with correct credentials. Failures are rate-limited per CAN ID, so a client stuck in a reconnect loop with a bad password will lock that account out of voice for the window. Because the Murmur user id is `int(name)`, the Mumble username must be the numeric CAN ID. The reserved ATIS account (cid `900` by default) bypasses the API entirely and is local to this repo; its password is **not** in the source — see below.

**No secret belongs in the tree.** `server/serverconf.py` resolves the Murmur Ice secret and the reserved ATIS password in order: environment variable → `server/server_secrets.json` (gitignored, `server_secrets.example.json` shows the shape) → for the Ice secret only, `icesecretwrite` straight out of `/etc/mumble/mumble-server.ini`, which is where it already lives, so a normal host needs no configuration at all. There is deliberately **no fallback default**: a working default is never changed and travels with every clone. Missing Ice secret → `login.py`/`fix_acl.py` exit non-zero with a message naming all three locations; missing ATIS password → `login.py` still serves normal users and only the reserved-account shortcut is disabled (it never compares against an empty password), while `server/ATIS/mumble.py` refuses to start. `test_serverconf.py` walks the repo and fails if either secret reappears in any `.py`.

**ATIS configuration → can-web.** `atis/netconfig.py` GETs `https://ceruleanavi.net/api/v1/atis/config`, implemented at `can-web/src/pages/api/v1/atis/config.ts` over the document in `can-web/src/data/atis/config.json`. Unauthenticated by necessity — the desktop client holds FSD/Mumble credentials, not a website session — and rate-limited per IP (`LIMITS.atisConfig`), with an ETag so a client that is already current gets a 304. **The document's schema is owned by *this* repo**, not can-web: it is `Station.to_dict()`/`Preset.to_dict()` in `atis/profile.py`, which is why its keys are snake_case in a camelCase codebase. can-web validates it on the first request (frequency band, duplicate callsigns, two stations on one frequency, preset without a template) and answers 500 with the reasons rather than publishing a broken configuration to every operator; `can-web/src/data/atis/README.md` is the editing guide. Adding a field to `Station` means adding it there too, and old clients keep working because unknown keys are ignored.

**ATIS datafeed → can-fsd.** `server/ATIS/request.py` polls `https://data.ceruleanavi.net/v1/data.json`, can-fsd's datafeed (`internal/api`, HTTP port 20350), and `server/ATIS/mumble.py` speaks every `atis[]` entry it finds. It consumes `atis[].callsign`, `.frequency` and `.text_atis` — can-fsd guarantees `text_atis` is a JSON array, never null, and has golden-file tests pinning that document. Two details owned by can-fsd: a station lands in `atis[]` only if its callsign ends in `_ATIS`, and `frequency` is a full MHz string (`"128.500"`) where **`199.998` means "no frequency set"** — never build a `FREQ_*` channel from it. Splitting `text_atis` on `|` into English|Chinese is a convention of *this* repo, not of the datafeed.

**Which clients speak FSD.** Two do, and they are the only ones: `atis/fsdclient.py` logs a station in as an `_ATIS` controller (`#AA`), and `xpc/fsdpilot.py` logs an aircraft in as a pilot (`#AP`). Packet layouts for both come from can-fsd's `internal/fsd/conn.go`, `handler.go` and `docs/protocol.md`.

The other three — `client/`, `xplane_client/`, `controller/` — speak **only** Mumble and never touch the FSD port. A controller's presence on the network comes from whatever ATC client they run (EuroScope and friends), exactly as TrackAudio does it; keep it that way, because the voice server has no roster check and an FSD login from `controller/` would imply one.

In both FSD clients the ninth `$ID` field (challenge) is deliberately omitted so the server never starts a VATSIM `$ZC` challenge that only official clients hold keys for.

No authorisation is shared: can-fsd checks the `division` roster before a controller may staff a position, but the voice server has no equivalent — any account that authenticates can join any `FREQ_*` channel.

The naming has caught up: the network is **Cerulean Aviation Network (formerly AirwaySN)**, can-fsd takes its own name from `config.json`'s `version`, and this repo now hardcodes `ceruleanavi.net`, `data.ceruleanavi.net`, `fsd.ceruleanavi.net` and `audio.ceruleanavi.net` (the Mumble host, renamed from `hjdczy.top` and then from `audio.airwaysn.org`). **`airwaysn.org` no longer resolves at all**, which is why the rename could not stop at the defaults: `mumble_host`, `fsd_host`, `datafeed_url` and `config_url` are all *written back into the settings file*, so an existing install keeps using the dead domain and reports it as a connection timeout with every line in the settings dialog looking perfectly normal. `OLD_MUMBLE_HOSTS`/`OLD_FSD_HOSTS` in `xpc/settings.py` and `msfs/settings.py`, and `OLD_FSD_HOSTS`/`OLD_DATAFEED_URLS`/`OLD_CONFIG_URLS` in `atis/settings.py`, rewrite exactly those old values on load and leave anything else alone — a member who pointed a client at a test server or an intranet mirror meant it.

## Reference docs

- `xplane_client/API.md` — X-Plane UDP protocol notes (BECN discovery on `239.255.1.1:49707`, RREF requests, dataref precision) and a SimConnect-vs-X-Plane comparison table. Applies to `xpc/xplane.py` too, except that XPC subscribes rather than polls.
- `client/API.md` — vendored upstream pymumble API reference, not project documentation.

## Credentials, versions and the update check

Four things that were each wrong in a way nothing reported, fixed together. They share a shape:
the mechanism was present and the check inside it was not, so from the outside everything looked
healthy.

**The voice server's certificate is pinned by SHA-256 fingerprint.** `mumblecompat.py` used to
build its TLS context with `check_hostname = False` and `verify_mode = CERT_NONE`, justified in the
docstring by the fact that the official Mumble client authenticates a server *by fingerprint*
rather than by CA chain — but no fingerprint check existed here, and pymumble does none either, so
neither half of that argument held. The password on that socket is the member's **website**
password (can-api's `VerifyNetworkCredential` accepts it against either column), so anyone able to
sit in the middle got the whole account.

The check has to go where it now is and nowhere else. pymumble 1.6.1 **wraps before it connects**
(`mumble.py`'s `connect`): `ssl.wrap_socket(std_sock, …)` on an unconnected socket, then
`control_socket.connect(...)` — which is where the handshake happens — and only *then* the
`Version` and `Authenticate` messages. So `_PinnedSSLSocket` overrides the socket's `connect()`
and verifies before returning, which is the one point after the handshake and before the
credential. Wrapping `Mumble.connect` from outside is too late: the password has already gone.
It is an `ssl.SSLSocket` subclass installed via `SSLContext.sslsocket_class`, not a proxy, because
pymumble's main loop `select`s on it and `_RetryingSocket` wraps it again.

`CertificatePinError` is **deliberately not an `OSError`**: pymumble's `connect()` catches
`socket.error` around exactly that region, so an `OSError` would be swallowed into a plain
"connection failed" and then retried three times by `BoundedReconnect` — a man in the middle
retried three times is still a man in the middle, only now the log reads like a flaky network.

Two operational facts follow, and the first one can strand every client at once:

- **Murmur's certificate lives in `/var/lib/mumble-server`.** Lose that volume and the server
  self-signs a new one, at which point every shipped client refuses to connect and there is no
  remote fix. `PINNED_FINGERPRINTS` is a tuple so a rotation can ship **both** fingerprints, wait
  for people to update, and only then drop the old one.
- `CAN_MUMBLE_FINGERPRINTS` (comma or space separated, colons and case tolerated) replaces the
  built-in list — the escape hatch for that rotation and for self-hosted servers. An **empty**
  value means "not set", not "no pinning", or the variable would itself be a bypass switch.

Only `audio.ceruleanavi.net` is enforced. `xpc`/`msfs` let a member set `mumble_host`, and pointing
a client at a test server or an intranet mirror is deliberate — those get the fingerprint logged at
INFO instead, so anyone who wants to pin their own server can read it off. The current pin was
taken from the live server (`openssl s_client -connect audio.ceruleanavi.net:64738 </dev/null |
openssl x509 -noout -fingerprint -sha256`); both A records serve the same certificate.

**A packaged release reports the tag it was built from, and `DEV` no longer overrides that.**
`version.py`'s `DEV = True` has been on `main` since 2026-08-07, and `release.yml` fires on every
push to `main` without ever flipping it — there is no `sed` on `version.py` anywhere in the
workflow. So `version()` returned `_bump_patch(release_version())` in shipped builds: the package
tagged `v2.2.3` called itself `2.2.4 (Dev Build 1)`. That is not merely cosmetic. The update check
compares numerically on both sides, so a user on v2.2.3 self-reporting `2.2.4` is told "you are
current" when v2.2.4 actually ships — **every release permanently skips the one after it**, on all
four clients, silently.

The fix is to stop depending on anyone remembering to flip a constant. `freeze()` records whether
`CAN_VERSION` was supplied (`RELEASE_KEY` in `buildinfo.json`), which is exactly what separates a
CI build — whose number comes from counting tags and really will be published — from a hand-run
`pyinstaller`. `is_dev()` reads that mark, and `version()`/`display()`/`full()` all go through it.
`DEV` therefore governs only source runs and local packages, which genuinely are test builds.
"Any `buildinfo.json` means release" would have been wrong: `freeze()` writes it into the component
directory, so one local build would make every later source run claim to be a release.

**`update.py` matches can-api's actual response, which it never did.** It tested a top-level
`update_available` that can-api has never sent (the verdict is nested: `update.available`), so the
check returned "no update" every single time, on all four clients, and looked exactly like there
being no update. Behind that sat a second fault: the top-level `client` field is the package **name
as a string**, while the per-build object with `version`/`size`/`download` lives under
`clients[<name>]` — so fixing only the first line would have produced `AttributeError: 'str' object
has no attribute 'get'` on a line outside the `try` (which covers only the `urlopen`), escaping
into an unguarded worker thread where `update_found` simply never fires. The parse is now inside
its own `try` for that reason. `xpc/test_xpc.py`'s `UpdateCheckTest` builds the real can-api
payload; when this was written the old mocks reproduced can-web's long-dead shape, which is why
every one of them passed against dead code.

**The pilot clients no longer write the network password to disk by default.** `xpc`/`msfs`
persisted `password` in cleartext next to the username, in a settings file written to the **current
working directory** — an X-Plane or MSFS community folder, a synced game directory, a support
bundle. `remember_password` now defaults off and `save()` writes the field only when it is on. A
file from an older version is recognised by the *absence* of the `remember_password` key: the
password is read into memory so the session in progress still connects, the file is rewritten
immediately rather than at the next save, and `password_migrated` makes the client say so — the
same shape as the `OLD_MUMBLE_HOSTS` rewrite. Dropping it without saying anything would look like
the client losing the password; keeping it would leave the leak in place.

**Still open: the pbh sign, and it is not safe to guess.** can-fsd's `normaliseSigned`
(`internal/fsd/packet.go`) begins `v = -v`, so it decodes pitch and bank as the negation of what
`xpc`/`msfs`'s `fsdpilot.py` encodes. `test_xpc.py`'s reference `unpack_pbh` — which
`fsdpilot.py` names as the arbiter — is a transcription of that Go function that dropped the same
line, so `PbhTest` round-trips can-audio against a copy of its own convention and passes while both
halves may be wrong. Neither side's tests pin it: can-fsd's `TestPitchBankHeading` checks only the
heading round-trip and that all three axes stay in range, and `docs/protocol.md` documents the bit
layout without mentioning a sign. **Which side matches the external openfsd/EuroScope convention
cannot be decided from this tree**, and changing the wrong one inverts aircraft attitude for
everybody, so it has been left alone. Settle it against openfsd or a real EuroScope capture, not
against either copy here.
