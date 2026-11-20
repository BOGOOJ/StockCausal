# -*- coding: utf-8 -*-
"""backfill.py —— 用程序批量回补历史事件数据（不手工查）。

历史日期只走东财 / 交易所，因为：
- 同花顺龙虎榜页面只有最新一个交易日，历史日期去请求它等于白跑一次；
- 同花顺两融总量只给最近 10 个交易日，没有历史接口。

回补的数据集：
    龙虎榜个股 / 龙虎榜席位 / 两融个股 / 大宗交易 / ETF 份额（上交所，支持 STAT_DATE）

支持断点续传：已经在 CSV 里出现过的交易日直接跳过，中断后重跑会接着补。

用法：
    python scripts\\backfill.py --days 250
    python scripts\\backfill.py --from 2024-11-01 --to 2026-11-19
    python scripts\\backfill.py --days 250 --datasets longhu,dzjy
    python scripts\\backfill.py --days 250 --dry-run     # 只列出要补哪些日子
"""
import argparse
import sys
import time
from datetime import datetime, timedelta

import fetch_em
import fetch_exchange
import store

DATASETS = ("longhu_stock", "longhu_seat", "rzrq_stock", "dzjy", "etf_share")


def existing_dates(dataset):
    """该数据集里已经有哪些交易日期（一次读盘，避免每天读整个 CSV）。"""
    return {row.get("交易日期") for row in store.read_rows(dataset) if row.get("交易日期")}


def trading_days(date_from, date_to):
    """按自然日倒序列出候选日（跳过周末）；节假日的空数据会被自动跳过。"""
    days = []
    cursor = datetime.strptime(date_to, "%Y-%m-%d").date()
    start = datetime.strptime(date_from, "%Y-%m-%d").date()
    while cursor >= start:
        if cursor.weekday() < 5:
            days.append(cursor.strftime("%Y-%m-%d"))
        cursor -= timedelta(days=1)
    return days


def do_longhu(date):
    stocks = fetch_em.longhu(date)
    if not stocks:
        return 0, 0
    names = {row["代码"]: row["名称"] for row in stocks}
    for row in stocks:
        row["交易日期"] = date
        row["数据来源"] = "东财"
        row["抓取时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    seats = fetch_em.longhu_seats(date)
    for row in seats:
        row["交易日期"] = date
        row["名称"] = names.get(row["代码"], "")
        row["数据来源"] = "东财"
        row["抓取时间"] = stocks[0]["抓取时间"]
    store.replace_day("longhu_stock", date, stocks)
    store.replace_day("longhu_seat", date, seats)
    return len(stocks), len(seats)


def do_rzrq(date):
    rows = fetch_em.rzrq_stock(date)
    if not rows:
        return 0
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for row in rows:
        row["交易日期"] = date
        row["数据来源"] = "东财"
        row["抓取时间"] = stamp
    store.replace_day("rzrq_stock", date, rows)
    return len(rows)


def do_dzjy(date):
    rows = fetch_em.dzjy(date)
    if not rows:
        return 0
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for row in rows:
        row["交易日期"] = date
        row["数据来源"] = "东财"
        row["抓取时间"] = stamp
    store.replace_day("dzjy", date, rows)
    return len(rows)


def do_etf(date):
    rows = fetch_exchange.etf_sse(date)
    if not rows:
        return 0
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for row in rows:
        row["交易日期"] = date
        row["数据来源"] = "上交所"
        row["抓取时间"] = stamp
    store.replace_day("etf_share", date, rows)
    return len(rows)


HANDLERS = {"longhu": do_longhu, "rzrq": do_rzrq, "dzjy": do_dzjy, "etf": do_etf}
COVERED = {"longhu": ("longhu_stock", "longhu_seat"), "rzrq": ("rzrq_stock",),
           "dzjy": ("dzjy",), "etf": ("etf_share",)}


def main(argv):
    parser = argparse.ArgumentParser(description="批量回补历史事件数据（东财 + 上交所）")
    parser.add_argument("--days", type=int, default=250, help="回补最近 N 个交易日（默认 250）")
    parser.add_argument("--from", dest="date_from", default="", help="起始日期 YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", default="", help="结束日期 YYYY-MM-DD（默认上一个交易日）")
    parser.add_argument("--datasets", default="longhu,rzrq,dzjy,etf",
                        help="要补哪些：longhu,rzrq,dzjy,etf（逗号分隔）")
    parser.add_argument("--dry-run", action="store_true", help="只列出要补的日子，不发请求")
    args = parser.parse_args(argv)

    selected = [d.strip() for d in args.datasets.split(",") if d.strip()]
    for name in selected:
        if name not in HANDLERS:
            print("未知数据集：%s（可选 %s）" % (name, ",".join(HANDLERS)))
            return 2

    if args.date_to:
        date_to = args.date_to
    else:
        # 龙虎榜/两融都在收盘后晚间才发布，18:30 前不把今天算进来
        now = datetime.now()
        date_to = now.strftime("%Y-%m-%d")
        if (now.hour, now.minute) < (18, 30):
            date_to = (now.date() - timedelta(days=1)).strftime("%Y-%m-%d")
    if args.date_from:
        date_from = args.date_from
    else:
        date_from = (datetime.strptime(date_to, "%Y-%m-%d")
                     - timedelta(days=int(args.days * 1.5))).strftime("%Y-%m-%d")

    days = trading_days(date_from, date_to)
    print("候选交易日 %d 个：%s ~ %s" % (len(days), days[-1], days[0]))
    have = {name: {store_name: existing_dates(store_name) for store_name in COVERED[name]}
            for name in selected}
    todo = [d for d in days
            if any(d not in have[name][store_name]
                   for name in selected for store_name in COVERED[name])]
    print("其中需要补的 %d 天（已存在的不动）" % len(todo))
    if args.dry_run:
        for d in todo:
            print("  ", d)
        return 0
    if not todo:
        print("没有需要补的日期")
        return 0

    started = time.time()
    total = {name: 0 for name in selected}
    filled_days = 0
    for index, date in enumerate(todo, 1):
        line = []
        for name in selected:
            stores = COVERED[name]
            if all(date in have[name][s] for s in stores):
                continue
            try:
                count = HANDLERS[name](date)
            except Exception as exc:
                print("  [%s] %s 失败：%s" % (date, name, str(exc)[:90]))
                continue
            total[name] += count if isinstance(count, int) else sum(count)
            line.append("%s=%s" % (name, count))
        if line:
            filled_days += 1
            elapsed = time.time() - started
            eta = elapsed / index * (len(todo) - index) / 60 if index else 0
            print("[%d/%d] %s  %s  (已用 %.1f 分钟，预计还要 %.1f 分钟)"
                  % (index, len(todo), date, " ".join(line), elapsed / 60, eta))

    print("\n回补完成：%d 天有数据，用时 %.1f 分钟" % (filled_days, (time.time() - started) / 60))
    for name in selected:
        print("  %-8s 累计 %d 行" % (name, total[name]))
    store.log("回补完成 %s ~ %s，数据行数 %s" % (date_from, date_to, total))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
