# -*- coding: utf-8 -*-
"""universe.py —— 生成全市场股票名单 + 行业映射。

数据源：东财 datacenter 的 F10 报表 RPT_F10_ORG_BASICINFO
（push2 名单接口已被限流，但 datacenter 正常；一次 50 页拿全 2.4 万行）

产出：
    watchlist.txt            全市场股票代码（供 kline.py 下载日线）
    data/industry_map.csv    代码/名称/上市日期/东财一级/二级/三级/风格组/交易所板块

过滤规则（写在这里，改规则改这里）：
    1. 剔 ST/*ST
    2. 剔北交所、B 股
    3. 上市满 3 年（LISTING_DATE）
    4. 只要正常上市状态的

用法：
    python scripts\\universe.py                # 生成并写入
    python scripts\\universe.py --dry-run      # 只打印统计，不写文件
"""
import argparse
import csv
import os
import sys
import time
from datetime import date, timedelta

import net
import store

REPORT = "RPT_F10_ORG_BASICINFO"
BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REF = "https://data.eastmoney.com/"
PAGE_SIZE = 500

WATCHLIST = os.path.join(store.ROOT, "watchlist.txt")
INDUSTRY_CSV = os.path.join(store.DATA_DIR, "industry_map.csv")

MIN_YEARS = 3
INDICES = [("1A0001", "上证指数"), ("399001", "深证成指")]

# 东财 2016 版一级行业（共 27 个）→ 5 个风格组。用精确匹配，避免关键词误伤。
LEVEL1_TO_STYLE = {
    "金融": "金融地产",
    "房地产": "金融地产",

    "电子设备": "科技成长",
    "信息技术": "科技成长",
    "互联网": "科技成长",
    "文化传媒": "科技成长",
    "国防与装备": "科技成长",
    "电气设备": "科技成长",          # 电池/光伏/风电为主

    "医药生物": "消费医药",
    "食品饮料": "消费医药",
    "家电": "消费医药",
    "纺织服装": "消费医药",
    "商贸零售": "消费医药",
    "休闲、生活及专业服务": "消费医药",
    "农林牧渔": "消费医药",
    "轻工制造": "消费医药",

    "机械设备": "周期制造",
    "交运设备": "周期制造",
    "基础化工": "周期制造",
    "有色金属": "周期制造",
    "建筑": "周期制造",
    "建材": "周期制造",
    "钢铁": "周期制造",
    "化石能源": "周期制造",

    "公用事业": "公用稳定",
    "交通运输": "公用稳定",
}


def style_of(em2016):
    level1 = (em2016 or "").split("-")[0].strip()
    return LEVEL1_TO_STYLE.get(level1, "其他")


def fetch_all():
    rows = []
    page = 1
    total = None
    while page <= 80:
        url = ("%s?reportName=%s&columns=SECURITY_CODE,SECURITY_NAME_ABBR,LISTING_DATE,"
               "EM2016,TRADE_MARKET,LISTING_STATE,BOARD_NAME_1LEVEL"
               "&pageNumber=%d&pageSize=%d&sortColumns=SECURITY_CODE&sortTypes=1"
               % (BASE, REPORT, page, PAGE_SIZE))
        data = net.get_json(url, referer=REF, kind="em")
        result = (data or {}).get("result")
        if not result:
            break
        rows.extend(result.get("data") or [])
        pages = result.get("pages") or 1
        if page >= pages:
            break
        page += 1
        time.sleep(0.2)
    return rows


def keep(row, cutoff):
    code = str(row.get("SECURITY_CODE") or "")
    name = str(row.get("SECURITY_NAME_ABBR") or "")
    market = str(row.get("TRADE_MARKET") or "")
    listing = str(row.get("LISTING_DATE") or "")[:10]
    state = str(row.get("LISTING_STATE") or "")
    if len(code) != 6 or not code.isdigit():
        return False, "非A股代码"
    if state != "0":
        return False, "非正常上市(状态%s)" % state
    if code.startswith(("4", "8", "920", "900", "200")):
        return False, "北交所/B股"
    if "北" in market:
        return False, "北交所"
    if "ST" in name.upper():
        return False, "ST"
    if "退" in name:
        return False, "退市"
    if len(listing) != 10:
        return False, "无上市日期"
    if listing > cutoff:
        return False, "上市不满3年"
    return True, ""


def main(argv):
    parser = argparse.ArgumentParser(description="生成全市场名单 + 行业映射")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    cutoff = (date.today() - timedelta(days=int(MIN_YEARS * 365.25))).strftime("%Y-%m-%d")
    print("拉取东财 F10 全量名单……")
    rows = fetch_all()
    print("  原始 %d 行（含退市/异地）" % len(rows))

    seen = {}
    dropped = {}
    for row in rows:
        code = str(row.get("SECURITY_CODE") or "")
        if code in seen:
            continue
        ok, why = keep(row, cutoff)
        if not ok:
            dropped[why] = dropped.get(why, 0) + 1
            continue
        em = row.get("EM2016") or ""
        parts = em.split("-")
        seen[code] = {
            "代码": code,
            "名称": str(row.get("SECURITY_NAME_ABBR") or ""),
            "上市日期": str(row.get("LISTING_DATE") or "")[:10],
            "一级": parts[0] if len(parts) > 0 else "",
            "二级": parts[1] if len(parts) > 1 else "",
            "三级": parts[2] if len(parts) > 2 else "",
            "风格组": style_of(em),
            "板块": str(row.get("TRADE_MARKET") or ""),
        }

    print("  剔除原因：%s" % dropped)
    print("  入库 %d 只；上市日期截止 %s" % (len(seen), cutoff))

    by_style = {}
    by_level1 = {}
    for item in seen.values():
        by_style[item["风格组"]] = by_style.get(item["风格组"], 0) + 1
        by_level1[item["一级"]] = by_level1.get(item["一级"], 0) + 1
    print("  风格组：%s" % dict(sorted(by_style.items(), key=lambda x: -x[1])))
    print("  一级行业 %d 个，前 8：%s"
          % (len(by_level1), dict(sorted(by_level1.items(), key=lambda x: -x[1])[:8])))

    if args.dry_run:
        return 0

    store.ensure_dirs()
    with open(INDUSTRY_CSV, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["代码", "名称", "上市日期", "一级", "二级",
                                                "三级", "风格组", "板块"])
        writer.writeheader()
        for code in sorted(seen):
            writer.writerow(seen[code])

    lines = [
        "# 全市场股票名单（由 scripts/universe.py 自动生成，不要手改）",
        "# 规则：上市满%d年 / 非ST / 非北交所 / 非B股；共 %d 只 + %d 个指数"
        % (MIN_YEARS, len(seen), len(INDICES)),
        "# 生成时间：%s" % time.strftime("%Y-%m-%d %H:%M"),
        "# 行业与风格组见 data/industry_map.csv",
        "",
    ]
    for code, name in INDICES:
        lines.append("%-8s %s" % (code, name))
    for code in sorted(seen):
        lines.append("%-8s %s" % (code, seen[code]["名称"]))
    with open(WATCHLIST, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")

    print("已写入：%s（%d 行）" % (WATCHLIST, len(seen) + len(INDICES) + 5))
    print("        %s" % INDUSTRY_CSV)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
