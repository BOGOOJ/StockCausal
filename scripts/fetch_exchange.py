# -*- coding: utf-8 -*-
"""ETF 份额：上交所（支持历史）+ 深交所（只有当日）。

同花顺数据中心没有基金/ETF 板块，这两个交易所接口才是原始出处。

两个坑（实测）：
- 上交所这个接口的分页是坏的：pageNo=2 会原样返回第 1 页。所以一次性
  要 pageSize=2000 拿全，再按代码去重。
- 深交所接口没有日期参数，只能拿到「当前快照」，历史回补时只能跳过。
"""
import re
import time

import net

_SSE_ONE = ("https://query.sse.com.cn/commonQuery.do"
            "?sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L"
            "&isPagination=true&pageHelp.pageSize=2000&pageHelp.pageNo=1"
            "&pageHelp.beginPage=1&pageHelp.cacheSize=1&pageHelp.endPage=1%s")
_SSE_REF = "https://www.sse.com.cn/"
_SZSE = ("http://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON"
         "&CATALOGID=%s&TABKEY=tab1&PAGENO=%d")
_SZSE_REF = "http://www.szse.cn/"
_TAG = re.compile(r"<[^>]+>")


def _clean(value):
    return re.sub(r"\s+", " ", _TAG.sub("", value or "")).strip()


def _fnum(value):
    text = _clean(str(value)).replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def etf_sse(date):
    """上交所 ETF 份额（单位：万份）。date 支持历史。"""
    extra = "&STAT_DATE=%s" % date if date else ""
    data = net.get_json(_SSE_ONE % extra, referer=_SSE_REF, kind="sse")
    help_block = (data or {}).get("pageHelp") or {}
    items = help_block.get("data") or []
    rows = []
    seen = set()
    for item in items:
        code = item.get("SEC_CODE")
        if not code or code in seen:
            continue
        seen.add(code)
        rows.append({
            "交易日期": (item.get("STAT_DATE") or "")[:10],
            "交易所": "上交所",
            "代码": code,
            "名称": item.get("SEC_NAME"),
            "总份额(万份)": _fnum(item.get("TOT_VOL")),
            "类型": item.get("ETF_TYPE") or "",
            "管理人": "",
            "数据来源": "上交所",
        })
    return rows


def etf_szse():
    """深交所 ETF 列表（单位：万份）。只有当日快照，没有历史参数。"""
    rows = []
    page = 1
    while page <= 200:
        data = net.get_json(_SZSE % ("1945", page), referer=_SZSE_REF, kind="szse")
        if not isinstance(data, list) or not data:
            break
        block = data[0]
        meta = block.get("metadata") or {}
        items = block.get("data") or []
        if not items:
            break
        for item in items:
            rows.append({
                "交易日期": time.strftime("%Y-%m-%d"),
                "交易所": "深交所",
                "代码": _clean(item.get("sys_key")),
                "名称": _clean(item.get("kzjcurl")).split(" ")[0],
                "总份额(万份)": _fnum(item.get("dqgm")),
                "类型": "ETF",
                "管理人": _clean(item.get("glrmc")),
                "数据来源": "深交所",
            })
        page_count = meta.get("pagecount") or 1
        if page >= page_count:
            break
        page += 1
    return rows
