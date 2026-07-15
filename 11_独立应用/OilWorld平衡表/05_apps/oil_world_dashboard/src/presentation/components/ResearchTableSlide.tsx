import type { CombinationData } from "../../model";
import type { PresentationSlideConfig } from "../config";
import { presentationPeriodFooterDetails } from "../presentationPeriodBasis";
import { PresentationNotesCard } from "./PresentationNotesCard";
import { RegionResearchTable } from "./RegionResearchTable";
import { SourceFooter } from "./SourceFooter";
import {
  PRESENTATION_STOCK_USAGE_FOOTER_NOTE,
  PRESENTATION_STOCK_USAGE_RATIO,
} from "../presentationStockUsageRatio";

export function ResearchTableSlide({
  slide,
  regionData,
}: {
  slide: PresentationSlideConfig;
  regionData: ReadonlyMap<string, CombinationData>;
}) {
  const orderedRegions = [...slide.regions].sort((left, right) => left.layoutOrder - right.layoutOrder);
  const allMetrics = [...regionData.values()].flatMap((data) => data.metrics);
  const ratioNote = slide.metrics.includes(PRESENTATION_STOCK_USAGE_RATIO)
    ? slide.euStockUsageRatio === "external-exports-confirmed"
      ? `${PRESENTATION_STOCK_USAGE_FOOTER_NOTE} EU出口已确认是对非EU/第三国出口，按区域公式计算。`
      : slide.euStockUsageRatio === "exports-scope-unconfirmed"
        ? `${PRESENTATION_STOCK_USAGE_FOOTER_NOTE} 欧盟出口范围无法从原表确认，本页不计算库存/使用比。`
        : PRESENTATION_STOCK_USAGE_FOOTER_NOTE
    : undefined;
  return (
    <div
      className="presentation-slide presentation-research-slide"
      data-slide-type={slide.slideType}
      data-layout={slide.layoutMode}
    >
      <section className="presentation-table-grid" aria-label={slide.title} data-layout={slide.layoutMode}>
        {orderedRegions.map((region) => {
          const data = regionData.get(region.region);
          return data
            ? <RegionResearchTable key={region.region} data={data} region={region} slide={slide} />
            : <div className="presentation-region-loading" key={region.region}>{slide.productLabel}－{region.label}<span>读取中…</span></div>;
        })}
        {(slide.layoutMode === "two-by-three-notes" || slide.layoutMode === "two-by-four-notes") && (
          <PresentationNotesCard slide={slide} regionData={regionData} />
        )}
      </section>
      <SourceFooter
        metrics={allMetrics}
        note="颜色仅表示数值方向，不代表利多或利空；缺失、冲突、不可比及不适用值显示—"
        periodDetails={presentationPeriodFooterDetails(slide, orderedRegions)}
        ratioNote={ratioNote}
      />
    </div>
  );
}
