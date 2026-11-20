# -*- coding: utf-8 -*-
"""status.py —— 随时看数据攒到哪了（不联网，只读本地文件）。

用法：
    python scripts\\status.py
    python scripts\\status.py --watch    # 每 30 秒刷新一次，Ctrl+C 退出
"""
import argparse
import csv
import os
import time
from datetime import datetime

import store

DATASETS = (
    ("longhu_stock", "龙虎榜个股"),
    ("longhu_seat", "龙虎榜席位"),
    ("rzrq_market", "两融总量"),
    ("rzrq_stock", "两融个股"),
    ("dzjy", "大宗交易"),
    ("etf_share", "ETF份额"),
)


def scan(dataset):
    path = store.path_of(dataset)
    if not os.path.exists(path):
        return None
    dates = set()
    rows = 0
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            rows += 1
            day = row.get("交易日期")
            if day:
                dates.add(day)
    if not dates:
        return {"rows": rows, "days": 0, "start": "-", "end": "-", "size": os.path.getsize(path)}
    return {"rows": rows, "days": len(dates), "start": min(dates), "end": max(dates),
            "size": os.path.getsize(path)}


def features_info():
    path = os.path.join(store.DATA_DIR, "features.csv")
    if not os.path.exists(path):
        return None
    rows = 0
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        for _ in csv.DictReader(fh):
            rows += 1
    return {"rows": rows, "time": datetime.fromtimestamp(os.path.getmtime(path)).strftime("%m-%d %H:%M")}


def render():
    lines = ["数据状态 %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ""]
    lines.append("%-10s %10s %6s  %-12s %-12s %9s" % ("数据集", "行数", "天数", "最早", "最新", "文件"))
    lines.append("-" * 68)
    for name, label in DATASETS:
        info = scan(name)
        if not info:
            lines.append("%-10s %10s %6s  %-12s %-12s %9s" % (label, "-", "-", "无文件", "", ""))
            continue
        lines.append("%-10s %10d %6d  %-12s %-12s %8.1fMB"
                     % (label, info["rows"], info["days"], info["start"], info["end"],
                        info["size"] / 1024 / 1024))
    feat = features_info()
    lines.append("")
    lines.append("特征表：%s" % ("还没生成" if not feat else
                                "%d 行（%s 生成）" % (feat["rows"], feat["time"])))
    kline = os.path.join(store.DATA_DIR, "kline.db")
    if os.path.exists(kline):
        lines.append("日线库：%.1fMB" % (os.path.getsize(kline) / 1024 / 1024))
    return "\n".join(lines)


def main(argv):
    parser = argparse.ArgumentParser(description="看数据状态")
    parser.add_argument("--watch", action="store_true", help="每 30 秒刷新")
    args = parser.parse_args(argv)
    if not args.watch:
        print(render())
        return 0
    try:
        while True:
            os.system("cls")
            print(render())
            print("\n（每 30 秒刷新，Ctrl+C 退出）")
            time.sleep(30)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
