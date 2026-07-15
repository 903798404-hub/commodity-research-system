# Oil World 2026-06 Palm Oil 年度数据范围专项核查

## 1. 核查范围与结论摘要

- 原始工作簿：`2026-6/油世界季度表-June 2026.xlsx`
- 目录来源：工作簿 `目录` 工作表，以及 `2026-6/17OilsFatsandBiodiesel.htm`、`2026-6/Asia.htm`
- USDA 范围来源：相邻 USDA 项目的 `public/data/index.json`，Palm Oil 商品代码为 `4243000`，当前矩阵范围依次为 `G2、Global、Indonesia、Malaysia、Thailand`。
- 本轮只进行了读取和结构分析；未修改 Excel、源码、配置、数据快照、前端或 USDA 项目，也未接入 Palm Oil。

核心结论：

1. 全球 Palm Oil 市场年度数据应以 `AN26392` 的分章节明细表为主体，以 `AN26391` 补充直接披露的全球 `Stocks/Usage`。
2. `AN26392` 的完整 Oct–Sept 年度列为 `B:F`（2025/26F 至 2021/22）；`G:J` 是半年列，`K:M` 是 Jan–Dec 自然年列，均不得混入市场年度矩阵。
3. `AN26392` 只直接披露全球 Opening Stocks，没有 Indonesia、Malaysia、Thailand 的 Opening Stocks 行。国家 Beginning Stocks 只能从相同国家上一市场年度 Ending Stocks 顺延；因此 2021/22 无法从本表安全生成 Beginning Stocks。
4. Indonesia 在 Oct–Sept Imports 章节没有国家行；Thailand 同样没有 Imports 行。不得把缺行解释为 0。
5. Malaysia 是五个 USDA 范围中最接近完整国家市场年度平衡表的对象：Production、Imports、Exports、Disappearance、Ending Stocks 都直接存在，Beginning Stocks 和 Stocks/Use Ratio 需要派生。
6. G2 在原表中没有直接行。Production、Exports、Domestic Consumption、Ending Stocks及部分 Beginning Stocks、Stocks/Use Ratio 可由 Indonesia + Malaysia 安全派生；但 Indonesia Imports 缺失，因此 G2 Imports 和完整供需平衡不能安全生成。
7. 各指标章节的显式国家集合明显不同；每个章节的 `Oth countries` 是不同残差范围，不能把各章节的 `Oth countries` 当成同一国家集合进行标准平衡校验。

## 2. 目录页确认的 Palm Oil 报表

| 类型 | report_id / 工作表 | 目录页报表名称 | 工作簿实际标题 |
|---|---|---|---|
| 全球汇总 | AN26391 | PALM OIL : Summary World Supply and Demand Balance - - Quarterly & Annual (1000 T) | 同目录标题 |
| 全球分章节明细 | AN26392 | PALM OIL : World Supply and Demand Balance (1000 T) | 同目录标题 |
| Indonesia 自然年度平衡 | AN64402 | INDONESIA : Palm oil Balance (1000 T) | 同目录标题 |
| Indonesia 面积/单产/产量 | AN64405 | INDONESIA: Mature Area, Yields and Production of Palm Oil | 同目录标题 |
| Malaysia 自然年度平衡 | AN70402 | MALAYSIA : Palm oil Balance (1000 T) | 同目录标题 |
| Malaysia 面积/单产/产量 | AN70403 | MALAYSIA: Mature Area, Yields and Production of Palm Oil | 同目录标题 |
| Malaysia 月度 | AN2639X1 | MONTHLY DATA: Malaysian Palm Oil Production, Exports & Stocks (1000 T) | 工作表内分为 Production、Exports、End.stocks 三段 |
| Indonesia 月度 | AN2639X2 | MONTHLY DATA: Indonesian Palm Oil Production, Exports & Stocks (1000 T) | 工作表内分为 Production、Exports、End.stocks 三段 |
| 重点国家月度进口 | AN2639X3 | MONTHLY DATA: Palm Oil Imports of Key Countries (1000 T) | 工作表内按国家分段 |
| EU-27 月度贸易 | AN2639X4 | MONTHLY DATA: EU-27 Palm Oil Trade with non-EU Countries (1000 T) | 工作表内分为 Imports、Exports 两段 |

## 3. 全球年度供需表详细结构

### 3.1 主表 AN26392

- `report_id`：`AN26392`
- 工作表名称：`AN26392`
- 完整标题：`PALM OIL : World Supply and Demand Balance (1000 T)`
- 使用区域：`A1:M153`
- 标题行：第 1 行（`A1:M1`）
- 主表头行：第 2 行（`A2:M2`）
- 各章节重复表头行：第 28、73、99、139 行
- 完整市场年度数据列：`B:F`
  - `B`：Oct Sept 25/26F
  - `C`：Oct Sept 24/25
  - `D`：Oct Sept 23/24
  - `E`：Oct Sept 22/23
  - `F`：Oct Sept 21/22
- 必须排除的半年列：
  - `G:H`：Apr Sept 2026、Apr Sept 2025
  - `I:J`：Oct Mar 25/26、Oct Mar 24/25
- 必须排除的自然年列：`K:M`（Jan Dec 2025、2024、2023）
- 原始数量单位：`1000 T`
- 标准数量单位：可原样保持 `1000 T`
- 市场年度：10 月至次年 9 月
- 预测标记规则：原始期间文本以 `F` 结尾时标记为 `explicit_forecast`；本表完整市场年度中只有 `25/26F` 明确带 `F`。不得改写原始期间文字。

章节边界如下：

| 标准指标 | 原始章节/指标 | 章节表头 | 国家/数据行 | Total 行 | 备注 |
|---|---|---:|---:|---:|---|
| Beginning Stocks | `Open'g stocks` | 第2行 | 第3行 | 不适用 | 仅全球一行，没有国家明细 |
| Production | `Production` | 第4行 | 第5–23行 | 第24行 | Indonesia 第18行、Malaysia 第20行、Thailand 第21行 |
| Imports | `Palm oil Imports` | 第28行 | 第29–68行 | 第69行 | Malaysia 第56行；无 Indonesia、Thailand 行 |
| Exports | `Palm oil Exports` | 第73行 | 第74–94行 | 第95行 | Indonesia 第87行、Malaysia 第88行、Thailand 第92行 |
| Domestic Consumption | `Palm oil Disappear.(a)` | 第99行 | 第100–134行 | 第135行 | Indonesia 第122行、Malaysia 第127行、Thailand 第131行 |
| Ending Stocks | `Palm oil Ending stocks` | 第139行 | 第140–152行 | 第153行 | Indonesia 第148行、Malaysia 第149行、Thailand 第151行 |
| Stocks/Use Ratio | 本表没有该章节 | 不适用 | 不适用 | 不适用 | 必须从 AN26391 获取全球直接值，或按国家派生 |

### 3.2 全球汇总补充表 AN26391

- `report_id`：`AN26391`
- 工作表名称：`AN26391`
- 完整标题：`PALM OIL : Summary World Supply and Demand Balance - - Quarterly & Annual (1000 T)`
- 使用区域：`A1:Q9`
- 标题行：第 1 行
- 表头行：第 2 行
- 指标行：第 3–9 行
- 完整 Oct–Sept 年度列：`B:F`（25/26F 至 21/22）
- 必须排除：`G:N` 的季度列，以及 `O:Q` 的 Jan–Dec 年度列
- 数量单位：`1000 T`
- `Stocks/Usage(b)`：第 9 行，单位为 `%`
- 预测标记：与 AN26392 相同，`25/26F` 为明确预测。

AN26391 的全球指标均为直接披露值；但国家可用性必须以 AN26392 的分章节国家行判断。

## 4. Indonesia 与 Malaysia 年度表

### 4.1 Indonesia

#### AN64402 自然年度平衡表

- 标题：`INDONESIA : Palm oil Balance (1000 T)`
- 使用区域：`A1:G8`
- 标题行：第 1 行；表头行：第 2 行；数据区：`A3:G8`
- 期间：Jan Dec 2026 至 Jan Dec 2021
- 单位：`1000 T`
- 直接指标：`Open'g stocks、Production、Imports、Exports、Dom.Disappear(a)、Ending stocks`
- 无 Stocks/Use Ratio 行。
- 2026 列没有 `F` 或星号，不能从本表认定为明确预测。如果未来按现有 Oil World 规则接入，未来完整期间但无 `F` 只能标为 `implicit_forecast`，同时保留原始期间文本。

#### AN64405 面积/单产/产量表

- 标题：`INDONESIA: Mature Area, Yields and Production of Palm Oil`
- 使用区域：`A1:I5`
- 期间：Jan Dec 2027 至 Jan Dec 2020
- 指标及单位：Area（1000 ha）、Yields（T per ha）、Crop（1000 T）
- 属于自然年度辅助生产表，不解决 Oct–Sept 平衡表中的 Imports 或 Beginning Stocks 缺口。

### 4.2 Malaysia

#### AN70402 自然年度平衡表

- 标题：`MALAYSIA : Palm oil Balance (1000 T)`
- 使用区域：`A1:G8`
- 标题行：第 1 行；表头行：第 2 行；数据区：`A3:G8`
- 期间：Jan Dec 2026 至 Jan Dec 2021
- 单位：`1000 T`
- 直接指标：`Open'g stocks、Production、Imports、Exports、Dom.Disappear(a)、Ending stocks`
- 无 Stocks/Use Ratio 行。
- 2026 列没有 `F` 或星号；预测状态处理原则与 AN64402 相同。

#### AN70403 面积/单产/产量表

- 标题：`MALAYSIA: Mature Area, Yields and Production of Palm Oil`
- 使用区域：`A1:I5`
- 期间：Jan Dec 2027 至 Jan Dec 2020
- 指标及单位：Area（1000 ha）、Yields（T per ha）、Crop（1000 T）
- 属于自然年度辅助生产表。

重要区别：AN64402 和 AN70402 中的 Opening Stocks 是直接值，但对应 Jan–Dec 自然年度；在与 USDA 进行 Oct–Sept 市场年度对比的 AN26392 口径下，Indonesia 和 Malaysia 的 Beginning Stocks 并不直接存在，只能由上一市场年度 Ending Stocks 顺延。两种期间口径不得混用。

## 5. USDA Palm Oil 范围可用性矩阵

以下矩阵严格按 AN26392 的完整 Oct–Sept 年度列 `B:F` 判断；全球 Stocks/Use Ratio 使用 AN26391。`derived` 只表示计算在同一期间、同一单位和同一国家范围内可审计，不代表已授权生成数据。

| country | Beginning Stocks | Production | Imports | Exports | Domestic Consumption | Ending Stocks | Stocks/Use Ratio |
|---|---|---|---|---|---|---|---|
| G2 | **derived**：Indonesia + Malaysia 上一年度 Ending Stocks；仅22/23–25/26 | **derived**：AN26392 第18行 + 第20行 | **missing**：Indonesia 在 Imports 章节无行，不能把缺失当0 | **derived**：第87行 + 第88行 | **derived**：第122行 + 第127行 | **derived**：第148行 + 第149行 | **derived**：G2 Ending Stocks ÷ G2 Domestic Consumption ×100；不得相加国家比率 |
| Global | **direct**：AN26392 第3行 | **direct**：第24行 Total | **direct**：第69行 Total | **direct**：第95行 Total | **direct**：第135行 Total | **direct**：第153行 Total | **direct**：AN26391 第9行 |
| Indonesia | **derived**：上一年度第148行 Ending Stocks；仅22/23–25/26 | **direct**：第18行 | **missing**：Imports 章节无 Indonesia 行 | **direct**：第87行 | **direct**：第122行 | **direct**：第148行 | **derived**：Ending Stocks ÷ Disappearance ×100 |
| Malaysia | **derived**：上一年度第149行 Ending Stocks；仅22/23–25/26 | **direct**：第20行 | **direct**：第56行 | **direct**：第88行 | **direct**：第127行 | **direct**：第149行 | **derived**：Ending Stocks ÷ Disappearance ×100 |
| Thailand | **derived**：上一年度第151行 Ending Stocks；仅22/23–25/26 | **direct**：第21行 | **missing**：Imports 章节无 Thailand 行 | **direct**：第92行 | **direct**：第131行 | **direct**：第151行 | **derived**：Ending Stocks ÷ Disappearance ×100 |

补充判定：

- **Indonesia/Malaysia Beginning Stocks**：Jan–Dec 专属平衡表中直接存在；Oct–Sept 全球明细表中不直接存在。用于 USDA 市场年度比较时只能顺延上一年度 Ending Stocks，且 2021/22 必须保持缺失。
- **G2**：原表没有 G2 行。除 Imports 外，Indonesia 和 Malaysia 的相关行处于相同 Oct–Sept 年度、相同 `1000 T` 单位，可逐年求和。G2 Ratio 必须用合计后的 Ending Stocks 与 Domestic Consumption 重新计算。由于 Imports 缺失，G2 不能作为完整、可平衡的 USDA 式供需矩阵安全发布。
- **Thailand**：Production、Exports、Disappearance、Ending Stocks 直接存在；Imports 缺失，Beginning Stocks 和 Ratio 需要派生，因此不完整。
- **Global**：Production、Imports、Exports、Disappearance、Ending Stocks 应分别使用相应章节的 `Total` 行；Opening Stocks 使用第3行；Stocks/Use Ratio 使用 AN26391 第9行。

## 6. 章节国家覆盖范围与 Other Countries

各章节国家列表不一致：

- Production 章节以主要生产国为主，包含 Indonesia、Malaysia、Thailand，但不包含 EU-27、China、India、United States 等多数进口/消费国。
- Imports 章节包含 Malaysia，但没有 Indonesia、Thailand。
- Exports 章节包含 Indonesia、Malaysia、Thailand。
- Disappearance 章节包含 Indonesia、Malaysia、Thailand，并覆盖主要消费国。
- Ending Stocks 章节只列少量重点库存国家，范围最窄。
- Opening Stocks 没有国家明细，仅有全球值。

因此：

1. 各章节中的 `Oth countries` 不是相同国家集合，而是“该章节未单列国家的残差”。
2. 不得把 Production 的 `Oth countries` 与 Imports、Exports、Disappearance、Ending Stocks 的 `Oth countries` 拼成一套国家平衡表。
3. 这种范围差异很可能触发标准平衡关系警告，属于原始报表聚合口径差异，不应通过改数消除。
4. Global 的各章节 Total 可以在全球层面组合，因为它们明确代表各章节世界总量；国家层面则必须逐指标判断是否存在直接行。

## 7. Palm Oil 月度工作表

| report_id / 工作表 | 国家或范围 | 指标 | 年份范围 | 月份范围 | 单位 | 星号与脚注核查 |
|---|---|---|---|---|---|---|
| AN2639X1 | Malaysia | Production、Exports、End.stocks | 2022–2026 | Jan–Dec；Production/Exports 有 Jan.-Dec 合计 | 1000 T | 工作表 `A1:F52` 未发现星号；本地目录链接指向的 `stats/AN2639X1.HTM` 不存在，无法从现有文件确认星号含义或相关脚注 |
| AN2639X2 | Indonesia | Production、Exports、End.stocks | 2022–2026 | Jan–Dec；Production/Exports 有 Jan.-Dec 合计 | 1000 T | 工作表 `A1:F52` 未发现星号；`stats/AN2639X2.HTM` 不存在，无法从现有文件确认 |
| AN2639X3 | Russia、Egypt、S.Africa,Rep、U.S.A.、Mexico、Bangladesh、China,PR、India、Pakistan、Philippines、Turkiye、Grand Total | Imports | 21/22–25/26 | Oct–Sept，并有 Oct.-Sept 合计 | 1000 T | 工作表 `A1:F224` 未发现星号；`stats/AN2639X3.HTM` 不存在，无法从现有文件确认 |
| AN2639X4 | EU-27 | Imports from non-EU Countries、Exports to non-EU Countries | 21/22–25/26 | Oct–Sept，并有 Oct.-Jun 与 Oct.-Sept 合计 | 1000 T | 工作表 `A1:F36` 未发现星号；25/26 的 Jul–Sept 与 Oct.-Sept 为 `..`，不是星号；`stats/AN2639X4.HTM` 不存在，无法从现有文件确认 |

目录 HTML 仅保留上述四个明细 HTML 的链接，当前 `stats` 目录没有对应文件。现有工作簿也没有可见星号或星号脚注。因此，对截图或其他版本原始报告中星号所代表的预测、估计或其他状态，结论必须是：**无法从现有文件确认**。

## 8. 建议的解析配置（仅建议，未创建配置）

### 8.1 全球市场年度主体

```text
report_id: AN26392
parser: sectioned_global_balance
category: Oils
commodity: Oil, Palm
frequency: annual_market_year
period_columns: B:F
exclude_columns: G:M
original_unit: 1000 T
standard_unit: 1000 T
market_year_start_month: 10
market_year_end_month: 9
title_row: 1
header_rows: [2, 28, 73, 99, 139]
sections:
  beginning_stocks: row 3
  production: rows 5:24
  imports: rows 29:69
  exports: rows 74:95
  domestic_consumption: rows 100:135
  ending_stocks: rows 140:153
forecast_rule: original_period ends with F => explicit_forecast
```

### 8.2 全球库存消费比补充

```text
report_id: AN26391
parser: global_summary
period_columns: B:F
exclude_columns: G:Q
stocks_use_ratio_row: 9
```

### 8.3 国家自然年度表

`AN64402`、`AN70402` 应使用独立的 `country_balance_calendar_year` 模板，明确 `market_year_start_month=1`、`market_year_end_month=12`。不得把这些 Jan–Dec 数值伪装成 Oct–Sept 市场年度数据。

### 8.4 派生规则

- Beginning Stocks：同一国家、同一单位的上一完整市场年度 Ending Stocks 顺延；没有上一年度来源时保持缺失。
- Stocks/Use Ratio：`Ending Stocks / Domestic Consumption × 100`；保留组成来源，不得把国家比率相加。
- G2：仅对 Indonesia、Malaysia 均直接存在且期间一致的指标求和；任一组成国缺失时结果保持缺失。
- Imports 缺行不得转成 0。

## 9. 预计记录数与矩阵数

以 AN26392 的 5 个完整 Oct–Sept 年度、7 个展示指标和 USDA 五个范围估算：

- 逻辑数据格：`5 countries × 5 periods × 7 metrics = 175`
- 可直接或安全派生的非空记录：约 `156`
- 必须保持缺失：`19`
  - Indonesia、Malaysia、Thailand、G2 的 2021/22 Beginning Stocks：4 格
  - Indonesia、Thailand、G2 的 Imports：15 格
- 如果为所有 USDA 范围建立允许缺失值的矩阵：最多新增 5 个矩阵。
- 如果严格禁止发布不完整 G2：建议先最多建立 4 个来源国家/全球矩阵，约 127 条非空记录；G2 暂不发布。
- 如果第一步只接入完全直接披露的全球矩阵：1 个矩阵、35 条非空记录。

上述数量不包括 AN64402/AN70402 的 Jan–Dec 自然年度数据，也不包括月度表、面积和单产表。

## 10. 创建 2026-06-r2 前仍需解决的问题

1. 明确第一批国家矩阵是否允许保留 Imports 等关键指标缺失；不得为了形成完整平衡而填 0。
2. 明确 Indonesia、Malaysia 是否采用 AN26392 的 Oct–Sept 市场年度口径，还是另设 Jan–Dec 自然年度页面；两者不能合并成一个期间序列。
3. 决定 Malaysia 2021/22 Beginning Stocks、Indonesia/Thailand/G2 2021/22 Beginning Stocks的处理方式；当前证据要求保持缺失。
4. 决定 G2 是暂缓发布，还是允许以“部分指标派生、Imports 缺失、不可执行完整平衡校验”的质量状态发布。
5. 为派生 Beginning Stocks、Stocks/Use Ratio 和 G2 建立完整的 source_range、组成国家和派生方法追溯设计。
6. 对 `Oth countries` 设置范围差异质量状态，避免将其纳入标准国家平衡校验。
7. 如需使用月度 2026 数据的预测/估计状态，必须取得包含星号定义或脚注的原始明细报告；当前文件不足以确认。
8. 在任何修正版发布前，需新增 Palm Oil 专项结构漂移、期间排除、缺失不补零、派生追溯、G2完整性和平衡警告测试。

## 11. 最终接入建议

- **可以直接接入现有年度页面的完整对象**：Global；Beginning Stocks、Production、Imports、Exports、Domestic Consumption、Ending Stocks、Stocks/Use Ratio 均有直接来源。
- **可以接入但包含派生或缺失的对象**：
  - Malaysia：Beginning Stocks、Stocks/Use Ratio 需派生；2021/22 Beginning Stocks 缺失。
  - Indonesia：Beginning Stocks、Stocks/Use Ratio 需派生；Imports 缺失。
  - Thailand：Beginning Stocks、Stocks/Use Ratio 需派生；Imports 缺失。
- **必须保持缺失的指标**：Indonesia Imports、Thailand Imports、G2 Imports，以及无法由前一年 Ending Stocks支持的 2021/22 国家/G2 Beginning Stocks。
- **G2 是否可安全生成**：可以安全生成部分指标，但不能安全生成完整 USDA 式供需矩阵。建议在 Imports 口径解决前不发布 G2，或仅以明确的“不完整派生矩阵”发布并禁用完整平衡校验。
- **月度数据**：需要独立月度模型，本轮不应进入年度矩阵。

