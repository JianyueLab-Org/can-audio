"""从本网数据源（can-fsd 的 /v1/data.json）读两样东西。

1. **本人此刻在管什么席位** —— 管制员在 EuroScope 那边上了席位之后，语音这边
   不该再手动敲一遍频率。按 CID 在 `controllers[]` 里找自己，把频率捡回来。
2. **CID 到呼号的对照表** —— Mumble 的用户名就是纯数字的 CAN 号（server/login.py
   就是这么发 id 的），所以语音层报上来的"谁在说话"是个数字。"最后通话: 1005"
   对管制员没有任何意义，要显示成 "CES2345"。

字段名对过 can-fsd 的 internal/api/datafeed.go：`pilots[]` 和 `controllers[]`
每项都有 `cid` 和 `callsign`，`controllers[]` 另有 `frequency`（整数兆赫字符串，
如 "128.500"）和 `facility`。

只用标准库 urllib——这个客户端不该为了查个呼号引入 requests。
"""

import json
import logging
import urllib.request

import radiostack

log = logging.getLogger("datafeed")

DEFAULT_DATAFEED_URL = "https://data.ceruleanavi.net/v1/data.json"

# 数据源前面挡着 Cloudflare，非浏览器形态的 User-Agent 一律 403
# （"can-controller"、"python-urllib/x.y" 都被拒）。带上 Mozilla 前缀才放行，
# 后面仍然标明自己是谁。
_USER_AGENT = "Mozilla/5.0 (compatible; CanController/1.0)"

FACILITY_OBSERVER = 0
# can-fsd 用这个频率表示"没设频率"。拿它去拼 FREQ_ 频道会得到一个谁也不在的
# 频道，所以必须挡掉。
NO_FREQUENCY_KHZ = 199998


def fetch(url=None, timeout=15):
    """拉取数据源。失败返回 None——查不到呼号不该影响通话。"""
    try:
        request = urllib.request.Request(url or DEFAULT_DATAFEED_URL,
                                         headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as e:
        log.warning("could not read the datafeed: %s", e)
        return None


def controllers_for(cid, data):
    """找这个 CID 此刻正在管的全部席位，按数据源顺序返回。"""
    cid = str(cid or "").strip()
    if not cid or not data:
        return []

    positions = []
    for entry in data.get("controllers") or []:
        if str(entry.get("cid", "")).strip() != cid:
            continue
        try:
            facility = int(entry.get("facility", 0))
        except (TypeError, ValueError):
            facility = 0
        if facility > FACILITY_OBSERVER:
            positions.append(entry)
    return positions


def controller_for(cid, data):
    """找这个 CID 的第一个管制席位，没有则返回 None。

    保留这个单席位查询供现有调用方使用；需要支持多席位时使用
    :func:`controllers_for`。
    """
    return next(iter(controllers_for(cid, data)), None)


def frequency_khz(entry):
    """把席位的 frequency 解析成整数千赫。不能用就返回 None。

    挡掉两种：199.998（can-fsd 的"没设频率"占位）和任何解析不出、或者不在甚高频
    范围里的值。宁可不自动加，也不要把人送进一个空频道。
    """
    if not entry:
        return None
    raw = entry.get("frequency")
    if raw in (None, ""):
        return None
    try:
        khz = radiostack.parse_frequency(raw)
    except (ValueError, TypeError, OverflowError):
        # OverflowError 不是假想的：json.loads 默认认裸的 Infinity，而
        # int(round(inf * 1000)) 抛的正是它，只接 ValueError 会让它一路
        # 冒到 Qt 槽函数里去
        log.warning("could not parse the frequency %r of position %s, skipping it",
                    raw, entry.get("callsign", "?"))
        return None
    if khz == NO_FREQUENCY_KHZ:
        return None
    return khz


def online_positions(data):
    """此刻在线的所有管制席位，[(呼号, 千赫, facility)]，按频率排序。

    判定和 controller_for 一致：席位类型要高于观察员，频率要能解析、且不是
    can-fsd 那个"没设频率"的占位。挂着观察员的、没设频率的，列出来只会让人
    去点一个谁也不在的频道。

    同一个频率上可能有多个席位（比如同一频率的主备），全都留着——列表是给人
    看的，合并掉反而看不出谁在上面。
    """
    found = []
    if not data:
        return found
    for entry in data.get("controllers") or []:
        try:
            facility = int(entry.get("facility", 0))
        except (TypeError, ValueError):
            facility = 0
        if facility <= FACILITY_OBSERVER:
            continue
        khz = frequency_khz(entry)
        if khz is None:
            continue
        callsign = str(entry.get("callsign", "")).strip()
        if not callsign:
            continue
        found.append((callsign, khz, facility))
    found.sort(key=lambda item: (item[1], item[0]))
    return found


def roster(data):
    """CID → 呼号。飞行员和管制席位都收进来。

    数据源里 cid 是字符串，这里也一律用字符串做键——Mumble 报上来的用户名同样
    是字符串，两边不统一的话查一辈子都查不到。
    """
    table = {}
    if not data:
        return table
    for group in ("pilots", "controllers", "atis"):
        for entry in data.get(group) or []:
            cid = str(entry.get("cid", "")).strip()
            callsign = str(entry.get("callsign", "")).strip()
            if cid and callsign:
                table[cid] = callsign
    return table
