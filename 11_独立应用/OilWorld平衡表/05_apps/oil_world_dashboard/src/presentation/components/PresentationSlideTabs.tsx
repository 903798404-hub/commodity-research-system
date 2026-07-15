import type { PresentationSlideConfig } from "../config";

export function PresentationSlideTabs({
  slides,
  currentPage,
  onPage,
}: {
  slides: readonly PresentationSlideConfig[];
  currentPage: number;
  onPage: (page: number) => void;
}) {
  const ordered = [...slides].sort((left, right) => left.layoutOrder - right.layoutOrder);
  return (
    <nav className="presentation-slide-tabs" aria-label="演示页面切换">
      {ordered.map((slide, index) => {
        const active = index === currentPage;
        return (
          <button
            type="button"
            className={active ? "is-active" : ""}
            aria-current={active ? "page" : undefined}
            onClick={() => onPage(index)}
            key={slide.slideId}
          >
            <span>{slide.shortTitle}</span>
            <small>{slide.pageNumber} / {ordered.length}</small>
          </button>
        );
      })}
    </nav>
  );
}
