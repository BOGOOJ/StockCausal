# -*- coding: utf-8 -*-
"""东方财富数据接口封装（datacenter 通用查询 + 全 A 股名单）。"""
import urllib.parse

import net

_BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_REF = "https://data.eastmoney.com/"
_CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
_CLIST_REF = "https://quote.eastmoney.com/"
_UNIVERSE_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"


def _query(report, date_col, date, sort_cols=None, page_size=500, max_pages=60):
    """按日期取一张报表的全部分页。

    注意：东财每个报表的排序字段必须和它的日期列同名，否则会返回
    success=false「排序列表不存在」。所以默认用 date_col 当排序列。
    """
    sort_cols = sort_cols or date_col
    filt = "(%s='%s')" % (date_col, date)
    rows = []
    page = 1
    while page <= max_pages:
        url = ("%s?reportName=%s&columns=ALL&pageNumber=%d&pageSize=%d"
               "&sortColumns=%s&sortTypes=-1&filter=%s"
               % (_BASE, report, page, page_size, sort_cols, net.quote(filt)))
        data = net.get_json(url, referer=_REF, kind="em")
        result = (data or {}).get("result")
        if not result:
            break
        rows.extend(result.get("data") or [])
        pages = result.get("pages") or 1
        if page >= pages:
            break
        page += 1
    return rows


def _num(value, digits=2, scale=1.0):
    if value is None or value == "":
        return None
    try:
        return round(float(value) / scale, digits)
    except (TypeError, ValueError):
        return None


def _day(value):
    return (value or "")[:10]


def longhu(date):
    """龙虎榜个股（全量，含上榜原因）。金额单位：万元。"""
    rows = _query("RPT_DAILYBILLBOARD_DETAILSNEW", "TRADE_DATE", date)
    out = []
    for item in rows:
        out.append({
            "交易日期": _day(item.get("TRADE_DATE")),
            "代码": item.get("SECURITY_CODE"),
            "名称": item.get("SECURITY_NAME_ABBR"),
            "现价": _num(item.get("CLOSE_PRICE")),
            "涨跌幅": _num(item.get("CHANGE_RATE")),
            "成交金额(亿元)": _num(item.get("ACCUM_AMOUNT"), 4, 1e8),
            "龙虎榜净买入额(万元)": _num(item.get("BILLBOARD_NET_AMT"), 2, 1e4),
            "龙虎榜买入额(万元)": _num(item.get("BILLBOARD_BUY_AMT"), 2, 1e4),
            "龙虎榜卖出额(万元)": _num(item.get("BILLBOARD_SELL_AMT"), 2, 1e4),
            "上榜原因": item.get("EXPLANATION") or "",
            "解读": item.get("EXPLAIN") or "",
            "数据来源": "东财",
        })
    return out


def longhu_seats(date):
    """龙虎榜买卖前五营业部。金额单位：万元。"""
    out = []
    for report, direction, sort_field in (
        ("RPT_BILLBOARD_DAILYDETAILSBUY", "买入", "BUY"),
        ("RPT_BILLBOARD_DAILYDETAILSSELL", "卖出", "SELL"),
    ):
        rows = _query(report, "TRADE_DATE", date)
        groups = {}
        for item in rows:
            key = (item.get("SECURITY_CODE"), item.get("EXPLANATION"))
            groups.setdefault(key, []).append(item)
        for (_code, _why), items in groups.items():
            items.sort(key=lambda x: -(x.get(sort_field) or 0))
            for rank, item in enumerate(items[:5], 1):
                out.append({
                    "交易日期": _day(item.get("TRADE_DATE")),
                    "代码": item.get("SECURITY_CODE"),
                    "名称": "",
                    "方向": direction,
                    "排名": rank,
                    "营业部": item.get("OPERATEDEPT_NAME") or "",
                    "买入额(万元)": _num(item.get("BUY"), 2, 1e4),
                    "卖出额(万元)": _num(item.get("SELL"), 2, 1e4),
                    "净额(万元)": _num(item.get("NET"), 2, 1e4),
                    "上榜原因": item.get("EXPLANATION") or "",
                    "数据来源": "东财",
                })
    return out


def rzrq_stock(date):
    """个股两融明细（全量约 4000+ 只）。金额单位：元。"""
    rows = _query("RPTA_WEB_RZRQ_GGMX", "DATE", date)
    out = []
    for item in rows:
        out.append({
            "交易日期": _day(item.get("DATE")),
            "代码": item.get("SCODE"),
            "名称": item.get("SECNAME"),
            "市场": item.get("MARKET"),
            "融资余额(元)": _num(item.get("RZYE")),
            "融资买入额(元)": _num(item.get("RZMRE")),
            "融资偿还额(元)": _num(item.get("RZCHE")),
            "融资净买入(元)": _num(item.get("RZJME")),
            "融券余量(股)": _num(item.get("RQYL")),
            "融券余额(元)": _num(item.get("RQYE")),
            "融资融券余额(元)": _num(item.get("RZRQYE")),
            "数据来源": "东财",
        })
    return out


def dzjy(date):
    """大宗交易（全量）。东财按买卖双方配对成笔。"""
    rows = _query("RPT_DATA_BLOCKTRADE", "TRADE_DATE", date)
    out = []
    for item in rows:
        out.append({
            "交易日期": _day(item.get("TRADE_DATE")),
            "代码": item.get("SECURITY_CODE"),
            "名称": item.get("SECURITY_NAME_ABBR"),
            "成交价(元)": _num(item.get("DEAL_PRICE")),
            "成交量(万股)": _num(item.get("DEAL_VOLUME"), 2, 1e4),
            "成交额(万元)": _num(item.get("DEAL_AMT"), 2, 1e4),
            "溢价率": _num(item.get("PREMIUM_RATIO")),
            "收盘价(元)": _num(item.get("CLOSE_PRICE")),
            "买方营业部": item.get("BUYER_NAME") or "",
            "卖方营业部": item.get("SELLER_NAME") or "",
            "数据来源": "东财",
        })
    return out


def universe():
    """全 A 股名单（含部分退市股），返回 [{代码,名称,市场,上市日期}]。"""
    out = []
    page = 1
    total = None
    while page <= 80:
        url = ("%s?pn=%d&pz=100&po=0&np=1&fltt=2&invt=2&fid=f12&fs=%s&fields=f12,f14,f13,f26"
               % (_CLIST, page, net.quote(_UNIVERSE_FS)))
        data = net.get_json(url, referer=_CLIST_REF, kind="em")
        block = (data or {}).get("data") or {}
        diff = block.get("diff") or []
        if total is None:
            total = block.get("total") or 0
        if not diff:
            break
        for item in diff:
            out.append({
                "代码": item.get("f12"),
                "名称": item.get("f14"),
                "市场": "sh" if item.get("f13") == 1 else "sz",
                "上市日期": str(item.get("f26") or ""),
            })
        if len(out) >= total:
            break
        page += 1
    return out


def kline(secid, fqt=0):
    """单只日线（备用源）。返回 [(日期, 开, 高, 低, 收, 量手, 涨跌幅)]。"""
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
           "&fields1=f1,f2,f3,f4,f5,f6"
           "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
           "&klt=101&fqt=%d&beg=0&end=20500101" % (secid, fqt))
    data = net.get_json(url, referer=_CLIST_REF, kind="em")
    block = (data or {}).get("data") or {}
    out = []
    for line in block.get("klines") or []:
        parts = line.split(",")
        if len(parts) < 9:
            continue
        out.append({
            "日期": parts[0],
            "开盘": float(parts[1]),
            "收盘": float(parts[2]),
            "最高": float(parts[3]),
            "最低": float(parts[4]),
            "成交量(手)": float(parts[5]),
            "收盘_后复权": None,
            "涨跌幅": float(parts[8]) if parts[8] not in ("", "-") else None,
        })
    return out


def secid(code):
    """东财 secid：沪市 1.xxxxxx，深市/北交所 0.xxxxxx。"""
    return ("1." if code[0] in "56" else "0.") + code
