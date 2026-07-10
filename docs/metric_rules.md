# 商品页面指标规则

所有表格按市场年度展示。数量指标应用 README 中的单位转换规则；期末库销比按百分比展示，保留一位小数。

## Oil, Soybean

1. 大豆产量（关联 `Oilseed, Soybean` 的 `Production`）
2. 大豆压榨（关联 `Oilseed, Soybean` 的 `Crush`）
3. 期初库存
4. 产量
5. 进口量
6. 出口量
7. 消费量
8. 工业消费
9. 食用消费
10. 期末库存
11. 期末库销比

## Meal, Soybean

1. 大豆产量（关联 `Oilseed, Soybean` 的 `Production`）
2. 大豆压榨（关联 `Oilseed, Soybean` 的 `Crush`）
3. 期初库存
4. 产量
5. 进口量
6. 出口量
7. 消费量
8. 饲用及损耗消费
9. 期末库存
10. 期末库销比

## Oilseed, Soybean

1. 期初库存
2. 产量
3. 进口量
4. 压榨量
5. 出口量
6. 消费量
7. 期末库存
8. 期末库销比

## 扩展规则

其他油脂和粕类页面沿用相应 Soybean 页面模板，并按商品名关联其上游油籽。属性名称必须以 USDA 的 `Attribute_Description` 为准；缺失值应明确显示为缺失，而非填充为零。
