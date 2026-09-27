"""把他机放进 MSFS。

这是 MSFS 版相对 xpc 最大的简化：**不需要插件**。X-Plane 的绘图 API 只有插件
够得着，所以 xpc 要拆成两个进程加一条 UDP 通道；SimConnect 允许外部进程直接
建 AI 飞机，一个进程就够了。

TCAS 也是白送的——AI 飞机是真的 SimObject，座舱的交通显示本来就看得见，不像
X-Plane 还要单独去填 sim/cockpit2/tcas/targets/*。

    创建   AICreateNonATCAircraft(title, 尾号, 初始位置, requestID)
    移动   SetDataOnSimObject(objectID, 位置定义)
    接管   AIReleaseControl(objectID) + FREEZE_*_SET（objectID 回来之后各一次）
    移动   SetDataOnSimObject(objectID, 位置定义)，InjectionLoop 每秒 30 次
    删除   AIRemoveObject(objectID, requestID)

**objectID 是异步回来的，而且必须自己关联。** 创建函数只是把请求发出去，真正的
objectID 通过 SIMCONNECT_RECV_ID_ASSIGNED_OBJECT_ID 消息回来，靠 dwRequestID 对
上是哪一次创建。Python-SimConnect 自带的 dispatch 确实处理了这条消息，但它把结果
塞进 os.environ["SIMCONNECT_OBJECT_ID"]，**不带 RequestID**——同时建 20 架飞机
时全写同一个变量，根本分不清谁是谁。所以这里接管了 dispatch：先按 requestID 记
进自己的表，再把消息交回原来的处理函数。
"""

import ctypes
import logging
import threading
import time

log = logging.getLogger("inject")

# 我们自己的 requestID 从这里开始编，避开包里内建请求用的号段
REQUEST_BASE = 10000

# 一次最多放多少架。MSFS 对 AI 数量没有硬上限，但每架都是完整的飞机模型，
# 放太多会掉帧——按距离排序后截断，远的先扔。
MAX_AIRCRAFT = 40

# objectID 等这么久还不回来就放弃这条记录（补删挂账，下一轮重建）。
# 位置太远之类的失败没有逐条回执，不设时限的话记录会永远停在"还在等"。
PENDING_TIMEOUT = 15.0
# 同一个模型连着建败几次才拉黑。EXCEPTION 消息对不上是哪次请求，一次失败
# 就拉黑会把同一轮里无辜的模型全部错杀（一分钟内能把整个机队拉黑）。
BLACKLIST_AFTER = 3

# 注入循环的频率。MSFS 每帧画的是最后一次写进去的位置，写得越稀，画面上
# 越是一格一格地跳。
FRAME_RATE = 30.0

# 接管 AI 飞机用的三个模拟器事件。AICreateNonATCAircraft 建出来的飞机归 MSFS
# 的 AI 管：它自己的飞行模型和自动驾驶在两次写入之间继续推飞机，下一次
# SetDataOnSimObject 再把它拽回来——画面上就是抽搐。冻住经纬度、高度和姿态，
# 再 AIReleaseControl，位置就只由我们写。
FREEZE_EVENTS = (b"FREEZE_LATITUDE_LONGITUDE_SET", b"FREEZE_ALTITUDE_SET",
                 b"FREEZE_ATTITUDE_SET")
# 这三个事件的客户端事件号。避开 Python-SimConnect 自己的号段：它的
# EventID 枚举从 0 起编，map_to_sim_event 每映射一个往后加一。
FREEZE_EVENT_BASE = 90000
# SimConnect.h 的 SIMCONNECT_GROUP_PRIORITY_HIGHEST 和
# SIMCONNECT_EVENT_FLAG_GROUPID_IS_PRIORITY。带上这个标志时 GroupID 参数
# 就是优先级。
GROUP_PRIORITY_HIGHEST = 1
EVENT_FLAG_GROUPID_IS_PRIORITY = 0x00000010
# 记多少个"接管请求的包号 -> 哪架飞机"，给 EXCEPTION 认领用
CONTROL_PACKETS_KEPT = 512


class _Definition:
    """SetDataOnSimObject 用的数据定义。

    字段顺序必须和写进去的结构体逐项对应，错位了飞机会出现在地球另一边。
    """

    FIELDS = [
        (b"PLANE LATITUDE", b"degrees"),
        (b"PLANE LONGITUDE", b"degrees"),
        (b"PLANE ALTITUDE", b"feet"),
        (b"PLANE PITCH DEGREES", b"degrees"),
        (b"PLANE BANK DEGREES", b"degrees"),
        (b"PLANE HEADING DEGREES TRUE", b"degrees"),
        (b"AIRSPEED TRUE", b"knots"),
        # 这里不能放 SIM ON GROUND：它不是可写的 SimVar，而定义里混进一个
        # 不可写的字段会让**整条** SetDataOnSimObject 以 SET_DATA_FAILED
        # 收场——飞机建在初始位置之后就再也不动，日志还一片干净。
        # 在地面与否只在创建时通过 INITPOSITION.OnGround 告诉模拟器。
    ]


class TrafficInjector:
    """把 TrafficTable 的快照映射成 MSFS 里的 AI 飞机。"""

    def __init__(self, sim, definition_id=1000):
        self.sim = sim
        self.definition_id = definition_id
        self.aircraft = {}          # 呼号 -> {object_id, title, request_id}
        self.available = False

        self._lock = threading.Lock()
        self._pending = {}          # requestID -> 呼号（等 objectID 回来）
        self._requested_titles = {}  # requestID -> 模型名，出错时才知道是谁
        self._assigned = {}         # requestID -> objectID
        # 已经不要了、但 objectID 还在路上的那些请求。飞机在模拟器里是**已经建
        # 出来**的，等号码回来必须补一刀删掉，否则它会以最后的位置永久停在天上。
        self._orphaned = set()
        self.bad_titles = set()     # 模拟器拒绝生成过的模型；匹配时要排除掉
        self._title_failures = {}   # 模型名 -> 连续建败次数
        self._next_request = REQUEST_BASE
        self._enums = None
        # 映射成功的冻结事件号。映射失败时是空的，飞机照样建、照样动，只是
        # 不冻结（日志里有一条警告）。
        self._freeze_events = ()
        # SimConnect 包号 -> (呼号, 对象号, 哪个请求)。EXCEPTION 只带包号，
        # 靠这张表才说得出是哪架飞机的冻结/释放被拒了。
        self._control_packets = {}
        # sync() 和 clear() 各自会发一串 DLL 调用，一个在注入线程、一个在
        # Qt 线程（断开/退出时）。不串行化的话断开那一刻两串请求交错着发。
        self._op_lock = threading.Lock()

        try:
            self._setup()
            self.available = True
        except Exception as e:
            log.warning("traffic injection is unavailable: %s", e)

    # ---------- 初始化 ----------
    def _setup(self):
        from SimConnect import Enum as sc_enum

        self._enums = sc_enum
        self._install_dispatch()
        self._define_position()
        self._map_freeze_events()

    def _install_dispatch(self):
        """接管 ASSIGNED_OBJECT_ID，按 requestID 关联。

        包自带的处理只写一个环境变量，不带 requestID，多架飞机同时创建时无法
        区分。这里在它前面插一层，记完再原样交回去。

        **光换 `my_dispatch_proc` 这个属性是没用的。** Python-SimConnect 在
        `__init__` 里就把它包成了一个 ctypes 跳板：

            self.my_dispatch_proc_rd = self.dll.DispatchProc(self.my_dispatch_proc)

        而收消息的循环调的是那个跳板（`SimConnect.py:181` 的
        `CallDispatch(..., self.my_dispatch_proc_rd, ...)`），跳板里存的是**构造
        那一刻的原方法**。所以只改属性的话，我们这一层从头到尾不会被调用一次。

        症状极具迷惑性：创建请求发得出去（日志里一行行"请求把 X 放进模拟器"），
        但 ASSIGNED_OBJECT_ID 一个都收不到——于是 `record["object_id"]` 永远是
        None，`_sync_one` 每轮都停在"还在等 objectID"，**他机被生成在初始位置
        之后就再也不动**，离线了也删不掉（没有对象号就没法 AIRemoveObject），
        而机型问到之后的重新匹配又会再建一架，飞机就这么翻倍。EXCEPTION 消息
        同样收不到，所以模拟器拒绝过的模型也进不了 bad_titles，每轮重试。
        真实日志（v2.0.3）里的样子：10 次创建请求、0 个对象号、0 次移除、
        0 条警告。

        所以跳板也要一起换掉。循环每轮都重读 `my_dispatch_proc_rd`，所以运行中
        替换是安全的、立即生效。
        """
        sc = self.sim
        enums = self._enums
        original = sc.my_dispatch_proc

        def dispatch(pData, cbData, pContext):
            try:
                kind = pData.contents.dwID
                if (kind ==
                        enums.SIMCONNECT_RECV_ID.SIMCONNECT_RECV_ID_ASSIGNED_OBJECT_ID):
                    body = ctypes.cast(
                        pData,
                        ctypes.POINTER(enums.SIMCONNECT_RECV_ASSIGNED_OBJECT_ID)
                    ).contents
                    with self._lock:
                        self._assigned[int(body.dwRequestID)] = int(body.dwObjectID)
                elif kind == enums.SIMCONNECT_RECV_ID.SIMCONNECT_RECV_ID_EXCEPTION:
                    self._note_exception(ctypes.cast(
                        pData,
                        ctypes.POINTER(enums.SIMCONNECT_RECV_EXCEPTION)).contents)
            except Exception as e:
                log.debug("handling a SimConnect message raised: %s", e)
            return original(pData, cbData, pContext)

        sc.my_dispatch_proc = dispatch
        # 收消息的循环用的是这个跳板，不换它等于什么都没做（见上面那段）
        try:
            sc.my_dispatch_proc_rd = sc.dll.DispatchProc(dispatch)
        except Exception as e:
            # 换不掉就退回原样：他机会不动，但客户端其余部分照常工作，
            # 而且日志里说得清是为什么。
            log.warning("could not rebind the SimConnect dispatch callback, "
                        "traffic will not move: %s", e)
            sc.my_dispatch_proc = original

    def _note_exception(self, body):
        """SimConnect 的异步错误。

        建 AI 飞机失败是通过这条消息回来的，包自带的日志只打一个
        SIMCONNECT_EXCEPTION_CREATE_OBJECT_FAILED，不说是哪架、哪个模型——
        实测日志里就是这样，完全没法查。这里把模型名补上。

        dwSendID 对应发出去的那个包，但要 GetLastSentPacketID 才能对上；我们
        没记那个，所以退而求其次：把最近一次请求过的模型都列出来。这已经足够
        指认是哪个模型建不出来了。
        """
        exceptions = self._enums.SIMCONNECT_EXCEPTION
        # 编号从枚举里取，别硬编码——CREATE_OBJECT_FAILED 是 22，我一开始
        # 按"排第 12 位"猜成了 12，那其实是 TOO_MANY_REQUESTS。
        interesting = {
            int(exceptions.SIMCONNECT_EXCEPTION_CREATE_OBJECT_FAILED):
                "建不出来（模型名对不上，或这个机型不能作为 AI 生成）",
            int(exceptions.SIMCONNECT_EXCEPTION_OBJECT_OUTSIDE_REALITY_BUBBLE):
                "位置太远，超出模拟器的加载范围",
            int(exceptions.SIMCONNECT_EXCEPTION_OBJECT_CONTAINER):
                "模型容器有问题（装得不完整？）",
        }
        # 老版本的枚举里不一定有，缺了就不认这一条，别把整个处理炸掉
        data_error = getattr(exceptions, "SIMCONNECT_EXCEPTION_DATA_ERROR", None)
        if data_error is not None:
            interesting[int(data_error)] = \
                "SetDataOnSimObject 被拒（数据定义里混进了不可写的 SimVar？）"
        try:
            code = int(body.dwException)
        except Exception:
            return
        if self._note_control_exception(body, code):
            return
        reason = interesting.get(code)
        if reason is None:
            return

        with self._lock:
            titles = list(self._requested_titles.values())
            waiting = list(self._pending.values())
        log.warning("the simulator refused to create traffic: %s. waiting: %s, "
                    "models in use: %s", reason, waiting or "none",
                    titles or "none")
        # 位置太远（reality bubble）是暂时的、也不指认任何模型——什么都不用
        # 收拾：建败的那条记录等 PENDING_TIMEOUT 超时自己重来，把同一轮里
        # 正在建的**别的**飞机一起丢掉只会造成拆了又建的抖动。
        if code != int(exceptions.SIMCONNECT_EXCEPTION_CREATE_OBJECT_FAILED):
            return
        with self._lock:
            # EXCEPTION 对不上是哪次请求。只有同一轮里就一个模型才能指认；
            # 多个模型在场时给每个记一次嫌疑，连续 BLACKLIST_AFTER 次才拉黑
            # ——一次失败全体拉黑的话，一分钟就能把整个机队错杀干净。
            distinct = set(titles)
            for title in distinct:
                self._title_failures[title] = self._title_failures.get(title, 0) + 1
                if (len(distinct) == 1
                        or self._title_failures[title] >= BLACKLIST_AFTER):
                    self.bad_titles.add(title)
                    log.warning("model %r is now blacklisted", title)
            self._requested_titles.clear()
            for callsign in waiting:
                record = self.aircraft.get(callsign)
                if record is not None and record.get("object_id") is None:
                    self.aircraft.pop(callsign, None)
            # 这里把所有在等的请求都丢掉了，可其中有些是会建成功的——它们的
            # objectID 随后就到，没人认领就成了天上的幽灵。全部记成待补删，
            # 真没建出来的那几个 AIRemoveObject 会自己失败，无害。
            self._orphaned.update(self._pending)
            self._pending.clear()

    def _note_control_exception(self, body, code):
        """接管请求（AIReleaseControl / FREEZE_*_SET）被拒的话，说清是哪架。

        包号在 Python-SimConnect 的结构体里叫 `UNKNOWN_SENDID`：它把头文件里
        的静态常量 UNKNOWN_SENDID 当成了字段，于是真正的 dwSendID 落在这个
        名字上（包自己的 handle_exception_event 也是拿它去对 LastID）。
        """
        send_id = getattr(body, "UNKNOWN_SENDID", None)
        if send_id is None:
            return False
        try:
            send_id = int(send_id)
        except (TypeError, ValueError):
            return False
        with self._lock:
            control = self._control_packets.pop(send_id, None)
        if control is None:
            return False
        callsign, object_id, what = control
        try:
            name = self._enums.SIMCONNECT_EXCEPTION(code).name
        except Exception:
            name = str(code)
        log.warning("the simulator refused %s for %s (object %d): %s",
                    what, callsign, object_id, name)
        return True

    def _define_position(self):
        """注册位置的数据定义。只需要做一次。"""
        for name, unit in _Definition.FIELDS:
            hr = self.sim.dll.AddToDataDefinition(
                self.sim.hSimConnect, self.definition_id, name, unit,
                self._enums.SIMCONNECT_DATATYPE.SIMCONNECT_DATATYPE_FLOAT64,
                0, self._enums.SIMCONNECT_UNUSED)
            if hr != 0:
                raise RuntimeError(f"AddToDataDefinition({name!r}) 失败: {hr}")

    def _map_freeze_events(self):
        """把三个 FREEZE_*_SET 映射成我们自己的客户端事件号。只需要做一次。

        失败不影响注入本身：飞机照样建、照样写位置，只是没有冻结。
        """
        mapped = []
        for offset, name in enumerate(FREEZE_EVENTS):
            event_id = FREEZE_EVENT_BASE + offset
            error = self._call("MapClientEventToSimEvent",
                               self.sim.hSimConnect, event_id, name)
            if error:
                log.warning("could not map %s, injected traffic will not be "
                            "frozen: %s", name.decode(), error)
                self._freeze_events = ()
                return
            mapped.append((event_id, name.decode()))
        self._freeze_events = tuple(mapped)

    def _call(self, name, *args):
        """调一个 SimConnect 函数。成功返回 None，失败返回一句说明。

        包里的 restype 是 ctypes.HRESULT：失败的 HRESULT 在 Windows 上会直接
        抛 OSError，而不是返回非零。两种都要接住。
        """
        try:
            hr = getattr(self.sim.dll, name)(*args)
        except OSError as e:
            return f"{name}: {e}"
        except Exception as e:
            return f"{name}: {type(e).__name__}: {e}"
        if hr:
            try:
                return f"{name}: HRESULT {int(hr) & 0xFFFFFFFF:#010x}"
            except (TypeError, ValueError):
                return f"{name}: HRESULT {hr!r}"
        return None

    def _remember_packet(self, callsign, object_id, what):
        """记下刚发出去的那个包的包号，EXCEPTION 回来时好认领。

        GetLastSentPacketID 返回的是这个连接上**最后**发出的包。simlink 的轮询
        线程也在同一个连接上发请求，两次调用之间插进一个它的包的话，这里会
        记错号——结果只是少认领一条异常，异常本身仍由包自己的日志打出来。
        """
        packet = ctypes.c_ulong(0)
        if self._call("GetLastSentPacketID", self.sim.hSimConnect,
                      ctypes.byref(packet)):
            return
        with self._lock:
            self._control_packets[int(packet.value)] = (callsign, object_id, what)
            while len(self._control_packets) > CONTROL_PACKETS_KEPT:
                self._control_packets.pop(next(iter(self._control_packets)))

    def _take_control(self, callsign, object_id):
        """把刚建好的飞机从 MSFS 的 AI 手里拿过来：释放 AI 控制，冻结位置和姿态。

        每个对象号只做一次：objectID 回来、被认领的那一刻。换模型是删了重建，
        新对象号回来时会再走一遍。返回是否全部发出去了。
        """
        failures = []
        error = self._call("AIReleaseControl", self.sim.hSimConnect,
                           object_id, self._request_id())
        if error:
            failures.append(error)
        else:
            self._remember_packet(callsign, object_id, "AIReleaseControl")
        for event_id, name in self._freeze_events:
            error = self._call("TransmitClientEvent", self.sim.hSimConnect,
                               object_id, event_id, 1, GROUP_PRIORITY_HIGHEST,
                               EVENT_FLAG_GROUPID_IS_PRIORITY)
            if error:
                failures.append(error)
            else:
                self._remember_packet(callsign, object_id, name)
        if failures:
            log.warning("could not take %s (object %d) off AI control: %s",
                        callsign, object_id, "; ".join(failures))
            return False
        if not self._freeze_events:
            log.warning("%s (object %d) is released from AI control but not "
                        "frozen: the freeze events are not mapped",
                        callsign, object_id)
            return False
        log.info("%s (object %d) is frozen and released from AI control",
                 callsign, object_id)
        return True

    def _request_id(self):
        with self._lock:
            self._next_request += 1
            return self._next_request

    # ---------- 同步 ----------
    def sync(self, entries):
        """让模拟器里的 AI 飞机和这份快照一致。

        entries 是 traffic.TrafficTable.snapshot() 的输出，已经按距离排好序。
        """
        if not self.available:
            return
        with self._op_lock:
            self._collect_assigned()

            entries = entries[:MAX_AIRCRAFT]
            seen = set()
            for entry in entries:
                callsign = entry.get("callsign")
                if not callsign:
                    continue
                seen.add(callsign)
                try:
                    self._sync_one(callsign, entry)
                except Exception as e:
                    log.debug("updating %s raised: %s", callsign, e)

            for callsign in [c for c in list(self.aircraft) if c not in seen]:
                self.remove(callsign)

    def _collect_assigned(self):
        """把已经回来的 objectID 认领到对应的飞机上。

        还要收拾"号码回来晚了"的那些：飞机一创建就飞出范围（或者中途来了个
        SimConnect 异常）时，remove() 那会儿 object_id 还是 None，删不了——可是
        模拟器那边是真的建出来了。不在这里补删的话，它会以最后的位置永远停在
        天上，而我们连它的号码都不再记得。
        """
        now = time.time()
        claimed = []
        with self._lock:
            ready = [(rid, oid) for rid, oid in self._assigned.items()
                     if rid in self._pending]
            for rid, oid in ready:
                callsign = self._pending.pop(rid)
                self._assigned.pop(rid, None)
                self._requested_titles.pop(rid, None)
                record = self.aircraft.get(callsign)
                if record is not None:
                    record["object_id"] = oid
                    claimed.append((callsign, oid))
                    # 建成了就洗清嫌疑——失败计数只数**连续**的
                    self._title_failures.pop(record.get("title"), None)
                    log.info("%s is in the simulator (object %d, model %s)",
                             callsign, oid, record.get("title", "?"))
            strays = [(rid, self._assigned.pop(rid))
                      for rid in list(self._assigned) if rid in self._orphaned]
            for rid, _ in strays:
                self._orphaned.discard(rid)
                self._requested_titles.pop(rid, None)

            # 等了太久还没等到 objectID 的：多半是那次创建静默失败了（比如
            # 位置太远）。把记录丢掉、请求挂账补删，下一轮该重建的自然重建。
            # 不设时限的话这条记录永远停在"还在等"，飞机永远出不来。
            for callsign in list(self.aircraft):
                record = self.aircraft[callsign]
                if (record.get("object_id") is None
                        and now - record.get("requested_at", now) > PENDING_TIMEOUT):
                    rid = record.get("request_id")
                    if self._pending.pop(rid, None) is not None:
                        self._orphaned.add(rid)
                    self._requested_titles.pop(rid, None)
                    self.aircraft.pop(callsign, None)
                    log.debug("gave up waiting for %s's objectID, will recreate",
                              callsign)

        # DLL 调用放到锁外面：_request_id() 自己也要拿这把锁
        for callsign, object_id in claimed:
            record = self.aircraft.get(callsign)
            frozen = self._take_control(callsign, object_id)
            if record is not None:
                record["frozen"] = frozen
        for _, object_id in strays:
            try:
                self.sim.dll.AIRemoveObject(self.sim.hSimConnect, object_id,
                                            self._request_id())
                log.info("swept up an aircraft that was no longer needed (object %d)",
                         object_id)
            except Exception as e:
                log.debug("the traffic sweep raised: %s", e)

    def _sync_one(self, callsign, entry):
        record = self.aircraft.get(callsign)
        title = entry.get("model") or ""

        if record is not None and title and record.get("title") != title:
            # 机型问到了或者变了，换模型只能删了重建
            self.remove(callsign)
            record = None

        if record is None:
            if not title:
                return          # 还没匹配到模型，等下一轮
            self._create(callsign, entry, title)
            return

        object_id = record.get("object_id")
        if object_id is None:
            return              # 还在等 objectID
        self._move(object_id, entry)

    def _create(self, callsign, entry, title):
        if title in self.bad_titles:
            # 这个模型已经证明建不出来，别每轮都再试一次
            return

        init = self._enums.SIMCONNECT_DATA_INITPOSITION()
        init.Latitude = entry["latitude"]
        init.Longitude = entry["longitude"]
        init.Altitude = entry["altitude"]
        # FSD 的姿态是抬头为正、右滚为正；MSFS 的 PLANE PITCH/BANK 正好反过来
        # （simlink 读的时候取了负，写回去也要取负）。不翻的话进近的飞机在
        # 别人模拟器里全程俯冲。
        init.Pitch = -entry.get("pitch", 0.0)
        init.Bank = -entry.get("bank", 0.0)
        init.Heading = entry.get("heading", 0.0)
        init.OnGround = 1 if entry.get("on_ground") else 0
        init.Airspeed = int(entry.get("groundspeed", 0))

        request_id = self._request_id()
        hr = self.sim.dll.AICreateNonATCAircraft(
            self.sim.hSimConnect, title.encode("utf-8"),
            callsign.encode("utf-8")[:12], init, request_id)
        if hr != 0:
            # HRESULT 直接非零是连接/参数层面的失败（句柄坏了、连接掉了），
            # 不是这个模型的错——拉黑它会把当时恰好在用的模型全部错杀
            log.warning("creating %s failed (model %r): HRESULT %s", callsign, title, hr)
            return

        with self._lock:
            self._pending[request_id] = callsign
            self._requested_titles[request_id] = title
        self.aircraft[callsign] = {"object_id": None, "title": title,
                                   "request_id": request_id,
                                   "requested_at": time.time()}
        # 机型还没问到时先拿通用模型顶上，半秒后 #SB 回来会用正确模型再建一次。
        # 两条里只有后一条是结论，前一条降到 DEBUG——他机多的时候这类占了大头。
        level = log.debug if not entry.get("equipment") else log.info
        level("asking the simulator to create %s: model %r, request %d",
              callsign, title, request_id)

    def _move(self, object_id, entry):
        # 姿态取负：见 _create 里的注释
        values = (ctypes.c_double * len(_Definition.FIELDS))(
            entry["latitude"], entry["longitude"], entry["altitude"],
            -entry.get("pitch", 0.0), -entry.get("bank", 0.0),
            entry.get("heading", 0.0), float(entry.get("groundspeed", 0)))
        self.sim.dll.SetDataOnSimObject(
            self.sim.hSimConnect, self.definition_id, object_id,
            0, 0, ctypes.sizeof(values), values)

    def remove(self, callsign):
        record = self.aircraft.pop(callsign, None)
        if not record:
            return
        object_id = record.get("object_id")
        with self._lock:
            request_id = record.get("request_id")
            was_pending = self._pending.pop(request_id, None) is not None
            if object_id is None and was_pending:
                # 号码还没回来。飞机在模拟器里已经建出来了，这里删不掉，
                # 记一笔等 _collect_assigned 补删。
                self._orphaned.add(request_id)
        if object_id is None:
            return
        try:
            self.sim.dll.AIRemoveObject(self.sim.hSimConnect, object_id,
                                        self._request_id())
            log.info("removed %s from the simulator", callsign)
        except Exception as e:
            log.debug("removing %s raised: %s", callsign, e)

    def clear(self):
        """全部清掉。断开连接和退出时都要调，否则飞机会留在天上不动。

        和 sync() 串行化：clear 多从 Qt 线程来（断开/退出），一轮 sync 正在
        注入线程上跑的话，两串 SimConnect 请求会交错着发。
        """
        with self._op_lock:
            for callsign in list(self.aircraft):
                self.remove(callsign)


class InjectionLoop:
    """在自己的线程上按固定频率调 step()，把他机写进模拟器。

    原来是 Qt 定时器每 200 ms 起一个线程做一轮 sync：5 Hz 的写入，MSFS 每帧
    画的是最后一次写进去的位置，于是一卡一卡。这里换成一条常驻线程、每秒
    FRAME_RATE 次。

    不挂在 SimConnect 的 "Frame" 系统事件上：那个事件在包的 dispatch 线程上
    回调，而 objectID、EXCEPTION 和 simlink 读 SimVar 的回复都走同一个线程，
    每帧四十次 SetDataOnSimObject 会压着它们；帧率还跟着模拟器走，144 fps
    就是 144 轮。

    节拍用 time.sleep：Python 3.11 起它在 Windows 上用高精度等待计时器，
    Event.wait 仍是 15.6 ms 的系统粒度，33 ms 的周期会抖成 31/47。
    """

    def __init__(self, step, rate=FRAME_RATE, name="traffic-inject"):
        self.step = step
        self.period = 1.0 / rate
        self.name = name
        self._stop = threading.Event()
        self._thread = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._stop,),
                                        name=self.name, daemon=True)
        self._thread.start()

    def stop(self, timeout=2.0):
        """停下并等线程退出。返回之后不会再有 step() 在跑（超时除外）。"""
        thread = self._thread
        self._stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                log.warning("the traffic injection loop did not stop within %.1f s",
                            timeout)
        self._thread = None

    def _run(self, stop):
        deadline = time.perf_counter()
        while not stop.is_set():
            try:
                self.step()
            except Exception as e:
                log.warning("injecting traffic into the simulator raised: %s", e)
            deadline += self.period
            now = time.perf_counter()
            if deadline < now:
                # 落后了（模拟器卡了一下）就从现在重新数，别连着补跑
                deadline = now
                continue
            time.sleep(deadline - now)
