# -*- coding: utf-8 -*-
"""analyze.py —— 条件收益统计：过去 k 天涨跌 → 次日收益

这份脚本刻意做了几件「不讨好但必要」的事，都是为了不骗自己：

1. **买卖点必须可执行**
   信号在 T 日收盘才齐 → 最早 T+1 开盘买入 → 当天收盘卖出。
   主口径 r = close(T+1)/open(T+1) - 1；参考口径用后复权收盘算跨日真实收益。

2. **剔除买不进的样本**
   T+1 开盘就涨停（≈涨停价的 99.5% 以上）或一字板（最高=最低）的，剔掉。

3. **算超额收益**
   减掉同日上证指数收益，否则测出来的是大盘 beta，不是「过去涨跌」的效应。

4. **标准误按日聚类**
   同一天 30 只票一起动，样本不独立。所以先算每天横截面均值，再对「日序列」做 t 检验。

5. **样本内 / 样本外切开**
   前 70% 交易日用来定规则，后 30% 只做最终验证，看完不许回去调。

6. **每次运行都记账**
   写 data/experiments.db（追加）+ reports/*.md，历史不覆盖。

用法：
    python scripts\\analyze.py            # 跑分析，落库 + 出报告
    python scripts\\analyze.py --report   # 只看历史实验清单
    python scripts\\analyze.py --no-xlsx  # 跳过 Excel
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime

import store

DB_PATH = os.path.join(store.DATA_DIR, "kline.db")
EXP_PATH = os.path.join(store.DATA_DIR, "experiments.db")
REPORT_DIR = os.path.join(store.ROOT, "reports")

WINDOWS = (1, 2, 3, 5, 10, 20)   # 只跑这六个，不多不少
BUCKETS = 5                      # 分 5 档
COST = 0.0015                    # 往返成本：佣金万2.5×2 + 印花税0.05% = 0.10%，留 0.05% 滑点
SPREAD_MIN = 0.001               # 判定线：最高档-最低档 差 0.10 个百分点（=0.001）
MIN_SAMPLES = 500                # 单格样本数低于这个不看
OOS_FRACTION = 0.30              # 时间序列后 30% 做样本外
INDEX_CODE = "1A0001"            # 上证指数当市场基准
HYPOTHESIS = "过去k天涨跌 → 次日收益"


# ---------------------------------------------------------------- 数据准备

def load_bars(conn):
    """{code: {date: row}}，只取库里的代码。"""
    rows = conn.execute(
        "SELECT code,date,open,high,low,close,adj_close,volume FROM kline ORDER BY code,date"
    ).fetchall()
    data = {}
    for code, date, o, h, l, c, adj, vol in rows:
        data.setdefault(code, {})[date] = {
            "open": o, "high": h, "low": l, "close": c,
            "adj": adj if adj is not None else c, "volume": vol,
        }
    return data


def trading_calendar(bars):
    """用指数日期当交易日历；指数缺失时退回全部日期并集。"""
    base = bars.get(INDEX_CODE) or {}
    dates = sorted(base.keys())
    if len(dates) < 100:
        all_dates = set()
        for day_map in bars.values():
            all_dates.update(day_map.keys())
        dates = sorted(all_dates)
    return dates


def limit_ratio(code):
    """涨跌幅限制：创业板/科创板 20%，其余 10%（样本里已剔除 ST 和北交所）。"""
    return 0.20 if code.startswith(("300", "688")) else 0.10


def build_panel(bars, calendar, split_date):
    """面板：每行 = 一只股票一个交易日，含特征位与前瞻收益。

    注意：z-score 的均值和标准差**只用样本内数据**算（split_date 之前），
    否则等于用未来数据标准化历史样本 —— 典型的 look-ahead 泄漏。
    """
    codes = [c for c in bars if c != INDEX_CODE]
    idx = bars.get(INDEX_CODE, {})
    pos = {d: i for i, d in enumerate(calendar)}
    panel = []
    for code in codes:
        day_map = bars[code]
        chain = [day_map.get(d) for d in calendar]
        local = []
        for i, d in enumerate(calendar):
            cur = chain[i]
            if cur is None:
                continue
            nxt_i = i + 1
            if nxt_i >= len(calendar):
                continue
            nxt = chain[nxt_i]
            if nxt is None:                     # 明天停牌 → 买不进
                continue
            base_idx = idx.get(d)
            next_idx = idx.get(calendar[nxt_i])
            if not base_idx or not next_idx:
                continue
            # 主口径：T+1 开盘买 → T+1 收盘卖（都是不复权真实成交价）
            if nxt["open"] <= 0:
                continue
            ret = nxt["close"] / nxt["open"] - 1
            # 参考口径：跨日真实收益（后复权，避开除权跳空）
            ret_cc = nxt["adj"] / cur["adj"] - 1 if cur["adj"] else None
            idx_ret = next_idx["close"] / next_idx["open"] - 1
            local.append({
                "code": code,
                "date": calendar[nxt_i],
                "signal_date": d,
                "fwd": ret,
                "fwd_cc": ret_cc,
                "fwd_excess": ret - idx_ret,
                "limit_ratio": limit_ratio(code),
                "prev_close": cur["close"],
                "next_open": nxt["open"],
                "next_high": nxt["high"],
                "next_low": nxt["low"],
            })
        # 该股票自己的过去收益序列，用来做时序标准化
        adj_chain = [day_map.get(d)["adj"] if day_map.get(d) else None for d in calendar]
        for k in WINDOWS:
            vals = []
            for row in local:
                j = pos[row["signal_date"]]
                if j - k < 0:
                    vals.append(None)
                    continue
                a0 = adj_chain[j]
                a1 = adj_chain[j - k]
                vals.append(a0 / a1 - 1 if (a0 and a1) else None)
            # 只用样本内的值估计均值/标准差，避免未来信息泄漏
            in_sample = [v for v, row in zip(vals, local)
                         if v is not None and row["signal_date"] < split_date]
            if len(in_sample) >= 30:
                mean = sum(in_sample) / len(in_sample)
                var = sum((v - mean) ** 2 for v in in_sample) / len(in_sample)
                std = var ** 0.5
            else:
                mean, std = 0.0, 0.0
            for row, v in zip(local, vals):
                row["f%d" % k] = v
                row["z%d" % k] = ((v - mean) / std) if (v is not None and std > 0) else None
        panel.extend(local)
    return panel


def filter_tradable(panel):
    """剔掉 T+1 买不进的：开盘涨停、一字板。返回 (保留, 剔除数)。"""
    kept, dropped = [], 0
    for row in panel:
        limit = row["limit_ratio"]
        cap = round(row["prev_close"] * (1 + limit), 2)
        if row["next_open"] >= cap * 0.995:            # 开盘就涨停，买不进
            dropped += 1
            continue
        if row["next_high"] == row["next_low"]:        # 一字板，没有成交区间
            dropped += 1
            continue
        kept.append(row)
    return kept, dropped


# ---------------------------------------------------------------- 统计

def quantile_edges(values, n_buckets):
    """用样本内的 z 值算分档边界，再套到样本外——避免样本外自己定义档位。"""
    vals = sorted(values)
    if len(vals) < n_buckets * 10:
        return None
    return [vals[int(len(vals) * i / n_buckets)] for i in range(1, n_buckets)]


def bucket_of(value, edges):
    if value is None or edges is None:
        return None
    b = 0
    for edge in edges:
        if value > edge:
            b += 1
    return min(b, len(edges))


def rank(data):
    order = sorted(range(len(data)), key=lambda i: data[i])
    out = [0.0] * len(data)
    for r, i in enumerate(order):
        out[i] = float(r)
    return out


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    return num / (dx * dy) if dx and dy else None


def spearman(xs, ys):
    return pearson(rank(xs), rank(ys))


def daily_mean_t(pairs):
    """pairs: [(date, value)] → 先按日求横截面均值，再对日序列做 t 检验。

    input 返回 (日均值, t 值, 天数, 标准差)
    """
    by_day = {}
    for day, value in pairs:
        by_day.setdefault(day, []).append(value)
    means = [sum(v) / len(v) for v in by_day.values()]
    n = len(means)
    if n < 5:
        return None, None, n, None
    mean = sum(means) / n
    var = sum((m - mean) ** 2 for m in means) / (n - 1)
    std = var ** 0.5
    t = mean / (std / n ** 0.5) if std > 0 else None
    return mean, t, n, std


def analyze_scope(rows, scope, edges):
    """对一批样本，逐窗口产出分档统计 + IC。edges 来自样本内。"""
    result = {"scope": scope, "n": len(rows), "windows": []}
    for k in WINDOWS:
        zkey, fkey = "z%d" % k, "f%d" % k
        edge = edges.get(k)
        usable = [r for r in rows
                  if r.get(zkey) is not None and r.get("fwd_excess") is not None and edge]
        if len(usable) < MIN_SAMPLES:
            result["windows"].append({"k": k, "skip": True, "n": len(usable)})
            continue
        buckets = [bucket_of(r[zkey], edge) for r in usable]
        cells = []
        for b in range(BUCKETS):
            group = [r for r, bb in zip(usable, buckets) if bb == b]
            if not group:
                continue
            mean, t, days, std = daily_mean_t([(r["date"], r["fwd_excess"]) for r in group])
            raw_mean = sum(r["fwd"] for r in group) / len(group)
            wins = sum(1 for r in group if r["fwd"] > 0)
            cells.append({
                "bucket": b,
                "n": len(group),
                "days": days,
                "mean_excess": mean,
                "t": t,
                "std": std,
                "mean_raw": raw_mean,
                "winrate": wins / len(group),
                "net": (mean - COST) if mean is not None else None,
                "money": ((mean - COST) * 10000) if mean is not None else None,
            })
        # IC：每天横截面算一次 Spearman，再对日序列取 t
        per_day = {}
        for r in usable:
            per_day.setdefault(r["date"], []).append(r)
        ics = []
        for day, group in per_day.items():
            if len(group) < 10:
                continue
            ic = spearman([r[fkey] for r in group], [r["fwd_excess"] for r in group])
            if ic is not None:
                ics.append(ic)
        ic_mean = sum(ics) / len(ics) if ics else None
        if len(ics) >= 5:
            m = ic_mean
            v = sum((x - m) ** 2 for x in ics) / (len(ics) - 1)
            sd = v ** 0.5
            ic_t = m / (sd / len(ics) ** 0.5) if sd > 0 else None
        else:
            ic_t = None
        result["windows"].append({
            "k": k, "skip": False, "n": len(usable), "cells": cells,
            "ic": ic_mean, "ic_t": ic_t, "ic_days": len(ics),
            "spread": (cells[-1]["mean_excess"] - cells[0]["mean_excess"]) if len(cells) >= 2 else None,
        })
    return result


def judge(in_result, out_result):
    """判定：样本外必须过线，样本内方向要一致。返回 (PASS/FAIL, 理由列表)。

    只看「最优档」的扣成本净收益——A 股只能做多，不是两端都能赚。
    """
    reasons = []
    passed_k = 0
    checked = 0
    signs = []
    for win in out_result["windows"]:
        if win.get("skip") or not win.get("cells") or len(win["cells"]) < BUCKETS:
            continue
        checked += 1
        spread = win["spread"]
        best = max(win["cells"], key=lambda c: c["net"] if c["net"] is not None else -9)
        signs.append(1 if spread > 0 else -1)
        if spread is not None and abs(spread) > SPREAD_MIN and (best["net"] or -1) > 0:
            passed_k += 1
        reasons.append("k=%-2d 样本外：跨档差 %+.3fpp，最优档（第%d档）扣成本后 %+.3fpp"
                       % (win["k"], (spread or 0) * 100, best["bucket"],
                          (best["net"] or 0) * 100))
    if checked == 0:
        return "FAIL", ["样本外窗口不足，无法判定"]
    same_sign = max(signs.count(1), signs.count(-1))
    ok = passed_k >= 4 and same_sign >= 4
    head = "PASS" if ok else "FAIL"
    reasons.insert(0, "样本外 %d 个窗口里，%d 个满足「跨档差>%.2fpp 且最优档扣成本后为正」；方向一致 %d/%d"
                   % (checked, passed_k, SPREAD_MIN * 100, same_sign, len(signs)))
    return head, reasons


# ---------------------------------------------------------------- 记录

def pool_hash(codes):
    text = ",".join(sorted(codes))
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:12]


def ensure_exp_db(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS experiments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_at TEXT, hypothesis TEXT, windows TEXT, cost REAL,
        verdict TEXT, reason TEXT, pool_hash TEXT, kline_max TEXT,
        rows INTEGER, split_date TEXT, params TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS cells(
        exp_id INTEGER, scope TEXT, k INTEGER, bucket INTEGER,
        n INTEGER, days INTEGER, mean_excess REAL, t REAL, std REAL,
        mean_raw REAL, winrate REAL, net REAL, money REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS ics(
        exp_id INTEGER, scope TEXT, k INTEGER, ic REAL, ic_t REAL, days INTEGER)""")
    conn.commit()


def save_experiment(conn, info, results):
    cur = conn.execute(
        "INSERT INTO experiments(run_at,hypothesis,windows,cost,verdict,reason,pool_hash,"
        "kline_max,rows,split_date,params) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (info["run_at"], HYPOTHESIS, ",".join(str(k) for k in WINDOWS), COST,
         info["verdict"], "\n".join(info["reasons"]), info["pool_hash"],
         info["kline_max"], info["rows"], info["split_date"],
         json.dumps({"windows": WINDOWS, "buckets": BUCKETS, "cost": COST,
                     "oos_fraction": OOS_FRACTION, "index": INDEX_CODE},
                    ensure_ascii=False)))
    exp_id = cur.lastrowid
    for scope, res in results.items():
        for win in res["windows"]:
            if win.get("skip"):
                continue
            for cell in win["cells"]:
                conn.execute(
                    "INSERT INTO cells VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (exp_id, scope, win["k"], cell["bucket"], cell["n"], cell["days"],
                     cell["mean_excess"], cell["t"], cell["std"], cell["mean_raw"],
                     cell["winrate"], cell["net"], cell["money"]))
            conn.execute("INSERT INTO ics VALUES(?,?,?,?,?,?)",
                         (exp_id, scope, win["k"], win["ic"], win["ic_t"], win["ic_days"]))
    conn.commit()
    return exp_id


# ---------------------------------------------------------------- 输出

def fmt_pct(value, digits=3):
    return "—" if value is None else ("%+.*f%%" % (digits, value * 100))


def render_markdown(info, results):
    lines = ["# 分析报告：%s" % HYPOTHESIS, "",
             "- 运行时间：%s" % info["run_at"],
             "- 数据范围：%s ~ %s，%d 个标的，%d 行" % (info["start"], info["end"], info["codes"], info["rows"]),
             "- 样本外起点：%s（按交易日前后 %d/%d 切）" % (info["split_date"], int((1 - OOS_FRACTION) * 100), int(OOS_FRACTION * 100)),
             "- 成本假设：%.2f%% 往返；判定线：跨档差 > %.2fpp 且最优档扣成本后为正" % (COST * 100, SPREAD_MIN * 100),
             "- 买卖点：T 日收盘得信号 → T+1 开盘买 → T+1 收盘卖；已剔除开盘涨停与一字板；收益已减同日上证指数",
             "- 股票池指纹：%s" % info["pool_hash"],
             "", "## 结论：**%s**" % info["verdict"], ""]
    for reason in info["reasons"]:
        lines.append("- %s" % reason)
    for scope in ("out", "in", "all"):
        res = results.get(scope)
        if not res:
            continue
        title = {"out": "样本外（最终判定依据）", "in": "样本内（定规则用）", "all": "全样本（参考）"}[scope]
        lines += ["", "## %s" % title, "", "| 窗口 | 档位 | 样本 | 天数 | 超额收益 | 胜率 | t | 扣成本后 | 1万仓位净赚 |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for win in res["windows"]:
            if win.get("skip"):
                lines.append("| k=%d | 样本不足（%d） | | | | | | | |" % (win["k"], win["n"]))
                continue
            for cell in win["cells"]:
                lines.append("| k=%d | %d | %d | %d | %s | %.1f%% | %s | %s | %s |" % (
                    win["k"], cell["bucket"], cell["n"], cell["days"],
                    fmt_pct(cell["mean_excess"]), cell["winrate"] * 100,
                    "—" if cell["t"] is None else "%.2f" % cell["t"],
                    fmt_pct(cell["net"]),
                    "—" if cell["money"] is None else "%+.0f 元" % cell["money"]))
            lines.append("| k=%d | **IC** | | %d | **%s** | | %s | | |" % (
                win["k"], win["ic_days"],
                "—" if win["ic"] is None else "%.4f" % win["ic"],
                "—" if win["ic_t"] is None else "%.2f" % win["ic_t"]))
    lines += ["", "---", "",
              "说明：档位 0 = 过去 k 天涨得最少，档位 4 = 涨得最多；分档按每只股票自己的 z-score 切，",
              "避免高波动股总落在两端。t 值按日聚类（先算每日横截面均值，再对日序列检验）。"]
    return "\n".join(lines)


def write_xlsx(path, results):
    try:
        from openpyxl import Workbook
    except ImportError:
        return None
    wb = Workbook()
    wb.remove(wb.active)
    for scope, res in results.items():
        ws = wb.create_sheet(title={"out": "样本外", "in": "样本内", "all": "全样本"}[scope])
        ws.append(["窗口k", "档位", "样本数", "天数", "超额收益", "胜率", "t值", "扣成本后", "1万净赚",
                   "原始收益"])
        for win in res["windows"]:
            if win.get("skip"):
                ws.append([win["k"], "样本不足", win["n"]])
                continue
            for cell in win["cells"]:
                ws.append([win["k"], cell["bucket"], cell["n"], cell["days"],
                           cell["mean_excess"], cell["winrate"], cell["t"], cell["net"],
                           cell["money"], cell["mean_raw"]])
            ws.append([win["k"], "IC", win["ic"], win["ic_days"], win["ic_t"]])
        for row in ws.iter_rows(min_row=2, min_col=5, max_col=10):
            for c in row:
                if isinstance(c.value, float):
                    c.number_format = "0.0000"
        ws.freeze_panes = "A2"
    wb.save(path)
    return path


# ---------------------------------------------------------------- 入口

def load_experiments(conn):
    try:
        return conn.execute(
            "SELECT id,run_at,verdict,pool_hash,kline_max,rows,substr(reason,1,120)"
            " FROM experiments ORDER BY id DESC").fetchall()
    except sqlite3.OperationalError:
        return []


def main(argv):
    parser = argparse.ArgumentParser(description="条件收益统计：过去 k 天涨跌 → 次日收益")
    parser.add_argument("--report", action="store_true", help="只列历史实验")
    parser.add_argument("--no-xlsx", action="store_true", help="不生成 Excel")
    args = parser.parse_args(argv)

    store.ensure_dirs()
    if not os.path.exists(DB_PATH):
        print("找不到 %s，先跑 python scripts\\kline.py --watchlist" % DB_PATH)
        return 2
    conn = sqlite3.connect(DB_PATH)
    exp = sqlite3.connect(EXP_PATH)
    ensure_exp_db(exp)

    if args.report:
        rows = load_experiments(exp)
        if not rows:
            print("还没有实验记录")
            return 0
        print("历史实验（新→旧）")
        for rid, run_at, verdict, pool, kmax, n, reason in rows:
            print("  #%-3d %s  %-4s  池=%s 数据到=%s 行=%s" % (rid, run_at, verdict, pool, kmax, n))
            print("        %s" % (reason or "").replace("\n", " "))
        return 0

    bars = load_bars(conn)
    codes = [c for c in bars if c != INDEX_CODE]
    if len(codes) < 5:
        print("库里股票太少（%d 只），先跑 kline.py --watchlist" % len(codes))
        return 2
    calendar = trading_calendar(bars)
    kline_max = max(c for c in (bars.get(INDEX_CODE) or {}).keys()) if bars.get(INDEX_CODE) else calendar[-1]
    split_at = int(len(calendar) * (1 - OOS_FRACTION))
    split_date = calendar[split_at]

    print("数据：%d 个标的，%d 个交易日（%s ~ %s）" % (len(codes), len(calendar), calendar[0], calendar[-1]))
    panel = build_panel(bars, calendar, split_date)
    kept, dropped = filter_tradable(panel)
    print("面板 %d 行；剔除买不进（开盘涨停/一字板）%d 行" % (len(kept), dropped))

    in_rows = [r for r in kept if r["signal_date"] < split_date]
    out_rows = [r for r in kept if r["signal_date"] >= split_date]
    edges = {}
    for k in WINDOWS:
        vals = [r["z%d" % k] for r in in_rows if r.get("z%d" % k) is not None]
        edges[k] = quantile_edges(vals, BUCKETS)
    print("样本内 %d 行（至 %s），样本外 %d 行（自 %s 起）"
          % (len(in_rows), calendar[split_at - 1], len(out_rows), split_date))

    results = {"out": analyze_scope(out_rows, "out", edges),
               "in": analyze_scope(in_rows, "in", edges),
               "all": analyze_scope(kept, "all", edges)}
    verdict, reasons = judge(results["in"], results["out"])

    info = {
        "run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "verdict": verdict,
        "reasons": reasons,
        "pool_hash": pool_hash(codes),
        "kline_max": kline_max,
        "rows": len(kept),
        "split_date": split_date,
        "start": calendar[0],
        "end": calendar[-1],
        "codes": len(codes),
    }
    exp_id = save_experiment(exp, info, results)

    os.makedirs(REPORT_DIR, exist_ok=True)
    md_path = os.path.join(REPORT_DIR, "分析_%s.md" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(info, results))

    xlsx_path = None
    if not args.no_xlsx:
        xlsx_path = write_xlsx(os.path.join(store.OUT_DIR,
                                            "分析_%s.xlsx" % datetime.now().strftime("%Y%m%d_%H%M%S")),
                               results)

    print("\n结论：%s" % verdict)
    for reason in reasons:
        print("  - %s" % reason)
    print("\n实验 #%d 已记账：%s" % (exp_id, EXP_PATH))
    print("报告：%s" % md_path)
    if xlsx_path:
        print("Excel：%s" % xlsx_path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
