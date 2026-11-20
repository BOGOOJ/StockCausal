# -*- coding: utf-8 -*-
"""CSV 落库：按「交易日整段替换」写入，带行数守卫和原子写。"""
import csv
import os
import shutil
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
BAK_DIR = os.path.join(DATA_DIR, "_bak")
RAW_DIR = os.path.join(DATA_DIR, "raw")
LOG_DIR = os.path.join(ROOT, "logs")
OUT_DIR = os.path.join(ROOT, "out")

# 每张表的固定列顺序：列名里自带单位，避免混源口径打架
COLUMNS = {
    "longhu_stock": ["交易日期", "序号", "上榜标记", "代码", "名称", "现价", "涨跌幅",
                     "成交金额(亿元)", "龙虎榜净买入额(万元)", "上榜原因", "数据来源", "抓取时间"],
    "longhu_seat": ["交易日期", "代码", "名称", "方向", "排名", "营业部",
                    "买入额(万元)", "卖出额(万元)", "净额(万元)", "上榜原因", "数据来源", "抓取时间"],
    "rzrq_market": ["交易日期", "融资余额_上海(亿元)", "融资余额_深圳(亿元)", "融资余额_北京(亿元)",
                    "融资余额_合计(亿元)", "融资买入额_上海(亿元)", "融资买入额_深圳(亿元)",
                    "融资买入额_北京(亿元)", "融资买入额_合计(亿元)", "融券余额_上海(亿元)",
                    "融券余额_深圳(亿元)", "融券余额_北京(亿元)", "融券余额_合计(亿元)",
                    "融资融券余额_上海(亿元)", "融资融券余额_深圳(亿元)", "融资融券余额_北京(亿元)",
                    "融资融券余额_合计(亿元)", "数据来源", "抓取时间"],
    "rzrq_stock": ["交易日期", "代码", "名称", "市场", "融资余额(元)", "融资买入额(元)", "融资偿还额(元)",
                   "融资净买入(元)", "融券余量(股)", "融券余额(元)", "融资融券余额(元)",
                   "数据来源", "抓取时间"],
    "dzjy": ["交易日期", "代码", "名称", "成交价(元)", "成交量(万股)", "成交额(万元)", "溢价率",
             "收盘价(元)", "买方营业部", "卖方营业部", "数据来源", "抓取时间"],
    "etf_share": ["交易日期", "交易所", "代码", "名称", "总份额(万份)", "类型", "管理人",
                  "数据来源", "抓取时间"],
}

MARKET_COLUMNS = COLUMNS["rzrq_market"][1:-2]
for _i, _name in enumerate(MARKET_COLUMNS):
    pass


def ensure_dirs():
    for path in (DATA_DIR, BAK_DIR, RAW_DIR, LOG_DIR, OUT_DIR):
        os.makedirs(path, exist_ok=True)


def path_of(dataset):
    return os.path.join(DATA_DIR, dataset + ".csv")


def read_rows(dataset):
    path = path_of(dataset)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _backup(path):
    if not os.path.exists(path):
        return
    os.makedirs(BAK_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    shutil.copy2(path, os.path.join(BAK_DIR, "%s_%s.csv" % (os.path.basename(path)[:-4], stamp)))
    backups = sorted(f for f in os.listdir(BAK_DIR) if f.endswith(".csv"))
    for name in backups[:-20]:
        try:
            os.remove(os.path.join(BAK_DIR, name))
        except OSError:
            pass


def _write(dataset, rows):
    ensure_dirs()
    path = path_of(dataset)
    columns = COLUMNS[dataset]
    _backup(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in columns})
    os.replace(tmp, path)


def replace_day(dataset, date, rows, date_field="交易日期"):
    """把某一天的数据整段替换掉。返回 (是否写入, 说明)。

    行数守卫：同一天若已有数据、来源不同、且新数据行数更少，判定为截断，拒绝覆盖。
    """
    old = read_rows(dataset)
    old_same = [r for r in old if r.get(date_field) == date]
    keep = [r for r in old if r.get(date_field) != date]
    if not rows:
        if old_same:
            return False, ("REJECT_EMPTY %s %s: 这次没取到数据，保留已有 %d 行"
                           % (dataset, date, len(old_same)))
        return False, "NO_DATA %s %s" % (dataset, date)
    if old_same and rows:
        old_src = {r.get("数据来源", "") for r in old_same}
        new_src = {r.get("数据来源", "") for r in rows}
        if old_src != new_src and len(rows) < len(old_same):
            return False, ("REJECT_TRUNCATED %s %s: 新数据 %d 行 < 已有 %d 行且来源不同，保留旧数据"
                           % (dataset, date, len(rows), len(old_same)))
    merged = keep + rows
    merged.sort(key=lambda r: (r.get(date_field, ""), str(r.get("代码", ""))))
    _write(dataset, merged)
    _log("[写入] %-12s %s  %d 行（当日合计 %d 行）"
         % (dataset, date, len(rows), len(merged)))
    return True, "ok"


def save_raw(name, payload, date):
    ensure_dirs()
    folder = os.path.join(RAW_DIR, date.replace("-", ""))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    mode = "wb" if isinstance(payload, bytes) else "w"
    kwargs = {} if mode == "wb" else {"encoding": "utf-8"}
    with open(path, mode, **kwargs) as fh:
        fh.write(payload)
    return path


def _log(message):
    ensure_dirs()
    line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    with open(os.path.join(LOG_DIR, "run.log"), "a", encoding="utf-8") as fh:
        fh.write(line)


def log(message):
    print(message)
    _log(message)
