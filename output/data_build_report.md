# 数据构建报告

- 原始 CSV：`API_PSD_JSON`
- 当前报告月份：2026-07
- 已保存处理后快照：`data/snapshots/usda_psd/2026-07/`
- 数据模式：研究白名单模式，仅生成核心油脂油粕商品与指定国家组合。
- 年份过滤：仅保留 Market_Year >= 2018 的数据
- 生成商品数：9
- 生成国家数：16
- 生成 matrix JSON 数：54
- Oil, Soybean + United States：成功生成
- 无法识别 category 的 Commodity_Description：0
- 缺少关键字段的数据行：0

## 研究白名单

- 大豆链：Oilseed, Soybean / Meal, Soybean / Oil, Soybean；United States、Brazil、Argentina、China、India。
- 菜籽链：Oilseed, Rapeseed / Meal, Rapeseed / Oil, Rapeseed；Canada、Australia、European Union、Ukraine、Russia。
- 棕榈油：Oil, Palm；Malaysia、Indonesia、Thailand。
- 葵花籽链：Oilseed, Sunflowerseed / Oil, Sunflowerseed；Russia、Ukraine、European Union、Argentina。
- 已排除 Soybean (Local)、Soybeans Local、大豆（本地）等本地大豆变体。

## 聚合口径

- G3 = United States + Brazil + Argentina，仅用于 Oilseed, Soybean / Meal, Soybean / Oil, Soybean；库存/总使用比按 G3 期末库存 ÷（G3 国内消费 + G3 出口）重新计算，缺失时回退为 G3 期末库存 ÷（G3 总分配 - G3 期末库存）。
- Global 优先使用 USDA World 原始口径；本次未发现可识别 World/Global 原始国家记录，以下 Global 均为 synthetic sum（原始数据全部国家记录汇总）。
- Oilseed, Soybean: USDA World
- Meal, Soybean: USDA World
- Oil, Soybean: USDA World
- Oilseed, Rapeseed: USDA World
- Meal, Rapeseed: USDA World
- Oil, Rapeseed: USDA World
- Oil, Palm: USDA World
- Oilseed, Sunflowerseed: USDA World
- Oil, Sunflowerseed: USDA World

## 棕榈油 G2 聚合口径

- Oil, Palm + G2：成功生成
- G2 = Malaysia + Indonesia；仅用于 Oil, Palm。
- 双方共同有效 Market_Year 数量（Production、Imports、Exports、Domestic Consumption、Ending Stocks 均有效且单位一致）：9（2018、2019、2020、2021、2022、2023、2024、2025、2026）
- 单边缺失或单位不一致：未发现

- 最新共同有效年度：2026
- 最新共同有效年度关键数据（万吨）：Production 6710；Imports 45；Exports 3995；Domestic Consumption 2764.5；Ending Stocks 692.1；Total Use 6759.5；库存/总使用比 10.2%。
- 库存/总使用比使用 G2 聚合后的期末库存与总使用量重新计算，未对 Malaysia 和 Indonesia 的比率进行相加、平均或加权平均。

### 三个共同市场年度抽查

| Market Year | G2 Production = Malaysia + Indonesia | G2 Exports = Malaysia + Indonesia | G2 Domestic Consumption = Malaysia + Indonesia | G2 Ending Stocks = Malaysia + Indonesia |
| --- | --- | --- | --- | --- |
| 2018 | 6230 = 2080 + 4150 | 4664.1 = 1836.2 + 2827.9 | 1700.7 = 352.2 + 1348.5 | 535.7 = 244.8 + 290.9 |
| 2019 | 6175.5 = 1925.5 + 4250 | 4346.1 = 1721.2 + 2624.9 | 1815.4 = 355.9 + 1459.5 | 629.8 = 172.2 + 457.6 |
| 2020 | 6135.4 = 1785.4 + 4350 | 4319.9 = 1587.8 + 2732.1 | 1894.2 = 324.2 + 1570 | 681.1 = 175.6 + 505.5 |

说明：matrix 文件按 Commodity_Code + Country_Code 拆分；数量单位为 (1000 MT) 或 1000 MT 时已转换为万吨。
