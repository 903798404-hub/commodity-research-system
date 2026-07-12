# 阶段验收报告

验收日期：2026-07-12
正式项目路径：`C:\Users\xx202\Desktop\codex自动更新\codex-projects\USDA平衡表`

## 数据与构建结果

- 原始数据文件：`raw/psd_oilseeds_202606.csv`（本次只读，未修改）。
- 数据构建命令：`pnpm run build:data`。
- 生产构建命令：`pnpm run build`。
- 数据完整性检查：`pnpm run check:data`，结果为 53 个 matrix、fatal 0、warning 0、info 0。
- 市场年度规则测试：5 项全部通过。
- `public/data/index.json` 与 `public/data/matrix/` 均存在且可读取。
- 当前生成 9 个商品、15 个国家或聚合口径、53 个 matrix JSON。

## 默认页面与指标口径

默认选择为 `Oils + Oil, Soybean + United States`，浏览器实际加载正常。

页面表格和图表仅使用“库存/总使用比”，未发现旧名称“期末库销比”“库存/国内消费比”或旧字段 `stockToConsumptionRatio`。已扫描 `public/data/` 下全部 JSON 文件，未发现以上旧字段或旧中文名。

库存/总使用比计算公式：

`库存/总使用比 = Ending Stocks / Total Use × 100`

其中优先使用：

`Total Use = Domestic Consumption + Exports`

如国内消费或出口缺失、无效，或合计分母不大于 0，则回退为：

`Total Use = Total Distribution - Ending Stocks`

分母仍无效时结果保持为空；展示为百分比并保留一位小数。

默认图表标题会显示当前选择的指标，例如“供需指标趋势：期末库存、库存/总使用比”。图例和 Tooltip 均直接使用当前指标名称，因此不会再显示旧口径名称。

## 默认组合的最新年度核对

组合：`Oil, Soybean + United States`
最新市场年度：`2026`（页面标签 `26/27`）

| 字段 | 数值 |
| --- | ---: |
| Ending Stocks（期末库存） | 85.1 万吨 |
| Domestic Consumption（消费量） | 1485.6 万吨 |
| Exports（出口量） | 18.1 万吨 |
| Total Use（消费量 + 出口量） | 1503.7 万吨 |
| 库存/总使用比 | 5.7% |

## 运行时切换验证

以下组合均已在本地页面实际切换并成功加载平衡表与“库存/总使用比”行：

- `Oil, Soybean + Brazil`
- `Oil, Soybean + Argentina`
- `Meal, Soybean + United States`
- `Oilseed, Soybean + United States`

## 本地启动方式

1. `pnpm run build:data`
2. `pnpm run dev -- --host 127.0.0.1 --port 5173`
3. 浏览器访问 `http://127.0.0.1:5173`

如需生产构建校验，执行 `pnpm run build`。本机没有全局 npm 时，以上 pnpm 命令可直接使用项目已有依赖环境执行。

## 下一阶段建议

在不改变当前指标口径的前提下，可优先补充面向研究使用的口径注释、下载字段说明和发布前性能优化；生产构建目前仅存在 Vite 的大包体积提示，不影响本次验收通过。
