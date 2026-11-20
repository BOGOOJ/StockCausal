# -*- coding: utf-8 -*-
"""features.py —— 把「资金」算成可检验的指标。

口径（2026-11-20 与用户确认）：

1. **事件型指标留 NULL，不填 0**
   「今天没上榜」和「今天净买入额恰好是 0」是两件事，填 0 会把 98.6% 的样本挤进 0 档。
   所以另外单独给一个 is_on_longhu 哑变量。

2. **跨股票比较用「当日横截面分位」，跟自己比用「z-score」**
   这里只负责算出**原始值**；分位/z 化留给分析脚本（要用样本内数据算，避免未来泄漏）。

3. **窗口**
   T+1 主判定，T+1~T+3 只看持续性——由分析脚本负责。

产出：
    data\\features.csv          每行 = 一只股票一个交易日
    data\\market_features.csv   每行 = 一个交易日（市场层）

用法：
    python scripts\\features.py                 # 只算 watchlist 里的股票
    python scripts\\features.py --all-codes     # 全市场（会慢，文件大）
"""
import argparse
import csv
import os
import sqlite3
import sys
from datetime import datetime

import store

KLINE_DB = os.path.join(store.DATA_DIR, "kline.db")
FEATURE_CSV = os.path.join(store.DATA_DIR, "features.csv")
MARKET_CSV = os.path.join(store.DATA_DIR, "market_features.csv")
WATCHLIST = os.path.join(store.ROOT, "watchlist.txt")

COLUMNS = [
    "code", "date",
    "margin_balance_yuan",      # 融资余额
    "margin_growth_5",          # 融资余额 5 日增速
    "margin_growth_20",         # 融资余额 20 日增速
    "margin_net_intensity",     # 融资净买入 / 融资余额
    "is_on_longhu",             # 当天是否上龙虎榜（0/1）
    "lhb_net_ratio",            # 龙虎榜净买入额 / 当日成交额（仅上榜日为值）
    "lhb_inst_buy_cnt",         # 买方前 5 里「机构专用」家数 0~5
    "lhb_top5_buy_ratio",       # 买方前 5 买入额合计 / 当日成交额
    "block_discount",           # 大宗平均溢价率 %
    "block_amt_wan",            # 大宗成交额合计（万元）
]


def read_pool(all_codes=False):
    codes = set()
    with sqlite3.connect(KLINE_DB) as conn:
        for (code,) in conn.execute("SELECT DISTINCT code FROM kline"):
            codes.add(code)
    if not all_codes and os.path.exists(WATCHLIST):
        wanted = set()
        with open(WATCHLIST, "r", encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.split("#")[0].strip()
                if line:
                    wanted.add(line.split()[0])
        codes = {c for c in codes if c in wanted}
    return codes


def iter_csv(dataset, wanted=None):
    """流式读 CSV，wanted 给定时只留这些代码。"""
    path = store.path_of(dataset)
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            code = row.get("代码")
            if wanted is not None and code not in wanted:
                continue
            yield row


def fnum(value):
    if value in ("", None):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_kline(codes):
    """(code, date) → 收盘、涨跌幅、成交额（成交额列可能还没补）。"""
    out = {}
    with sqlite3.connect(KLINE_DB) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(kline)")}
        has_amount = "amount" in cols
        sql = "SELECT code,date,close,pct_chg%s FROM kline" % (",amount" if has_amount else "")
        for row in conn.execute(sql):
            code, date = row[0], row[1]
            if code not in codes:
                continue
            out[(code, date)] = {
                "close": row[2],
                "pct": row[3],
                "amount_yuan": row[4] if has_amount else None,
            }
    return out


def margin_features(codes):
    """融资余额增速等。返回 {(code,date): {...}} 和 每只股票的日期序列。"""
    series = {}
    for row in iter_csv("rzrq_stock", codes):
        code, date = row.get("代码"), row.get("交易日期")
        balance = fnum(row.get("融资余额(元)"))
        if not code or not date or not balance:
            continue
        series.setdefault(code, []).append((date, balance, fnum(row.get("融资净买入(元)"))))
    out = {}
    for code, rows in series.items():
        rows.sort()
        for i, (date, balance, net) in enumerate(rows):
            growth5 = balance / rows[i - 5][1] - 1 if i >= 5 and rows[i - 5][1] else None
            growth20 = balance / rows[i - 20][1] - 1 if i >= 20 and rows[i - 20][1] else None
            intensity = (net / balance) if (net is not None and balance) else None
            out[(code, date)] = {
                "margin_balance_yuan": balance,
                "margin_growth_5": growth5,
                "margin_growth_20": growth20,
                "margin_net_intensity": intensity,
            }
    return out


def longhu_features(codes):
    """龙虎榜指标：只在上榜日有值，没上榜留 None（不填 0）。"""
    base = {}
    for row in iter_csv("longhu_stock", codes):
        code, date = row.get("代码"), row.get("交易日期")
        net_wan = fnum(row.get("龙虎榜净买入额(万元)"))
        turnover_yi = fnum(row.get("成交金额(亿元)"))
        if not code or not date:
            continue
        ratio = None
        if net_wan is not None and turnover_yi:
            ratio = net_wan / (turnover_yi * 10000.0)
        base[(code, date)] = {
            "is_on_longhu": 1,
            "lhb_net_ratio": ratio,
            "lhb_inst_buy_cnt": None,
            "lhb_top5_buy_ratio": None,
            "_turnover_yi": turnover_yi,
        }

    seat_buy = {}
    for row in iter_csv("longhu_seat", codes):
        if row.get("方向") != "买入":
            continue
        key = (row.get("代码"), row.get("交易日期"))
        amount = fnum(row.get("买入额(万元)"))
        seat_buy.setdefault(key, {"sum": 0.0, "inst": 0})
        if amount:
            seat_buy[key]["sum"] += amount
        if "机构专用" in (row.get("营业部") or ""):
            seat_buy[key]["inst"] += 1

    for key, item in base.items():
        seat = seat_buy.get(key)
        if seat:
            item["lhb_inst_buy_cnt"] = seat["inst"]
            turnover_wan = (item["_turnover_yi"] or 0) * 10000.0
            if turnover_wan > 0:
                item["lhb_top5_buy_ratio"] = seat["sum"] / turnover_wan
        item.pop("_turnover_yi", None)
    return base


def block_features(codes):
    """大宗交易：平均溢价率与成交额。"""
    agg = {}
    for row in iter_csv("dzjy", codes):
        key = (row.get("代码"), row.get("交易日期"))
        prem = fnum(row.get("溢价率"))
        amt = fnum(row.get("成交额(万元)"))
        item = agg.setdefault(key, {"sum": 0.0, "n": 0, "amt": 0.0})
        if prem is not None:
            item["sum"] += prem
            item["n"] += 1
        if amt:
            item["amt"] += amt
    return {key: {
        "block_discount": (v["sum"] / v["n"]) if v["n"] else None,
        "block_amt_wan": round(v["amt"], 2) if v["amt"] else None,
    } for key, v in agg.items()}


def market_features():
    """市场层：ETF 总份额及其日变化。"""
    totals = {}
    for row in iter_csv("etf_share"):
        date = row.get("交易日期")
        share = fnum(row.get("总份额(万份)"))
        if not date or share is None:
            continue
        totals[date] = totals.get(date, 0.0) + share
    dates = sorted(totals)
    out = []
    for i, date in enumerate(dates):
        prev = totals[dates[i - 1]] if i else None
        out.append({
            "date": date,
            "etf_total_share": round(totals[date], 2),
            "etf_share_chg": round(totals[date] - prev, 2) if prev is not None else None,
        })
    return out


def main(argv):
    parser = argparse.ArgumentParser(description="把资金数据算成特征")
    parser.add_argument("--all-codes", action="store_true", help="全市场（慢、文件大）")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if not os.path.exists(KLINE_DB):
        print("找不到 kline.db，先跑 kline.py --watchlist")
        return 2
    codes = read_pool(args.all_codes)
    if not codes:
        print("没有可用代码")
        return 2

    margin = margin_features(codes)
    lhb = longhu_features(codes)
    block = block_features(codes)
    kline = load_kline(codes)

    keys = set(kline) | set(margin) | set(lhb)
    rows = []
    for key in sorted(keys):
        code, date = key
        record = {col: None for col in COLUMNS}
        record["code"], record["date"] = code, date
        record["is_on_longhu"] = 0
        record.update(margin.get(key, {}))
        record.update(lhb.get(key, {}))
        record.update(block.get(key, {}))
        rows.append(record)

    store.ensure_dirs()
    with open(FEATURE_CSV, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        for record in rows:
            writer.writerow({col: ("" if record.get(col) is None else record[col]) for col in COLUMNS})

    market = market_features()
    with open(MARKET_CSV, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["date", "etf_total_share", "etf_share_chg"])
        writer.writeheader()
        writer.writerows(market)

    if not args.quiet:
        filled_lhb = sum(1 for r in rows if r.get("lhb_net_ratio") is not None)
        filled_margin = sum(1 for r in rows if r.get("margin_growth_5") is not None)
        dates = sorted({r["date"] for r in rows})
        print("特征表：%d 行，%d 只股票，%s ~ %s" % (len(rows), len(codes),
                                              dates[0] if dates else "-", dates[-1] if dates else "-"))
        print("  有龙虎榜净买占比的：%d 行（只在上榜日有值）" % filled_lhb)
        print("  有融资余额5日增速的：%d 行" % filled_margin)
        print("  市场层：%d 个交易日" % len(market))
        print("  输出：%s" % FEATURE_CSV)
        print("        %s" % MARKET_CSV)
        print("  生成时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
