import type { PresentationSlideConfig } from "../config";

interface PresentationGroup {
  groupId: string;
  groupTitle: string;
  groupOrder: number;
}

function groupsFromSlides(slides: readonly PresentationSlideConfig[]): PresentationGroup[] {
  const groups = new Map<string, PresentationGroup>();
  for (const slide of slides) {
    if (!groups.has(slide.groupId)) {
      groups.set(slide.groupId, {
        groupId: slide.groupId,
        groupTitle: slide.groupTitle,
        groupOrder: slide.groupOrder,
      });
    }
  }
  return [...groups.values()].sort((left, right) => left.groupOrder - right.groupOrder);
}

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
  const current = ordered[currentPage] ?? ordered[0];
  if (!current) return null;
  const groups = groupsFromSlides(ordered);
  const groupSlides = ordered.filter((slide) => slide.groupId === current.groupId);

  return (
    <nav className="presentation-slide-tabs" aria-label="演示页面切换">
      <div className="presentation-system-tabs" aria-label="演示体系切换">
        {groups.map((group) => {
          const active = group.groupId === current.groupId;
          const firstPage = ordered.findIndex((slide) => slide.groupId === group.groupId);
          return (
            <button
              type="button"
              className={active ? "is-active" : ""}
              aria-pressed={active}
              onClick={() => onPage(firstPage)}
              key={group.groupId}
            >
              {group.groupTitle}
            </button>
          );
        })}
      </div>
      <div className="presentation-group-slide-tabs" aria-label={`${current.groupTitle}页面切换`}>
        {groupSlides.map((slide) => {
          const pageIndex = ordered.findIndex((item) => item.slideId === slide.slideId);
          const active = pageIndex === currentPage;
          return (
            <button
              type="button"
              className={active ? "is-active" : ""}
              aria-current={active ? "page" : undefined}
              onClick={() => onPage(pageIndex)}
              key={slide.slideId}
            >
              <span>{slide.shortTitle}</span>
              <small>{slide.groupPageNumber} / {groupSlides.length}</small>
            </button>
          );
        })}
      </div>
    </nav>
  );
}
