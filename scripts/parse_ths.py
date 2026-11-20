# -*- coding: utf-8 -*-
"""同花顺数据中心页面解析（龙虎榜 / 两融 / 大宗）。

实测要点（2026-11-19）：
- 三个页面都服务端直出，不需要 hexin-v cookie，编码 GBK。
- 龙虎榜主表：一张只有表头的表 + 一张数据表；数据表首列是「3日」这类上榜标记，
  只在每只股票第一条有值，其余为空。
- 龙虎榜席位：每只股票一个 <div class="stockcont" stockcode="002463">，
  里面两张 m-table-nosort 分别是「买入金额最大的前5名营业部」和卖出榜；
  上榜原因写在 <p>名称(代码)明细：…</p>。
- 两融页：第 1 张表是市场总量（两级表头 × 沪/深/北/合计），第 2 张表只有前 50 名。
- 大宗页：只给「最近 50 笔」，会跨日截断，只用来核对。
"""
import re

_DATE_INPUT = re.compile(r'class="m_text_date startday"[^>]*value="(\d{4}-\d{2}-\d{2})"')
_DATE_REPORT = re.compile(r'report="(\d{4}-\d{2}-\d{2})"')
_TABLE = re.compile(r"<table([^>]*)>(.*?)</table>", re.S)
_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
_TAG = re.compile(r"<[^>]+>")
_STOCKCONT = re.compile(
    r'<div class="stockcont"\s+stockcode="(\d{6})"(.*?)(?=<div class="stockcont"|<div class="footer|$)',
    re.S,
)
_NUM = re.compile(r"^(-?[\d.]+)\s*(亿|万)?$")


def strip_tags(html):
    text = _TAG.sub("", html or "")
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", text).strip()


def _cells(row_html):
    return [strip_tags(c) for c in _CELL.findall(row_html)]


def _tables(html):
    """[(table 属性, [[单元格, ...], ...]), ...]"""
    out = []
    for attr, body in _TABLE.findall(html or ""):
        rows = [_cells(r) for r in _ROW.findall(body)]
        rows = [r for r in rows if r]
        out.append((attr.strip(), rows))
    return out


def num(text, unit=None):
    """把 '13.24亿' / '213.40万' / '4.73%' / '-' 转成数字。

    unit='yi' 时统一成亿元，unit='wan' 时统一成万元，None 时为原数。
    """
    text = strip_tags(text).replace(",", "").replace("%", "")
    if text in ("", "-", "--", "—", "None"):
        return None
    match = _NUM.match(text)
    if not match:
        try:
            return float(text)
        except ValueError:
            return None
    value = float(match.group(1))
    src = match.group(2)
    if unit == "yi":
        return round(value / 10000.0, 4) if src == "万" else round(value, 4)
    if unit == "wan":
        return round(value * 10000.0, 2) if src == "亿" else round(value, 2)
    return value


def find_date(html):
    match = _DATE_INPUT.search(html or "")
    if match:
        return match.group(1)
    match = _DATE_REPORT.search(html or "")
    return match.group(1) if match else None


def parse_longhu(html):
    """返回 {'date', 'stocks', 'seats'}。列表和席位都来自同一页。"""
    date = find_date(html)
    stocks = []
    for _attr, rows in _tables(html):
        if len(rows) < 10:
            continue
        first = rows[0]
        if len(first) < 7 or not re.fullmatch(r"\d{6}", first[1] or ""):
            continue
        for index, cells in enumerate(rows, 1):
            if len(cells) < 7:
                continue
            stocks.append({
                "序号": index,
                "上榜标记": cells[0],
                "代码": cells[1],
                "名称": cells[2],
                "现价": num(cells[3]),
                "涨跌幅": num(cells[4]),
                "成交金额(亿元)": num(cells[5], "yi"),
                "龙虎榜净买入额(万元)": num(cells[6], "wan"),
            })
        break

    seats = []
    reasons = {}
    for code, block in _STOCKCONT.findall(html):
        reason_match = re.search(r"明细：([^<]+)</p>", block)
        if reason_match:
            reasons.setdefault(code, strip_tags(reason_match.group(1)))
        for _attr, rows in _tables(block):
            if not rows:
                continue
            head = " ".join(rows[0])
            if "买入金额最大" in head:
                direction = "买入"
            elif "卖出金额最大" in head:
                direction = "卖出"
            else:
                continue
            rank = 0
            for cells in rows[1:]:
                if len(cells) < 4 or not cells[0]:
                    continue
                rank += 1
                seats.append({
                    "代码": code,
                    "方向": direction,
                    "排名": rank,
                    "营业部": cells[0],
                    "买入额(万元)": num(cells[1]),
                    "卖出额(万元)": num(cells[2]),
                    "净额(万元)": num(cells[3]),
                })

    for row in stocks:
        row["上榜原因"] = reasons.get(row["代码"], "")
    for row in seats:
        row["上榜原因"] = reasons.get(row["代码"], "")
    return {"date": date, "stocks": stocks, "seats": seats}


_AREAS = ("上海", "深圳", "北京", "合计")
_GROUPS = (
    ("本日融资余额", "融资余额"),
    ("本日融资买入额", "融资买入额"),
    ("本日融券余量余额", "融券余额"),
    ("本日融资融券余额", "融资融券余额"),
)


def parse_rzrq_market(html):
    """返回市场两融总量：每天一行，16 个数（4 指标 × 沪/深/北/合计），单位亿元。"""
    for _attr, rows in _tables(html):
        if len(rows) < 11 or len(rows[0]) != 5:
            continue
        if "交易日期" not in rows[0][0]:
            continue
        area_row = rows[1]
        if len(area_row) != 16:
            continue
        body = []
        for cells in rows[2:]:
            if not cells or not re.match(r"\d{4}-\d{2}-\d{2}", cells[0]):
                continue
            if len(cells) < 17:
                continue
            row = {"交易日期": cells[0]}
            for col in range(16):
                group = _GROUPS[col // 4][1]
                area = area_row[col] if col < len(area_row) else _AREAS[col % 4]
                row["%s_%s(亿元)" % (group, area)] = num(cells[col + 1])
            body.append(row)
        return body
    return []


_DZJY_FIELDS = ("序号", "交易日期", "代码", "名称", "最新价", "成交价(元)",
                "成交量(万股)", "溢价率", "买方营业部", "卖方营业部")


def parse_dzjy(html):
    """大宗交易页（最近 50 笔，跨日）。"""
    for _attr, rows in _tables(html):
        if len(rows) < 40:
            continue
        if "交易日期" not in " ".join(rows[0]):
            continue
        out = []
        for cells in rows[1:]:
            if len(cells) < 10 or not re.match(r"\d{4}-\d{2}-\d{2}", cells[1]):
                continue
            out.append({
                "序号": num(cells[0]),
                "交易日期": cells[1],
                "代码": cells[2],
                "名称": cells[3],
                "最新价": num(cells[4]),
                "成交价(元)": num(cells[5]),
                "成交量(万股)": num(cells[6]),
                "溢价率": num(cells[7]),
                "买方营业部": cells[8],
                "卖方营业部": cells[9],
            })
        return out
    return []
