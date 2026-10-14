"""取 METAR 报文。

只用标准库 urllib，不额外引入依赖。地址可以在设置里改——默认这个也是
can-fsd 自己在用的气象源（config.json 的 weather 项），所以和网络上其它
地方看到的天气是同一份。
"""

import logging
import os
import re
import ssl
import sys
import time
import urllib.request

import certifi

log = logging.getLogger("weather")

DEFAULT_METAR_URL = "https://metar.vatsim.net/metar.php?id="
_USER_AGENT = "can-atis"

# 气象源在 CDN 后面，偶发一次连接/TLS 抖动是常事。默认自动重试一次：一次抖动
# 不该让这个席位整整一个刷新周期（默认 300 秒）都没有天气。
RETRIES = 1
RETRY_DELAY = 1.0


def _ca_bundle():
    """返回随程序分发的 CA bundle，源码运行时回退到 certifi。"""
    packaged_bundle = os.path.join(
        getattr(sys, "_MEIPASS", ""), "certifi", "cacert.pem")
    if os.path.isfile(packaged_bundle):
        return packaged_bundle
    return certifi.where()


def _ssl_context():
    return ssl.create_default_context(cafile=_ca_bundle())


class WeatherError(Exception):
    pass


def _explain(error):
    """把底层异常翻译成能照着查的说法。

    urlopen 抛出来的原文是给程序员看的。证书校验失败尤其容易被误读成"服务器
    坏了"——实测遇到过一次 certificate has expired，而服务器证书本身好好的，
    几分钟后自己就恢复了。真要排查，能动的就是本机时间和系统根证书。
    """
    text = str(error)
    if "CERTIFICATE_VERIFY_FAILED" in text:
        return (f"{text}；证书校验没过。服务器证书通常没问题，先看本机时间对不对、"
                f"系统根证书是不是太旧，或者有没有中间人代理")
    return text


def normalize(line, icao):
    """去掉 METAR/SPECI 前缀，确认确实是这个机场的报文。"""
    line = line.strip()
    for prefix in ("METAR ", "SPECI "):
        if line.startswith(prefix):
            line = line[len(prefix):].strip()
    return line if line.startswith(icao) else None


def fetch_metar(icao, url=None, timeout=15, retries=None):
    """取一份原始 METAR 电码。失败抛 WeatherError。

    网络和 TLS 层面的失败会重试 `retries` 次——ICAO 写错这种不会，那重试多少
    遍都是一样的结果。
    """
    icao = (icao or "").strip().upper()
    if not re.match(r'^[A-Z]{4}$', icao):
        raise WeatherError(f"{icao} 不是 4 位 ICAO 代码")

    if retries is None:
        retries = RETRIES
    target = (url or DEFAULT_METAR_URL) + icao
    request = urllib.request.Request(target, headers={"User-Agent": _USER_AGENT})

    text = None
    last_error = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(
                    request, timeout=timeout, context=_ssl_context()) as response:
                text = response.read().decode("utf-8", errors="replace")
            break
        except Exception as e:
            last_error = e
            # 日志里留下地址：换过气象源之后，"取不到天气"到底是谁的问题，
            # 光看报错文字是分不出来的
            log.warning("fetching the METAR for %s failed (attempt %d/%d, %s): %s",
                        icao, attempt + 1, retries + 1, target, e)
            if attempt < retries:
                time.sleep(RETRY_DELAY)
    if text is None:
        raise WeatherError(f"取 {icao} 的 METAR 失败: {_explain(last_error)}")

    for line in text.splitlines():
        normalized = normalize(line, icao)
        if normalized:
            return normalized
    raise WeatherError(f"气象源里没有 {icao} 的报文")
