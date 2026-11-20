# -*- coding: utf-8 -*-
"""命令行入口：每日事件数据（龙虎榜/两融/大宗/ETF 份额）+ 总调度。"""
import argparse
import json
import os
import sys
import time

import fetch_em
import fetch_exchange
import net
import parse_ths
import report
import store

THS_LONGHU = "https://data.10jqka.com.cn/market/longhu/"
THS_RZRQ = "https://data.10jqka.com.cn/market/rzrq/"
THS_DZJY = "https://data.10jqka.com.cn/market/dzjy/"
THS_REF = "https://data.10jqka.com.cn/"


def today():
    return time.strftime("%Y-%m-%d")


def default_target():
    """收盘后（17:30 之后）取今天，否则退到上一个工作日。

    龙虎榜/两融/大宗/ETF 份额都是收盘后才发布，盘中跑今天的日期一定是空的。
    """
    from datetime import date, timedelta
    day = date.today()
    if (time.localtime().tm_hour, time.localtime().tm_min) < (17, 30):
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day.strftime("%Y-%m-%d")


def stamp():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _fill(rows, source):
    now = stamp()
    for row in rows:
        row["数据来源"] = source
        row["抓取时间"] = now
    return rows


def grab_longhu(target, summary):
    """龙虎榜个股 + 席位：同花顺优先（一页含席位），日期对不上或失败才回退东财。"""
    try:
        raw = net.fetch(THS_LONGHU, referer=THS_REF, kind="ths")
        store.save_raw("longhu.html", raw, target)
        parsed = parse_ths.parse_longhu(net.decode(raw, "gbk"))
        date = parsed["date"] or target
        if parsed["stocks"] and date == target:
            stocks = _fill(parsed["stocks"], "同花顺")
            seats = _fill(parsed["seats"], "同花顺")
            for row in stocks:
                row["交易日期"] = date
            for row in seats:
                row["交易日期"] = date
            store.replace_day("longhu_stock", date, stocks)
            store.replace_day("longhu_seat", date, seats)
            summary.append(("龙虎榜个股", date, len(stocks), "同花顺"))
            summary.append(("龙虎榜席位", date, len(seats), "同花顺"))
            return
        store.log("[提示] 同花顺龙虎榜日期是 %s（目标 %s），改走东财" % (date, target))
    except Exception as exc:
        store.log("[提示] 同花顺龙虎榜失败：%s，改走东财" % str(exc)[:90])

    stocks = _fill(fetch_em.longhu(target), "东财")
    seats = _fill(fetch_em.longhu_seats(target), "东财")
    names = {row["代码"]: row["名称"] for row in stocks}
    for row in seats:
        row["交易日期"] = target
        row["名称"] = names.get(row["代码"], "")
    for row in stocks:
        row["交易日期"] = target
    store.replace_day("longhu_stock", target, stocks)
    store.replace_day("longhu_seat", target, seats)
    summary.append(("龙虎榜个股", target, len(stocks), "东财"))
    summary.append(("龙虎榜席位", target, len(seats), "东财"))


def grab_rzrq(target, summary):
    """两融：市场总量走同花顺（页面自带最近 10 个交易日），个股明细走东财全量。

    同花顺这个页面偶尔会返回只有表头的空壳（数据没渲染出来），重试两次。
    """
    rows = []
    for attempt in range(3):
        try:
            raw = net.fetch(THS_RZRQ, referer=THS_REF, kind="ths")
            store.save_raw("rzrq.html", raw, target)
            rows = parse_ths.parse_rzrq_market(net.decode(raw, "gbk"))
            if rows:
                break
            store.log("[提示] 同花顺两融页返回空表，第 %d 次重试……" % (attempt + 1))
            time.sleep(3)
        except Exception as exc:
            store.log("[提示] 同花顺两融总量失败：%s" % str(exc)[:90])
            time.sleep(3)
    if rows:
        _fill(rows, "同花顺")
        for row in rows:
            store.replace_day("rzrq_market", row["交易日期"], [row])
        latest = max(row["交易日期"] for row in rows)
        summary.append(("两融总量", latest, len(rows), "同花顺"))
    else:
        store.log("[提示] 同花顺两融总量没取到（下次运行会补）")

    rows = _fill(fetch_em.rzrq_stock(target), "东财")
    for row in rows:
        row["交易日期"] = target
    store.replace_day("rzrq_stock", target, rows)
    summary.append(("两融个股", target, len(rows), "东财"))


def grab_dzjy(target, summary):
    """大宗交易：东财全量；同花顺只给最近 50 笔且跨日截断，仅做核对。"""
    rows = _fill(fetch_em.dzjy(target), "东财")
    for row in rows:
        row["交易日期"] = target
    store.replace_day("dzjy", target, rows)
    summary.append(("大宗交易", target, len(rows), "东财"))

    if target >= today():
        try:
            raw = net.fetch(THS_DZJY, referer=THS_REF, kind="ths")
            store.save_raw("dzjy.html", raw, target)
            ths_rows = parse_ths.parse_dzjy(net.decode(raw, "gbk"))
            same_day = [r for r in ths_rows if r["交易日期"] == target]
            if same_day:
                store.log("[核对] 大宗：同花顺当天 %d 笔，东财 %d 笔（同花顺只给最近 50 笔，会截断）"
                          % (len(same_day), len(rows)))
        except Exception as exc:
            store.log("[核对] 同花顺大宗页没取到：%s" % str(exc)[:80])


def grab_etf(target, summary):
    """ETF 份额：上交所（可查历史）+ 深交所（只有当日）。"""
    try:
        sse = _fill(fetch_exchange.etf_sse(target), "上交所")
        for row in sse:
            row["交易日期"] = target
        store.replace_day("etf_share", target, sse)
        summary.append(("ETF份额-沪", target, len(sse), "上交所"))
    except Exception as exc:
        store.log("[提示] 上交所 ETF 份额失败：%s" % str(exc)[:90])

    # 深交所没有日期参数，只能拿到「当前快照」：只在收盘后、且目标就是今天时采，
    # 否则会把盘中/别的交易日的数当成目标日的数据。
    now = time.localtime()
    if target == today() and (now.tm_hour, now.tm_min) >= (15, 30):
        try:
            szse = _fill(fetch_exchange.etf_szse(), "深交所")
            for row in szse:
                row["交易日期"] = target
            old = [r for r in store.read_rows("etf_share")
                   if r.get("交易日期") == target and r.get("交易所") == "上交所"]
            store.replace_day("etf_share", target, old + szse)
            summary.append(("ETF份额-深", target, len(szse), "深交所"))
        except Exception as exc:
            store.log("[提示] 深交所 ETF 份额失败：%s" % str(exc)[:90])
    else:
        store.log("[提示] 深交所 ETF 份额只有「当日收盘后」的快照，本次跳过（目标 %s）" % target)


def run(target, do_export=True):
    store.ensure_dirs()
    store.log("=" * 60)
    store.log("开始采集 目标日期 %s（%s）" % (target, stamp()))
    summary = []
    for name, func in (("龙虎榜", grab_longhu), ("两融", grab_rzrq),
                       ("大宗", grab_dzjy), ("ETF", grab_etf)):
        try:
            func(target, summary)
        except Exception as exc:
            store.log("[失败] %s：%s" % (name, str(exc)[:120]))

    store.log("-" * 60)
    if summary:
        width = max(len(item[0]) for item in summary)
        for name, date, count, source in summary:
            store.log("  %-*s %s  %6d 行  [%s]" % (width, name, date, count, source))
    total = sum(item[2] for item in summary)
    store.log("合计 %d 行数据" % total)

    if do_export:
        path = report.build_daily(target)
        if path:
            store.log("Excel 已生成：%s" % path)
    return 0 if total else 2


def backfill(days, do_export=False):
    from datetime import date, datetime, timedelta
    start = datetime.strptime(default_target(), "%Y-%m-%d").date()
    count = 0
    done = 0
    while done < days:
        day = start - timedelta(days=count)
        count += 1
        if day.weekday() >= 5:
            continue
        done += 1
        target = day.strftime("%Y-%m-%d")
        store.log("---- 回补 %s ----" % target)
        try:
            run(target, do_export=False)
        except Exception as exc:
            store.log("[失败] %s：%s" % (target, str(exc)[:100]))
    if do_export:
        report.build_daily(date.today().strftime("%Y-%m-%d"))
    return 0


def main(argv):
    parser = argparse.ArgumentParser(description="同花顺每日数据采集")
    sub = parser.add_subparsers(dest="cmd")

    daily = sub.add_parser("daily", help="抓当天")
    daily.add_argument("--no-export", action="store_true", help="不出 Excel")
    daily.add_argument("--date", default="", help="指定日期 YYYY-MM-DD（历史只能走东财）")

    back = sub.add_parser("backfill", help="回补最近 N 个交易日")
    back.add_argument("--days", type=int, default=30)
    back.add_argument("--export", action="store_true")

    exporter = sub.add_parser("export", help="从 CSV 出 Excel")
    exporter.add_argument("--date", default="")

    verify = sub.add_parser("verify", help="同花顺 vs 东财 行数核对（不写库）")
    verify.add_argument("--date", default="")

    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 1

    hint = net.proxy_hint()
    if hint:
        store.log(hint)

    if args.cmd == "daily":
        target = args.date or default_target()
        return run(target, do_export=not args.no_export)
    if args.cmd == "backfill":
        return backfill(args.days, do_export=args.export)
    if args.cmd == "export":
        target = args.date or default_target()
        path = report.build_daily(target)
        if path:
            print("已生成 %s" % path)
            return 0
        print("没有生成 Excel（日期 %s）——原因见上面的提示；data/*.csv 里数据是完整的" % target)
        return 2
    if args.cmd == "verify":
        return verify(args.date or default_target())
    return 1


def verify(target):
    """只核对行数，不写库。"""
    print("核对 %s" % target)
    try:
        raw = net.fetch(THS_LONGHU, referer=THS_REF, kind="ths")
        ths = parse_ths.parse_longhu(net.decode(raw, "gbk"))
        print("  同花顺龙虎榜：日期 %s，个股 %d，席位 %d"
              % (ths["date"], len(ths["stocks"]), len(ths["seats"])))
    except Exception as exc:
        print("  同花顺龙虎榜失败：%s" % str(exc)[:90])
    for name, func in (("龙虎榜个股", fetch_em.longhu), ("大宗", fetch_em.dzjy),
                       ("两融个股", fetch_em.rzrq_stock)):
        try:
            print("  东财%s：%d 行" % (name, len(func(target))))
        except Exception as exc:
            print("  东财%s失败：%s" % (name, str(exc)[:80]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
