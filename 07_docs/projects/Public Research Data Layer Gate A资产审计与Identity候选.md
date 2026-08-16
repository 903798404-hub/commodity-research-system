# Public Research Data Layer Gate A 资产审计与 Identity 候选

## 结论先行

HUMAN GATE A 所需的全量资产审计、Candidate Catalog、资产分类和 Identity 候选已完成。当前状态是 `HUMAN_GATE_A_PENDING`，未开始 Tankan Provider、Lutou Snapshot Provider 或新 Canonical Contract 实现。

本轮建议冻结的核心决策是：

1. `origin_system` 与 `acquisition_channel` 永久分离。Lutou 当前是 `origin_system=lutou` + `acquisition_channel=manual_snapshot`；未来直连时只更换 acquisition channel。
2. 公共层采用 `dataset_id` + `series_id` + optional `provider_series_id`。`series_id` 是 provider-neutral 业务概念；`provider_series_id` 是上游表/列/端点/映射键。
3. 已有 Tankan `source_series_id` 不升格为 canonical `series_id`。它的真实语义是 provider-native 映射身份，例如 `.zh` / `.en` 是同一 canonical 市场概念的不同来源系列。
4. 全量编目不等于全量 MarketQuote 化。价格、FX、Weather、Fundamental、Basis/Freight/CNF 继续保持真实语义边界。
5. 当前只冻结 Identity 模型和 Catalog 规则，不把未核实的单位、币种、频率、地理或产品语义猜成 canonical series。

## 审计边界与安全证据

| 项目 | 结果 |
| --- | --- |
| 隔离分支 | `feat/public-research-data-sources` |
| 隔离 worktree | `market-data-worktrees/public-research-data-sources` |
| 起点 HEAD | `a944199a6083a37ca1c7816b1a5584f9337a6898` |
| 起点 Tree | `4120f0c60f4faa2690258d46120318ce18ed4815` |
| runtime classification | `isolated-dev` |
| runtime ID | `isolated-dev-public-research-data-sources-20260816T070304Z-f210e55df1df` |
| Tankan 读取 | direct database, PostgreSQL, server/transaction read-only |
| Tankan 查询安全 | 226 条；仅 `SELECT` / `SHOW` / `EXPLAIN (FORMAT JSON)`；0 写语句；未使用 `EXPLAIN ANALYZE` |
| Lutou 读取 | offline/manual snapshots only |
| 正式源文件 | 只读，未修改、未覆盖、未重新生成 |
| 生产/服务器/部署 | 未连接、未构建、未部署 |

Tankan 首次 metadata 模板因 Psycopg 对字面量 `pg_%` 的 placeholder guard 被本地拒绝，未发送写操作。修正为 `pg_%%` 后完整重跑。该事件不改变数据库只读结论。

## Candidate Catalog 概览

机器可读 Catalog：[`02_configs/public_research_data_catalog.candidate.json`](../../02_configs/public_research_data_catalog.candidate.json)

| 指标 | 数量 |
| --- | ---: |
| dataset 候选 | 652 |
| Tankan dataset | 48 |
| Lutou dataset | 604 |
| Lutou weather table | 565 |
| Lutou oils/market table | 39 |
| provider-series/measurement-column 候选 | 6,432 |
| Catalog 语义上仍需 Unclassified 处理的 dataset | 8 |

Catalog 是可审计的轻量 JSON manifest，不读取业务数据，不做选源、promotion、价差计算或汇率换算。它保留每个 source object 的字段、schema fingerprint、数据量、日期证据、分类、质量问题、provider identity 候选和 canonicalization status。

6,432 是 provider-side series/measurement-column 候选数，不是已冻结 canonical series 数。当前所有未完成单位、维度和日期语义映射的 `series_id_candidate` 均显式为 `null`，而不是猜测值。

其中 6,432 个 ID 是由 `provider dataset + measurement column` 生成的 synthetic discovery keys，用于防止宽表列在编目中丢失。它们与 international-spread 历史开发中的 14 个真实 Tankan `source_series_id` 映射是两类证据。后者已单独写入 Catalog `existing_provider_identity_evidence`，不会被 synthetic column key 替代。

## Tankan 全库资产总览

### 库级盘点

| 项目 | 结果 |
| --- | ---: |
| schema | 10 |
| table | 48 |
| view / materialized view / foreign table / partitioned table | 0 |
| column | 431 |
| constraint | 257 |
| index | 122 |
| 已知库容量 | 740,139,008 bytes |
| identity candidate | 48 |
| high-value asset | 40 |

原始名称/字段启发式分类为：21 `UNCLASSIFIED`、9 international spot candidates、2 biodiesel candidates、3 FX candidates、1 international spread candidate、12 market/time-series candidates。这是 source discovery 分类，不是最终 canonical contract 分类。Catalog 在可以由 schema/table 业务名称证明时进一步标注 trade、crop、livestock、balance 等 domain，仍保留 8 个无法安全确定 domain 或 contract 的资产。

该“8个”是 `domain=unclassified` 与 `canonical_contract_candidate=unclassified` 的并集，不是对原始 21 个 `UNCLASSIFIED` 的覆盖或删除。原始 source discovery 分类全部保留在 `asset_class`。

### 高价值市场、外盘与价差资产

| Dataset | 规模/范围 | 判断 |
| --- | --- | --- |
| `market.foreign_futures_price_raw` | 约 711,387 行；1969-06-26–2026-08-13 | CBOT/BMD/ICE/EURONEXT，含豆类、玉米、棕榈油、菜类；1969 起始值需进一步验证 |
| `market.foreign_futures_price` | 约 310,728 行；2000-01-03–2026-08-13 | 标准化形态的外盘历史价格候选 |
| `market.foreign_futures_live` | 10 行 | 外盘实时合约价格；与历史数据的时间语义需分开 |
| `market.foreign_position` | 约 126,150 行 | 外盘持仓，属 Fundamental/position，不是 MarketQuote |
| `market.futures_spread` | 约 102,507 行 | 现有价差资产；需审查是原始观测还是派生结果 |
| `market.basis_fob` | 约 79,656 行 | Soybean/Meal/Oil FOB basis；应保留 Basis 语义 |
| `market.soybean_param` | 约 24,613 行 | CNF、CBOT/DCE 合约、出油/出粕率、税费；import-profit ACL 高价值候选 |
| `market.palm_oil_param` | 约 19,884 行 | CNF、合约、税费 |
| `market.rapeseed_param` | 约 2,663 行 | CNF、内盘合约、出油/出粕率、税费 |
| `market.rapeseed_oil_param` | 约 55 行 | CNF、内盘合约、税费 |
| `market.india_import_profit` | 约 8,645 行 | CNF、FX、税费、内价和利润混合表；不得整表 MarketQuote 化 |

Tankan 的国际现货与进口成本资产主要是 FOB basis、CNF 参数和进口成本/利润参数；外盘期货表不应因自动分类名为 `INTERNATIONAL_SPOT_CANDIDATE` 就被当作 physical spot。

### FX

- `market.exchange_rate`：约 11,474 行，1981-01-02–2026-08-12，包含 spot 及 1M–12M tenor。
- `market.exchange_rate_live`：13 个 tenor，包含 bid/ask/mid。
- FX 最新历史日比外盘期货早一天，不能默认同日完整。
- `pig.pig_import` 被名称启发式归入 FX candidate，但业务上更可能是生猪进口观测，已作为误分类/需复核证据保留。

### Biodiesel / 能源

- `trade.usda_trade.product` 实值命中 `BIODIESEL >B30`、`BIODIESEL B100`、`BIODIESEL B30-99`、`BIODIESEL B>30`。
- `trade.canada_trade.category` 存在生物柴油类别；该表只有 year/month，不能伪造日粒度 business date。
- Tankan 没有由当前证据证明的 FAME/HVO/UCO/POME/PME/PFAD/SAF 专门 dataset。

### 其他意外高价值资产

- `balance.usda_psd`：约 120,282 行，含产量、单产、压榨、消费、进出口和库存。
- `public.mpob_balance`：棕榈油供需平表资产。
- `public.ar_crop_progress`、`br_crop_area`、`br_crop_progress`、`br_crop_stage`、`ca_crop_progress`、`us_crop_acreage`、`us_crop_progress`：阿根廷、巴西、加拿大、美国作物进度/面积资产。
- `pig.*`：生猪价格、产量、库存、冻品、屠宰、体重、比价、利润成本等独立 livestock 研究资产。
- `trade.china_trade`、`trade.usda_trade`、`trade.canada_trade`：贸易 typed observation 资产，不应归为价格。

## Lutou Offline Snapshot 资产总览

Lutou 数据库当前不可连接不是 blocker。所有下述资产均保持 `origin_system=lutou`，SQL/Excel 只表示 `acquisition_channel=manual_snapshot`。

### Weather

| 项目 | 结果 |
| --- | ---: |
| CREATE TABLE | 565 |
| INSERT rows | 480,062 |
| 有 DDL 且有数据的表 | 565 |
| 空表 | 0 |
| 表内重复日期 | 0 |
| 全体日期证据 | 1940-01-01–2026-08-27 |
| precipitation | 215 tables |
| soil moisture | 102 tables |
| temperature generic / max / min | 182 / 33 / 33 tables |
| observed / ECMWF / GFS | 135 / 215 / 215 tables |
| `_raw` / explicit forecast | 455 / 66 tables |

天气快照包含 19 个 geography prefix，既有国家也有 EU、Malay 等不同地理层级。典型观测降水/最高/最低温度约为 1994-01-01–2026-08-12，观测土壤水分约为 2018 年至 2026-08-09，forecast 覆盖 2026-08-13–2026-08-27。

`*_raw` soil 表中 1940 年的 date axis 与 2020–2026 年宽列组合，是季节基准轴，不是 1940 年实际观测。所有表缺少 DDL comment/单位，土壤水分单位未知，因此不冻结 canonical weather series。

当前 mature weather importer 仅消费 10 个美国表，现有 refresh 产出 12 个 weather 文件。公共层后续应在现有输入边界前建 ACL，不改写成熟 weather domain、processed parquet、历史基准或季节权重。

### 油脂油料价格快照

| 项目 | 结果 |
| --- | ---: |
| SQL table | 39 |
| INSERT records | 299,655 |
| SQL bytes | 125,615,726 |
| Excel visible sheets | 16 |
| 可解析业务日期 | 299,649 |
| 原始业务日期范围 | 1969-06-26–2027-06-15 |

主要资产包括：

- `basis_price` 67,051 行，是现有 basis/import-profit 消费边界，不改写现有逻辑。
- `oil_world_prices` 2,380 行，包含国际油脂油料 physical/spot 价格维度。
- 美国大豆 6,635、加拿大菜系 8,191、澳大利亚菜籽 3,381、印度豆油 10,542、印度棕榈油 6,056、印度尼西亚棕榈油 5,652、欧洲菜系 8,585、欧洲棕榈油 8,147、欧洲葵系 7,113、马来西亚棕榈油 5,860 行。
- 这些宽表存在 FOB/CIF/freight/basis/币种/单位等不同列语义；必须按列证据拆分，不得整表统一成 MarketQuote。
- `ca_ice_canola`、`eu_euronext_rapeseed`、`my_bmd_palm`、`us_cbot_corn/soybean/soymeal/soyoil` 是具体月份合约宽表，单表约 6,500–7,100 行。
- `外盘期货价格` 6,891 行、`外盘期货` 9,483 行、`外盘持仓` 4,317 行。持仓是 position/fundamental，不是 price。
- `汇率` 9,451 行、`美元兑人民币历史汇率` 4,151 行、`美元兑人民币实时汇率` 13 行，应进入独立 FX contract。
- `内盘期货价格_实时` 41 行、`内盘期货价格_收盘` 6,289 行，是意外发现的国内市场资产。

39/39 表已完成业务日期 profiling。6 个无法解析值全部来自 `内盘期货价格_实时` 的 `日期=0`/无效实时记录。`1969-06-26` 和 `1970-01-01` 行仍有实际价格值，本轮不将其当作 sentinel 删除或隐藏。`2027-06-15` 来自 `美元兑人民币实时汇率.ValueDate` 的一年期远期到期日，是合理前瞻期限候选，不能直接判为异常。

保守的存储频率候选为：35 表 `daily-like_storage`、1 表 `weekly-like_storage`（阿根廷豆油）、1 表 `monthly-like_storage`（USD/CNY 实时远期曲线）、1 表 `singleton`（内盘实时快照）、1 表不规则约周频（俄罗斯葵系）。这些标签仅描述快照存储日期间隔，不代表每个价格列的经济发布频率已被证明。

`01_data/manual/榨利表/大豆-进口榨利.xlsx` 仅作为现有消费关系和历史口径证据。工作簿含多个 formula cache `#N/A/#REF` 问题，且混合计算结果，因此未将 workbook 本身或其派生列建模为独立 Lutou raw dataset。

### Lutou Biodiesel / 能源专项

`生物柴油_价格` 有 11,089 行、51 列，是当前两个来源中最直接的能源/生物柴油市场资产。字段证据覆盖：

- ICE crude（USD/bbl）、ICE gasoil（USD/t）。
- 美国/欧洲 low-sulfur diesel。
- 美国多地区 SME（USD/gal 或 cents/gal）。
- TME、RME ARA 及月份价格（USD/t）。
- RIN D4/D6、LCFS。

PFAD 可在其他油脂/原料表中寻找到证据，但当前快照没有证明独立 FAME、HVO、UCO、POME、PME 或 SAF dataset。它们必须保持 Not found/Unclassified，不能由名称联想生成资产。

## Identity 候选定义

### Dataset identity

`dataset_id` 表示稳定的公共数据集家族，不包含快照路径、批次、文件 hash 或 acquisition channel。当前 Catalog 中的 `dataset_id_candidate` 是 Gate A 可审核键，例如：

```text
tankan.market.foreign_futures_price
tankan.market.exchange_rate
tankan.trade.usda_trade
lutou.oils.oil-world-prices
lutou.weather.native-<stable-native-name-hash>
```

对中文 native table 使用基于原生名称的稳定 hash，避免不可逆的随意拼音。完整原生名称始终保留在 `source_native_name` 和 `provider_dataset_id_candidate`。未来 Lutou 改为 direct database 时，dataset ID 不变，只更新 acquisition/provenance。

### Series identity

```text
series_id
  = provider-neutral canonical research concept
  = 业务维度 + 度量 + 单位/日期语义确认后才分配

provider_series_id
  = optional provider-native mapping identity
  = provider table/column/endpoint/source mapping key
```

候选模板：

```text
market.quote.{instrument}.{price_type}.{session}
fx.{base_currency}.{quote_currency}.{tenor}.{side_or_fixing}
weather.{metric}.{geography}.{model_or_observed}.{forecast_role}
fundamental.{subject}.{measure}.{geography}.{frequency}
basis.{commodity}.{origin}.{destination_or_market}.{contract_tenor}
```

不把 `origin_system` 强制放入 canonical `series_id`，因为多来源可以观测同一概念；来源差异由 provenance 和 provider identity 保留。如果业务定义本身就是“某 provider 发布的指标”，provider 才是该业务概念的显式维度。

### `source_series_id` 复核

`tankan.ffpr.cbot.soybean.zh` 与 `tankan.ffpr.cbot.soybean.en` 等已有 ID 同时出现，Source Policy 使用它们表示 legacy/auto 来源并执行 cutover/选源。它们不是两个 canonical CBOT soybean 概念。

因此建议：

- 公共合同新名为 `provider_series_id`。
- Tankan adapter 可在迁移期将现有 `source_series_id` 无损映射为 `provider_series_id`，不要立即破坏现有 international-spread 证据或消费模型。
- canonical business key 不包含 provider series；provider candidate/raw key 包含 provider series。
- `source_row_sha256`、snapshot SHA、candidate ID、capture timestamp 属 provenance/immutability，不是 dataset/series identity。

上述 canonical business key 是未来公共层目标。当前 international-spread 开发成果中的 `MarketQuote` 兼容 key 仍包含 `source_series_id`；Gate B 只能通迁移/ACL 分层处理，不得宣称该兼容 key 已被替换。

### Provenance

最小 provenance 建议包含：

```text
origin_system
acquisition_channel
source_locator
provider_dataset_id
optional provider_series_id
schema_version
mapping_version
captured_at
optional snapshot_sha256 / source_row_sha256
```

`source_locator` 是可更换的物理定位，不得参与 canonical ID。

## Canonical Contract 边界

| Contract | 进入条件 | 明确排除 |
| --- | --- | --- |
| `MarketQuote` | 可验证的 instrument、business date、price type、session、currency、unit 和 scalar price | FX curve、position、trade、balance、weather、混合利润表 |
| FX | base/quote currency、tenor、bid/ask/mid/fixing、value date 语义完整 | 不借用 futures MarketQuote |
| Weather | 延续 mature weather contract，公共层通过 acquisition ACL 提供 | 不重写 weather domain，不把预报和观测混为一条 series |
| Fundamental | balance/trade/crop/livestock/position 按真实消费者增加 typed observation | Gate A 不一次建空泛型体系 |
| Basis/Freight/CNF | 保留 origin/destination/tenor/quote basis 等业务语义 | 不因数值字段就全部 MarketQuote 化 |

`import_profit` 继续保留 `CbotContract`、`DceContract`、`FxCurvePoint`、`FxCurve`、CNF、`MarketSnapshot` 和 calculator。公共数据只能通过 ACL 进入，不直接替换成熟 bounded context。

## 尚未标准化的资产

1. Lutou weather 的单位、部分 geography level、model/member 和 seasonal baseline 语义。
2. Lutou 国际油脂/能源宽表的每列币种、单位、市场、价格类型和 tenor。
3. Tankan 长表的 canonical series 维度组合、单位和频率。
4. Biodiesel 的产品/等级/地区/单位标准化，以及 RIN/LCFS 与成品油价格的不同 contract 语义。
5. Freight、CNF、Basis 在宽表中的拆分与 canonical boundary。
6. Tankan operational/config 对象：`domestic_contract_rule`、`ric_mapping`、`scheduler_job`、`sync_log`、`sync_queue`、`feedback` 等不应自动进入研究 contract。
7. 当前未证明的 FAME/HVO/UCO/POME/PME/SAF dataset。

## Gate A 待批准项

请批准或指定修改以下冻结项：

1. 采用 `dataset_id` + canonical `series_id` + optional `provider_series_id` 的三层身份结构。
2. 将现有 Tankan `source_series_id` 定位为 provider identity，通迁移兼容映射进入 `provider_series_id`。
3. 冻结 `origin_system` / `acquisition_channel` / `source_locator` / provenance 的分离规则。
4. 准许 Candidate Catalog `0.1-candidate` 作为 Gate B 实现输入，后续仅在有真实消费者时进行 canonicalization。
5. 同意 MarketQuote / FX / Weather / Fundamental / Basis-Freight-CNF 的上述边界。

在 Gate A 获得批准前，不实现 Tankan Provider、Lutou Provider、Psycopg 依赖或新 canonical contracts。
