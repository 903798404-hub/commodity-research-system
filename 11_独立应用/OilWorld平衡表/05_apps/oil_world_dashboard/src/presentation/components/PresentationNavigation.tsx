import type { ReleaseList } from "../../model";
import type { PresentationSlideConfig } from "../config";

export function PresentationNavigation({
  page,
  slides,
  release,
  releases,
  visible,
  onPrevious,
  onNext,
  onRelease,
  onFullscreen,
  detailUrl,
}: {
  page: number;
  slides: readonly PresentationSlideConfig[];
  release: string;
  releases: ReleaseList | null;
  visible: boolean;
  onPrevious: () => void;
  onNext: () => void;
  onRelease: (release: string) => void;
  onFullscreen: () => void;
  detailUrl: string;
}) {
  const ordered = [...slides].sort((left, right) => left.layoutOrder - right.layoutOrder);
  const current = ordered[page];
  return (
    <nav className={`presentation-navigation${visible ? " is-visible" : ""}`} aria-label="演示页面控制">
      <div className="presentation-navigation__pages">
        <button type="button" onClick={onPrevious} disabled={page === 0}>← 上一页</button>
        <strong>{current.pageNumber} / {ordered.length}</strong>
        <button type="button" onClick={onNext} disabled={page === ordered.length - 1}>下一页 →</button>
        <small>键盘 ← → 可翻页</small>
      </div>
      <label>发布期
        <select value={release} onChange={(event) => onRelease(event.target.value)}>
          {(releases?.releases ?? []).filter((item) => item.available).map((item) => (
            <option key={item.release} value={item.release}>{item.label} · {item.release}</option>
          ))}
        </select>
      </label>
      <a href={detailUrl}>返回详细看板</a>
      <button type="button" className="presentation-fullscreen" onClick={onFullscreen}>全屏</button>
    </nav>
  );
}
