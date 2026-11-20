# -*- coding: utf-8 -*-
"""analyze_features.py —— 检验「资金指标 → 次日收益」。

和 analyze.py 的区别：那边测的是价格自相关，这边测的是 data\\features.csv 里的资金指标。

口径（与用户确认过）：
- 收益：T 日收盘得信号 → T+1 开盘买 → T+1 收盘卖，减去同日上证指数收益
- 剔除 T+1 开盘涨停 / 一字板（买不进）
- 标准化：z-score 只用**样本内**数据；分档边界也只用样本内
- 判定：样本外 6??不，是「每个指标」—— 跨档差 > 0.10pp 且最优档扣成本后为正
- 记录：追加到 data\\experiments.db，报告写 reports/

用法：
    python scripts\\analyze_features.py                     # 跑全部连续型指标
    python scripts\\analyze_features.py --feature margin_growth_5
    python scripts\\analyze_features.py --list
"""
import argparse
import csv
import json
import os
import sqlite3
import sys
from datetime import datetime

import analyze as A
import store

FEATURE_CSV = os.path.join(store.DATA_DIR, "features.csv")

# 连续型指标（每天都有值，可做全样本分档）
CONTINUOUS = (
    ("margin_growth_5", "融资余额5日增速"),
    ("margin_growth_20", "融资余额20日增速"),
    ("margin_net_intensity", "融资净买入强度"),
)
# 事件型指标（只在上榜日有值，只在榜单样本内排序）
EVENT = (
    ("lhb_net_ratio", "龙虎榜净买占比"),
    ("lhb_inst_buy_cnt", "买方机构专用家数"),
    ("lhb_top5_buy_ratio", "买方前五集中度"),
)


def load_features():
    if not os.path.exists(FEATURE_CSV):
        return {}
    out = {}
    with open(FEATURE_CSV, "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            code, date = row.get("code"), row.get("date")
            if not code or not date:
                continue
            values = {}
            for key in ("margin_growth_5", "margin_growth_20", "margin_net_intensity",
                        "lhb_net_ratio", "lhb_inst_buy_cnt", "lhb_top5_buy_ratio",
                        "is_on_longhu"):
                raw = row.get(key)
                if raw not in ("", None):
                    try:
                        values[key] = float(raw)
                    except ValueError:
                        pass
            out[(code, date)] = values
    return out


def forward_returns(bars, calendar):
    """(code, signal_date) → 次日超额收益（T+1 开→收 减 同日指数）。"""
    idx = bars.get(A.INDEX_CODE, {})
    out = {}
    for i in range(len(calendar) - 1):
        d, d1 = calendar[i], calendar[i + 1]
        base, nxt = idx.get(d), idx.get(d1)
        if not base or not nxt or nxt["open"] <= 0:
            continue
        idx_ret = nxt["close"] / nxt["open"] - 1
        for code, day_map in bars.items():
            if code == A.INDEX_CODE:
                continue
            cur, nx = day_map.get(d), day_map.get(d1)
            if not cur or not nx or nx["open"] <= 0:
                continue
            cap = round(cur["close"] * (1 + A.limit_ratio(code)), 2)
            if nx["open"] >= cap * 0.995 or nx["high"] == nx["low"]:
                continue
            out[(code, d)] = nx["close"] / nx["open"] - 1 - idx_ret
    return out


def zscore_by_code(samples, split_date):
    """只用样本内数据算每只股票的均值/标准差，再套到全样本。"""
    stats = {}
    for code, date, value in samples:
        if date >= split_date:
            continue
        stats.setdefault(code, []).append(value)
    norm = {}
    for code, values in stats.items():
        n = len(values)
        if n < 20:
            continue
        mean = sum(values) / n
        var = sum((v - mean) ** 2 for v in values) / n
        norm[code] = (mean, var ** 0.5)
    out = []
    for code, date, value in samples:
        pair = norm.get(code)
        if not pair or pair[1] <= 0:
            continue
        out.append((code, date, (value - pair[0]) / pair[1]))
    return out


def analyze(feature, label, values, fwd, split_date=None, event_only=False):
    samples = []
    for (code, date), item in values.items():
        if feature not in item:
            continue
        key = (code, date)
        if key not in fwd:
            continue
        if event_only and not item.get("is_on_longhu"):
            continue
        samples.append((code, date, item[feature]))
    if len(samples) < A.MIN_SAMPLES:
        return None

    # 样本内外按「这个指标自己有数据的交易日」切，而不是按价格日历——
    # 否则回补不足时，样本内会几乎没有特征数据。
    dates = sorted({d for _c, d, _v in samples})
    if len(dates) < 60:
        return None
    split_date = dates[int(len(dates) * (1 - A.OOS_FRACTION))]

    normalised = zscore_by_code(samples, split_date)
    if len(normalised) < A.MIN_SAMPLES:
        return None
    values_only = [v for _c, d, v in normalised if d < split_date]
    edges = A.quantile_edges(values_only, A.BUCKETS)
    if not edges:
        return None

    def cells_for(chosen):
        buckets = {A.bucket_of(v, edges): [] for v in [0]}
        grouped = {}
        for code, date, z in chosen:
            b = A.bucket_of(z, edges)
            grouped.setdefault(b, []).append((date, fwd[(code, date)], fwd[(code, date)]))
        out = []
        for b in sorted(grouped):
            rows = grouped[b]
            mean, t, days, std = A.daily_mean_t([(d, ex) for d, ex, _ in rows])
            wins = sum(1 for _d, _e, _r in rows if _r > 0)
            out.append({
                "bucket": b, "n": len(rows), "days": days,
                "mean_excess": mean, "t": t, "std": std,
                "winrate": wins / len(rows),
                "net": (mean - A.COST) if mean is not None else None,
                "money": ((mean - A.COST) * 10000) if mean is not None else None,
            })
        return out

    out_rows = [s for s in normalised if s[1] >= split_date]
    in_rows = [s for s in normalised if s[1] < split_date]
    result = {"feature": feature, "label": label, "event_only": event_only,
              "n": len(normalised), "split_date": split_date,
              "date_start": dates[0], "date_end": dates[-1], "days": len(dates)}
    for scope, chosen in (("out", out_rows), ("in", in_rows), ("all", normalised)):
        cells = cells_for(chosen) if len(chosen) >= A.MIN_SAMPLES else []
        spread = (cells[-1]["mean_excess"] - cells[0]["mean_excess"]) if len(cells) >= 2 else None
        best = max(cells, key=lambda c: c["net"] if c["net"] is not None else -9) if cells else None
        # IC：每日横截面 Spearman
        per_day = {}
        for code, date, z in chosen:
            per_day.setdefault(date, []).append((z, fwd[(code, date)]))
        ics = []
        for date, group in per_day.items():
            if len(group) < 10:
                continue
            ic = A.spearman([g[0] for g in group], [g[1] for g in group])
            if ic is not None:
                ics.append(ic)
        ic_mean = sum(ics) / len(ics) if ics else None
        result[scope] = {"cells": cells, "spread": spread, "best": best,
                         "ic": ic_mean, "ic_days": len(ics)}
    o = result["out"]
    cells = o["cells"]
    # 判定要比「跨档差>0.1pp」严得多，否则 U 形噪声也会被判成通过：
    # ① 单调性：档位与超额收益的秩相关 |rho| >= 0.8
    # ② 极端档的 |t| >= 2
    # ③ 每档样本 >= 100
    rho = None
    monotone = False
    extreme_t = 0.0
    min_n = 0
    if len(cells) == A.BUCKETS:
        rho = A.spearman(list(range(len(cells))), [c["mean_excess"] or 0 for c in cells])
        monotone = rho is not None and abs(rho) >= 0.8
        extreme_t = max(abs(cells[0]["t"] or 0), abs(cells[-1]["t"] or 0))
        min_n = min(c["n"] for c in cells)
    passed = (o["spread"] is not None and abs(o["spread"]) > A.SPREAD_MIN
              and o["best"] and (o["best"]["net"] or -1) > 0
              and monotone and extreme_t >= 2.0 and min_n >= 100)
    result["mono_rho"] = rho
    result["extreme_t"] = extreme_t
    result["min_n"] = min_n
    result["verdict"] = "PASS" if passed else "FAIL"
    return result


def save(results, info):
    conn = sqlite3.connect(os.path.join(store.DATA_DIR, "experiments.db"))
    A.ensure_exp_db(conn)
    for res in results:
        reason = []
        o = res["out"]
        if o["spread"] is None:
            reason.append("样本外样本不足")
        else:
            reason.append("样本外跨档差 %+.3fpp，最优档（第%d档）扣成本后 %+.3fpp"
                          % (o["spread"] * 100, o["best"]["bucket"], (o["best"]["net"] or 0) * 100))
            reason.append("单调性 |rho|=%s（需≥0.8）：%s"
                          % ("—" if res["mono_rho"] is None else "%.2f" % abs(res["mono_rho"]),
                             "通过" if res["mono_rho"] is not None and abs(res["mono_rho"]) >= 0.8 else "不通过"))
            reason.append("极端档 |t|=%.2f（需≥2）；最小档样本 %d（需≥100）"
                          % (res["extreme_t"], res["min_n"]))
            reason.append("样本外 IC %s（%d 天）"
                          % ("—" if o["ic"] is None else "%.4f" % o["ic"], o["ic_days"]))
        cur = conn.execute(
            "INSERT INTO experiments(run_at,hypothesis,windows,cost,verdict,reason,pool_hash,"
            "kline_max,rows,split_date,params) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (info["run_at"], res["label"], "-", A.COST, res["verdict"], "\n".join(reason),
             info["pool_hash"], info["kline_max"], res["n"], info["split_date"],
             json.dumps({"feature": res["feature"], "event_only": res["event_only"],
                         "buckets": A.BUCKETS, "cost": A.COST}, ensure_ascii=False)))
        exp_id = cur.lastrowid
        for scope in ("out", "in", "all"):
            for cell in res[scope]["cells"]:
                conn.execute("INSERT INTO cells VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (exp_id, scope, 0, cell["bucket"], cell["n"], cell["days"],
                              cell["mean_excess"], cell["t"], cell["std"], None,
                              cell["winrate"], cell["net"], cell["money"]))
    conn.commit()
    conn.close()


def render(results, info):
    lines = ["# 资金指标检验：%s" % info["run_at"], "",
             "- 股票数：%d；价格数据 %s ~ %s" % (info["codes"], info["start"], info["end"]),
             "- 收益口径：T+1 开盘买 → 收盘卖，减同日上证指数；已剔除涨停/一字板",
             "- 成本：%.2f%%；判定线：跨档差 > %.2fpp 且最优档扣成本后为正" % (A.COST * 100, A.SPREAD_MIN * 100),
             "- 样本内外按每个指标自己的交易日 70/30 切（回补深度决定可用天数）",
             ""]
    lines.append("| 指标 | 类型 | 样本 | 数据区间 | 天数 | 样本外起点 | 跨档差 | 最优档扣成本后 | IC | 结论 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for res in results:
        o = res["out"]
        lines.append("| %s | %s | %d | %s~%s | %d | %s | %s | %s | %s | **%s** |" % (
            res["label"], "事件型" if res["event_only"] else "连续型", res["n"],
            res["date_start"], res["date_end"], res["days"], res["split_date"],
            "—" if o["spread"] is None else "%+.3fpp" % (o["spread"] * 100),
            "—" if not o["best"] else "%+.3fpp" % ((o["best"]["net"] or 0) * 100),
            "—" if o["ic"] is None else "%.4f" % o["ic"],
            res["verdict"]))
    lines.append("")
    for res in results:
        if res["verdict"] != "PASS":
            continue
        lines.append("## 通过：%s" % res["label"])
        lines.append("| 档位 | 样本 | 天数 | 超额收益 | 胜率 | t | 扣成本后 | 1万净赚 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for cell in res["out"]["cells"]:
            lines.append("| %d | %d | %d | %s | %.1f%% | %s | %s | %s |" % (
                cell["bucket"], cell["n"], cell["days"],
                "%+.3f%%" % (cell["mean_excess"] * 100) if cell["mean_excess"] is not None else "—",
                cell["winrate"] * 100,
                "—" if cell["t"] is None else "%.2f" % cell["t"],
                "%+.3f%%" % (cell["net"] * 100) if cell["net"] is not None else "—",
                "%+.0f 元" % cell["money"] if cell["money"] is not None else "—"))
        lines.append("")
    return "\n".join(lines)


def main(argv):
    parser = argparse.ArgumentParser(description="资金指标检验")
    parser.add_argument("--feature", action="append", default=[])
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)

    pool = dict(CONTINUOUS)
    pool.update(dict(EVENT))
    if args.list:
        for key, label in CONTINUOUS:
            print("  连续型  %-22s %s" % (key, label))
        for key, label in EVENT:
            print("  事件型  %-22s %s" % (key, label))
        return 0

    features = load_features()
    if not features:
        print("没有 features.csv，先跑 python scripts\\features.py")
        return 2
    conn = sqlite3.connect(os.path.join(store.DATA_DIR, "kline.db"))
    bars = A.load_bars(conn)
    calendar = A.trading_calendar(bars)
    split_at = int(len(calendar) * (1 - A.OOS_FRACTION))
    split_date = calendar[split_at]
    fwd = forward_returns(bars, calendar)
    print("特征 %d 条，可交易样本 %d 条，样本外起点 %s" % (len(features), len(fwd), split_date))

    chosen = args.feature or list(pool)
    results = []
    for feature in chosen:
        label = pool.get(feature, feature)
        res = analyze(feature, label, features, fwd, split_date,
                      event_only=feature in dict(EVENT))
        if not res:
            print("  %-22s 样本不足，跳过" % feature)
            continue
        results.append(res)
        o = res["out"]
        print("  %-22s n=%-6d %s~%s 样本外起 %s 跨档差=%s 净收益=%s IC=%s → %s" % (
            feature, res["n"], res["date_start"][5:], res["date_end"][5:], res["split_date"][5:],
            "—" if o["spread"] is None else "%+.3fpp" % (o["spread"] * 100),
            "—" if not o["best"] else "%+.3fpp" % ((o["best"]["net"] or 0) * 100),
            "—" if o["ic"] is None else "%.4f" % o["ic"],
            res["verdict"]))

    if not results:
        print("没有可检验的指标")
        return 2

    codes = {c for c in bars if c != A.INDEX_CODE}
    info = {
        "run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "split_date": split_date,
        "codes": len(codes),
        "start": calendar[0],
        "end": calendar[-1],
        "pool_hash": A.pool_hash(codes),
        "kline_max": calendar[-1],
    }
    save(results, info)
    os.makedirs(os.path.join(store.ROOT, "reports"), exist_ok=True)
    path = os.path.join(store.ROOT, "reports", "资金指标_%s.md" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render(results, info))
    print("\n报告：%s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
