# -*- coding: utf-8 -*-
"""网络访问层：绕开系统代理、限速、重试、编码自适应。

本机实测坑：系统代理指向 127.0.0.1:7897，但代理软件没开时，
urllib 会直接报 10061 连接拒绝。这里统一用空 ProxyHandler 直连。
"""
import json
import random
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=_CTX),
)

# 同花顺必须慢（实测无间隔连打会 502/504），其它源可以快一点
_MIN_GAP = {"ths": 1.5, "kline": 1.5, "em": 0.35, "clist": 1.2, "sse": 0.4, "szse": 0.5}
_last_hit = {}
RETRY_WAITS = (1, 3, 7)


class NetError(Exception):
    """取数失败（重试用尽或不可重试）。"""


def _sleep_gap(kind):
    gap = _MIN_GAP.get(kind, 1.0)
    wait = gap - (time.time() - _last_hit.get(kind, 0.0))
    if wait > 0:
        time.sleep(wait + random.uniform(0, 0.3))


def fetch(url, referer=None, kind="ths", timeout=30, retries=3, allow_fail=False):
    """取回原始字节；502/504/超时/连接重置按 1s/3s/7s 退避重试。"""
    headers = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Connection": "close",
    }
    if referer:
        headers["Referer"] = referer
    last_err = None
    for attempt in range(retries):
        _sleep_gap(kind)
        try:
            req = urllib.request.Request(url, headers=headers)
            with _OPENER.open(req, timeout=timeout) as resp:
                body = resp.read()
            _last_hit[kind] = time.time()
            return body
        except urllib.error.HTTPError as exc:
            _last_hit[kind] = time.time()
            last_err = "HTTP %s %s" % (exc.code, exc.reason)
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)])
                continue
        except Exception as exc:
            _last_hit[kind] = time.time()
            last_err = "%s: %s" % (type(exc).__name__, exc)
            if attempt < retries - 1:
                time.sleep(RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)])
                continue
    if allow_fail:
        return None
    raise NetError("%s <-- %s" % (last_err, url))


def get_json(url, referer=None, kind="em", **kwargs):
    raw = fetch(url, referer=referer, kind=kind, **kwargs)
    if raw is None:
        return None
    return json.loads(raw.decode("utf-8", "replace"))


def decode(raw, prefer="gbk"):
    if raw is None:
        return ""
    for enc in (prefer, "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def quote(text):
    return urllib.parse.quote(text, safe="")


def proxy_hint():
    """系统代理开着但可能连不通时，给一句人话提示（程序已自动直连）。"""
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        return ""
    proxy = proxies.get("https") or proxies.get("http")
    if not proxy:
        return ""
    return ("提示：系统代理设置为 %s，本程序已自动绕过直连；"
            "若仍然取不到数据，请检查代理软件或网络。" % proxy)
