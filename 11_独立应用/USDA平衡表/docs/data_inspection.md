# USDA PS&D 原始数据检查

检查文件：`raw/psd_oilseeds_202606.csv`  
文件大小：78,363,180 字节  
检查范围：字段名及前 20 条数据记录（只读；未修改原始文件）。

## 字段

1. `Commodity_Code`
2. `Commodity_Description`
3. `Country_Code`
4. `Country_Name`
5. `Market_Year`
6. `Calendar_Year`
7. `Month`
8. `Attribute_ID`
9. `Attribute_Description`
10. `Unit_ID`
11. `Unit_Description`
12. `Value`

## 关键字段检查

| 字段 | 是否存在 |
| --- | --- |
| `Commodity_Description` | 是 |
| `Country_Name` | 是 |
| `Market_Year` | 是 |
| `Attribute_Description` | 是 |
| `Unit_Description` | 是 |
| `Value` | 是 |

## 前 20 行样例

这些记录均为 `Meal, Copra`、`Australia`、市场年度 `1964` 或 `1965`；示例属性包括 `Beginning Stocks`、`Crush`、`Domestic Consumption`、`Ending Stocks`、`Exports`、`Imports`、`Production`、`Total Distribution` 与 `Total Supply`。

| 行 | 商品 | 国家 | 市场年度 | 属性 | 单位 ID |
| --- | --- | --- | --- | --- | --- |
| 1 | Meal, Copra | Australia | 1964 | Beginning Stocks | 08 |
| 2 | Meal, Copra | Australia | 1964 | Crush | 08 |
| 3 | Meal, Copra | Australia | 1964 | Domestic Consumption | 08 |
| 4 | Meal, Copra | Australia | 1964 | Ending Stocks | 08 |
| 5 | Meal, Copra | Australia | 1964 | Exports | 08 |
| 6 | Meal, Copra | Australia | 1964 | Extr. Rate, 999.9999 | 23 |
| 7 | Meal, Copra | Australia | 1964 | Feed Waste Dom. Cons. | 08 |
| 8 | Meal, Copra | Australia | 1964 | Food Use Dom. Cons. | 08 |
| 9 | Meal, Copra | Australia | 1964 | Imports | 08 |
| 10 | Meal, Copra | Australia | 1964 | Industrial Dom. Cons. | 08 |
| 11 | Meal, Copra | Australia | 1964 | Production | 08 |
| 12 | Meal, Copra | Australia | 1964 | SME | 08 |
| 13 | Meal, Copra | Australia | 1964 | Total Distribution | 08 |
| 14 | Meal, Copra | Australia | 1964 | Total Supply | 08 |
| 15 | Meal, Copra | Australia | 1965 | Beginning Stocks | 08 |
| 16 | Meal, Copra | Australia | 1965 | Crush | 08 |
| 17 | Meal, Copra | Australia | 1965 | Domestic Consumption | 08 |
| 18 | Meal, Copra | Australia | 1965 | Ending Stocks | 08 |
| 19 | Meal, Copra | Australia | 1965 | Exports | 08 |
| 20 | Meal, Copra | Australia | 1965 | Extr. Rate, 999.9999 | 23 |

> 注：控制台抽样输出未完整展开 `Unit_Description` 和 `Value` 列；后续数据构建脚本将逐条读取这两个字段，并依据 `Unit_Description` 执行单位转换。
