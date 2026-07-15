import type { CombinationData } from "../../model";
import type { PresentationSlideConfig } from "../config";
import { presentationPeriodFooterDetails } from "../presentationPeriodBasis";
import { RegionResearchTable } from "./RegionResearchTable";
import { SourceFooter } from "./SourceFooter";

export function ResearchTableSlide({
  slide,
  regionData,
}: {
  slide: PresentationSlideConfig;
  regionData: ReadonlyMap<string, CombinationData>;
}) {
  const orderedRegions = [...slide.regions].sort((left, right) => left.layoutOrder - right.layoutOrder);
  const allMetrics = [...regionData.values()].flatMap((data) => data.metrics);
  return (
    <div className="presentation-slide presentation-research-slide" data-slide-type={slide.slideType}>
      <section className="presentation-table-grid" aria-label={slide.title}>
        {orderedRegions.map((region) => {
          const data = regionData.get(region.region);
          return data
            ? <RegionResearchTable key={region.region} data={data} region={region} slide={slide} />
            : <div className="presentation-region-loading" key={region.region}>大豆－{region.label}<span>读取中…</span></div>;
        })}
      </section>
      <SourceFooter
        metrics={allMetrics}
        note="颜色仅表示数值方向，不代表利多或利空；缺失、冲突、不可比及不适用值显示—"
        periodDetails={presentationPeriodFooterDetails(slide, orderedRegions)}
      />
    </div>
  );
}
