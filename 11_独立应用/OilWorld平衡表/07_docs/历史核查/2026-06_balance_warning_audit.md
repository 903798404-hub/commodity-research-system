# Oil World 2026-06 平衡关系警告专项核查

## 核查范围与约束

- 原始工作簿：C:\Users\xx202\Desktop\codex自动更新\codex-projects\油世界季度表\2026-6\油世界季度表-June 2026.xlsx
- 警告来源：output\balance_check_results.json
- 核查对象：现有全部 26 条平衡关系警告。
- 采用公式：Beginning Stocks + Production + Imports - Exports - Domestic Consumption - Ending Stocks。
- 下表数值单位均为 1000 T；这些工作表原始单位也是 1000 T，因此没有发生单位换算。
- Beginning Stocks 在这些报表中不是独立章节，而是同一报表上一市场年度 Ending Stocks 的结转；表中列出的单元格是实际追溯到的上一年度期末库存单元格。
- 本轮没有调整、补齐、删除或重新分配任何数值，也没有修改任何现有解析与发布文件。

## 结论摘要

26 条警告可完整解释：

- 24 条 Other Countries 警告来自 sectioned_global_balance 各指标章节的国家清单不同。“Other Countries”是各章节分别计算的剩余项，不代表一个跨章节固定、可闭合的地理集合。这是原始报表结构和口径问题，不是解析错误，也不是四舍五入造成。
- 2 条 AN24992 / Rapeseed Oil / European Union 警告是库存章节的国家行识别错误。Ending Stocks 章节把 Spain 与 EU-27 分列，当前数据仅把 EU-27 行当作完整 European Union，遗漏 Spain。补入 Spain 后两期均精确平衡。
- 重点项 AN33992 / Soybean Meal / Other Countries / 2024/25 的 -10,346 千吨差额，逐格取值和单位均正确；原因是五个章节中 Other Countries 的覆盖范围不同，不是解析错误。
- 没有发现期间列识别错误、单位转换错误、章节边界错误、Other Countries 国家行识别错误、Total 汇总行重复纳入或派生 Domestic Consumption 错误。
- 可见章节汇总与 Total 偶有 0–3 千吨的四舍五入差，但它不是这 26 条警告中任何一条的主要原因。

## 原始章节与指标名称

|章节档案|Production 原始章节/指标|Imports 原始章节/指标|Exports 原始章节/指标|Domestic Consumption 原始章节/指标|Ending Stocks 原始章节/指标|Beginning Stocks 原始来源|
|---|---|---|---|---|---|---|
|AN34992|Meal Output / Meal Output|Rapeseed meal Imports / Imports|Rapeseed meal Exports / Exports|Rapeseed meal Disappear.(a) / Disappear.|Rapeseed meal Ending stocks / Ending Stocks|上一期间 Rapeseed meal Ending stocks / Ending Stocks|
|AN33992|Meal Output / Meal Output|Soybean meal Imports / Imports|Soybean meal Exports / Exports|Soybean meal Disappear.(a) / Disappear.|Soybean meal Ending stocks / Ending Stocks|上一期间 Soybean meal Ending stocks / Ending Stocks|
|AN34792|Meal Output / Meal Output|Sunflowermeal Imports / Imports|Sunflowermeal Exports / Exports|Sunflowermeal Disappear.(a) / Disappear.|Sunflowermeal Ending stocks / Ending Stocks|上一期间 Sunflowermeal Ending stocks / Ending Stocks|
|AN24992|Production / Production|Rapeseed oil Imports / Imports|Rapeseed oil Exports / Exports|Rapeseed oil Disappear.(a) / Disappear.|Rapeseed oil Ending stocks / Ending Stocks|上一期间 Rapeseed oil Ending stocks / Ending Stocks|
|AN23992|Production / Production|Soybean oil Imports / Imports|Soybean oil Exports / Exports|Soybean oil Disappear.(a) / Disappear.|Soybean oil Ending stocks / Ending Stocks|上一期间 Soybean oil Ending stocks / Ending Stocks|
|AN24792|Production / Production|Sunfloweroil Imports / Imports|Sunfloweroil Exports / Exports|Sunfloweroil Disappear.(a) / Disappear.|Sunfloweroil Ending stocks / Ending Stocks|上一期间 Sunfloweroil Ending stocks / Ending Stocks|

## 逐条核查

下表的“值@单元格”全部来自原始 Excel；公式列依次为 BS+P+I-E-D-ES。结论档案 OC 与 EU 的完整判断见表后。

|序号|report_id|commodity|country|period|Beginning Stocks|Production|Imports|Exports|Domestic Consumption|Ending Stocks|实际计算|差额|章节档案|结论档案|
|---:|---|---|---|---|---:|---:|---:|---:|---:|---:|---|---:|---|---|
|1|AN34992|Rapeseed Meal|Other Countries|2022/23|81@F79|2,977@E17|403@E35|232@E49|2,489@E69|136@E79|81+2977+403-232-2489-136|+604|AN34992|OC|
|2|AN34992|Rapeseed Meal|Other Countries|2023/24|136@E79|3,185@D17|333@D35|181@D49|2,457@D69|143@D79|136+3185+333-181-2457-143|+873|AN34992|OC|
|3|AN34992|Rapeseed Meal|Other Countries|2024/25|143@D79|3,251@C17|365@C35|267@C49|2,803@C69|140@C79|143+3251+365-267-2803-140|+549|AN34992|OC|
|4|AN34992|Rapeseed Meal|Other Countries|2025/26|140@C79|3,932@B17|440@B35|606@B49|3,201@B69|154@B79|140+3932+440-606-3201-154|+551|AN34992|OC|
|5|AN33992|Soybean Meal|Other Countries|2022/23|2,803@F144|9,172@E31|9,086@E70|1,935@E91|26,039@E128|2,602@E144|2803+9172+9086-1935-26039-2602|-9,515|AN33992|OC|
|6|AN33992|Soybean Meal|Other Countries|2023/24|2,602@E144|10,083@D31|11,904@D70|2,508@D91|28,700@D128|3,137@D144|2602+10083+11904-2508-28700-3137|-9,756|AN33992|OC|
|7|AN33992|Soybean Meal|Other Countries|2024/25|3,137@D144|12,012@C31|11,967@C70|2,072@C91|31,830@C128|3,560@C144|3137+12012+11967-2072-31830-3560|-10,346|AN33992|OC|
|8|AN33992|Soybean Meal|Other Countries|2025/26|3,560@C144|13,775@B31|13,335@B70|2,266@B91|33,259@B128|3,822@B144|3560+13775+13335-2266-33259-3822|-8,677|AN33992|OC|
|9|AN34792|Sunflower Meal|Other Countries|2022/23|23@F80|2,182@E15|530@E40|192@E55|3,453@E71|38@E80|23+2182+530-192-3453-38|-948|AN34792|OC|
|10|AN34792|Sunflower Meal|Other Countries|2023/24|38@E80|2,225@D15|676@D40|219@D55|3,649@D71|33@D80|38+2225+676-219-3649-33|-962|AN34792|OC|
|11|AN34792|Sunflower Meal|Other Countries|2024/25|33@D80|2,172@C15|701@C40|210@C55|3,748@C71|33@C80|33+2172+701-210-3748-33|-1,085|AN34792|OC|
|12|AN34792|Sunflower Meal|Other Countries|2025/26|33@C80|2,509@B15|798@B40|255@B55|3,916@B71|40@B80|33+2509+798-255-3916-40|-871|AN34792|OC|
|13|AN24992|Rapeseed Oil|European Union|2024/25|622@D106|10,651@C5|291@C31|579@C63|10,403@C85|588@C106|622+10651+291-579-10403-588|-6|AN24992|EU|
|14|AN24992|Rapeseed Oil|European Union|2025/26|588@C106|11,038@B5|710@B31|543@B63|11,160@B85|622@B106|588+11038+710-543-11160-622|+11|AN24992|EU|
|15|AN24992|Rapeseed Oil|Other Countries|2022/23|453@F114|847@E25|441@E49|552@E79|1,232@E99|581@E114|453+847+441-552-1232-581|-624|AN24992|OC|
|16|AN24992|Rapeseed Oil|Other Countries|2023/24|581@E114|916@D25|342@D49|628@D79|1,203@D99|550@D114|581+916+342-628-1203-550|-542|AN24992|OC|
|17|AN24992|Rapeseed Oil|Other Countries|2024/25|550@D114|1,073@C25|596@C49|738@C79|1,401@C99|514@C114|550+1073+596-738-1401-514|-434|AN24992|OC|
|18|AN24992|Rapeseed Oil|Other Countries|2025/26|514@C114|1,156@B25|584@B49|773@B79|1,525@B99|604@B114|514+1156+584-773-1525-604|-648|AN24992|OC|
|19|AN23992|Soybean Oil|Other Countries|2022/23|1,329@F155|1,693@E30|1,418@E67|965@E101|3,303@E135|1,143@E155|1329+1693+1418-965-3303-1143|-971|AN23992|OC|
|20|AN23992|Soybean Oil|Other Countries|2023/24|1,143@E155|1,830@D30|1,404@D67|1,213@D101|3,274@D135|1,246@D155|1143+1830+1404-1213-3274-1246|-1,356|AN23992|OC|
|21|AN23992|Soybean Oil|Other Countries|2024/25|1,246@D155|2,067@C30|1,921@C67|1,289@C101|3,764@C135|1,673@C155|1246+2067+1921-1289-3764-1673|-1,492|AN23992|OC|
|22|AN23992|Soybean Oil|Other Countries|2025/26|1,673@C155|2,391@B30|1,839@B67|1,229@B101|3,899@B135|1,728@B155|1673+2391+1839-1229-3899-1728|-953|AN23992|OC|
|23|AN24792|Sunflower Oil|Other Countries|2022/23|868@F149|1,224@E20|2,090@E65|773@E98|3,640@E131|1,024@E149|868+1224+2090-773-3640-1024|-1,255|AN24792|OC|
|24|AN24792|Sunflower Oil|Other Countries|2023/24|1,024@E149|1,175@D20|2,577@D65|851@D98|4,170@D131|982@D149|1024+1175+2577-851-4170-982|-1,227|AN24792|OC|
|25|AN24792|Sunflower Oil|Other Countries|2024/25|982@D149|1,019@C20|2,469@C65|967@C98|4,407@C131|1,011@C149|982+1019+2469-967-4407-1011|-1,915|AN24792|OC|
|26|AN24792|Sunflower Oil|Other Countries|2025/26|1,011@C149|1,162@B20|2,570@B65|1,252@B98|4,391@B131|1,002@B149|1011+1162+2570-1252-4391-1002|-1,902|AN24792|OC|

### 结论档案 OC：Other Countries

- 原因分类：不同指标章节中的 Other Countries 聚合范围不一致；同时属于“原始 Oil World 数据本身不满足标准平衡关系”的结构性情形。
- 是否属于解析错误：否。各数值、期间、单位、章节与国家行均逐格匹配原表。
- 是否属于原始报表口径问题：是。每个章节的 Other Countries 都是该章节列示国家之外的剩余项。
- 是否影响 USDA 对比页面使用：影响把这组数据当作同一地理范围进行完整供需平衡比较；不妨碍单独展示有清楚来源的 Production、Imports、Exports、Disappearance 或 Ending Stocks。
- 是否建议继续展示：建议继续展示单项指标，但不得标示为可闭合的 USDA 式平衡表。
- 页面是否需要质量警告：需要。建议显示“各章节 Other Countries 覆盖范围不同，指标不可用于闭合平衡”的中高等级提示。
- 是否需要修改解析器或配置：不需要。可在后续页面/质量元数据中增加 scope_mismatch 标识，但不应改数。

### 结论档案 EU：AN24992 European Union

- 原因分类：国家行识别错误；具体发生在 Ending Stocks 章节。
- 是否属于解析错误：是。原表 Ending Stocks 章节 A105 为 Spain，A106 为 EU-27；当前把 A106 单独作为完整 European Union。
- 是否属于原始报表口径问题：否。原表可通过 Spain + EU-27 component 得到完整欧盟库存。
- 是否影响 USDA 对比页面使用：影响 European Union 的 Ending Stocks、由其结转的 Beginning Stocks及平衡结果。Production、Imports、Exports 和 Disappearance 的直接来源值不受这个错误影响。
- 是否建议继续展示：修正发布前，不建议把当前 European Union 库存值当作完整欧盟值展示；其他直接来源流量指标可以保留。
- 页面是否需要质量警告：需要严重等级提示，直到修正版快照可用。
- 是否需要修改解析器或配置：需要，但本轮不修改。应把 EU Ending Stocks 定义为 Spain 行与 EU-27 component 行之和，并以修正后的期末库存派生下一年度 Beginning Stocks，同时保留两个源单元格。

## 重点警告复核：AN33992 / Soybean Meal / Other Countries / 2024/25

- Beginning Stocks = 3,137，原始单元格 D144，来自上一期间 Soybean meal Ending stocks。
- Production = 12,012，原始单元格 C31，原始章节 Meal Output，原始指标 Meal Output。
- Imports = 11,967，原始单元格 C70，原始章节 Soybean meal Imports。
- Exports = 2,072，原始单元格 C91，原始章节 Soybean meal Exports。
- Domestic Consumption = 31,830，原始单元格 C128，原始章节 Soybean meal Disappear.(a)，原始指标 Disappear.。
- Ending Stocks = 3,560，原始单元格 C144，原始章节 Soybean meal Ending stocks。
- 计算：3,137 + 12,012 + 11,967 - 2,072 - 31,830 - 3,560 = -10,346 千吨。
- 原因：五个章节分别列示不同国家，C144、C31、C70、C91、C128 中的 Other Countries 不是同一个国家集合。数值提取、期间列、单位和公式均无误。

## sectioned_global_balance 国家范围一致性

显式顶层国家/地区数量按 Production / Imports / Exports / Disappearance / Ending Stocks 排列：

|report_id|五章节显式国家/地区数|是否完全一致|代表性差异|
|---|---|---|---|
|AN34992|12 / 12 / 8 / 14 / 4|否|Production 列示 Canada、India、Russia；Imports 列示 Bangladesh、Indonesia、Türkiye、Vietnam；Ending Stocks 仅 Germany、Canada、US、Japan。|
|AN33992|26 / 33 / 15 / 31 / 10|否|Argentina、Brazil 在 Production、Exports、Ending Stocks 中显式出现，但不在 Imports 中显式出现；库存章节仅列 10 项。|
|AN34792|10 / 14 / 9 / 10 / 3|否|Production 与 Disappearance 虽均为 10 项，具体集合仍不同；Ending Stocks 仅 Russia、Ukraine、Argentina。|
|AN24992|20 / 18 / 16 / 14 / 8|否|五个章节的数量均不同；库存章节另有 Spain 与 EU-27 的分列结构。|
|AN23992|25 / 31 / 20 / 28 / 14|否|Ending Stocks 增列 Uruguay，同时缺少多个在其他章节显式出现的国家。|
|AN24792|15 / 39 / 18 / 27 / 12|否|Imports 39 项，Production 15 项，Ending Stocks 12 项，差异显著。|

因此，六张 sectioned_global_balance 报表中的 Other Countries 均不能跨章节直接组成一个固定地理范围的标准平衡关系。

## EU 汇总与交叉检查

- AN24992 Exports 章节的成员国明细行 55:62 在 2024/25 合计 579、2025/26 合计 543，分别精确等于 EU 汇总行 63；解析结果使用汇总行，没有把成员国与汇总行重复相加。
- AN24992 Ending Stocks 章节中 Spain（A105）与 EU-27（A106）是并列组成项。章节可见行求和必须同时包含这两行；当前仅取 A106，造成 EU 库存少计 Spain。
- 原表校验：2024/25 正确期初库存为 D105+D106=15+622=637，正确期末库存为 C105+C106=9+588=597，差额为 0。
- 原表校验：2025/26 正确期初库存为 C105+C106=9+588=597，正确期末库存为 B105+B106=20+622=642，差额为 0。
- 影响不限于两条已报警记录：AN24992 / Rapeseed Oil / European Union 的 Ending Stocks 2021/22–2025/26，以及由此结转的 Beginning Stocks 2022/23–2025/26，均遗漏 Spain 分量；其中其他年度只是误差尚未越过当前警告阈值。
- 没有证据表明 EU 汇总与成员国、Other Europe 或其他区域被重复纳入标准化数据。确定的问题是库存章节对 Spain + EU-27 的层级解释不足。

## 分类汇总与使用建议

|汇总项|数量|
|---|---:|
|原始报表口径问题|24|
|聚合范围不一致|24|
|解析错误|2|
|四舍五入作为主要原因|0|
|尚无法解释|0|

影响 USDA 对比的数据组合：

- 24 个 Other Countries 组合：六个品种、2022/23–2025/26。单项指标可比，但不可作为同一地区的闭合供需平衡使用。
- AN24992 / Rapeseed Oil / European Union：Ending Stocks 2021/22–2025/26、Beginning Stocks 2022/23–2025/26，以及依赖这些值的平衡展示。

需要修改解析器或配置的具体项目：

- 仅 AN24992 Ending Stocks 的 European Union 聚合规则：Spain + EU-27 component；下一年度 Beginning Stocks 必须由修正后的完整 EU 期末库存结转；来源应记录两个单元格或单元格范围。

不需要修改解析器、只需页面提示的项目：

- 六张 sectioned_global_balance 报表的全部 24 条 Other Countries 警告。建议添加 scope_mismatch 质量标识和说明，不改变原始值。

## 不可变快照下的修正版发布策略

2026-06 已是不可变快照，不应原地覆盖。若批准修复，建议新建独立不可变修订版，例如 data/release-corrections/2026-06/r1（以及对应公开数据目录），清单记录 revision=1、supersedes_release=2026-06、原始快照与源文件哈希、受影响数据键、修正原因和完整测试结果。修订版只修正 AN24992 European Union 的 Beginning/Ending Stocks 及依赖的矩阵和平衡结果；24 条 Other Countries 数据保持原值并附质量提示。latest.json 是否指向修订版，应在修订版全套校验通过后按明确的发布规则更新，不能改写原 2026-06 快照。
