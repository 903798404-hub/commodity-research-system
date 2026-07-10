# 数据构建报告

- 原始 CSV：`psd_oilseeds_202606.csv`
- 生成商品数：27
- 生成国家数：168
- 生成 matrix JSON 数：1592
- Oil, Soybean + United States：成功生成
- 无法识别 category 的 Commodity_Description：0
- 缺少关键字段的数据行：0

说明：matrix 文件按 Commodity_Code + Country_Code 拆分；数量单位为 (1000 MT) 或 1000 MT 时已转换为万吨。
