# 代码逻辑说明

这份文档回答四个问题：**做了什么、有什么用、代码怎么跑起来的、用了什么**。

---

## 一、这个项目是什么、有什么用

**一句话**：每天收盘后，把 A 股市场的「资金痕迹」抓成本地数据，并维护一个自选池的日线库，用来验证交易想法。

**要解决的问题**：把「我觉得这票会涨」变成「历史上这类情况，次日上涨的概率是多少」。

**不做什么**：不预测、不荐股、不做回测、不上传任何数据。

**已经实现 / 还没实现**：

| 能力 | 状态 |
|---|---|
| 每日事件数据（龙虎榜/两融/大宗/ETF份额） | ✅ |
| 自选池日线库（近 3 年） | ✅ |
| Excel 汇总导出 | ✅ |
| 研究样本池自动挑选 | ✅ |
| **统计分析（analyze.py）** | ✅ |
| 回测 / 模拟盘 | ❌ 没开始 |

---

## 二、数据流全景

```
                     ┌──────────────────────────────┐
   同花顺 data 站  ──▶│ parse_ths.py  (GBK 页面解析)  │──┐
   同花顺 d.10jqka ──▶│ kline.py      (JSONP 解码)   │  │
   东财 datacenter ──▶│ fetch_em.py   (JSON 报表)    │──┼──▶ store.py ──▶ data\*.csv
   东财 push2his    ──▶│ kline.py      (备用源)       │  │      │        data\kline.db
   上交所 / 深交所 ──▶│ fetch_exchange.py            │──┘      │
                     └──────────────────────────────┘         ▼
                                                      report.py ──▶ out\*.xlsx
```

所有网络请求都经过 `net.py`（统一限速、重试、绕系统代理）。

---

## 三、每个文件干什么

| 文件 | 职责 | 关键函数 | 输入 → 输出 |
|---|---|---|---|
| `net.py` | 网络层：UA/Referer、限速、重试、编码自适应 | `fetch` / `get_json` / `decode` | URL → bytes |
| `parse_ths.py` | 同花顺三个数据页面的 HTML 解析 | `parse_longhu` / `parse_rzrq_market` / `parse_dzjy` | HTML → 行列表 |
| `fetch_em.py` | 东财 datacenter 报表 + 全 A 名单 | `longhu` / `longhu_seats` / `rzrq_stock` / `dzjy` / `universe` / `kline` | 日期 → 行列表 |
| `fetch_exchange.py` | 沪深 ETF 份额 | `etf_sse` / `etf_szse` | 日期 → 行列表 |
| `store.py` | CSV 落库：固定列、按日整段替换、行数守卫、原子写 | `replace_day` / `read_rows` / `log` | 行列表 → CSV |
| `kline.py` | 日线库：同花顺 all.js 解码 → SQLite | `decode_alljs` / `update_one` / `export` | 代码 → kline.db |
| `pick_watchlist.py` | 按机械规则挑选研究样本池 | `load_detail` / `pick` / `render` | 全 A 名单 → watchlist.txt |
| `report.py` | 把当日 CSV 合成 Excel | `build_daily` | CSV → xlsx |
| `main.py` | 命令行入口、流程编排、摘要输出 | `run` / `backfill` / `verify` | 参数 → 落库 + 摘要 |
| `analyze.py` | 条件收益统计（过去 k 天涨跌 → 次日收益） | `build_panel` / `analyze_scope` / `judge` | kline.db → 报告 + experiments.db |

### 调用关系

```
main.py ──┬─▶ parse_ths.py ─┐
          ├─▶ fetch_em.py ──┼─▶ store.py ─▶ data\*.csv
          ├─▶ fetch_exchange.py ┘
          └─▶ report.py ─▶ out\*.xlsx

kline.py ─┬─▶ net.py
          ├─▶ fetch_em.py（后复权兜底）
          └─▶ store.py（路径/日志）
```

---

## 四、关键设计决策与理由

| 决策 | 理由 |
|---|---|
| **不纯用同花顺** | 同花顺数据中心没有 ETF 份额；大宗只给「最近 50 笔」会跨日截断；两融个股只给前 50 名。所以缺口用东财/交易所补 |
| **每行带「数据来源」列** | 混源统计前必须能筛单一来源，否则口径打架 |
| **CSV 按「交易日整段替换」** | 采集单位天然是“一整天”，整段替换 = 幂等，避免主键设计错误导致重复行 |
| **行数守卫** | 同日新数据行数明显少于旧数据时拒绝写入（`REJECT_TRUNCATED`），防止截断数据覆盖完整数据 |
| **空数据不清空** | 取数失败返回空时保留旧数据（`REJECT_EMPTY`），防网络抖动删库 |
| **股票只用不复权（seg=00）** | 前复权对老股会变负数/被缩放，跨口径混用即废数据 |
| **涨跌幅用后复权序列逐日计算** | 东财 kline 的涨跌幅字段是不复权口径，除权日失真（实测差 2.56pp） |
| **日线存 SQLite 而非 CSV** | 单只 726 行可以，全市场 1800 万行只有 SQLite 扛得住 |
| **同花顺限速 1.5 秒/次** | 实测无间隔连打 10 次即 502；1.5 秒间隔连发 15 只全成功 |
| **请求绕系统代理** | 本机系统代理指向未启动的端口，urllib 会直接 10061 拒绝；空 `ProxyHandler` 直连正常 |
| **收盘前丢弃当日 K 线** | 15:30 前的当日 K 线是未走完的盘中价 |

---

## 五、用到的外部接口

| 用途 | 地址 | 关键参数 / 坑 |
|---|---|---|
| 龙虎榜页面 | `data.10jqka.com.cn/market/longhu/` | GBK；翻页接口要 hexin-v，返回 401，所以只能拿当日 |
| 两融页面 | `data.10jqka.com.cn/market/rzrq/` | 只给最近 10 个交易日；偶发返回只有表头的空壳，需重试 |
| 大宗页面 | `data.10jqka.com.cn/market/dzjy/` | 只给最近 50 笔，会跨日 |
| 全历史日线 | `d.10jqka.com.cn/v6/line/hs_<代码>/<seg>/all.js` | `seg=00` 不复权 / `01` 前复权 / `02` 后复权；价格按「最低+三个差值」编码，单位分 |
| 东财报表 | `datacenter-web.eastmoney.com/api/data/v1/get` | `reportName` + `filter`；**`sortColumns` 必须与日期列同名**，否则返回「排序列表不存在」 |
| 东财行情 | `push2his.eastmoney.com/api/qt/stock/kline/get` | 支持 `fqt=0/1/2`；连发 20 只即封 IP，只做单只兜底 |
| 东财名单 | `push2.eastmoney.com/api/qt/clist/get` | 5931 只（含退市股）；字段 f12 代码 / f14 名称 / f26 上市日期 / f100 行业 |
| 上交所 ETF | `query.sse.com.cn/commonQuery.do?sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L` | 支持 `STAT_DATE` 查历史；**分页是坏的**，只能一次要 pageSize=2000 |
| 深交所 ETF | `szse.cn/api/report/ShowReport/data?CATALOGID=1945` | 只有当日快照；78 页 × 10 条；代码/名称在 HTML 里要剥标签 |

---

## 六、数据表结构

### CSV（6 张，列名自带单位）

| 表 | 主键语义 | 行数级 |
|---|---|---|
| `longhu_stock` | 一只股票的一条上榜记录 | 每天 80~100 |
| `longhu_seat` | 一个买卖前 5 席位 | 每天 800~1000 |
| `rzrq_market` | 一个交易日的沪深北两融总量 | 每天 1（总量表按日一行） |
| `rzrq_stock` | 一只股票一天 | 每天约 4500 |
| `dzjy` | 一笔大宗交易 | 每天 40~100 |
| `etf_share` | 一只 ETF 一天 | 每天约 1700（沪深合计） |

列定义集中在 `store.py` 的 `COLUMNS` 字典——**改列名要三处同步**：产出方（parse_ths / fetch_em）、`store.COLUMNS`、`report._TEXT_COLUMNS`。

### SQLite（`data\kline.db`）

```sql
stock(code TEXT PRIMARY KEY, name TEXT, market TEXT, last_sync TEXT, bars INTEGER, source TEXT)

kline(code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
      volume INTEGER,      -- 单位：股
      adj_close REAL,      -- 后复权收盘，仅用于算收益
      pct_chg REAL,        -- 复权后的真实涨跌幅 %
      PRIMARY KEY(code, date))
```

---

## 七、失败处理与防呆

| 场景 | 行为 |
|---|---|
| 同花顺 502/504 | 退避重试 1/3/7 秒，仍失败则切东财 |
| 页面日期 ≠ 目标日期 | 切东财（同花顺首页只有最新一个交易日） |
| 两融页面返回空壳 | 重试 3 次，间隔 3 秒 |
| 某数据集整体失败 | 记录日志、跳过该数据集，其它照常 |
| 取到 0 行 | 不覆盖已有数据 |
| 同日新数据比旧数据少且来源不同 | 拒绝覆盖 |
| 写 CSV 中途失败 | 先写 `.tmp` 再原子替换 |
| 非交易日 / 收盘前 | 目标日期自动退到上一个工作日；当日未收盘 K 线丢弃 |

---

## 八、分析口径（analyze.py）

**问题**：过去 k 天涨跌，能不能预测次日收益？（k = 1/2/3/5/10/20，六个窗口一次全跑，不许挑）

五个必须做对的点，每一个都是防自欺的：

| 点 | 做法 | 不做会怎样 |
|---|---|---|
| **买卖点可执行** | 信号在 T 日收盘才齐 → T+1 开盘买入 → T+1 收盘卖出 | 用 T 日收盘价成交 = 未来函数 |
| **剔除买不进的** | T+1 开盘 ≥ 涨停价 99.5%，或一字板（最高=最低）→ 剔除 | 涨停股次日的高收益是假的，会把效应吹大 |
| **算超额收益** | 减去同日上证指数收益 | 测到的是大盘 beta，不是「过去涨跌」的效应 |
| **标准误按日聚类** | 先算每日横截面均值，再对日序列做 t 检验 | 同一天 30 只票一起动，样本不独立，t 值虚高 2~3 倍 |
| **样本内外切开** | 前 70% 交易日定规则，后 30% 只做最终验证 | 反复看样本外 = 把样本外变成样本内 |

**分档方式**：特征的标准化（z-score）用**每只股票自己样本内的均值/标准差**，分档边界也从样本内算完再套到样本外——避免样本外自己定义档位。

**判定规则**（硬编码在 `judge()`）：

1. 样本外 6 个窗口里，至少 4 个满足「最高档 − 最低档 > 0.10pp」
2. 且最优档扣掉 0.15% 往返成本后仍为正（只看最优档，因为 A 股只能做多）
3. 方向一致 ≥ 4/6

三条不过就是 FAIL，不许换窗口重试。

**记录**（每次运行追加，不覆盖）：

- `data/experiments.db`：`experiments`（跑什么、什么参数、什么结论、股票池指纹、数据截止日）+ `cells`（每个格子的样本数/超额收益/胜率/t/扣成本后/1万仓位净赚）+ `ics`（每个窗口的 IC 及其 t 值）
- `reports/分析_YYYYMMDD_HHMMSS.md`：人可读的完整表格
- `out/分析_*.xlsx`：Excel 版

---

## 九、怎么扩展

**加一个事件数据源**：
1. 在 `fetch_*.py` 写取数函数，返回「列名 → 值」的字典列表
2. 在 `store.COLUMNS` 加一行列定义
3. 在 `main.py` 写一个 `grab_xxx(target, summary)` 并加进 `run()` 的元组
4. 在 `report.SHEETS` 加一行，Excel 就会多一个 sheet

**加一条分析**：
1. 新写 `analyze.py`，只读 `data\kline.db` / `data\*.csv`
2. 结果写 `reports\` 和 `data\experiments.db`，**不要覆盖历史**，每次追加
