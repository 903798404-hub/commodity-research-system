import type { MetricData } from "../model";
import type { PresentationRegionConfig, PresentationSlideConfig } from "./config";

export interface PresentationPeriodBasisConfig {
  product: string;
  region: string;
  period_family: string;
  source_role: string;
  start_month: string | null;
  end_month: string | null;
  display_label: string;
  source_report_id: readonly string[];
  source_note: string;
  footer_detail?: string;
  prefer_formal_basis?: boolean;
}

const GLOBAL_BALANCE_DETAIL = "Global：世界供需表按 Sep–Aug（压榨行及 Aug 31 库存）呈现，进出口表按 Oct–Sep。";
const G3_BALANCE_DETAIL = "G3：混合口径｜US Sep–Aug · BR Jan–Dec · AR Apr–Mar；贸易/压榨汇总表另按 Oct–Sep。";
const PRODUCTION_DETAIL = "生产条件：AN13993 只注明主要收获期，不提供作物年度起止月。";
const G3_PRODUCTION_DETAIL = "G3生产条件：混合口径；成员起止月原表未注明（收获期：US Sep–Nov · BR Jan–Mar · AR Apr–May）。";

export const PRESENTATION_PERIOD_BASIS: readonly PresentationPeriodBasisConfig[] = [
  {
    product: "Soybeans",
    region: "Global",
    period_family: "mixed",
    source_role: "mixed",
    start_month: null,
    end_month: null,
    display_label: "混合口径｜供需 Sep–Aug · 贸易 Oct–Sep",
    source_report_id: ["AN139900", "AN13992"],
    source_note: "AN139900《SOYBEANS: World Supply and Demand》注明 Crush (Sept/Aug) 和 Aug 31 库存；AN13992《World Crushings, Exports and Imports》表头为 Oct/Sept。",
    footer_detail: GLOBAL_BALANCE_DETAIL,
  },
  {
    product: "Soybeans",
    region: "United States",
    period_family: "marketing_year",
    source_role: "balance",
    start_month: "Sep",
    end_month: "Aug",
    display_label: "Sep–Aug｜Oil World作物年度",
    source_report_id: ["AN40502B"],
    source_note: "《U.S.A.: Soybean Balance》各年度表头明确为 Sept/Aug。",
    prefer_formal_basis: true,
  },
  {
    product: "Soybeans",
    region: "Brazil",
    period_family: "calendar_year",
    source_role: "balance",
    start_month: "Jan",
    end_month: "Dec",
    display_label: "Jan–Dec｜自然年",
    source_report_id: ["AN51000A"],
    source_note: "《BRAZIL: Soybean Balance》全年栏表头明确为 Jan/Dec 及自然年年份。",
    prefer_formal_basis: true,
  },
  {
    product: "Soybeans",
    region: "Argentina",
    period_family: "marketing_year",
    source_role: "balance",
    start_month: "Apr",
    end_month: "Mar",
    display_label: "Apr–Mar｜Oil World作物年度",
    source_report_id: ["AN50001"],
    source_note: "《ARGENTINA: Soybean Balance》完整年度栏表头明确为 Apr/Mar。",
    prefer_formal_basis: true,
  },
  {
    product: "Soybeans",
    region: "China",
    period_family: "marketing_year",
    source_role: "balance",
    start_month: "Sep",
    end_month: "Aug",
    display_label: "Sep–Aug｜Oil World作物年度",
    source_report_id: ["AN62801"],
    source_note: "《CHINA, PR: Soybean Balance》各年度表头明确为 Sept/Aug。",
    prefer_formal_basis: true,
  },
  {
    product: "Soybeans",
    region: "G3",
    period_family: "mixed",
    source_role: "mixed",
    start_month: null,
    end_month: null,
    display_label: "混合口径｜US Sep–Aug · BR Jan–Dec · AR Apr–Mar",
    source_report_id: ["AN40502B", "AN51000A", "AN50001", "AN13992", "AN13993"],
    source_note: "G3由美国、巴西、阿根廷组成，成员国家平衡表分别为 Sept/Aug、Jan/Dec、Apr/Mar；汇总贸易表为 Oct/Sept。",
    footer_detail: G3_BALANCE_DETAIL,
  },
  ...["Global", "United States", "Brazil", "Argentina", "China"].map((region) => ({
    product: "Soybeans",
    region,
    period_family: "crop_year",
    source_role: "production_table",
    start_month: null,
    end_month: null,
    display_label: "起止月原表未注明｜Oil World作物年度",
    source_report_id: region === "Global" ? ["AN139900", "AN13993"] : ["AN13993"],
    source_note: "《SOYBEANS: World Production, Yields and Harvested Area》仅列主要收获月份；脚注只解释跨年年份归属，未提供作物年度起止月。",
    footer_detail: PRODUCTION_DETAIL,
  } satisfies PresentationPeriodBasisConfig)),
  {
    product: "Soybeans",
    region: "G3",
    period_family: "crop_year",
    source_role: "production_table",
    start_month: null,
    end_month: null,
    display_label: "混合口径｜成员起止月原表未注明",
    source_report_id: ["AN13993"],
    source_note: "G3生产指标由AN13993成员行汇总；原表仅列US Sep–Nov、BR Jan–Mar、AR Apr–May收获期，不提供年度起止月。",
    footer_detail: G3_PRODUCTION_DETAIL,
  },
  ...[
    {
      product: "Soybean Oil",
      report: "AN23992",
      title: "SOYBEAN OIL: World Supply and Demand Balance",
      footer: "豆油：AN23992 的产量、进出口、消费和库存各区块均按 Oct–Sep 表头列示。",
    },
    {
      product: "Soybean Meal",
      report: "AN33992",
      title: "SOYBEAN MEAL: World Supply and Demand Balance",
      footer: "豆粕：AN33992 的产量、进出口、消费和库存各区块均按 Oct–Sep 表头列示。",
    },
  ].flatMap(({ product, report, title, footer }) =>
    ["Global", "United States", "Brazil", "Argentina", "China", "G3"].map((region) => ({
      product,
      region,
      period_family: "marketing_year",
      source_role: "balance",
      start_month: "Oct",
      end_month: "Sep",
      display_label: "Oct–Sep｜Oil World作物年度",
      source_report_id: [report],
      source_note: `《${title}》完整年度栏明确为 Oct/Sept；${region === "G3" ? "G3成员行也位于同一表头下。" : "正式发布字段与原表一致。"}`,
      footer_detail: footer,
      prefer_formal_basis: true,
    } satisfies PresentationPeriodBasisConfig)),
  ),
  ...[
    {
      region: "Global",
      start: null,
      end: null,
      label: "混合口径｜作物年度供需 · 贸易Oct–Sep",
      family: "mixed",
      role: "mixed",
      reports: ["AN149900", "AN14992"],
      note: "AN149900世界供需表以作物年度列示且Crush行注明July/June；AN14992贸易表明确为Oct/Sept，原表没有全表统一起止月。",
    },
    {
      region: "Canada", start: "Aug", end: "Jul", label: "Aug–Jul｜Oil World作物年度",
      family: "marketing_year", role: "balance", reports: ["AN40001"], note: "《CANADA : Rapeseed/Canola Balance》表头明确为Aug July。",
    },
    {
      region: "European Union", start: "Jul", end: "Jun", label: "Jul–Jun｜Oil World作物年度",
      family: "marketing_year", role: "balance", reports: ["AN049104"], note: "《EU-27 : Summary of Rapeseed Supply & Demand》表头明确为July June。",
    },
    {
      region: "Australia", start: "Oct", end: "Sep", label: "Oct–Sep｜Oil World作物年度",
      family: "marketing_year", role: "balance", reports: ["AN80501"], note: "《AUSTRALIA : Rapeseed Balance》表头明确为Oct Sept。",
    },
    {
      region: "China", start: "Jun", end: "May", label: "Jun–May｜Oil World作物年度",
      family: "marketing_year", role: "balance", reports: ["AN62802"], note: "《CHINA,PR : Rapeseed Balance》表头明确为June May。",
    },
    {
      region: "Russia", start: "Jul", end: "Jun", label: "Jul–Jun｜Oil World作物年度",
      family: "marketing_year", role: "balance", reports: ["AN17503"], note: "《RUSSIA : Rapeseed Balance》表头明确为July June。",
    },
    {
      region: "Ukraine", start: "Jul", end: "Jun", label: "Jul–Jun｜Oil World作物年度",
      family: "marketing_year", role: "balance", reports: ["AN18103"], note: "《UKRAINE : Rapeseed Balance》表头明确为July June。",
    },
  ].map(({ region, start, end, label, family, role, reports, note }) => ({
    product: "Rapeseed / Canola",
    region,
    period_family: family,
    source_role: role,
    start_month: start,
    end_month: end,
    display_label: label,
    source_report_id: reports,
    source_note: note,
    footer_detail: region === "Global"
      ? "菜籽Global：世界供需表为作物年度结构，贸易表按Oct–Sep，原表没有统一月份。"
      : undefined,
    prefer_formal_basis: region !== "Global",
  } satisfies PresentationPeriodBasisConfig)),
  ...["Global", "Canada", "European Union", "Australia", "China", "Russia", "Ukraine"].map((region) => ({
    product: "Rapeseed / Canola",
    region,
    period_family: "crop_year",
    source_role: "production_table",
    start_month: null,
    end_month: null,
    display_label: "起止月原表未注明｜Oil World作物年度",
    source_report_id: ["AN14993"],
    source_note: "《RAPESEED / CANOLA : World Production, Yields and Harvested Area》仅列HARVEST主要收获月份，不提供作物年度起止月。",
    footer_detail: "菜籽生产：AN14993只列主要收获月份，不能作为作物年度起止月份。",
  } satisfies PresentationPeriodBasisConfig)),
  ...[
    { product: "Rapeseed Oil", report: "AN24992", title: "RAPESEED OIL : World Supply and Demand Balance" },
    { product: "Rapeseed Meal", report: "AN34992", title: "RAPESEED MEAL : World Supply and Demand Balance" },
  ].flatMap(({ product, report, title }) =>
    ["Global", "Canada", "European Union", "Australia", "China", "Russia", "Ukraine"].map((region) => ({
      product,
      region,
      period_family: "marketing_year",
      source_role: "balance",
      start_month: "Oct",
      end_month: "Sep",
      display_label: "Oct–Sep｜Oil World作物年度",
      source_report_id: [report],
      source_note: `《${title}》各完整年度区块表头均明确为Oct Sept。`,
      footer_detail: `${product === "Rapeseed Oil" ? "菜油" : "菜粕"}：${report}完整年度供需区块按Oct–Sep列示。`,
      prefer_formal_basis: true,
    } satisfies PresentationPeriodBasisConfig)),
  ),
  ...[
    {
      region: "Global", start: null, end: null,
      label: "混合口径｜供需作物年度 · 压榨Sep–Aug · 贸易Oct–Sep",
      family: "mixed", role: "mixed", reports: ["AN147900", "AN14792"],
      note: "AN147900世界供需表以作物年度列示，Crush行明确Sept/Aug，成员库存日期不一致；AN14792贸易表完整年度明确Oct/Sept。",
    },
    {
      region: "Russia", start: "Sep", end: "Aug", label: "Sep–Aug｜Oil World作物年度",
      family: "marketing_year", role: "balance", reports: ["AN17501"], note: "《RUSSIA : Sunflowerseed Balance》表头明确为Sept Aug。",
    },
    {
      region: "Ukraine", start: "Sep", end: "Aug", label: "Sep–Aug｜Oil World作物年度",
      family: "marketing_year", role: "balance", reports: ["AN18101"], note: "《UKRAINE : Sunflowerseed Balance》表头明确为Sept Aug。",
    },
    {
      region: "European Union", start: "Aug", end: "Jul", label: "Aug–Jul｜Oil World作物年度",
      family: "marketing_year", role: "balance", reports: ["AN049106"], note: "《EU-27 : Summary of Sunflowerseed Supply & Demand》正式序列表头明确为Aug July。",
    },
    {
      region: "Argentina", start: "Jan", end: "Dec", label: "Jan–Dec｜自然年",
      family: "calendar_year", role: "balance", reports: ["AN50002"], note: "《ARGENTINA : Sunflowerseed Balance》表头明确为Jan Dec自然年。",
    },
  ].map(({ region, start, end, label, family, role, reports, note }) => ({
    product: "Sunflowerseed",
    region,
    period_family: family,
    source_role: role,
    start_month: start,
    end_month: end,
    display_label: label,
    source_report_id: reports,
    source_note: note,
    footer_detail: region === "Global"
      ? "葵花籽Global：世界供需按作物年度、压榨按Sep–Aug、贸易按Oct–Sep，原表没有统一月份。"
      : undefined,
    prefer_formal_basis: region !== "Global",
  } satisfies PresentationPeriodBasisConfig)),
  ...["Global", "Russia", "Ukraine", "European Union", "Argentina"].map((region) => ({
    product: "Sunflowerseed",
    region,
    period_family: "crop_year",
    source_role: "production_table",
    start_month: null,
    end_month: null,
    display_label: "起止月原表未注明｜Oil World作物年度",
    source_report_id: ["AN14793"],
    source_note: "《SUNFLOWERSEED : World Production, Yields and Harvested Area》仅列HARVEST主要收获月份，不提供作物年度起止月。",
    footer_detail: "葵花籽生产：AN14793只列主要收获月份，不能作为作物年度起止月份。",
  } satisfies PresentationPeriodBasisConfig)),
  ...[
    { product: "Sunflower Oil", report: "AN24792", title: "SUNFLOWER OIL : World Supply and Demand Balance" },
    { product: "Sunflower Meal", report: "AN34792", title: "SUNFLOWER MEAL : World Supply and Demand Balance" },
  ].flatMap(({ product, report, title }) =>
    ["Global", "Russia", "Ukraine", "European Union", "Argentina"].map((region) => ({
      product,
      region,
      period_family: "marketing_year",
      source_role: "balance",
      start_month: "Oct",
      end_month: "Sep",
      display_label: "Oct–Sep｜Oil World作物年度",
      source_report_id: [report],
      source_note: `《${title}》各完整年度区块表头均明确为Oct Sept。`,
      footer_detail: `${product === "Sunflower Oil" ? "葵油" : "葵粕"}：${report}完整年度供需区块按Oct–Sep列示。`,
      prefer_formal_basis: true,
    } satisfies PresentationPeriodBasisConfig)),
  ),
  ...["Global", "Indonesia", "Malaysia", "G2", "India"].map((region) => ({
    product: "Palm Oil",
    region,
    period_family: "marketing_year",
    source_role: "balance",
    start_month: "Oct",
    end_month: "Sep",
    display_label: "Oct–Sep｜Oil World作物年度",
    source_report_id: ["AN26392"],
    source_note: "《PALM OIL : World Supply and Demand Balance (1000 T)》第2行明确将完整年度列标为Oct Sept；Apr–Sept、Oct–Mar和Jan–Dec片段不进入演示页。",
    footer_detail: "棕榈油：AN26392完整年度供需区块按Oct–Sep列示；演示页不读取半年或自然年片段。",
    prefer_formal_basis: true,
  } satisfies PresentationPeriodBasisConfig)),
] as const;

function basisConfig(slide: PresentationSlideConfig, region: PresentationRegionConfig): PresentationPeriodBasisConfig {
  const production = slide.slideType === "production-conditions";
  const result = PRESENTATION_PERIOD_BASIS.find((item) =>
    item.product === slide.product
    && item.region === region.region
    && (production
      ? item.period_family === "crop_year" && item.source_role === "production_table"
      : item.period_family !== "crop_year" || item.source_role !== "production_table"),
  );
  if (!result) throw new Error(`Missing presentation period basis: ${slide.product}/${region.region}/${slide.slideId}`);
  return result;
}

const SPECIFIC_FORMAL_BASIS: Readonly<Record<string, string>> = {
  "Sept–Aug": "Sep–Aug",
  "Sep–Aug": "Sep–Aug",
  "Jan–Dec": "Jan–Dec",
  "Apr–Mar": "Apr–Mar",
  "Oct–Sept": "Oct–Sep",
  "Oct–Sep": "Oct–Sep",
  "Aug–July": "Aug–Jul",
  "Aug–Jul": "Aug–Jul",
  "July–June": "Jul–Jun",
  "Jul–Jun": "Jul–Jun",
  "June–May": "Jun–May",
  "Jun–May": "Jun–May",
};

export function presentationPeriodBasis(
  slide: PresentationSlideConfig,
  region: PresentationRegionConfig,
  metrics: readonly MetricData[],
): PresentationPeriodBasisConfig & { resolved_label: string } {
  const config = basisConfig(slide, region);
  if (!config.prefer_formal_basis) return { ...config, resolved_label: config.display_label };

  const formalLabels = new Set(metrics
    .map((metric) => metric.period_basis ? SPECIFIC_FORMAL_BASIS[metric.period_basis] : undefined)
    .filter(Boolean));
  if (formalLabels.size !== 1) return { ...config, resolved_label: config.display_label };
  const formalLabel = [...formalLabels][0];
  const familyLabel = config.period_family === "calendar_year" ? "自然年" : "Oil World作物年度";
  return { ...config, resolved_label: `${formalLabel}｜${familyLabel}` };
}

export function presentationPeriodFooterDetails(
  slide: PresentationSlideConfig,
  regions: readonly PresentationRegionConfig[],
): string[] {
  return [...new Set(regions.map((region) => basisConfig(slide, region).footer_detail).filter((detail): detail is string => Boolean(detail)))];
}
