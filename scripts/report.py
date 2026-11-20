# -*- coding: utf-8 -*-
"""Excel 汇总：把当日 CSV 拼成一个 xlsx；没装 openpyxl 就自动装一次。"""
import os
import subprocess
import sys
import time

import store

SHEETS = (
    ("longhu_stock", "龙虎榜", True),
    ("longhu_seat", "龙虎榜席位", True),
    ("rzrq_market", "两融总量", False),   # 只有最近 10 个交易日，全量展示
    ("rzrq_stock", "两融个股", True),
    ("dzjy", "大宗交易", True),
    ("etf_share", "ETF份额", True),
)

_NUMBER_HINTS = ("价", "额", "余额", "份额", "数量", "量", "率", "涨跌")

# 这些列即使看着像数字也要保持文本（代码前导 0 会丢）
_TEXT_COLUMNS = {
    "交易日期", "代码", "名称", "营业部", "上榜原因", "数据来源", "抓取时间",
    "上榜标记", "市场", "类型", "管理人", "方向", "解读", "交易所", "买方营业部",
    "卖方营业部",
}


def _coerce(column, value):
    """CSV 读回来都是字符串，写 Excel 前把数值列转回数字。"""
    if column in _TEXT_COLUMNS or value in ("", None):
        return value
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def _load_openpyxl():
    try:
        import openpyxl  # noqa: F401
        return True
    except ImportError:
        pass
    print("首次导出需要 openpyxl，正在安装（用清华源，约 10 秒）……")
    env = dict(os.environ)
    # pip 会读 Windows 系统代理（registry），代理没开时会连不上；这里强制直连
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        env.pop(key, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    for args in (["-m", "pip", "install", "openpyxl", "-q", "--disable-pip-version-check",
                  "-i", "https://pypi.tuna.tsinghua.edu.cn/simple",
                  "--trusted-host", "pypi.tuna.tsinghua.edu.cn"],
                 ["-m", "pip", "install", "openpyxl", "-q", "--disable-pip-version-check"]):
        try:
            subprocess.run([sys.executable] + args, check=True, timeout=240, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            import openpyxl  # noqa: F401
            return True
        except Exception as exc:
            print("  安装失败：%s" % str(exc)[:70])
    return False


def _width_for(values):
    width = 8
    for value in values:
        text = str(value if value is not None else "")
        width = max(width, min(46, int(sum(2 if ord(ch) > 127 else 1 for ch in text)) + 2))
    return width


def build_daily(date):
    """date: YYYY-MM-DD。返回生成的 xlsx 路径，没有数据返回 None。"""
    data = {}
    for dataset, _title, by_date in SHEETS:
        rows = store.read_rows(dataset)
        if by_date:
            rows = [r for r in rows if r.get("交易日期") == date]
        data[dataset] = rows
    if not any(data.values()):
        print("当天没有数据，跳过 Excel")
        return None

    if not _load_openpyxl():
        print("openpyxl 装不上（可能没网），数据已在 data/*.csv，用 Excel 直接打开即可")
        return None

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="4472C4")

    intro = wb.active
    intro.title = "说明"
    intro.append(["文件", "说明"])
    notes = [
        ("生成时间", time.strftime("%Y-%m-%d %H:%M:%S")),
        ("数据日期", date),
        ("龙虎榜", "个股上榜记录 + 买卖前 5 营业部席位；金额单位见列名"),
        ("两融", "市场总量（最近 10 个交易日，亿元）+ 个股明细（元），个股为全量"),
        ("大宗交易", "按买卖双方配对成笔；同花顺只给最近 50 笔，本表以东财全量为准"),
        ("ETF份额", "上交所 + 深交所官方数据；同花顺没有这项数据"),
        ("数据来源", "每行都有「数据来源」列，可据此筛选单一来源"),
        ("注意", "停牌日无 K 线；百分比为 %，金额单位已在列名标注"),
    ]
    for row in notes:
        intro.append(list(row))
    intro.column_dimensions["A"].width = 14
    intro.column_dimensions["B"].width = 76

    for dataset, title, _by_date in SHEETS:
        rows = data[dataset]
        if not rows:
            continue
        sheet = wb.create_sheet(title)
        columns = store.COLUMNS[dataset]
        sheet.append(columns)
        for cell in sheet[1]:
            cell.font = head_font
            cell.fill = head_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
        for row in rows:
            sheet.append([_coerce(col, row.get(col, "")) for col in columns])
        for index, col_name in enumerate(columns, 1):
            letter = get_column_letter(index)
            values = [row.get(col_name, "") for row in rows[:200]]
            sheet.column_dimensions[letter].width = _width_for([col_name] + values)
            if any(hint in col_name for hint in _NUMBER_HINTS):
                for cell in sheet[letter][1:]:
                    if isinstance(cell.value, (int, float)):
                        cell.number_format = "#,##0.00"
        sheet.freeze_panes = "A2"
        if rows:
            sheet.auto_filter.ref = "A1:%s%d" % (get_column_letter(len(columns)), len(rows) + 1)

    store.ensure_dirs()
    path = os.path.join(store.OUT_DIR, "每日数据_%s.xlsx" % date.replace("-", ""))
    wb.save(path)
    return path
