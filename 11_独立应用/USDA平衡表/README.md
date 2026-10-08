# USDA PS&D 平衡表

## 项目目标

开发、发布及宿主存储维护统一遵循[主仓库规范](../../07_docs/03_标准开发与生产发布规范.md)、[发布清单](../../07_docs/04_开发与发布检查清单.md)及[数据盘维护手册](../../07_docs/06_日常运行与数据更新手册.md#12-数据盘迁移与维护)。
实际运行数据盘及独立 USDA 数据挂载执行前重新核验；迁移不覆盖历史快照、
版本文件和原始数据，不隐式更新 USDA 或其他应用，页面发布仍验收 `/usda/` 相关路由。

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

## 市场年度口径说明

页面筛选器下方提供默认折叠的“市场年度口径说明”。说明表仅列示国家或地区、品种范围和市场年度；实际起止年份会根据当前选择的市场年度动态计算。

棕榈油适用统一口径：马来西亚、印度尼西亚及其 G2 聚合口径均为 10 月至次年 9 月。G2 表示马来西亚与印度尼西亚棕榈油合计；当前如尚未生成对应 G2 数据，页面不会凭配置虚构数据。

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

## 库存/总使用比

库存/总使用比 = `Ending Stocks / Total Use × 100`，其中 `Total Use = Domestic Consumption + Exports`。

若 `Domestic Consumption` 或 `Exports` 缺失、相加无效或分母不大于 0，则回退为 `Total Use = Total Distribution - Ending Stocks`。分母仍无效时保持为空。该指标以百分比展示并保留一位小数，适用于所有 Oils、Meals、Oilseeds 及其 G3、Global 聚合口径。

## 上游油籽关联规则

当用户选择 `Oils` 或 `Meals` 时，需额外读取同一国家、同一市场年度对应上游 `Oilseed` 的 `Production` 和 `Crush`。例如选择 `Oil, Soybean + United States`，展示“大豆产量”和“大豆压榨”；`Meal, Soybean` 同样处理。Rapeseed、Sunflowerseed、Cottonseed、Peanut、Palm Kernel 均按此规则关联。

## 后续部署

前端构建产物位于 `dist/`。数据处理后的 JSON 静态放置在 `public/data/`，可部署到任意静态托管平台（如 Vercel、Netlify 或企业静态服务器）。部署前应先运行 `npm run build:data` 和 `npm run build`。

## 月度快照与修正对比

- 每月新下载的原始 USDA CSV 应放在 `data/raw/usda_psd/YYYY-MM/`；原始文件只读，不直接修改。
- 在 `configs/usda_report_version.json` 中设置 `currentReportMonth` 和 `previousReportMonth`，格式均为 `YYYY-MM`。
- 运行 `npm run build:data` 后，最新前端数据仍写入 `public/data/`，同时会保存到 `data/snapshots/usda_psd/currentReportMonth/`，其中包含 `index.json` 与 `matrix/`。
- 运行 `npm run compare:data` 比较当前快照与上月快照；若上月快照尚不存在，会生成基准提示而不会报错。
- 表格的“最新同比”只比较当前 matrix 的最新市场年度与上一市场年度；它不同于本月相对上月快照的“月度修正”。

## 当前稳定版本说明

当前稳定版本采用研究白名单数据范围，涵盖 9 个核心商品、15 个国家或聚合口径，并生成 53 个可供前端加载的 matrix JSON 文件。豆系商品提供 G3（United States、Brazil、Argentina）和 Global 聚合视图；所有研究范围内商品均提供 Global 视图。

平衡表统一展示“库存/总使用比”，不再展示“期末库销比”或“库存/国内消费比”。该指标按本 README 的“库存/总使用比”规则重新计算，缺失或无效分母保持为空，不用 0 替代。

## 本地启动方式

本项目使用项目内的 pnpm 依赖环境运行；没有全局 npm 时，可直接使用 pnpm 等价命令：

1. 安装依赖：`pnpm install --frozen-lockfile`
2. 生成前端数据：`pnpm run build:data`
3. 启动本地开发服务器：`pnpm run dev -- --host 127.0.0.1 --port 5173`
4. 生产构建校验：`pnpm run build`

开发服务器启动后访问 [http://127.0.0.1:5173](http://127.0.0.1:5173)。原始数据始终只读；执行数据构建不会修改 `raw/` 中的 CSV 文件。
