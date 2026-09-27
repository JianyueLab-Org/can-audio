"""把他机放进 MSFS。

这是 MSFS 版相对 xpc 最大的简化：**不需要插件**。X-Plane 的绘图 API 只有插件
够得着，所以 xpc 要拆成两个进程加一条 UDP 通道；SimConnect 允许外部进程直接
建 AI 飞机，一个进程就够了。

TCAS 也是白送的——AI 飞机是真的 SimObject，座舱的交通显示本来就看得见，不像
X-Plane 还要单独去填 sim/cockpit2/tcas/targets/*。

    创建   AICreateNonATCAircraft(title, 尾号, 初始位置, requestID)
    移动   SetDataOnSimObject(objectID, 位置定义)
    接管   AIReleaseControl(objectID) + FREEZE_*_SET（objectID 回来之后各一次）
    地面   RequestDataOnSimObject(objectID, GROUND ALTITUDE / STATIC CG TO GROUND)
    移动   SetDataOnSimObject(objectID, 位置定义)，InjectionLoop 跟着模拟器的
           Frame 事件（没有 Frame 时 30 Hz）
    装饰   SetDataOnSimObject(objectID, 起落架/襟翼/前轮，各一个定义)，变了才写
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

# 后备定时器的频率。平时跟着模拟器的 "Frame" 事件走（见 InjectionLoop）；
# 这个事件不来的时候（订阅失败、暂停）才按这个频率自己写。
FRAME_RATE = 30.0
# 多久没收到 Frame 事件就退回后备定时器。暂停时 MSFS 不发 Frame。
FRAME_TIMEOUT = 0.25
# 跟帧写入的上限。144 fps 的机器每帧写四十架没有必要，超过这个频率就隔帧写。
MAX_FRAME_RATE = 60.0
# "Frame" 系统事件用的客户端事件号，和冻结事件一样避开包自己的号段。
FRAME_EVENT_ID = 90100

# 地面数据用的数据定义号，接在位置定义（definition_id）后面
GROUND_DEFINITION_OFFSET = 1         # GROUND ALTITUDE，每 GROUND_INTERVAL 个模拟帧一次
HEIGHT_DEFINITION_OFFSET = 2         # STATIC CG TO GROUND，只要一次
SURFACE_DEFINITION_OFFSET = 3        # 起落架/襟翼/前轮，每个字段一个定义
# SimConnect.h 的 SIMCONNECT_PERIOD_*
PERIOD_NEVER = 0
PERIOD_ONCE = 1
PERIOD_SIM_FRAME = 3
# 地面标高隔多少个模拟帧回一次。60 fps 下约 6 Hz，四十架是每秒二百多条消息，
# 都走 dispatch 线程，别再密了。
GROUND_INTERVAL = 10

# ---------- 地面贴合（xPilot network_aircraft.cpp 的 PerformGroundClamping） ----------
# 高于这个高度（英尺）不管地面，和 xPilot 一样
GROUND_CLAMP_CEILING = 18000.0
# 对方 AGL 低于这个值（英尺）持续 GROUND_USABLE_AFTER 秒，才用两边地面的差
MAX_USABLE_AGL = 100.0
GROUND_USABLE_AFTER = 2.0
# 爬升段（离地、AGL ≥ 这个值、目标偏移为零）用长窗口慢慢把偏移放回零
MIN_AGL_FOR_CLIMBOUT = 50.0
GROUND_WINDOW_LANDING = 2.0
GROUND_WINDOW_CLIMBOUT = 10.0
MIN_OFFSET_MAGNITUDE = 0.1
# 本地地面和对方地面差得比这还多（英尺），就当本地读数是坏的、不用。
# 两个模拟器的地形差通常几十英尺；冻住的 AI 对象万一报 0，高原机场上
# 这一项会把飞机往下拽几千英尺。
MAX_SCENERY_DIFFERENCE = 1000.0
# STATIC CG TO GROUND 的合理范围（英尺）。超出就当没读到。
MAX_MODEL_HEIGHT = 40.0

# 机型还没问到时最多等这么久再建（秒）。先拿通用模型建、半秒后再换，
# 画面上就是一架飞机闪一下变成另一架。只发 `@` 的老客户端未必回答，
# 超时后照样用通用模型建出来。
MODEL_WAIT = 3.0

# 起落架/襟翼/前轮。都是装饰，每个字段单独一个数据定义：SetDataOnSimObject
# 里有一个字段不可写，**整条**写入都会失败，混进位置定义的话飞机就冻住了
# （见 _Definition 里 SIM ON GROUND 那段）。被模拟器拒过的字段记一次日志、
# 以后不再写。
SURFACE_FIELDS = (
    ("gear", b"GEAR HANDLE POSITION", b"bool"),
    ("flaps_left", b"TRAILING EDGE FLAPS LEFT PERCENT", b"percent over 100"),
    ("flaps_right", b"TRAILING EDGE FLAPS RIGHT PERCENT", b"percent over 100"),
    ("nose_wheel", b"GEAR CENTER STEER ANGLE", b"percent over 100"),
)
# 前轮角度（度）换成满舵的比例时假定的满舵角。只是个近似，装饰用。
NOSE_WHEEL_FULL_DEFLECTION = 60.0
# 变化小于这个值不重写（比例单位）
SURFACE_EPSILON = 0.01


def ground_target_offset(altitude, agl, local_ground, on_ground, usable,
                         model_height=0.0):
    """目标高度偏移（英尺）：加到对方上报的高度上。

    - 报告在地面：落在本地地面上，再加模型离地高度。
    - 空中、近地数据可用：对方地面 = 高度 − AGL，偏移 = 本地地面 − 对方地面。
    - 其余：零。
    """
    if on_ground:
        return round(local_ground + model_height - altitude, 2)
    if usable and agl is not None:
        return round(local_ground - (altitude - agl), 2)
    return 0.0


def ground_blend_window(on_ground, agl, target):
    """偏移走完要多少秒：爬升段 10 秒，其余（近地、着陆）2 秒。"""
    if not on_ground and agl >= MIN_AGL_FOR_CLIMBOUT and target == 0.0:
        return GROUND_WINDOW_CLIMBOUT
    return GROUND_WINDOW_LANDING


def step_toward(value, target, step):
    if step >= abs(target - value):
        return target
    return value + step if target > value else value - step


class GroundClamp:
    """一架飞机的地面贴合状态。纯的：不读时钟，不碰模拟器。

    照 xPilot 的 PerformGroundClamping：18000 ft 以下，把两边地面的差作为偏移
    慢慢叠上去，报告在地面的飞机落在本地地面上，任何时候不低于本地地面。

    和 xPilot 的一处不同：它的"近地数据可用"（HasUsableTerrainElevationData）
    要求历史跨度 ≥ 2000 ms，可历史里只留最近 1750 ms，这个条件永远不成立，
    所以 xPilot 实际上只在报告在地面时才用偏移。这里按它的本意做：AGL 持续
    ≤ 100 ft 两秒就用。坡度检查没有搬（它的远端标高从没赋值，只剩本地那半）。
    """

    def __init__(self):
        self.offset = 0.0
        self.target = 0.0
        self.magnitude = 0.0
        self.low_since = None
        self.placed = False
        self.rejected = False

    def update(self, now, dt, altitude, agl, on_ground, local_ground,
               model_height=0.0):
        """返回要写进模拟器的高度（英尺）。local_ground 为 None 时原样返回。"""
        self.rejected = False
        if local_ground is None or altitude >= GROUND_CLAMP_CEILING:
            return altitude
        if (agl is not None
                and abs(local_ground - (altitude - agl)) > MAX_SCENERY_DIFFERENCE):
            self.rejected = True
            return altitude

        if agl is not None and agl <= MAX_USABLE_AGL:
            if self.low_since is None:
                self.low_since = now
            usable = now - self.low_since >= GROUND_USABLE_AFTER
        else:
            self.low_since = None
            usable = False

        target = ground_target_offset(altitude, agl, local_ground, on_ground,
                                      usable, model_height)
        if target != self.target:
            self.target = target
            self.magnitude = max(abs(target - self.offset), MIN_OFFSET_MAGNITUDE)
        if self.offset != self.target:
            if not self.placed:
                self.offset = self.target
            else:
                local_agl = agl if agl is not None else altitude - local_ground
                window = ground_blend_window(on_ground, local_agl, self.target)
                self.offset = step_toward(self.offset, self.target,
                                          self.magnitude * max(dt, 0.0) / window)
        self.placed = True
        return max(altitude + self.offset, local_ground + model_height)

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

    def __init__(self, sim, definition_id=1000, on_frame=None):
        self.sim = sim
        self.definition_id = definition_id
        # 模拟器每画一帧调一次（在 SimConnect 的 dispatch 线程上），只该做
        # 很轻的事：InjectionLoop.frame 只记个时间、置个事件。
        self.on_frame = on_frame
        self.frames_subscribed = False
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

        # 地面数据。dispatch 线程写、注入线程读，都是单个键的赋值/读取，
        # CPython 里是原子的，不上锁——每帧四十次读，别让它和 dispatch 抢锁。
        self._ground_ready = False
        self._height_ready = False
        self._data_requests = {}     # requestID -> ("ground"|"height", objectID)
        self._ground_requests = {}   # objectID -> 地面标高的 requestID
        self._ground = {}            # objectID -> GROUND ALTITUDE（英尺）
        self._model_height = {}      # objectID -> STATIC CG TO GROUND（英尺）
        self._ground_logged = False
        # 起落架/襟翼/前轮：字段名 -> 定义号；被拒过的字段从这里拿掉
        self._surface_definitions = {}
        self._surface_packets = {}   # 包号 -> 字段名
        # 机型还没问到、先等一等的飞机：呼号 -> 第一次想建它的时刻
        self._waiting_for_type = {}
        # 每帧复用的缓冲区，不在每架每帧上新建 ctypes 数组
        self._position_values = (ctypes.c_double * len(_Definition.FIELDS))()
        self._one_value = (ctypes.c_double * 1)()
        # SIMOBJECT_DATA 里数据区的偏移，_install_dispatch 里定
        self._data_offset = None

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
        # 下面三样都不影响位置写入：失败只记日志，飞机照样建、照样动
        self._define_ground()
        self._define_surfaces()
        self._subscribe_frames()

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
        received = enums.SIMCONNECT_RECV_ID
        # 这两个号在 SimConnect.h 里是固定的；测试替身的枚举里不一定有
        frame_kind = int(getattr(received, "SIMCONNECT_RECV_ID_EVENT_FRAME", 7))
        data_kind = int(getattr(received, "SIMCONNECT_RECV_ID_SIMOBJECT_DATA", 8))
        event_struct = getattr(enums, "SIMCONNECT_RECV_EVENT", None)
        data_struct = getattr(enums, "SIMCONNECT_RECV_SIMOBJECT_DATA", None)
        if data_struct is not None:
            self._data_offset = data_struct.dwData.offset

        def dispatch(pData, cbData, pContext):
            try:
                kind = pData.contents.dwID
                # 我们自己订的 Frame 和自己要的地面数据：处理完就返回，不交给
                # 包的处理——它不认识这两种消息，只会在 else 分支里打一条
                # 参数不对的 DEBUG 日志（--debug 时每帧一条 logging 报错）。
                if kind == frame_kind and event_struct is not None:
                    event = ctypes.cast(pData, ctypes.POINTER(event_struct)).contents
                    if event.uEventID == FRAME_EVENT_ID:
                        callback = self.on_frame
                        if callback is not None:
                            callback()
                        return None
                elif kind == data_kind and data_struct is not None:
                    body = ctypes.cast(pData, ctypes.POINTER(data_struct)).contents
                    if self._note_data(body):
                        return None
                elif (kind ==
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
        if self._note_surface_exception(body, code):
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

    def _define_ground(self):
        """地面标高和模型离地高度的数据定义。只需要做一次。

        MSFS 没有 XPLMProbeTerrain，只能让模拟器报每个 AI 对象脚下的
        GROUND ALTITUDE。两个量分开定义：请求里有一个字段读不到，整条请求
        都会失败，STATIC CG TO GROUND 拖累了地面标高就不划算了。
        """
        float64 = self._enums.SIMCONNECT_DATATYPE.SIMCONNECT_DATATYPE_FLOAT64
        unused = self._enums.SIMCONNECT_UNUSED
        for offset, name, flag in (
                (GROUND_DEFINITION_OFFSET, b"GROUND ALTITUDE", "_ground_ready"),
                (HEIGHT_DEFINITION_OFFSET, b"STATIC CG TO GROUND", "_height_ready")):
            error = self._call("AddToDataDefinition", self.sim.hSimConnect,
                               self.definition_id + offset, name, b"feet",
                               float64, 0, unused)
            if error:
                log.warning("could not define %s, injected traffic will not "
                            "follow the local ground: %s", name.decode(), error)
                return
            setattr(self, flag, True)

    def _define_surfaces(self):
        """起落架/襟翼/前轮，每个字段一个数据定义。"""
        float64 = self._enums.SIMCONNECT_DATATYPE.SIMCONNECT_DATATYPE_FLOAT64
        unused = self._enums.SIMCONNECT_UNUSED
        for index, (key, name, unit) in enumerate(SURFACE_FIELDS):
            definition = self.definition_id + SURFACE_DEFINITION_OFFSET + index
            error = self._call("AddToDataDefinition", self.sim.hSimConnect,
                               definition, name, unit, float64, 0, unused)
            if error:
                log.warning("could not define %s, it will not be written on "
                            "injected traffic: %s", name.decode(), error)
                continue
            self._surface_definitions[key] = definition

    def _subscribe_frames(self):
        """订阅模拟器的 "Frame" 系统事件，让注入跟着模拟器的帧走。"""
        error = self._call("SubscribeToSystemEvent", self.sim.hSimConnect,
                           FRAME_EVENT_ID, b"Frame")
        if error:
            log.warning("could not subscribe to the simulator's Frame event, "
                        "traffic injection stays on its %.0f Hz timer: %s",
                        FRAME_RATE, error)
            return
        self.frames_subscribed = True

    def _note_data(self, body):
        """我们要的地面数据回来了。是我们的就记下并返回 True。"""
        request = self._data_requests.get(int(body.dwRequestID))
        if request is None or self._data_offset is None:
            return False
        kind, object_id = request
        value = ctypes.c_double.from_address(
            ctypes.addressof(body) + self._data_offset).value
        if kind == "ground":
            self._ground[object_id] = value
            if not self._ground_logged:
                self._ground_logged = True
                log.info("the simulator reports the ground under injected "
                         "traffic (object %d: %.0f ft)", object_id, value)
        else:
            if 0.0 <= value <= MAX_MODEL_HEIGHT:
                self._model_height[object_id] = value
            else:
                self._model_height[object_id] = 0.0
            log.debug("object %d sits %.1f ft above the ground (STATIC CG TO "
                      "GROUND)", object_id, value)
        return True

    def _request_ground(self, callsign, object_id):
        """给刚认领的对象要地面标高（持续）和模型离地高度（一次）。"""
        if not self._ground_ready:
            return
        requests = [("ground", GROUND_DEFINITION_OFFSET, PERIOD_SIM_FRAME,
                     GROUND_INTERVAL, "the ground elevation request")]
        if self._height_ready:
            requests.append(("height", HEIGHT_DEFINITION_OFFSET, PERIOD_ONCE, 0,
                             "the model height request"))
        for kind, offset, period, interval, what in requests:
            request_id = self._request_id()
            # 先登记再发：回复可能在 DLL 调用返回之前就到了 dispatch 线程
            self._data_requests[request_id] = (kind, object_id)
            error = self._call("RequestDataOnSimObject", self.sim.hSimConnect,
                               request_id, self.definition_id + offset,
                               object_id, period, 0, 0, interval, 0)
            if error:
                self._data_requests.pop(request_id, None)
                log.warning("could not request %s for %s (object %d): %s",
                            what.replace("the ", "", 1), callsign, object_id,
                            error)
                continue
            self._remember_packet(callsign, object_id, what)
            if kind == "ground":
                self._ground_requests[object_id] = request_id

    def _forget_ground(self, object_id):
        """对象删掉之前，停掉它的地面请求、丢掉它的地面数据。"""
        request_id = self._ground_requests.pop(object_id, None)
        if request_id is not None:
            self._call("RequestDataOnSimObject", self.sim.hSimConnect,
                       request_id,
                       self.definition_id + GROUND_DEFINITION_OFFSET,
                       object_id, PERIOD_NEVER, 0, 0, 0, 0)
        for rid in [rid for rid, (_, oid) in self._data_requests.items()
                    if oid == object_id]:
            self._data_requests.pop(rid, None)
        self._ground.pop(object_id, None)
        self._model_height.pop(object_id, None)

    def _note_surface_exception(self, body, code):
        """起落架/襟翼/前轮的写入被拒：记一次，以后不再写这个字段。"""
        send_id = getattr(body, "UNKNOWN_SENDID", None)
        try:
            send_id = int(send_id)
        except (TypeError, ValueError):
            return False
        with self._lock:
            key = self._surface_packets.pop(send_id, None)
        if key is None:
            return False
        if self._surface_definitions.pop(key, None) is not None:
            try:
                name = self._enums.SIMCONNECT_EXCEPTION(code).name
            except Exception:
                name = str(code)
            simvar = dict((k, n) for k, n, _ in SURFACE_FIELDS)[key].decode()
            log.warning("the simulator refused writing %s on injected traffic "
                        "(%s); it will not be written again", simvar, name)
        return True

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
    def sync(self, entries, now=None):
        """让模拟器里的 AI 飞机和这份快照一致。

        entries 是 traffic.TrafficTable.snapshot() 的输出，已经按距离排好序。
        now 是单调时钟（perf_counter）的秒数，地面贴合和等机型都按它算。
        """
        if not self.available:
            return
        now = time.perf_counter() if now is None else now
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
                    self._sync_one(callsign, entry, now)
                except Exception as e:
                    log.debug("updating %s raised: %s", callsign, e)

            for callsign in [c for c in list(self.aircraft) if c not in seen]:
                self.remove(callsign)
            if self._waiting_for_type:
                for callsign in [c for c in self._waiting_for_type if c not in seen]:
                    del self._waiting_for_type[callsign]

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
            self._request_ground(callsign, object_id)
        for _, object_id in strays:
            try:
                self.sim.dll.AIRemoveObject(self.sim.hSimConnect, object_id,
                                            self._request_id())
                log.info("swept up an aircraft that was no longer needed (object %d)",
                         object_id)
            except Exception as e:
                log.debug("the traffic sweep raised: %s", e)

    def _sync_one(self, callsign, entry, now=None):
        now = time.perf_counter() if now is None else now
        record = self.aircraft.get(callsign)
        title = entry.get("model") or ""

        if record is not None and title and record.get("title") != title:
            # 机型问到了或者变了，换模型只能删了重建
            self.remove(callsign)
            record = None

        if record is None:
            if not title:
                return          # 还没匹配到模型，等下一轮
            if not self._type_known_or_waited(callsign, entry, now):
                return          # 机型还没问到，再等等，免得建了又换
            self._waiting_for_type.pop(callsign, None)
            self._create(callsign, entry, title)
            return

        object_id = record.get("object_id")
        if object_id is None:
            return              # 还在等 objectID
        self._move(object_id, entry, self._ground_altitude(callsign, record,
                                                           object_id, entry, now))
        if self._surface_definitions:
            self._write_surfaces(callsign, record, object_id, entry)

    def _type_known_or_waited(self, callsign, entry, now):
        """机型问到了，或者已经等够 MODEL_WAIT 了。"""
        if entry.get("equipment") or entry.get("csl"):
            return True
        first = self._waiting_for_type.setdefault(callsign, now)
        return now - first >= MODEL_WAIT

    def _ground_altitude(self, callsign, record, object_id, entry, now):
        """要写进模拟器的高度：贴合本地地面之后的。没有地面数据就原样。"""
        altitude = entry["altitude"]
        if not self._ground_ready:
            return altitude
        local_ground = self._ground.get(object_id)
        clamp = record.get("clamp")
        if clamp is None:
            clamp = record["clamp"] = GroundClamp()
        last = record.get("clamp_time")
        record["clamp_time"] = now
        dt = now - last if last is not None else 0.0
        adjusted = clamp.update(now, dt, altitude, entry.get("agl"),
                                bool(entry.get("on_ground")), local_ground,
                                self._model_height.get(object_id, 0.0))
        if clamp.rejected and not record.get("ground_rejected"):
            record["ground_rejected"] = True
            log.warning("the simulator's ground under %s (object %d) is %.0f ft, "
                        "more than %.0f ft from the sender's; not using it",
                        callsign, object_id, local_ground, MAX_SCENERY_DIFFERENCE)
        return adjusted

    def _write_surfaces(self, callsign, record, object_id, entry):
        """起落架/襟翼/前轮：值变了才写，每个字段一次 SetDataOnSimObject。"""
        on_ground = bool(entry.get("on_ground"))
        gear = entry.get("gear_down")
        if gear is None and on_ground:
            gear = True
        flaps = entry.get("flaps")
        wheel = entry.get("nose_wheel") or 0.0
        wheel = max(-1.0, min(1.0, wheel / NOSE_WHEEL_FULL_DEFLECTION))
        wanted = {
            "gear": None if gear is None else (1.0 if gear else 0.0),
            "flaps_left": flaps,
            "flaps_right": flaps,
            "nose_wheel": wheel if on_ground else 0.0,
        }
        written = record.get("surfaces")
        if written is None:
            written = record["surfaces"] = {}
        for key, definition in list(self._surface_definitions.items()):
            value = wanted.get(key)
            if value is None:
                continue
            value = float(value)
            last = written.get(key)
            if last is not None and abs(last - value) < SURFACE_EPSILON:
                continue
            self._one_value[0] = value
            error = self._call("SetDataOnSimObject", self.sim.hSimConnect,
                               definition, object_id, 0, 0,
                               ctypes.sizeof(self._one_value), self._one_value)
            written[key] = value
            if error:
                # 同步就被拒的话，异步那条大概也会来；这里只记一次、停写
                if self._surface_definitions.pop(key, None) is not None:
                    log.warning("could not write %s on injected traffic, it "
                                "will not be written again: %s",
                                dict((k, n) for k, n, _ in SURFACE_FIELDS)[key]
                                .decode(), error)
                continue
            packet = ctypes.c_ulong(0)
            if not self._call("GetLastSentPacketID", self.sim.hSimConnect,
                              ctypes.byref(packet)):
                with self._lock:
                    self._surface_packets[int(packet.value)] = key
                    while len(self._surface_packets) > CONTROL_PACKETS_KEPT:
                        self._surface_packets.pop(next(iter(self._surface_packets)))

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

    def _move(self, object_id, entry, altitude=None):
        # 缓冲区复用：SetDataOnSimObject 返回前就把数据拷走了，只有注入线程
        # 在 _op_lock 里写它。姿态取负：见 _create 里的注释
        values = self._position_values
        values[0] = entry["latitude"]
        values[1] = entry["longitude"]
        values[2] = entry["altitude"] if altitude is None else altitude
        values[3] = -entry.get("pitch", 0.0)
        values[4] = -entry.get("bank", 0.0)
        values[5] = entry.get("heading", 0.0)
        values[6] = float(entry.get("groundspeed", 0))
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
        if self._ground_ready:
            self._forget_ground(object_id)
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
    """在自己的线程上调 step()，把他机写进模拟器。

    **平时跟着模拟器的帧走。** TrafficInjector 订阅了 SimConnect 的 "Frame"
    系统事件，每帧在包的 dispatch 线程上调一次 frame()。frame() 只记个时间、
    置个事件；真正的积分和四十次 SetDataOnSimObject 在这条线程上做。放在
    dispatch 线程上做的话，objectID、EXCEPTION、地面标高和 simlink 读 SimVar
    的回复都得排在后面等。积压的几帧只唤醒一次，不会补跑。帧率高于
    MAX_FRAME_RATE 时隔帧写。

    **Frame 不来就退回定时器**（FRAME_RATE，30 Hz）：订阅失败、模拟器暂停
    （暂停时 MSFS 不发 Frame，可别人的飞机还在飞）都走这条。FRAME_TIMEOUT
    内又收到 Frame 就回到跟帧。切换时记一行日志。

    定时器用 time.sleep：Python 3.11 起它在 Windows 上用高精度等待计时器，
    Event.wait 超时仍是 15.6 ms 的系统粒度，33 ms 的周期会抖成 31/47。跟帧
    时用 Event.wait 没问题：被 set() 叫醒是立即的，粒度只影响超时。
    """

    def __init__(self, step, rate=FRAME_RATE, name="traffic-inject",
                 frame_timeout=FRAME_TIMEOUT, max_frame_rate=MAX_FRAME_RATE):
        self.step = step
        self.period = 1.0 / rate
        self.name = name
        self.frame_timeout = frame_timeout
        # 留一成余量：60 fps 下帧间隔抖到 16.5 ms 也不该被当成"太快"跳过
        self.min_frame_interval = 0.9 / max_frame_rate
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._last_frame = float("-inf")
        self._thread = None
        self.mode = None            # "frame" / "timer"，线程跑起来之后才有

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def frame(self):
        """模拟器画了一帧。在 SimConnect 的 dispatch 线程上调，只做两件小事。"""
        self._last_frame = time.perf_counter()
        self._wake.set()

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
        self._wake.set()            # 正在等下一帧的话，别让它等满超时
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                log.warning("the traffic injection loop did not stop within %.1f s",
                            timeout)
        self._thread = None
        self.mode = None

    def _set_mode(self, mode):
        if mode == self.mode:
            return
        self.mode = mode
        if mode == "frame":
            log.info("traffic injection follows the simulator's frames")
        else:
            log.info("traffic injection runs on its own %.0f Hz timer (no frame "
                     "events from the simulator)", 1.0 / self.period)

    def _step(self):
        try:
            self.step()
        except Exception as e:
            log.warning("injecting traffic into the simulator raised: %s", e)

    def _run(self, stop):
        deadline = time.perf_counter()
        last_step = float("-inf")
        while not stop.is_set():
            now = time.perf_counter()
            if now - self._last_frame < self.frame_timeout:
                self._set_mode("frame")
                woke = self._wake.wait(self.frame_timeout)
                if stop.is_set():
                    break
                if not woke:
                    continue        # 下一轮看到 Frame 断了，退回定时器
                self._wake.clear()
                now = time.perf_counter()
                if now - last_step < self.min_frame_interval:
                    continue
                last_step = now
                self._step()
                deadline = time.perf_counter()
                continue

            self._set_mode("timer")
            last_step = now
            self._step()
            deadline += self.period
            now = time.perf_counter()
            if deadline < now:
                # 落后了（模拟器卡了一下）就从现在重新数，别连着补跑
                deadline = now
                continue
            time.sleep(deadline - now)
