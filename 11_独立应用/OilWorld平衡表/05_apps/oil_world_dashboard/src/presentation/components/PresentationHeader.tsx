import type { PresentationSlideConfig } from "../config";
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
    : "万吨；库存/消费比：%";
  return (
    <header className="presentation-header">
      <div className="presentation-brand">
        <span>OIL WORLD</span>
        <h1>{slide.title}</h1>
        <p className="presentation-period-guidance">年度口径：各地区按原始Oil World报表口径，详见地区标题。</p>
        <PresentationSlideTabs slides={slides} currentPage={currentPage} onPage={onPage} />
      </div>
      <dl className="presentation-identity">
        <div><dt>发布期</dt><dd>{releaseLabel}</dd></div>
        <div><dt>上一发布期</dt><dd>{previousReleaseLabel}</dd></div>
        <div><dt>单位</dt><dd>{unitLabel}</dd></div>
        <div><dt>年度变化</dt><dd>最新年度 − 上一年度</dd></div>
        <div><dt>季度修正</dt><dd>当前发布期 − 上一发布期</dd></div>
        <div><dt>数据来源</dt><dd>Oil World正式发布数据</dd></div>
      </dl>
    </header>
  );
}
