import type { PresentationSlideConfig } from "../config";
import { ANNUAL_CHANGE_DEFINITION, QUARTERLY_REVISION_DEFINITION } from "../definitions";
import { PresentationSlideTabs } from "./PresentationSlideTabs";

export function PresentationHeader({
  slide,
  slides,
  currentPage,
  onPage,
  releaseLabel,
  previousReleaseLabel,
}: {
  slide: PresentationSlideConfig;
  slides: readonly PresentationSlideConfig[];
  currentPage: number;
  onPage: (page: number) => void;
  releaseLabel: string;
  previousReleaseLabel: string;
}) {
  const unitLabel = slide.slideType === "production-conditions"
    ? "产量：万吨；面积：千公顷；单产：吨/公顷"
    : "万吨；库存/使用比：%";
  return (
    <header className="presentation-header">
      <div className="presentation-brand">
        <span>OIL WORLD</span>
        <h1>{slide.title}</h1>
        <p className="presentation-subtitle">{slide.subtitle}</p>
        <p className="presentation-period-guidance">年度口径：各地区按原始Oil World报表口径，详见地区标题。</p>
      </div>
      <dl className="presentation-identity">
        <div><dt>发布期</dt><dd>{releaseLabel}</dd></div>
        <div><dt>上一发布期</dt><dd>{previousReleaseLabel}</dd></div>
        <div><dt>单位</dt><dd>{unitLabel}</dd></div>
        <div><dt>年度变化</dt><dd>{ANNUAL_CHANGE_DEFINITION}</dd></div>
        <div><dt>季度修正</dt><dd>{QUARTERLY_REVISION_DEFINITION}</dd></div>
        <div><dt>数据来源</dt><dd>Oil World正式发布数据</dd></div>
      </dl>
      <PresentationSlideTabs slides={slides} currentPage={currentPage} onPage={onPage} />
    </header>
  );
}
