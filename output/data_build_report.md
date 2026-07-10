# 数据构建报告

- 原始 CSV：`psd_oilseeds_202606.csv`
- 数据模式：研究白名单模式，仅生成核心油脂油粕商品与指定国家组合。
- 年份过滤：仅保留 Market_Year >= 2018 的数据
- 生成商品数：9
- 生成国家数：15
- 生成 matrix JSON 数：53
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

- G3 = United States + Brazil + Argentina，仅用于 Oilseed, Soybean / Meal, Soybean / Oil, Soybean；期末库销比按 G3 期末库存 ÷ G3 消费量重新计算。
- Global 优先使用 USDA World 原始口径；本次未发现可识别 World/Global 原始国家记录，以下 Global 均为 synthetic sum（原始数据全部国家记录汇总）。
- Oilseed, Soybean: synthetic sum
- Meal, Soybean: synthetic sum
- Oil, Soybean: synthetic sum
- Oilseed, Rapeseed: synthetic sum
- Meal, Rapeseed: synthetic sum
- Oil, Rapeseed: synthetic sum
- Oil, Palm: synthetic sum
- Oilseed, Sunflowerseed: synthetic sum
- Oil, Sunflowerseed: synthetic sum

说明：matrix 文件按 Commodity_Code + Country_Code 拆分；数量单位为 (1000 MT) 或 1000 MT 时已转换为万吨。
