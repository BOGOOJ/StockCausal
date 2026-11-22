# -*- coding: utf-8 -*-
"""按机械规则挑「研究样本股」，写进 watchlist.txt。

规则（全部硬编码，不留主观空间）：
1. 上市满 3 年（否则样本不够 726 根）
2. 剔 ST/*ST（涨跌幅限制不同，会污染统计）
3. 剔北交所（涨跌幅 30%，另一个游戏）
4. 当日成交额 > 1 亿（保证测出来的收益买得进）
5. 按流通市值分 4 档，每档取成交额最大的 N 只
6. 同一行业最多 2 只（防止全押一个赛道）

用法：
    python scripts/pick_watchlist.py              # 默认 30 只
    python scripts/pick_watchlist.py --n 120      # 扩池到 120 只
    python scripts/pick_watchlist.py --dry-run    # 只打印，不写文件
"""
import argparse
import json
import os
import sys
import time
from datetime import date, timedelta

import fetch_em
import net
import store

OUT_PATH = os.path.join(store.ROOT, "watchlist.txt")
INDICES = [("1A0001", "上证指数"), ("399001", "深证成指")]
BUCKET_RANGES = (
    ("大盘(≥1000亿)", 1000e8, 1e14),
    ("中大盘(300-1000亿)", 300e8, 1000e8),
    ("中小盘(100-300亿)", 100e8, 300e8),
    ("小盘(<100亿)", 0.0, 100e8),
)
MIN_TURNOVER = 1e8        # 当日成交额 > 1 亿
MIN_YEARS = 3             # 上市满 3 年
MAX_PER_INDUSTRY = 2


def quotas(total):
    """把总数平均分到 4 个市值档，余数从大盘开始补。"""
    base, rest = divmod(total, len(BUCKET_RANGES))
    return [base + (1 if i <= rest - 1 else 0) for i in range(len(BUCKET_RANGES))]


UNIVERSE_CACHE = os.path.join(store.DATA_DIR, "universe.json")
CACHE_DAYS = 7
_SINA = ("http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
         "Market_Center.getHQNodeData?page=%d&num=100&sort=symbol&asc=1&node=hs_a&symbol=&_s_r_a=page")


def load_sina():
    """东财 clist 被限流时的备用名单源。

    拿到：代码/名称/成交额/流通市值。
    拿不到：上市日期、行业——所以这两个过滤条件会被放宽，
    上市年限改到下载完日线后按 K 线根数检查。
    """
    rows = []
    page = 1
    while page <= 80:
        raw = net.fetch(_SINA % page, referer="http://finance.sina.com.cn/", kind="clist")
        text = raw.decode("gbk", "replace").strip()
        if not text or text == "[]":
            break
        try:
            items = json.loads(text)
        except json.JSONDecodeError:
            break
        if not items:
            break
        for item in items:
            try:
                amount = float(item.get("amount") or 0)          # 元
                nmc = float(item.get("nmc") or 0) * 1e4          # 万元 → 元
            except (TypeError, ValueError):
                continue
            rows.append({"f12": item.get("code"), "f14": item.get("name"),
                         "f6": amount, "f21": nmc, "f26": "", "f100": "未知"})
        page += 1
    return rows


def load_detail():
    """全 A 明细：代码/名称/成交额/总市值/流通市值/上市日期/行业。

    结果缓存 7 天——这个接口翻 60 页，反复拉容易被限流。
    东财 clist 不可用时自动退到新浪。
    """
    if os.path.exists(UNIVERSE_CACHE):
        age = (time.time() - os.path.getmtime(UNIVERSE_CACHE)) / 86400
        if age < CACHE_DAYS:
            with open(UNIVERSE_CACHE, "r", encoding="utf-8") as fh:
                rows = json.load(fh)
            print("  用缓存的名单（%.1f 天前）：%d 只" % (age, len(rows)))
            return rows

    rows = []
    try:
        fs = fetch_em._UNIVERSE_FS
        fields = "f12,f14,f6,f20,f21,f26,f100"
        page = 1
        total = None
        while page <= 80:
            url = ("%s?pn=%d&pz=100&po=1&np=1&fltt=2&invt=2&fid=f6&fs=%s&fields=%s"
                   % (fetch_em._CLIST, page, net.quote(fs), fields))
            data = net.get_json(url, referer=fetch_em._CLIST_REF, kind="clist")
            block = (data or {}).get("data") or {}
            diff = block.get("diff") or []
            if total is None:
                total = block.get("total") or 0
            if not diff:
                break
            rows.extend(diff)
            if len(rows) >= total:
                break
            page += 1
        print("  名单来源：东财 clist")
    except Exception as exc:
        print("  东财名单不可用（%s），改用新浪" % str(exc)[:60])
        rows = load_sina()
        print("  名单来源：新浪（无行业/上市日期，上市年限改由日线根数校验）")

    store.ensure_dirs()
    with open(UNIVERSE_CACHE, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False)
    return rows


def num(value):
    if value in (None, "-", ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def eligible(item, cutoff):
    code = str(item.get("f12") or "")
    name = str(item.get("f14") or "")
    turnover = num(item.get("f6"))
    list_date = str(item.get("f26") or "")
    if not code or not name:
        return False, "缺代码/名称"
    if "ST" in name.upper():
        return False, "ST"
    if code.startswith(("4", "8", "920")):
        return False, "北交所"
    if len(list_date) == 8:
        if "%s-%s-%s" % (list_date[:4], list_date[4:6], list_date[6:]) > cutoff:
            return False, "上市不满 3 年"
    # 新浪备用源没有上市日期，这里放行；上市年限改在下完日线后按 K 线根数校验
    if turnover is None or turnover < MIN_TURNOVER:
        return False, "成交额不足"
    return True, ""


def pick(rows, cutoff, total=30):
    """按市值分档 + 行业分散。total = 目标股票数（不含指数）。"""
    pools = {name: [] for name, _lo, _hi in BUCKET_RANGES}
    for item in rows:
        ok, _why = eligible(item, cutoff)
        if not ok:
            continue
        cap = num(item.get("f21"))
        turnover = num(item.get("f6"))
        if cap is None:
            continue
        for name, lo, hi in BUCKET_RANGES:
            if lo <= cap < hi:
                pools[name].append({
                    "代码": str(item["f12"]),
                    "名称": str(item["f14"]).replace(" ", ""),
                    "成交额亿": round(turnover / 1e8, 2),
                    "流通市值亿": round(cap / 1e8, 1),
                    "行业": str(item.get("f100") or "未知"),
                })
                break

    picked = []
    used_industry = {}
    for (name, _lo, _hi), quota in zip(BUCKET_RANGES, quotas(total)):
        # 按流通市值排序（不用当日成交额排序：那会偏向今天异动的热门股，
        # 样本就变成了「今天最热闹的」，不是「可交易的典型股」）
        pool = sorted(pools[name], key=lambda r: -r["流通市值亿"])
        taken = 0
        for item in pool:
            if taken >= quota:
                break
            industry = item["行业"]
            if industry != "未知" and used_industry.get(industry, 0) >= MAX_PER_INDUSTRY:
                continue
            item["档位"] = name
            picked.append(item)
            used_industry[industry] = used_industry.get(industry, 0) + 1
            taken += 1
    return picked


def render(picked):
    lines = [
        "# 研究样本池（由 scripts/pick_watchlist.py 自动生成，不要手改规则）",
        "# 规则：上市满3年 / 非ST / 非北交所 / 当日成交额>1亿 / 按流通市值分4档、每档取流通市值最大的 / 同行业≤2只（仅当名单源带行业字段时生效）",
        "# 本次名单源可能无行业/上市日期（新浪备用源）：行业上限未生效，上市年限改由日线根数校验",
        "# 为什么不按成交额排序：那会挑到「今天最热闹的票」，样本就不是「可交易的典型股」了",
        "# 注意：主板涨跌幅10%、创业板/科创板20%，混在一起统计时要知道这一点",
        "# 生成时间：%s    样本数：%d 只 + 2 个指数" % (time.strftime("%Y-%m-%d %H:%M"), len(picked)),
        "# 想加自己的票：另起一行写 6 位代码即可；想删就删，但删了要记下来（这是偏差的开始）",
        "",
    ]
    for code, name in INDICES:
        lines.append("%-8s %s" % (code, name))
    lines.append("")
    current = None
    for item in picked:
        if item["档位"] != current:
            current = item["档位"]
            lines.append("# ---- %s ----" % current)
        lines.append("%-8s %s   # 成交额%.2f亿 流通市值%.0f亿 %s"
                     % (item["代码"], item["名称"], item["成交额亿"], item["流通市值亿"], item["行业"]))
    return "\n".join(lines) + "\n"


def main(argv):
    parser = argparse.ArgumentParser(description="按机械规则挑研究样本股")
    parser.add_argument("--n", type=int, default=30, help="目标股票数（不含指数），默认 30")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    cutoff = (date.today() - timedelta(days=int(MIN_YEARS * 365.25))).strftime("%Y-%m-%d")
    print("全 A 明细下载中……")
    rows = load_detail()
    print("  拿到 %d 只（含退市股）" % len(rows))
    picked = pick(rows, cutoff, args.n)
    print("  符合规则并分层挑选：%d 只（目标 %d）" % (len(picked), args.n))
    text = render(picked)
    if args.dry_run:
        print(text)
        return 0
    with open(OUT_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print("已写入 %s" % OUT_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
