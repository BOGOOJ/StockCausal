# -*- coding: utf-8 -*-
"""analyze_features.py —— 资金指标检验（全市场版，SQL 聚合 + 分块，内存恒定）。

流程：
1. 从 kline.db 流式算「次日超额收益」→ data\\analysis.db 的 fwd 表（只建一次）
2. 每个指标：SQL 算 z-score（只用样本内统计）→ 分档 → 每日每档均值
3. Python 只处理「日 × 档」的几百行聚合结果，做 t 检验和 IC

口径（与之前一致，未改动）：
- T 日收盘得信号 → T+1 开盘买 → T+1 收盘卖；收益减去同日上证指数
- 剔除 T+1 开盘涨停 / 一字板
- 样本内外 70/30，按**该指标自己有数据的交易日**切
- 判定：跨档差 > 0.10pp 且 最优档扣成本后为正 且 |rho|≥0.8 且 极端档 |t|≥2 且 每档≥100

用法：
    python scripts\\analyze_features.py                    # 跑全部指标
    python scripts\\analyze_features.py --feature margin_growth_5
    python scripts\\analyze_features.py --neutral           # 加行业中性化
    python scripts\\analyze_features.py --by-style          # 加 5 风格组复核
    python scripts\\analyze_features.py --rebuild-fwd      # 强制重建收益表
"""
import argparse
import json
import math
import os
import sqlite3
import sys
from datetime import datetime

import analyze as A
import store

KLINE_DB = os.path.join(store.DATA_DIR, "kline.db")
FEATURE_DB = os.path.join(store.DATA_DIR, "features.db")
ANALYSIS_DB = os.path.join(store.DATA_DIR, "analysis.db")

CONTINUOUS = (("margin_growth_5", "融资余额5日增速"),
              ("margin_growth_20", "融资余额20日增速"),
              ("margin_net_intensity", "融资净买入强度"))
EVENT = (("lhb_net_ratio", "龙虎榜净买占比"),
         ("lhb_inst_buy_cnt", "买方机构专用家数"),
         ("lhb_top5_buy_ratio", "买方前五集中度"))
ALL = CONTINUOUS + EVENT

INDEX_CODES = ("1A0001", "399001")


def limit_ratio(code):
    return 0.20 if code.startswith(("300", "688")) else 0.10


def build_fwd(conn, kline):
    """流式算次日超额收益，落 fwd 表。"""
    conn.executescript("DROP TABLE IF EXISTS fwd;"
                       "CREATE TABLE fwd(code TEXT, date TEXT, ret REAL, PRIMARY KEY(code, date));")
    idx = {}
    for code in INDEX_CODES:
        for date, o, c in kline.execute(
                "SELECT date,open,close FROM kline WHERE code=? ORDER BY date", (code,)).fetchall():
            if code == "1A0001" or date not in idx:
                idx[date] = (o, c)
    print("  指数交易日 %d 个" % len(idx))

    cur_code, prev, buf, count = None, None, [], 0
    for code, date, o, h, l, c in kline.execute(
            "SELECT code,date,open,high,low,close FROM kline ORDER BY code,date"):
        if code in INDEX_CODES:
            continue
        if code != cur_code:
            cur_code, prev = code, None
        if prev is not None and o > 0:
            day_idx = idx.get(date)
            if day_idx:
                cap = round(prev[4] * (1 + limit_ratio(code)), 2)
                if o < cap * 0.995 and h != l:
                    idx_ret = day_idx[1] / day_idx[0] - 1 if day_idx[0] > 0 else 0.0
                    buf.append((code, prev[0], c / o - 1 - idx_ret))
        prev = (date, o, h, l, c)
        if len(buf) >= 50000:
            conn.executemany("INSERT OR REPLACE INTO fwd VALUES(?,?,?)", buf)
            count += len(buf)
            buf = []
    if buf:
        conn.executemany("INSERT OR REPLACE INTO fwd VALUES(?,?,?)", buf)
        count += len(buf)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fwd ON fwd(code, date)")
    conn.commit()
    print("  次日超额收益 %d 行" % conn.execute("SELECT COUNT(*) FROM fwd").fetchone()[0])
    return count


def quantile_from(sorted_rows, q):
    if not sorted_rows:
        return None
    idx = min(len(sorted_rows) - 1, max(0, int(len(sorted_rows) * q)))
    return sorted_rows[idx][0]


def analyse_feature(conn, feature, label, neutral=False, style=None):
    """返回结果 dict；样本不足返回 None。"""
    base_where = "%s IS NOT NULL" % feature
    if style:
        base_where += " AND style = '%s'" % style.replace("'", "''")

    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM feature WHERE %s ORDER BY date" % base_where)]
    if len(dates) < 60:
        return None
    split = dates[int(len(dates) * (1 - A.OOS_FRACTION))]

    conn.executescript("""
        DROP TABLE IF EXISTS tmp_stat;
        DROP TABLE IF EXISTS tmp_z;
        DROP TABLE IF EXISTS tmp_zb;
        DROP TABLE IF EXISTS tmp_zr;
    """)
    conn.execute("""
        CREATE TEMP TABLE tmp_stat AS
        SELECT code, COUNT(*) n, AVG(%s) mu,
               SQRT(AVG(%s*%s) - AVG(%s)*AVG(%s)) sd
        FROM feature WHERE %s AND date < ?
        GROUP BY code HAVING n >= 20 AND sd > 0
    """ % (feature, feature, feature, feature, feature, base_where), (split,))

    style_expr = "f.style"
    conn.execute("""
        CREATE TEMP TABLE tmp_z AS
        SELECT f.code, f.date, %s AS style,
               (f.%s - s.mu) / s.sd AS z
        FROM feature f JOIN tmp_stat s ON s.code = f.code
        WHERE f.%s
    """ % (style_expr, feature, base_where))
    if neutral:
        conn.execute("""
            CREATE TEMP TABLE tmp_zn AS
            SELECT code, date, style,
                   z - AVG(z) OVER (PARTITION BY date, style) AS z
            FROM tmp_z;
        """)
        conn.execute("DROP TABLE tmp_z")
        conn.execute("ALTER TABLE tmp_zn RENAME TO tmp_z")

    in_sample = conn.execute(
        "SELECT z FROM tmp_z WHERE date < ? ORDER BY z", (split,)).fetchall()
    if len(in_sample) < A.MIN_SAMPLES * 4:
        return None
    edges = [quantile_from(in_sample, q) for q in (0.2, 0.4, 0.6, 0.8)]
    if any(e is None for e in edges):
        return None

    conn.execute("""
        CREATE TEMP TABLE tmp_zb AS
        SELECT code, date, style,
               CASE WHEN z > ? THEN 4 WHEN z > ? THEN 3 WHEN z > ? THEN 2
                    WHEN z > ? THEN 1 ELSE 0 END AS bucket
        FROM tmp_z
    """, (edges[3], edges[2], edges[1], edges[0]))

    rows = conn.execute("""
        SELECT b.date, b.bucket, COUNT(*) n, AVG(f.ret) mean_ret
        FROM tmp_zb b JOIN fwd f ON f.code = b.code AND f.date = b.date
        GROUP BY b.date, b.bucket
    """).fetchall()

    per_bucket = {}
    all_means = {}
    for date, bucket, n, mean in rows:
        per_bucket.setdefault(bucket, []).append(mean)
        all_means.setdefault(date, []).append((bucket, n, mean))

    # IC：每日横截面 Pearson（z 与 ret），先用 SQL 聚合再在 Python 里算相关系数
    conn.execute("""
        CREATE TEMP TABLE tmp_zr AS
        SELECT b.code, b.date, b.bucket, b.style, z.z, f.ret
        FROM tmp_zb b
        JOIN tmp_z z ON z.code = b.code AND z.date = b.date
        JOIN fwd f ON f.code = b.code AND f.date = b.date
    """)
    ic_rows = conn.execute("""
        SELECT date, COUNT(*) n, SUM(z*ret) sxy, SUM(z) sx, SUM(ret) sy,
               SUM(z*z) sxx, SUM(ret*ret) syy
        FROM tmp_zr GROUP BY date HAVING n >= 20
    """).fetchall()

    ics = []
    for date, n, sxy, sx, sy, sxx, syy in ic_rows:
        denom = math.sqrt(max(n * sxx - sx * sx, 0) * max(n * syy - sy * sy, 0))
        if denom > 0:
            ics.append((n * sxy - sx * sy) / denom)

    def scope_cells(day_means):
        cells = []
        for bucket in range(5):
            values = day_means.get(bucket, [])
            if not values:
                continue
            mean = sum(values) / len(values)
            m = len(values)
            var = sum((v - mean) ** 2 for v in values) / (m - 1) if m > 1 else 0.0
            std = var ** 0.5
            t = mean / (std / m ** 0.5) if std > 0 and m > 1 else None
            cells.append({"bucket": bucket, "days": m,
                          "mean_excess": mean, "t": t,
                          "net": mean - A.COST,
                          "money": (mean - A.COST) * 10000,
                          "n": None})
        return cells

    out_means, in_means, all_means_b = {}, {}, {}
    for date, bucket, n, mean in rows:
        all_means_b.setdefault(bucket, []).append(mean)
        if date >= split:
            out_means.setdefault(bucket, []).append(mean)
        else:
            in_means.setdefault(bucket, []).append(mean)

    result = {"feature": feature, "label": label,
              "n": conn.execute("SELECT COUNT(*) FROM tmp_zr").fetchone()[0],
              "days": len(dates), "date_start": dates[0], "date_end": dates[-1],
              "split_date": split, "neutral": neutral, "style": style}
    for name, means in (("out", out_means), ("in", in_means), ("all", all_means_b)):
        cells = scope_cells(means)
        total = sum(len(v) for v in means.values())
        for c in cells:
            c["n"] = conn.execute(
                "SELECT COUNT(*) FROM tmp_zr WHERE bucket=? AND date %s ?"
                % (">=" if name == "out" else "<"), (c["bucket"], split)).fetchone()[0]
        spread = (cells[-1]["mean_excess"] - cells[0]["mean_excess"]) if len(cells) >= 2 else None
        best = max(cells, key=lambda c: c["net"]) if cells else None
        result[name] = {"cells": cells, "spread": spread, "best": best,
                        "total_days": total}

    # IC 只在样本外日期上汇总
    out_dates = {d for d in all_means if d >= split}
    out_ics = [ic for (d, *_), ic in zip(ic_rows, ics) if d in out_dates]
    ic_mean = sum(out_ics) / len(out_ics) if out_ics else None
    ic_t = None
    if len(out_ics) >= 5:
        m = ic_mean
        sd = (sum((x - m) ** 2 for x in out_ics) / (len(out_ics) - 1)) ** 0.5
        ic_t = m / (sd / len(out_ics) ** 0.5) if sd > 0 else None
    result["ic"] = ic_mean
    result["ic_t"] = ic_t
    result["ic_days"] = len(out_ics)

    o = result["out"]
    cells = o["cells"]
    rho, extreme_t, min_n = None, 0.0, 0
    if len(cells) == A.BUCKETS:
        rho = A.spearman(list(range(len(cells))), [c["mean_excess"] for c in cells])
        extreme_t = max(abs(cells[0]["t"] or 0), abs(cells[-1]["t"] or 0))
        min_n = min(c["n"] for c in cells)
    passed = (o["spread"] is not None and abs(o["spread"]) > A.SPREAD_MIN
              and o["best"] and o["best"]["net"] > 0
              and rho is not None and abs(rho) >= 0.8
              and extreme_t >= 2.0 and min_n >= 100)
    result["mono_rho"] = rho
    result["extreme_t"] = extreme_t
    result["min_n"] = min_n
    result["verdict"] = "PASS" if passed else "FAIL"
    return result


def main(argv):
    parser = argparse.ArgumentParser(description="资金指标检验（全市场）")
    parser.add_argument("--feature", action="append", default=[])
    parser.add_argument("--neutral", action="store_true", help="加行业中性化")
    parser.add_argument("--by-style", action="store_true", help="按 5 风格组分别跑")
    parser.add_argument("--rebuild-fwd", action="store_true")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)

    if args.list:
        for key, label in CONTINUOUS:
            print("  连续型  %-22s %s" % (key, label))
        for key, label in EVENT:
            print("  事件型  %-22s %s" % (key, label))
        return 0

    if not os.path.exists(FEATURE_DB):
        print("没有 features.db，先跑 python scripts\\features.py")
        return 2

    store.ensure_dirs()
    ac = sqlite3.connect(ANALYSIS_DB)
    ac.execute("PRAGMA journal_mode=WAL")
    ac.execute("ATTACH DATABASE ? AS fdb", (FEATURE_DB,))

    if args.rebuild_fwd or not ac.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='fwd'").fetchone()[0]:
        print("构建次日超额收益表（只做一次）……")
        kc = sqlite3.connect("file:%s?mode=ro" % KLINE_DB.replace("\\", "/"), uri=True)
        build_fwd(ac, kc)
        kc.close()
    else:
        print("复用已有收益表：%d 行" % ac.execute("SELECT COUNT(*) FROM fwd").fetchone()[0])

    # SQLite 的视图不能跨库引用，所以把 feature 复制进分析库再用（约 120MB 磁盘）
    ac.execute("DROP TABLE IF EXISTS main.feature")
    ac.execute("CREATE TABLE main.feature AS SELECT * FROM fdb.feature")
    ac.execute("DETACH DATABASE fdb")
    ac.commit()

    targets = args.feature or [k for k, _ in ALL]
    labels = dict(ALL)
    results = []
    for feature in targets:
        label = labels.get(feature, feature)
        res = analyse_feature(ac, feature, label, neutral=args.neutral)
        if not res:
            print("  %-22s 样本不足，跳过" % feature)
            continue
        results.append(res)
        o = res["out"]
        print("  %-22s n=%-8d %s~%s 样本外起 %s 跨档差=%+.3fpp 净=%+.3fpp IC=%s rho=%s t=%.2f → %s"
              % (feature, res["n"], res["date_start"][5:], res["date_end"][5:],
                 res["split_date"][5:],
                 (o["spread"] or 0) * 100,
                 ((o["best"]["net"] if o["best"] else 0) or 0) * 100,
                 "—" if res["ic"] is None else "%.4f" % res["ic"],
                 "—" if res["mono_rho"] is None else "%.2f" % abs(res["mono_rho"]),
                 res["extreme_t"], res["verdict"]))

    if not results:
        print("没有可检验的指标")
        return 2

    os.makedirs(os.path.join(store.ROOT, "reports"), exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(store.ROOT, "reports", "资金指标_%s.md" % stamp)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render(results, args))
    print("\n报告：%s" % path)
    ac.close()
    return 0


def render(results, args):
    lines = ["# 资金指标检验（全市场）：%s" % datetime.now().strftime("%Y-%m-%d %H:%M"), "",
             "- 中性化：%s；分组：%s" % ("是" if args.neutral else "否",
                                      "5 风格组" if args.by_style else "无"),
             "- 收益：T+1 开盘买 → 收盘卖，减上证；剔除涨停/一字板",
             "- 成本 %.2f%%；判定：跨档差>%.2fpp 且 最优档扣成本后>0 且 |rho|≥0.8 且 极端档|t|≥2 且 每档≥100"
             % (A.COST * 100, A.SPREAD_MIN * 100), "",
             "| 指标 | 样本 | 区间 | 天数 | 跨档差 | 最优档净 | IC | rho | 极端t | 最小档样本 | 结论 |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for res in results:
        o = res["out"]
        lines.append("| %s | %d | %s~%s | %d | %+.3fpp | %+.3fpp | %s | %s | %.2f | %d | **%s** |" % (
            res["label"], res["n"], res["date_start"], res["date_end"], res["days"],
            (o["spread"] or 0) * 100,
            ((o["best"]["net"] if o["best"] else 0) or 0) * 100,
            "—" if res["ic"] is None else "%.4f" % res["ic"],
            "—" if res["mono_rho"] is None else "%.2f" % abs(res["mono_rho"]),
            res["extreme_t"], res["min_n"], res["verdict"]))
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
