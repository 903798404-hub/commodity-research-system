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
