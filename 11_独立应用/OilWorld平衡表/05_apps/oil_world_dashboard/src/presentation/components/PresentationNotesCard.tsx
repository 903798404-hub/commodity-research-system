import type { CombinationData, MetricData } from "../../model";
import type { PresentationSlideConfig } from "../config";
import { PRESENTATION_CHANGE_DEFINITIONS } from "../definitions";
import { presentationPeriodBasis } from "../presentationPeriodBasis";
import { metricByIdentity } from "../selectors";

function metricsForRegion(
  slide: PresentationSlideConfig,
  data: CombinationData | undefined,
  periodFamily?: string,
  sourceRole?: string,
): MetricData[] {
  if (!data) return [];
  return slide.metrics
    .map((metric) => metricByIdentity(data, { metric, periodFamily, sourceRole }))
    .filter((metric): metric is MetricData => Boolean(metric));
}

function ratioSummary(slide: PresentationSlideConfig): string {
  if (!slide.metrics.includes("Stocks/Use Ratio") && !slide.metrics.includes("presentation_stock_usage_ratio")) {
    return "库存/使用比：生产条件页不计算。";
  }
  if (!slide.derivePresentationStockUsageRatio) {
    return "库存/使用比：采用正式数据中的direct或已审计derived值。";
  }
  const eu = slide.euStockUsageRatio === "external-exports-confirmed"
    ? "EU出口已确认是对外出口，按区域公式计算。"
    : slide.euStockUsageRatio === "exports-scope-unconfirmed"
      ? "欧盟出口范围无法从原表确认，本页不计算库存/使用比。"
      : "";
  return `网页比率：Global＝期末库存÷国内消费；国家＝期末库存÷（国内消费＋出口）。${eu}`;
}

export function PresentationNotesCard({
  slide,
  regionData,
}: {
  slide: PresentationSlideConfig;
  regionData: ReadonlyMap<string, CombinationData>;
}) {
  const orderedRegions = [...slide.regions].sort((left, right) => left.layoutOrder - right.layoutOrder);
  const bases = orderedRegions.map((region) => {
    const metrics = metricsForRegion(
      slide,
      regionData.get(region.region),
      region.periodFamily,
      region.sourceRole,
    );
    return { region, basis: presentationPeriodBasis(slide, region, metrics) };
  });
  const reports = [...new Set([...regionData.values()]
    .flatMap((data) => data.metrics)
    .flatMap((metric) => metric.source_report_id)
    .filter(Boolean))];
  const basisSummary = slide.slideType === "production-conditions"
    ? "生产指标只读取crop_year＋production_table序列。"
    : "供需指标按各地区正式平衡表口径；Global如含多序列则标记混合口径。";
  const hasCalendarBalance = slide.regions.some((region) =>
    region.periodFamily === "calendar_year" && region.sourceRole === "balance",
  );

  return (
    <aside className="presentation-notes-card" aria-label={`${slide.productLabel}本页说明`}>
      <div className="presentation-notes-title">
        <strong>本页说明</strong>
        <span>动态口径</span>
      </div>
      <div className="presentation-notes-body">
        <p>{basisSummary}</p>
        {hasCalendarBalance && <p>自然年供需与作物年度生产独立展示，不做年度转换或跨时间轴修正。</p>}
        <dl>
          {bases.map(({ region, basis }) => (
            <div key={region.region}><dt>{region.label}</dt><dd>{basis.resolved_label}</dd></div>
          ))}
        </dl>
        <p>{PRESENTATION_CHANGE_DEFINITIONS}</p>
        <p>“—”表示缺失、冲突、不适用、不可比或网页公式组成项不足。</p>
        <p>{ratioSummary(slide)}</p>
        {slide.notes?.map((note) => <p key={note}>{note}</p>)}
        <p><strong>主要来源：</strong>{reports.length ? reports.join(" · ") : "Oil World正式发布数据"}</p>
      </div>
    </aside>
  );
}
