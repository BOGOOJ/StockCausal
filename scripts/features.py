# -*- coding: utf-8 -*-
"""features.py —— 把资金数据算成指标（全市场版，走 SQLite 控内存）。

为什么要改成 SQLite：
    4,800 只 × 251 天 ≈ 120 万行，再用 Python 字典装会吃掉几个 G。
    本机可用内存只有 4.2G，所以中间数据全放磁盘，内存占用恒定。

输入：
    data\\rzrq_stock.csv     融资余额、融资净买入
    data\\longhu_stock.csv   龙虎榜净买额、当日成交额
    data\\longhu_seat.csv    买卖前 5 营业部
    data\\dzjy.csv           大宗交易溢价率
    data\\industry_map.csv   行业与风格组
    data\\kline.db           只用来取「哪些股票有哪些交易日」

输出：
    data\\features.db        表 feature（一行 = 一只股票一个交易日）

用法：
    python scripts\\features.py
    python scripts\\features.py --rebuild     # 重建工作库（改了 CSV 后用）
"""
import argparse
import csv
import os
import sqlite3
import sys
import time

import store

DATA = store.DATA_DIR
WORK_DB = os.path.join(DATA, "work.db")
FEATURE_DB = os.path.join(DATA, "features.db")
KLINE_DB = os.path.join(DATA, "kline.db")


def fnum(value):
    if value in ("", None):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_csv(path, wanted=None):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            if wanted is not None and row.get("代码") not in wanted:
                continue
            yield row


def load_industry():
    path = os.path.join(DATA, "industry_map.csv")
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            out[row.get("代码")] = (row.get("一级") or "", row.get("风格组") or "")
    return out


def build_work(conn, codes):
    """把四个大 CSV 塞进 SQLite 工作库（流式读，内存不涨）。"""
    conn.executescript("""
        DROP TABLE IF EXISTS margin_raw;
        DROP TABLE IF EXISTS lhb_raw;
        DROP TABLE IF EXISTS seat_raw;
        DROP TABLE IF EXISTS block_raw;
        CREATE TABLE margin_raw(code TEXT, date TEXT, balance REAL, net REAL);
        CREATE TABLE lhb_raw(code TEXT, date TEXT, net_wan REAL, turnover_yi REAL);
        CREATE TABLE seat_raw(code TEXT, date TEXT, direction TEXT, dept TEXT, buy_wan REAL);
        CREATE TABLE block_raw(code TEXT, date TEXT, premium REAL, amt_wan REAL);
    """)

    def batches(rows, sql, size=20000):
        buf = []
        for item in rows:
            buf.append(item)
            if len(buf) >= size:
                conn.executemany(sql, buf)
                buf = []
        if buf:
            conn.executemany(sql, buf)

    started = time.time()
    batches(
        ((r["代码"], r["交易日期"], fnum(r.get("融资余额(元)")), fnum(r.get("融资净买入(元)")))
         for r in read_csv(os.path.join(DATA, "rzrq_stock.csv"), codes)
         if r.get("代码") and r.get("交易日期") and fnum(r.get("融资余额(元)"))),
        "INSERT INTO margin_raw VALUES(?,?,?,?)")
    print("  融资 %d 行（%.0fs）" % (conn.execute("SELECT COUNT(*) FROM margin_raw").fetchone()[0],
                                 time.time() - started))

    batches(
        ((r["代码"], r["交易日期"], fnum(r.get("龙虎榜净买入额(万元)")), fnum(r.get("成交金额(亿元)")))
         for r in read_csv(os.path.join(DATA, "longhu_stock.csv"), codes)
         if r.get("代码") and r.get("交易日期")),
        "INSERT INTO lhb_raw VALUES(?,?,?,?)")

    batches(
        ((r["代码"], r["交易日期"], r.get("方向"), r.get("营业部"), fnum(r.get("买入额(万元)")))
         for r in read_csv(os.path.join(DATA, "longhu_seat.csv"), codes)
         if r.get("代码") and r.get("交易日期")),
        "INSERT INTO seat_raw VALUES(?,?,?,?,?)")

    batches(
        ((r["代码"], r["交易日期"], fnum(r.get("溢价率")), fnum(r.get("成交额(万元)")))
         for r in read_csv(os.path.join(DATA, "dzjy.csv"), codes)
         if r.get("代码") and r.get("交易日期")),
        "INSERT INTO block_raw VALUES(?,?,?,?)")

    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_margin ON margin_raw(code, date);
        CREATE INDEX IF NOT EXISTS idx_lhb ON lhb_raw(code, date);
        CREATE INDEX IF NOT EXISTS idx_seat ON seat_raw(code, date);
        CREATE INDEX IF NOT EXISTS idx_block ON block_raw(code, date);
    """)
    conn.commit()


def build_features(conn, industry):
    """一张 SQL 把指标算出来，避免 Python 逐行处理 120 万条。"""
    conn.executescript("""
        DROP TABLE IF EXISTS feature;
        CREATE TABLE feature(
            code TEXT, date TEXT,
            sw1 TEXT, style TEXT,
            margin_balance REAL, margin_growth_5 REAL, margin_growth_20 REAL,
            margin_net_intensity REAL,
            is_on_longhu INTEGER DEFAULT 0, lhb_net_ratio REAL,
            lhb_inst_buy_cnt INTEGER, lhb_top5_buy_ratio REAL,
            block_discount REAL, block_amt_wan REAL,
            PRIMARY KEY(code, date)
        );
    """)

    # 1) 融资指标：用窗口函数取 5 日 / 20 日前的余额
    conn.executescript("""
        INSERT INTO feature(code, date, margin_balance, margin_growth_5,
                            margin_growth_20, margin_net_intensity)
        SELECT code, date, balance,
               CASE WHEN LAG(balance, 5) OVER w > 0
                    THEN balance / LAG(balance, 5) OVER w - 1 END,
               CASE WHEN LAG(balance, 20) OVER w > 0
                    THEN balance / LAG(balance, 20) OVER w - 1 END,
               CASE WHEN balance > 0 THEN net / balance END
        FROM margin_raw
        WINDOW w AS (PARTITION BY code ORDER BY date);
    """)

    # 2) 龙虎榜：净买占比 + 机构家数 + 买方前五集中度
    conn.executescript("""
        UPDATE feature SET
            is_on_longhu = 1,
            lhb_net_ratio = (SELECT CASE WHEN l.turnover_yi > 0
                                         THEN l.net_wan / (l.turnover_yi * 10000.0) END
                             FROM lhb_raw l
                             WHERE l.code = feature.code AND l.date = feature.date
                             LIMIT 1)
        WHERE EXISTS (SELECT 1 FROM lhb_raw l
                      WHERE l.code = feature.code AND l.date = feature.date);
    """)
    conn.executescript("""
        UPDATE feature SET
            lhb_inst_buy_cnt = (SELECT COUNT(*) FROM seat_raw s
                                WHERE s.code = feature.code AND s.date = feature.date
                                  AND s.direction = '买入' AND s.dept LIKE '%机构专用%'),
            lhb_top5_buy_ratio = (
                SELECT CASE WHEN l.turnover_yi > 0
                            THEN (SELECT IFNULL(SUM(s.buy_wan), 0) FROM seat_raw s
                                  WHERE s.code = feature.code AND s.date = feature.date
                                    AND s.direction = '买入') / (l.turnover_yi * 10000.0)
                       END
                FROM lhb_raw l
                WHERE l.code = feature.code AND l.date = feature.date
                LIMIT 1)
        WHERE is_on_longhu = 1;
    """)

    # 3) 大宗
    conn.executescript("""
        UPDATE feature SET
            block_discount = (SELECT AVG(b.premium) FROM block_raw b
                              WHERE b.code = feature.code AND b.date = feature.date),
            block_amt_wan = (SELECT SUM(b.amt_wan) FROM block_raw b
                             WHERE b.code = feature.code AND b.date = feature.date)
        WHERE EXISTS (SELECT 1 FROM block_raw b
                      WHERE b.code = feature.code AND b.date = feature.date);
    """)

    # 4) 补行业与风格组
    conn.execute("CREATE INDEX IF NOT EXISTS idx_feat ON feature(code, date)")
    for code, (sw1, style) in industry.items():
        conn.execute("UPDATE feature SET sw1=?, style=? WHERE code=?", (sw1, style, code))
    conn.commit()


def main(argv):
    parser = argparse.ArgumentParser(description="算资金指标（全市场）")
    parser.add_argument("--rebuild", action="store_true", help="重建工作库")
    args = parser.parse_args(argv)

    if not os.path.exists(KLINE_DB):
        print("找不到 kline.db")
        return 2
    store.ensure_dirs()

    with sqlite3.connect(KLINE_DB) as kc:
        codes = {row[0] for row in kc.execute("SELECT DISTINCT code FROM kline")}
    print("日线库里有 %d 个标的" % len(codes))

    industry = load_industry()
    print("行业映射 %d 条（覆盖日线标的 %.1f%%）"
          % (len(industry), 100.0 * len(codes & set(industry)) / max(len(codes), 1)))

    conn = sqlite3.connect(WORK_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    if args.rebuild or conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='margin_raw'").fetchone()[0] == 0:
        print("读取 CSV 建工作库……")
        build_work(conn, codes)
    else:
        print("复用已有工作库（--rebuild 可重建）")

    print("计算指标……")
    started = time.time()
    build_features(conn, {k: v for k, v in industry.items() if k in codes})
    total = conn.execute("SELECT COUNT(*) FROM feature").fetchone()[0]
    dates = conn.execute("SELECT MIN(date), MAX(date), COUNT(DISTINCT date) FROM feature").fetchone()
    lhb = conn.execute("SELECT COUNT(*) FROM feature WHERE is_on_longhu=1").fetchone()[0]
    withm = conn.execute("SELECT COUNT(*) FROM feature WHERE margin_growth_5 IS NOT NULL").fetchone()[0]
    conn.commit()

    # 把结果搬到独立的 features.db（工作库可以随时删）
    with sqlite3.connect(FEATURE_DB) as fc:
        fc.execute("DROP TABLE IF EXISTS feature")
        fc.execute("""CREATE TABLE feature(
            code TEXT, date TEXT, sw1 TEXT, style TEXT,
            margin_balance REAL, margin_growth_5 REAL, margin_growth_20 REAL,
            margin_net_intensity REAL, is_on_longhu INTEGER, lhb_net_ratio REAL,
            lhb_inst_buy_cnt INTEGER, lhb_top5_buy_ratio REAL,
            block_discount REAL, block_amt_wan REAL, PRIMARY KEY(code, date))""")
        cursor = conn.execute("SELECT * FROM feature")
        while True:
            chunk = cursor.fetchmany(50000)
            if not chunk:
                break
            fc.executemany("INSERT OR REPLACE INTO feature VALUES(%s)"
                           % ",".join("?" * len(chunk[0])), chunk)
        fc.commit()

    print("完成（%.0fs）" % (time.time() - started))
    print("  特征表 %d 行，日期 %s ~ %s（%d 天）" % (total, dates[0], dates[1], dates[2]))
    print("  龙虎榜事件 %d 行；有融资5日增速 %d 行" % (lhb, withm))
    print("  输出：%s（%.1f MB）" % (FEATURE_DB, os.path.getsize(FEATURE_DB) / 1024 / 1024))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
