# 上交所逐笔数据盘口重建与交易策略

本项目是“量化交易系统的原理”课程作业。项目读取上交所 2026-09-23 的上证 50 逐笔委托、逐笔成交和行情快照数据，完成十档订单簿重建，并基于重建盘口实现一套遵守 A 股 `T+1` 约束的盘口压力策略。

> 本项目仅用于课程学习和研究，不构成投资建议。策略在单日样本中没有取得正超额收益，不能直接用于实盘。

## 项目内容

处理流程如下：

```text
逐笔委托 ord ─┐
              ├─ 按 Time + BizIndex 回放事件 ─ 重建十档盘口 ─ 盘口信号 ─ T+1 模拟交易
逐笔成交 exe ─┘                         │
                                        └─ 与行情快照 snp 校验
```

### 数据拆解

- `ord_20260923.parquet`：逐笔委托，包含新增、撤单、方向、价格、数量和原始委托号。
- `exe_20260923.parquet`：逐笔成交，包含成交价、成交量、买卖方委托号和主动方向。
- `snp_20260923.parquet`：行情快照，包含最新价、累计成交量和买卖十档。

关键编码：

| 字段 | 数值 | 含义 |
|---|---:|---|
| `OrderKind` | 65 (`A`) | 新增委托 |
| `OrderKind` | 68 (`D`) | 删除或撤销委托量 |
| `FunctionCode` / `BSFlag` | 66 (`B`) | 买方向 |
| `FunctionCode` / `BSFlag` | 83 (`S`) | 卖方向 |

订单使用 `(Channel, OrderOriNo)` 标识。连续竞价中，主动买成交扣减被动卖单，主动卖成交扣减被动买单；集合竞价成交方向为 0，扣减买卖双方。

### 盘口重建

`reconstruct_orderbook.py` 按事件顺序维护活动委托和各价位聚合数量，在原始快照时间点输出重建后的买一至买十、卖一至卖十。

重建文件包含：

- `Symbol`、`Time`、`TimeText`
- `ProcessedEvents`、`ActiveOrders`
- `BidPrice1`～`BidPrice10`、`BidVolume1`～`BidVolume10`
- `AskPrice1`～`AskPrice10`、`AskVolume1`～`AskVolume10`
- 价格与完整档位匹配数及 L1 校验标记

输出覆盖 50 只股票和 252,631 个快照时间点。高活跃股票可能因快照时间戳不能表示同一时间批次内的精确事件边界而出现校验差异；脚本不会使用原始快照反填重建结果。

### 交易策略

`orderbook_strategy.py` 使用以下信号：

1. 对前五档挂单量按档位距离加权，计算订单簿失衡度。
2. 根据买一、卖一数量计算微价格相对中间价的偏移。
3. 使用 `0.7 × 失衡度 + 0.3 × 微价格信号` 得到盘口压力分数。
4. 分数超过 `±0.70` 才允许顺势交易。

执行约束：

- 买入按卖一价、卖出按买一价成交，不使用中间价虚构成交。
- 每次 100 股，每只股票交易后冷却 10 分钟，每日最多交易 2 次。
- 检查一档可用数量和资金。
- 加入佣金、沪市过户费及卖出印花税。
- 期初持仓可以卖出；当日买入不计入可卖数量，遵守 `T+1`。
- 14:55 后停止开仓。

## 单日回测结果

默认初始资产为 1,000,000 元，其中计划使用 50% 建立隔夜持仓。由于整手约束和部分高价股票不足一手，实际期初股票市值为 344,902 元。

| 指标 | 结果 |
|---|---:|
| 成交次数 | 64 |
| 买入 / 卖出 | 47 / 17 |
| 总交易费用 | 359.48 元 |
| 最终资产 | 997,959.02 元 |
| 策略收益 | -0.2041% |
| 原始持仓基准收益 | -0.1855% |
| 超额收益 | -0.0186% |

这说明策略具备可执行的账户、费用和成交约束，但尚未证明存在稳定收益。只有一个交易日，无法完成训练集、验证集和样本外检验。

## 环境与运行

要求 Python 3.10 或以上版本。

```powershell
python -m pip install -r requirements.txt
```

把三份原始数据放入 `sse50_20260923/`，然后运行：

```powershell
python reconstruct_orderbook.py
python orderbook_strategy.py
```

常用策略参数：

```powershell
python orderbook_strategy.py --threshold 0.70 --cooldown-seconds 600 --max-trades-per-symbol 2
```

## 仓库结构

```text
.
├── README.md
├── requirements.txt
├── reconstruct_orderbook.py
├── orderbook_strategy.py
├── reconstructed_orderbook_20260923.parquet
├── reconstruction_validation_20260923.csv
└── strategy_output/
    ├── signals_20260923.csv
    ├── trades_20260923.csv
    └── strategy_summary_20260923.csv
```

## 原始数据校验

原始行情数据可能受课程或供应商授权限制，且 `ord` 文件超过 GitHub 普通单文件 100 MB 限制，因此不提交到仓库。

| 文件 | 字节数 | SHA-256 |
|---|---:|---|
| `exe_20260923.parquet` | 78,166,158 | `EC1774C557600549992F1C349705FDACDDDEABDB58750A8FBA4D2198F9207222` |
| `ord_20260923.parquet` | 125,584,504 | `5E3FA7F96EDC130262661BF379A5C367952327D36B0936E720CBA907E78BD61B` |
| `snp_20260923.parquet` | 26,806,291 | `AC69D2F1810A24A15CB4C90183A6D3EA7EE31ECCBCD98C58ED1F7F7F7C34AA87` |

发布前请确认课程和数据供应商允许公开衍生数据；如果不允许，应同时从仓库排除重建后的 Parquet 和策略信号文件。

