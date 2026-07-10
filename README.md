# USDA PS&D 平衡表

## 项目目标

把 USDA Production, Supply and Distribution (PS&D) 原始数据转换为一个可交互的油籽、油脂、粕类供需平衡表网站。用户将能选择 `Oilseeds`、`Oils` 或 `Meals`，再选择商品和国家，查看按市场年度排列的平衡表及后续图表。

## 文件夹结构

- `raw/`：原始 USDA 数据。标准输入路径为 `raw/usda_psd.csv`，**不得直接修改原始文件**。
- `scripts/`：数据清洗、字段标准化和 JSON 构建脚本。
- `public/data/`：前端读取的处理后 JSON 数据。
- `src/`：React 前端代码。
- `docs/`：需求、字段规则和开发流程。
- `output/`：数据检查报告与临时检查输出。

## 数据来源

数据来源为 USDA Foreign Agricultural Service 的 Production, Supply and Distribution (PS&D) 数据导出。当前放入的文件为 `raw/psd_oilseeds_202606.csv`；后续自动化流程以 `raw/usda_psd.csv` 作为标准文件名。原始数据仅作读取，不在项目中就地编辑。

## 更新流程

1. 下载最新 USDA PS&D 导出数据，放入 `raw/usda_psd.csv`。
2. 保留原始文件不变，运行 `npm run build:data`。
3. 脚本读取、校验和转换数据，并生成 `public/data/` 下的 JSON。
4. 运行 `npm run dev` 本地检查；通过后运行 `npm run build` 生成部署文件。

## 单位转换规则

若原始 `Unit_Description` 是 `1000 MT` 或 `(1000 MT)`，前端以“万吨”展示，计算方式为 `Value / 10`。其他单位必须保留原始单位并在数据处理规则中明确标注，不能按万吨误转换。

## 指标中文映射

| USDA 原始指标 | 中文展示 |
| --- | --- |
| Beginning Stocks | 期初库存 |
| Production | 产量 |
| Imports | 进口量 |
| Exports | 出口量 |
| Domestic Consumption | 消费量 |
| Industrial Dom. Cons. | 工业消费 |
| Food Use Dom. Cons. | 食用消费 |
| Feed Waste Dom. Cons. | 饲用及损耗消费 |
| Ending Stocks | 期末库存 |
| Crush | 压榨量 |
| Area Harvested | 收获面积 |
| Yield | 单产 |

## 库销比

期末库销比 = `Ending Stocks / Domestic Consumption`。若 `Domestic Consumption` 缺失，则使用 `Ending Stocks / Total Distribution`。前端以百分比展示，并保留一位小数。

## 上游油籽关联规则

当用户选择 `Oils` 或 `Meals` 时，需额外读取同一国家、同一市场年度对应上游 `Oilseed` 的 `Production` 和 `Crush`。例如选择 `Oil, Soybean + United States`，展示“大豆产量”和“大豆压榨”；`Meal, Soybean` 同样处理。Rapeseed、Sunflowerseed、Cottonseed、Peanut、Palm Kernel 均按此规则关联。

## 后续部署

前端构建产物位于 `dist/`。数据处理后的 JSON 静态放置在 `public/data/`，可部署到任意静态托管平台（如 Vercel、Netlify 或企业静态服务器）。部署前应先运行 `npm run build:data` 和 `npm run build`。
