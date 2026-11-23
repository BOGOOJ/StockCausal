# -*- coding: utf-8 -*-
"""日线行情库：同花顺全历史日线（备用东财）→ SQLite。

同花顺 all.js 解码规则（已用 600519 与东财逐值核对）：
    price 每 4 个整数 = [最低, 开盘-最低, 最高-最低, 收盘-最低]，单位「分」
    dates 是 MMDD 逗号串，年份靠 sortYear=[[年, 根数], ...] 累加还原
    volumn 是成交量，单位「股」
    URL 中间的 seg：00 不复权 / 01 前复权（老股会变负数，弃用）/ 02 后复权
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time

import fetch_em
import net
import store

DB_PATH = os.path.join(store.DATA_DIR, "kline.db")
WATCHLIST = os.path.join(store.ROOT, "watchlist.txt")
THS_URL = "http://d.10jqka.com.cn/v6/line/hs_%s/%s/all.js"
THS_REF = "http://stockpage.10jqka.com.cn/%s/"
JSONP = re.compile(r"\((.*)\)\s*$", re.S)

INDEX_PREFIX = ("1A", "1B", "399")
INDEX_CODE = "1A0001"      # 上证指数，用来当交易日后缀的参照

# 指数在同花顺和东财的代码不一样，备用源要映射一下
INDEX_EM = {
    "1A0001": "1.000001",   # 上证指数
    "1B0300": "1.000300",   # 沪深300
    "399001": "0.399001",   # 深证成指
    "399006": "0.399006",   # 创业板指
}


def is_index(code):
    return code.startswith(INDEX_PREFIX)


def normalize(raw_code):
    """保留大小写（指数代码 1A0001 有小写会 404），只去掉 hs_ 前缀。"""
    code = (raw_code or "").strip()
    if code.lower().startswith("hs_"):
        code = code[3:]
    return code


def decode_alljs(text):
    """把同花顺 all.js 的紧凑串解成 [{日期,开盘,最高,最低,收盘,成交量(股)}]。"""
    match = JSONP.search(text)
    if not match:
        raise ValueError("不是同花顺 JSONP 格式")
    obj = json.loads(match.group(1))
    factor = float(obj.get("priceFactor") or 100)
    prices = [int(x) for x in obj.get("price", "").split(",") if x not in ("", None)]
    volumes = obj.get("volumn", "").split(",")
    days = obj.get("dates", "").split(",")
    years = []
    for year, count in obj.get("sortYear") or []:
        years.extend([int(year)] * int(count))
    total = min(len(days), len(prices) // 4, len(years))
    rows = []
    for i in range(total):
        low = prices[4 * i] / factor
        open_ = low + prices[4 * i + 1] / factor
        high = low + prices[4 * i + 2] / factor
        close = low + prices[4 * i + 3] / factor
        try:
            volume = int(volumes[i]) if i < len(volumes) and volumes[i] else 0
        except ValueError:
            volume = 0
        rows.append({
            "日期": "%04d-%s-%s" % (years[i], days[i][:2], days[i][2:]),
            "开盘": round(open_, 2),
            "最高": round(high, 2),
            "最低": round(low, 2),
            "收盘": round(close, 2),
            "成交量(股)": volume,
        })
    return rows


def fetch_ths(code, segs, attempts=3):
    """依次尝试多个 seg；同花顺 502 很频繁，整体再来两轮，间隔 5 秒。"""
    last_error = None
    for attempt in range(attempts):
        for seg in segs:
            url = THS_URL % (code, seg)
            try:
                raw = net.fetch(url, referer=THS_REF % code, kind="kline", retries=2)
                return decode_alljs(net.decode(raw, "utf-8")), seg
            except Exception as exc:
                last_error = exc
        if attempt < attempts - 1:
            time.sleep(5)
    raise net.NetError("%s 同花顺取数失败：%s" % (code, last_error))


def fetch_em_fallback(code):
    """东财备用源：不复权 + 后复权收盘价。"""
    sid = INDEX_EM.get(code) or fetch_em.secid(code)
    base = fetch_em.kline(sid, fqt=0)
    try:
        adj = fetch_em.kline(sid, fqt=2)
        adj_map = {row["日期"]: row["收盘"] for row in adj}
    except Exception:
        adj_map = {}
    rows = []
    for row in base:
        rows.append({
            "日期": row["日期"],
            "开盘": row["开盘"],
            "最高": row["最高"],
            "最低": row["最低"],
            "收盘": row["收盘"],
            "成交量(股)": int(row["成交量(手)"] * 100),
            "收盘_后复权": adj_map.get(row["日期"]),
            "涨跌幅": row["涨跌幅"],
        })
    return rows


def connect():
    store.ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS stock(
        code TEXT PRIMARY KEY, name TEXT, market TEXT, last_sync TEXT, bars INTEGER, source TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS kline(
        code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
        volume INTEGER, adj_close REAL, pct_chg REAL,
        PRIMARY KEY(code, date))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kline_date ON kline(date)")
    conn.commit()
    return conn


def upsert(conn, code, rows, start_date):
    kept = [r for r in rows if r["日期"] >= start_date]
    if not kept:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO kline(code,date,open,high,low,close,volume,adj_close,pct_chg)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        [(code, r["日期"], r["开盘"], r["最高"], r["最低"], r["收盘"],
          r["成交量(股)"], r.get("收盘_后复权"), r.get("涨跌幅")) for r in kept])
    conn.commit()
    return len(kept)


def guard_today(rows):
    """收盘前跑，丢掉今天那根还没走完的 K 线。"""
    now = time.localtime()
    if (now.tm_hour, now.tm_min) < (15, 30):
        today = time.strftime("%Y-%m-%d")
        return [r for r in rows if r["日期"] != today]
    return rows


def update_one(conn, raw_code, start_date, log=print, latest=None, force=False, need_adj=True):
    code = normalize(raw_code)
    if not re.fullmatch(r"[0-9A-Za-z]{4,8}", code):
        return False, "代码格式不对：%s" % raw_code
    if latest and not force:
        have = conn.execute("SELECT MAX(date) FROM kline WHERE code=?", (code,)).fetchone()[0]
        if have and have >= latest:
            return True, "%s 已到 %s，跳过（--force 可强制重下）" % (code, have)
    # 指数没有分红除权，01(前复权) 就是原始值；股票只能用 00(不复权)，
    # 绝不能回退到 01——前复权对老股票会变成负数/被缩放，和「不复权」混在一起就废了。
    segs = ("01", "00") if is_index(code) else ("00",)
    source = "同花顺"
    try:
        rows, seg = fetch_ths(code, segs)
    except Exception as exc:
        log("   同花顺失败，改走东财：%s" % str(exc)[:80])
        if is_index(code) and code not in INDEX_EM:
            return False, "指数 %s 同花顺失败，且没有东财代码映射" % code
        try:
            rows = fetch_em_fallback(code if not is_index(code) else code)
            source = "东财"
            seg = "em"
        except Exception as exc2:
            return False, "两源都失败：%s" % str(exc2)[:100]

    rows = guard_today(rows)
    if not rows:
        return False, "没取到 K 线"

    # 后复权收盘价：用来算真实涨跌幅（前复权老股会变负数，不能用）
    adj_map = {r["日期"]: r.get("收盘_后复权") for r in rows if r.get("收盘_后复权")}
    if need_adj and source == "同花顺" and not is_index(code):
        try:
            adj_rows, _ = fetch_ths(code, ("02",))
            adj_map = {r["日期"]: r["收盘"] for r in adj_rows}
        except Exception:
            adj_map = {}
    if need_adj and not is_index(code) and not adj_map:
        # 同花顺后复权取不到时用东财兜底：只补一列收盘价，请求量很小
        try:
            adj_rows = fetch_em.kline(fetch_em.secid(code), fqt=2)
            adj_map = {r["日期"]: r["收盘"] for r in adj_rows}
        except Exception:
            adj_map = {}

    # 涨跌幅：在后复权序列内部算比值（不能和「不复权收盘」混用，
    # 否则遇到除权日或缺失日会算出假的涨跌幅）
    if adj_map:
        series = sorted(adj_map.items())
        pct = {}
        prev = None
        for day, value in series:
            pct[day] = round((value / prev - 1) * 100, 2) if prev else None
            prev = value
    else:
        pct = {}
        prev = None
        for row in sorted(rows, key=lambda r: r["日期"]):
            pct[row["日期"]] = round((row["收盘"] / prev - 1) * 100, 2) if prev else None
            prev = row["收盘"]

    kept = [r for r in rows if r["日期"] >= start_date]
    for row in kept:
        row["收盘_后复权"] = adj_map.get(row["日期"])
        row["涨跌幅"] = pct.get(row["日期"])
    count = upsert(conn, code, kept, start_date)
    conn.execute(
        "INSERT OR REPLACE INTO stock(code,name,market,last_sync,bars,source) VALUES(?,?,?,?,?,?)",
        (code, "", "index" if is_index(code) else "", kept[-1]["日期"], count, source))
    conn.commit()
    return True, "%s %d 根（%s，seg=%s）" % (code, count, source, seg)


def read_watchlist():
    if not os.path.exists(WATCHLIST):
        return []
    codes = []
    with open(WATCHLIST, "r", encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.split("#")[0].strip()
            if not line:
                continue
            codes.append(line.split()[0])
    return codes


def export(conn, code, start=None, end=None):
    code = normalize(code)
    sql = "SELECT date,open,high,low,close,volume,adj_close,pct_chg FROM kline WHERE code=?"
    args = [code]
    if start:
        sql += " AND date>=?"
        args.append(start)
    if end:
        sql += " AND date<=?"
        args.append(end)
    sql += " ORDER BY date"
    rows = conn.execute(sql, args).fetchall()
    if not rows:
        return None, 0
    store.ensure_dirs()
    path = os.path.join(store.OUT_DIR, "%s_日线.csv" % code)
    import csv
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["日期", "开盘", "最高", "最低", "收盘", "成交量(股)", "收盘_后复权", "涨跌幅%"])
        writer.writerows(rows)
    return path, len(rows)


def main(argv):
    parser = argparse.ArgumentParser(description="日线行情库（同花顺 all.js → SQLite）")
    parser.add_argument("--code", action="append", default=[], help="股票/指数代码，可重复")
    parser.add_argument("--watchlist", action="store_true", help="更新 watchlist.txt 里的全部代码")
    parser.add_argument("--all", action="store_true", help="全市场（慢，约 2.7 小时）")
    parser.add_argument("--years", type=float, default=3.0, help="只保留最近 N 年（默认 3）")
    parser.add_argument("--start", default="", help="起始日期 YYYY-MM-DD，覆盖 --years")
    parser.add_argument("--export", action="store_true", help="导出成 out/<代码>_日线.csv")
    parser.add_argument("--from", dest="date_from", default="", help="导出起始日期")
    parser.add_argument("--to", dest="date_to", default="", help="导出结束日期")
    parser.add_argument("--list", action="store_true", help="列出库里已有的代码")
    parser.add_argument("--force", action="store_true", help="已是最新也强制重下")
    parser.add_argument("--no-adj", action="store_true",
                        help="不取后复权（省一半请求；只做次日收益分析时够用）")
    args = parser.parse_args(argv)

    conn = connect()

    if args.start:
        start_date = args.start
    else:
        start_date = time.strftime("%Y-%m-%d", time.localtime(time.time() - args.years * 365 * 86400))

    if args.list:
        rows = conn.execute("SELECT code,last_sync,bars,source FROM stock ORDER BY code").fetchall()
        if not rows:
            print("库是空的")
        for code, last, bars, src in rows:
            print("  %-8s 最后更新 %s  %6d 根  %s" % (code, last, bars or 0, src or ""))
        return 0

    if args.export:
        targets = args.code or read_watchlist()
        if not targets:
            print("没有指定代码；用 --code 600519 或先填 watchlist.txt")
            return 2
        for code in targets:
            path, count = export(conn, code, args.date_from, args.date_to)
            if path:
                print("已导出 %s（%d 行）" % (path, count))
            else:
                print("%s 库里没有数据" % code)
        return 0

    targets = list(args.code)
    if args.all:
        print("取全 A 股名单……")
        targets.extend(item["代码"] for item in fetch_em.universe())
    elif args.watchlist or not targets:
        targets.extend(read_watchlist())
    seen = set()
    targets = [c for c in targets if not (c in seen or seen.add(c))]

    if not targets:
        print("watchlist.txt 是空的，也没有 --code。先往 watchlist.txt 里一行填一个代码。")
        return 2

    print("共 %d 个标的，只保留 %s 之后的数据" % (len(targets), start_date))
    latest = conn.execute("SELECT MAX(date) FROM kline WHERE code=?", (INDEX_CODE,)).fetchone()[0]
    if latest:
        print("库里的最新交易日：%s（已最新的标的自动跳过）" % latest)
    ok, failed = 0, []
    for i, code in enumerate(targets, 1):
        try:
            good, message = update_one(conn, code, start_date, log=lambda m: print(m),
                                       latest=latest, force=args.force,
                                       need_adj=not args.no_adj)
        except Exception as exc:
            good, message = False, "%s: %s" % (type(exc).__name__, str(exc)[:100])
        if good:
            ok += 1
            print("[%d/%d] %s" % (i, len(targets), message))
        else:
            failed.append((code, message))
            print("[%d/%d] 失败 %s ← %s" % (i, len(targets), code, message))
        conn.commit()

    print("\n完成：成功 %d，失败 %d" % (ok, len(failed)))
    for code, message in failed[:10]:
        print("  %s ← %s" % (code, message))
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
