# Oil World 2026-06 报表映射专项审计

> 本报告仅做原始报表结构与映射审计。原始Excel、目录页和报表单元格为最终依据；未生成业务数据、解析器、矩阵或发布快照。

## 一、总览

- 固定组合总数：**649**（体系 × 商品 × 国家/地区 × 指标）。
- direct：**286**；derived：**75**；missing：**55**；not_applicable：**182**；conflict：**51**。
- 目录筛选候选报表：**108**；最终采用：**30**。
- 最新年度绝对变化可计算：**358/649（55.2%）**；剔除not_applicable后为 **358/467（76.7%）**。
- 季度修正稳定键准备：**361/649（55.6%）**；剔除not_applicable后为 **361/467（77.3%）**。本期仅评估条件，不计算实际修正。

| system | direct | derived | missing | not_applicable | conflict | latest_ready | revision_ready |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 大豆体系 | 85 | 32 | 3 | 54 | 24 | 116 | 117 |
| 菜籽体系 | 106 | 19 | 37 | 63 | 6 | 124 | 125 |
| 葵花体系 | 77 | 16 | 11 | 45 | 16 | 92 | 93 |
| 棕榈油体系 | 18 | 8 | 4 | 20 | 5 | 26 | 26 |

## 二、核心口径结论

- 上游油籽：Crop→Production；Crush/Crushings→Crush；Domestic Consumption仅由同表Crushings + Other use派生。
- 油脂和油粕：原表Production或Meal Output统一推荐为Product Output；标准Production标记not_applicable，避免重复。
- Disappearance：逐表用供需恒等式核对后映射Domestic Consumption；原始脚注标记继续保留。
- 数量标准单位建议1000 T；Mn T只记录换算规则（×1000），本审计未改动原值。面积1000 ha、单产T/ha、比例%。
- Exp - Imp不在本次标准指标中使用，不能拆成Imports或Exports。空白和缺行均不解释为0。

## 三、大豆体系

推荐主报表：AN139900（Global种子平衡）、AN13992（共同Oct–Sept Crush/Trade）、AN13993（Production/Area/Yield）、AN40502B/AN50001/AN62801（国家种子平衡）、AN23992（豆油）、AN33992（豆粕）。AN51000A仅作为Brazil自然年冲突证据。

- United States与China种子余额可直接使用；Argentina为Apr–Mar完整市场年度。Brazil为Jan–Dec，暂缓进入主市场年度表。
- 豆油和豆粕国家指标优先来自共同Oct–Sept章节；Beginning Stocks只在同一Ending Stocks序列上安全顺延。
- G3不能用三张国家余额表直接相加库存或消费；可用同一AN13992章节构建完整Trade/Crush，用AN13993统一作物年度列构建Production/Area/加权Yield。

## 四、菜籽体系

推荐主报表：AN149900、AN14992、AN14993、AN40001、AN049104、AN80501、AN62802、AN17503、AN18103、AN24992、AN34992。

- Canada Aug–July、EU/Russia/Ukraine July–June、Australia Oct–Sept、China June–May，均为完整但不同市场年度；页面必须显示起止月份。
- China Rapeseed Exports原指标行存在但年度值全部空白，状态为missing，不能按0。
- EU Area/Yield可按AN14993所列成员行聚合；与国家平衡表Production有0–1千吨四舍五入差，季度更新需检查成员覆盖。
- 菜油/菜粕的章节国家列表不完全一致；缺失的库存或贸易行保持missing。AN24992 EU Ending Stocks原表同时有Spain与EU-27行，本审计不重算或调整。

## 五、葵花体系

推荐主报表：AN147900、AN14792、AN14793、AN17501、AN18101、AN049106、AN24792、AN34792。AN50002仅作为Argentina自然年冲突证据。

- Russia/Ukraine为Sept–Aug，EU为Aug–July；Argentina表为Jan–Dec自然年，因此Argentina Sunflowerseed主余额暂缓。
- EU Area/Yield可按AN14793成员行聚合，Production与国家余额存在1千吨四舍五入差。
- 葵油与葵粕继续按共同Oct–Sept章节逐指标使用；章节缺行不补0。

## 六、棕榈油体系

推荐唯一主表：AN26392《PALM OIL : World Supply and Demand Balance (1000 T)》。完整市场年度只取B:F（Oct–Sept 25/26F至21/22），G:M的Apr–Sept、Oct–Mar和Jan–Dec全部排除。

- Global使用各章节Total与全表Open'g stocks；不重算Total。
- Indonesia：Product Output、Exports、Disappearance、Ending Stocks直接；Beginning可由上年Ending顺延；Imports章节没有Indonesia行，必须missing。
- Malaysia：Product Output、Imports、Exports、Disappearance、Ending Stocks直接；Beginning可顺延。
- India：Imports、Disappearance、Ending Stocks直接；Beginning可顺延；Product Output和Exports缺行。
- G2：Product Output、Exports、Domestic Consumption、Ending Stocks及重叠年度Beginning Stocks可由Indonesia+Malaysia安全求和；Imports因Indonesia缺行不能发布。
- AN64402/AN70402为Jan–Dec自然年，AN64405/AN70403为自然年成熟面积专题，均不进入本次市场年度主映射。

## 七、派生规则

- **Beginning Stocks(t) = Ending Stocks(t-1)**（22个组合）：仅对同一Oct–Sept序列顺延；不使用Jan–Dec国家表。
- **Domestic Consumption = Crushings + Other use**（12个组合）：只在同一国家平衡表、同一年度和单位下派生。
- **Stocks/Use Ratio = Ending Stocks / Domestic Consumption × 100**（12个组合）：分母口径由同商品世界表直接公布的Stocks/usage关系验证；变化单位为百分点。
- **G3 = United States + Brazil + Argentina**（9个组合）：只发布所有组成国同年、同指标、同单位均有效的年度；空白不得按0。
- **G2 = Indonesia + Malaysia**（4个组合）：只发布所有组成国同年、同指标、同单位均有效的年度；空白不得按0。
- **Domestic Consumption = Crush + Other use**（3个组合）：Oil World世界平衡式确认该两项合计为总国内使用；保留原始指标名。
- **G3 = United States + Brazil + Argentina**（3个组合）：三个组成国位于同一Oct–Sept章节和同一单位；不得用空白补0。
- **EU Area Harvested = sum of listed EU member rows**（2个组合）：成员行合计与EU国家平衡表Production存在0–1千吨四舍五入差；季度更新需检查成员覆盖变化。
- **EU Yield = EU member Production sum / EU member Area sum**（2个组合）：成员行合计与EU国家平衡表Production存在0–1千吨四舍五入差；季度更新需检查成员覆盖变化。
- **G3 Beginning Stocks(t) = sum of member Ending Stocks(t-1)**（2个组合）：只发布组成国在相同Oct–Sept年度均有Ending Stocks的重叠年度。
- **G2 Beginning Stocks(t) = sum of member Ending Stocks(t-1)**（1个组合）：只发布组成国在相同Oct–Sept年度均有Ending Stocks的重叠年度。
- **G3 Area Harvested = United States + Brazil + Argentina**（1个组合）：同一世界作物年度列可聚合，但美国、阿根廷和巴西实际收获月份不同，页面必须披露Oil World作物年度口径。
- **G3 Production = United States + Brazil + Argentina**（1个组合）：同一世界作物年度列可聚合，但美国、阿根廷和巴西实际收获月份不同，页面必须披露Oil World作物年度口径。
- **G3 Yield = sum(Production) / sum(Area Harvested)**（1个组合）：同一世界作物年度列可聚合，但美国、阿根廷和巴西实际收获月份不同，页面必须披露Oil World作物年度口径。

## 八、冲突与无法确认项目

共51个conflict。主要类型：

- Brazil Soybeans与Argentina Sunflowerseed为Jan–Dec自然年，不能混入市场年度矩阵。
- G3三张国家余额表周期分别为Sept–Aug、Apr–Mar、Jan–Dec，库存和总消费不能直接相加。
- 油脂/油粕/Palm Oil未直接公布Stocks/Use Ratio，且Disappearance脚注(a)的完整分母定义未从现有文件确认。

另有55个missing，主要来自章节缺少国家行、China Rapeseed Exports全空、Indonesia Palm Oil Imports缺行及聚合组成国不完整。逐项见coverage_matrix.csv。

## 九、G3结论

仅同一Oil World世界表、相同Oct–Sept或统一作物年度列中的完整组成国指标可构建；三张国家平衡表周期不同，不能用于G3库存或总消费聚合。

- 可构建：Soybeans / Production
- 可构建：Soybeans / Imports
- 可构建：Soybeans / Exports
- 可构建：Soybeans / Crush
- 可构建：Soybeans / Area Harvested
- 可构建：Soybeans / Yield
- 可构建：Soybean Oil / Beginning Stocks
- 可构建：Soybean Oil / Imports
- 可构建：Soybean Oil / Exports
- 可构建：Soybean Oil / Product Output
- 可构建：Soybean Oil / Domestic Consumption
- 可构建：Soybean Oil / Ending Stocks
- 可构建：Soybean Meal / Beginning Stocks
- 可构建：Soybean Meal / Exports
- 可构建：Soybean Meal / Product Output
- 可构建：Soybean Meal / Domestic Consumption
- 可构建：Soybean Meal / Ending Stocks
- 阻断：Soybeans / Beginning Stocks: G3国家平衡表分别采用Sept–Aug、Apr–Mar和Jan–Dec，不能未经说明直接相加。
- 阻断：Soybeans / Domestic Consumption: G3国家平衡表分别采用Sept–Aug、Apr–Mar和Jan–Dec，不能未经说明直接相加。
- 阻断：Soybeans / Ending Stocks: G3国家平衡表分别采用Sept–Aug、Apr–Mar和Jan–Dec，不能未经说明直接相加。
- 阻断：Soybeans / Stocks/Use Ratio: G3国家平衡表分别采用Sept–Aug、Apr–Mar和Jan–Dec，不能未经说明直接相加。
- 阻断：Soybean Oil / Stocks/Use Ratio: 聚合Ending Stocks与Disappearance虽可求和，但脚注分母定义未从现有文件确认，暂不派生比例。
- 阻断：Soybean Meal / Imports: G3至少一个组成国在该章节缺行或缺值，不能补0。
- 阻断：Soybean Meal / Stocks/Use Ratio: 聚合Ending Stocks与Disappearance虽可求和，但脚注分母定义未从现有文件确认，暂不派生比例。

## 十、G2结论

AN26392共同Oct–Sept章节可用于完整成员指标求和；Indonesia Imports缺行，因此G2 Imports及完整标准平衡表不得发布。

- 可构建：Beginning Stocks
- 可构建：Exports
- 可构建：Product Output
- 可构建：Domestic Consumption
- 可构建：Ending Stocks
- 阻断：Imports: G2至少一个组成国在该章节缺行或缺值，不能补0。
- 阻断：Stocks/Use Ratio: 聚合Ending Stocks与Disappearance虽可求和，但脚注分母定义未从现有文件确认，暂不派生比例。

## 十一、建议第一批开发范围

- Global：九个商品的direct/derived数量指标与油籽直接Stocks/Use Ratio
- 完整市场年度国家油籽平衡表（排除Brazil Soybeans与Argentina Sunflowerseed自然年冲突）
- AN23992/33992/24992/34992/24792/34792/26392中实际存在的国家章节行
- G2的Product Output、Exports、Domestic Consumption、Ending Stocks及可顺延Beginning Stocks
- G3仅发布同一世界章节或统一作物年度表中组成国完整的指标

## 十二、建议暂缓范围

- Brazil Soybeans与Argentina Sunflowerseed Jan–Dec自然年余额
- 所有油脂/油粕/Palm Oil Stocks/Use Ratio，直到脚注分母定义确认
- 所有缺行或空白指标，尤其Indonesia Palm Oil Imports和G2 Imports
- 季度、半年、月度、Jan–Dec及截至某月累计报表

## 十三、来源追溯与季度更新要求

coverage_matrix.csv与JSON为每个组合记录report_id、工作表、标题/表头/数据行、章节边界、完整年度列、排除列、原始/标准单位、来源范围、脚注/星号、预测及派生状态。

quarter_revision_ready只表示未来可形成稳定键，不代表本期已计算修正。后续发布仍必须检测工作表、标题、章节、国家行、单位和完整年度列漂移；正常年度滚动不应视为错误。
