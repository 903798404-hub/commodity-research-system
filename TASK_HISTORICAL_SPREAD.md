你现在在项目：

项目根目录



请从零开始重构“本地历史价格 → 历史价差数据库 → 季节性价差图”的模块。



重要原则：

不要继续修旧的 build\_seasonal\_spread\_history.py。

不要使用 ak.futures\_zh\_daily\_sina 拉历史数据。

历史数据只来自本地 Excel：

01_data\\manual\_history\\spread\_system\_base.xlsx

01_data\\manual\_history\\historical\_price\_base.xlsx



其中：

spread\_system\_base.xlsx 是价差体系库，用来参考有哪些价差类型。

historical\_price\_base.xlsx 是历史价格库，是主数据源，里面是 Wind 导出的内盘历史价格，只有价格，没有价差。



请只在当前项目目录内操作，不要读取桌面文件，不要使用全局 Python，只能使用：

.\\.venv\\Scripts\\python.exe



请创建或覆盖以下脚本：

04_scripts\\import\_historical\_prices.py

04_scripts\\build\_spread\_config.py

04_scripts\\calculate\_historical\_spreads.py

04_scripts\\plot\_seasonal\_spreads.py

04_scripts\\run\_historical\_spread\_pipeline.py



同时请确保这些目录存在：

data

01_data\\manual\_history

output

06_outputs\\charts

logs



第一部分：import\_historical\_prices.py



目标：

读取 01_data\\manual\_history\\historical\_price\_base.xlsx，把 Wind 历史价格宽表转成标准长表。



输入：

01_data\\manual\_history\\historical\_price\_base.xlsx



输出：

01_data\\historical\_price\_long.xlsx

10_logs\\import\_historical\_prices\_YYYYMMDD\_HHMM.log



输出 Excel 包含两个 sheet：

price\_long

import\_log



price\_long 字段必须包括：

date

instrument

instrument\_cn

delivery\_month

price

source\_column

source\_file

updated\_at

status

error



要求：

自动读取 Excel 所有 sheet，优先选择包含日期列和大量“期货收盘价”字段的 sheet。

如果只有一个 sheet，就直接读取该 sheet。

自动识别日期列，可能叫“日期”“Date”“date”，也可能是第一列。

日期要转成真实日期。

价格列来自 Wind 宽表，列名可能类似：

期货收盘价(1月交割连续):豆粕

期货收盘价(5月交割连续):豆粕

期货收盘价(9月交割连续):豆粕

期货收盘价(1月交割连续):豆油

期货收盘价(5月交割连续):豆油

期货收盘价(9月交割连续):豆油

期货收盘价(1月交割连续):菜籽粕

期货收盘价(5月交割连续):菜籽粕

期货收盘价(9月交割连续):菜籽粕

期货收盘价(1月交割连续):菜籽油

期货收盘价(5月交割连续):菜籽油

期货收盘价(9月交割连续):菜籽油

期货收盘价(1月交割连续):棕榈油

期货收盘价(5月交割连续):棕榈油

期货收盘价(9月交割连续):棕榈油



品种映射：

豆粕 -> M

菜籽粕 -> RM

豆油 -> Y

菜籽油 -> OI

棕榈油 -> P



月份识别：

从“1月交割连续”“5月交割连续”“9月交割连续”中提取 delivery\_month = 1、5、9。



如果列名有轻微差异，尽量用正则识别：

交割连续前面的数字作为月份。

冒号后面的中文作为品种。

如果识别不了，就写入 import\_log，不要让脚本崩溃。



价格处理：

空值保持为空。

价格为 0 的值不要用于后续计算，status 写 suspicious\_zero。

正常价格 status 写 success。

不要前值填充。

不要把空值填 0。



第二部分：build\_spread\_config.py



目标：

生成标准历史价差配置表。

第一版可以不强行解析 spread\_system\_base.xlsx 的复杂格式，但必须保留读取它的动作，并把读取到的 sheet 名写入日志，作为后续扩展依据。



输入：

01_data\\manual\_history\\spread\_system\_base.xlsx



输出：

01_data\\historical\_spread\_config.xlsx

10_logs\\build\_spread\_config\_YYYYMMDD\_HHMM.log



输出 Excel 包含：

spread\_config

system\_workbook\_sheets

build\_log



spread\_config 字段必须包括：

spread\_group

spread\_name

leg1\_instrument

leg1\_month

leg2\_instrument

leg2\_month

formula

season\_start\_month

season\_end\_month

enabled

note



公式第一版只支持：

leg1-leg2



season\_start\_month 默认 10。

season\_end\_month 默认 4。

enabled 默认 TRUE。



必须生成以下价差：

M 1-5

M 5-9

M 9-1

RM 1-5

RM 5-9

RM 9-1

Y 1-5

Y 5-9

Y 9-1

OI 1-5

OI 5-9

OI 9-1

P 1-5

P 5-9

P 9-1

M-RM 1

M-RM 5

M-RM 9

Y-P 1

Y-P 5

Y-P 9

OI-Y 1

OI-Y 5

OI-Y 9

OI-P 1

OI-P 5

OI-P 9



价差腿规则：

M 1-5 = M 1月 - M 5月

M 5-9 = M 5月 - M 9月

M 9-1 = M 9月 - M 1月



RM、Y、OI、P 的月间价差同理。



跨品种价差：

M-RM 1 = M 1月 - RM 1月

M-RM 5 = M 5月 - RM 5月

M-RM 9 = M 9月 - RM 9月



Y-P 1 = Y 1月 - P 1月

Y-P 5 = Y 5月 - P 5月

Y-P 9 = Y 9月 - P 9月



OI-Y 1 = OI 1月 - Y 1月

OI-Y 5 = OI 5月 - Y 5月

OI-Y 9 = OI 9月 - Y 9月



OI-P 1 = OI 1月 - P 1月

OI-P 5 = OI 5月 - P 5月

OI-P 9 = OI 9月 - P 9月



第三部分：calculate\_historical\_spreads.py



目标：

读取历史价格长表和价差配置表，计算所有历史价差，生成历史价差数据库。



输入：

01_data\\historical\_price\_long.xlsx

01_data\\historical\_spread\_config.xlsx



输出：

01_data\\historical\_spread\_database.xlsx

10_logs\\calculate\_historical\_spreads\_YYYYMMDD\_HHMM.log



输出 Excel 包含：

spread\_long

failures

config\_used

summary



spread\_long 字段必须包括：

date

spread\_group

spread\_name

leg1\_instrument

leg1\_month

leg1\_price

leg2\_instrument

leg2\_month

leg2\_price

spread\_value

season

calendar\_offset

month\_day

status

error

updated\_at



计算逻辑：

用 date + instrument + delivery\_month 匹配两条腿价格。

spread\_value = leg1\_price - leg2\_price。

如果任意一条腿价格缺失，status 写 missing\_price，spread\_value 为空，不要填 0。

如果任意一条腿价格为 0，status 写 suspicious\_zero，spread\_value 为空。

如果成功，status 写 success。



season 规则：

只保留每年 10 月到次年 4 月的数据，用于季节性图。

10 月、11 月、12 月属于当年 season 的前半段。

次年 1 月、2 月、3 月、4 月属于上一年 season 的后半段。

例如：

2025-10-01 到 2026-04-30 属于 2025/2026。

2024-10-01 到 2025-04-30 属于 2024/2025。



calendar\_offset：

等于 date - season 起始日 10 月 1 日的天数。

month\_day：

格式 MM-DD。



任何单条价差失败不能导致整体脚本崩溃，要写入 failures。

summary 里要输出：

price\_long 行数

配置价差数量

spread\_long 行数

success 行数

missing\_price 行数

suspicious\_zero 行数

失败行数

每个 spread\_name 的成功记录数



第四部分：plot\_seasonal\_spreads.py



目标：

读取历史价差数据库，画季节性价差图。



输入：

01_data\\historical\_spread\_database.xlsx



默认画：

RM 5-9



输出：

06_outputs\\charts\\RM\_5-9\_YYYYMMDD\_HHMM.png

06_outputs\\charts\\RM\_5-9\_chart\_data\_YYYYMMDD\_HHMM.xlsx

10_logs\\plot\_seasonal\_spreads\_YYYYMMDD\_HHMM.log



要求：

只使用 status = success 的记录画图。

筛选 spread\_name = RM 5-9。

横轴使用 calendar\_offset。

横轴标签显示 month\_day，每隔大约 14 天显示一个。

每个 season 一条线。

最新 season 加粗，线宽比其他年份更大。

不要对未来日期补 0。

不要前值填充。

标题使用 RM 5-9。

图中显示图例。

保存 PNG，dpi 至少 150。

同时输出 chart\_data Excel，方便我检查图表底层数据。

chart\_data Excel 至少包含：

chart\_long

chart\_wide

plot\_log



如果 RM 5-9 没有 success 数据，仍然生成一张“无可用数据”的提示图，并写入日志。



中文字体：

如果系统里有 Microsoft YaHei，就使用 Microsoft YaHei。

如果没有，不要报错，使用默认字体即可。

确保负号能正常显示。



第五部分：run\_historical\_spread\_pipeline.py



目标：

一键运行完整流程。



运行顺序：

import\_historical\_prices.py

build\_spread\_config.py

calculate\_historical\_spreads.py

plot\_seasonal\_spreads.py



要求：

使用 subprocess 调用同一个虚拟环境 Python。

任何一步失败，要停止后续步骤并打印失败步骤。

全部成功后，在终端打印：

historical\_price\_long.xlsx 路径

historical\_spread\_config.xlsx 路径

historical\_spread\_database.xlsx 路径

RM 5-9 PNG 路径

RM 5-9 chart\_data Excel 路径

各步骤日志路径



请实际运行：

.\\.venv\\Scripts\\python.exe 04_scripts\\run\_historical\_spread\_pipeline.py



运行前请先检查依赖：

pandas

openpyxl

matplotlib



如果缺少依赖，请提示我用项目虚拟环境安装，不要使用全局 Python。

不要自动安装，先告诉我缺什么。



运行结束后请汇报：

新增了哪些脚本

读取到了哪些原始 Excel

识别到了哪些品种和月份

price\_long 生成多少行

spread\_config 生成多少条价差

spread\_long 生成多少行

RM 5-9 是否画图成功

生成文件路径

失败原因



