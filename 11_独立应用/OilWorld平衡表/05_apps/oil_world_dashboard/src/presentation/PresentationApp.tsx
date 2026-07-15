import { useCallback, useEffect, useRef, useState, type PointerEvent } from "react";
import {
  DataLoadError,
  loadBootstrap,
  loadCombination,
  loadCombinationComparison,
  loadReleaseIndex,
} from "../api";
import { applyQuarterRevisions } from "../comparison";
import type { CombinationData, ReleaseIndex, ReleaseList } from "../model";
import { fileFor } from "../selectors";
import {
  PRESENTATION_SLIDES,
  pageFromSearch,
  pageIndexForKey,
  releaseFromSearch,
  searchForRelease,
  searchForSlide,
} from "./config";
import { PresentationHeader } from "./components/PresentationHeader";
import { PresentationNavigation } from "./components/PresentationNavigation";
import { PresentationShell } from "./components/PresentationShell";
import { ResearchTableSlide } from "./components/ResearchTableSlide";
import { applyPresentationStockUsageRatio } from "./presentationStockUsageRatio";

const BASE_URL = import.meta.env.BASE_URL;

function errorMessage(reason: unknown): string {
  return reason instanceof DataLoadError ? reason.message : "无法读取Oil World演示数据。";
}

export default function PresentationApp() {
  const [releases, setReleases] = useState<ReleaseList | null>(null);
  const [index, setIndex] = useState<ReleaseIndex | null>(null);
  const [release, setRelease] = useState("");
  const [page, setPage] = useState(0);
  const [regionData, setRegionData] = useState<Map<string, CombinationData>>(new Map());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [retryKey, setRetryKey] = useState(0);
  const [fullscreen, setFullscreen] = useState(false);
  const [controlsVisible, setControlsVisible] = useState(true);
  const controlsTimer = useRef<number | null>(null);
  const slide = PRESENTATION_SLIDES[page];

  const hideControlsLater = useCallback(() => {
    if (controlsTimer.current) window.clearTimeout(controlsTimer.current);
    controlsTimer.current = window.setTimeout(() => setControlsVisible(false), 2200);
  }, []);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    loadBootstrap(BASE_URL)
      .then(async ({ latest, releases: releaseList, index: latestIndex }) => {
        if (!active) return;
        const available = releaseList.releases.filter((item) => item.available).map((item) => item.release);
        const selectedRelease = releaseFromSearch(window.location.search, available, latest.release);
        const selectedPage = pageFromSearch(window.location.search, PRESENTATION_SLIDES);
        const selectedIndex = selectedRelease === latest.release
          ? latestIndex
          : await loadReleaseIndex(BASE_URL, selectedRelease);
        if (!active) return;
        setReleases(releaseList);
        setRelease(selectedRelease);
        setIndex(selectedIndex);
        setPage(selectedPage);
        const releaseSearch = searchForRelease(window.location.search, selectedRelease);
        const stateSearch = searchForSlide(releaseSearch, PRESENTATION_SLIDES[selectedPage].slideId);
        window.history.replaceState(null, "", `${window.location.pathname}${stateSearch}`);
      })
      .catch((reason) => {
        if (!active) return;
        console.error("Oil World presentation bootstrap failed", reason);
        setError(errorMessage(reason));
      })
      .finally(() => active && setLoading(false));
    return () => { active = false; };
  }, [retryKey]);

  useEffect(() => {
    if (!index || !release || !slide) return;
    const previousRelease = releases?.releases.find((item) => item.release === release)?.previous_release ?? null;
    const previousIndexPromise = previousRelease ? loadReleaseIndex(BASE_URL, previousRelease) : Promise.resolve(null);
    let active = true;
    setLoading(true);
    setError("");
    setRegionData(new Map());
    Promise.all(slide.regions.map(async (region) => {
      const selection = { system: slide.system, product: slide.product, region: region.region };
      const file = fileFor(index, selection);
      if (!file) throw new DataLoadError(`${slide.product} / ${region.region}在${release}中没有正式发布数据。`);
      const previousIndex = await previousIndexPromise;
      const previousFile = previousIndex ? fileFor(previousIndex, selection) : undefined;
      const [payload, comparison, previousPayload] = await Promise.all([
        loadCombination(BASE_URL, release, file.path),
        previousRelease
          ? loadCombinationComparison(BASE_URL, previousRelease, release, slide.system, slide.product, region.region)
          : Promise.resolve(null),
        previousRelease && previousFile
          ? loadCombination(BASE_URL, previousRelease, previousFile.path)
          : Promise.resolve(null),
      ]);
      const enriched = applyQuarterRevisions(payload, comparison);
      const presentationData = slide.derivePresentationStockUsageRatio
        ? applyPresentationStockUsageRatio(enriched, previousPayload, {
            periodFamily: region.periodFamily,
            sourceRole: region.sourceRole,
            scope: region.stockUsageScope,
          })
        : enriched;
      return [region.region, presentationData] as const;
    }))
      .then((entries) => active && setRegionData(new Map(entries)))
      .catch((reason) => {
        if (!active) return;
        console.error("Oil World presentation slide failed", reason);
        setRegionData(new Map());
        setError(errorMessage(reason));
      })
      .finally(() => active && setLoading(false));
    return () => { active = false; };
  }, [index, release, releases, slide]);

  useEffect(() => {
    if (!releases || !release) return;
    let active = true;
    const onPopState = () => {
      const available = releases.releases.filter((item) => item.available).map((item) => item.release);
      const requestedRelease = releaseFromSearch(window.location.search, available, release);
      setPage(pageFromSearch(window.location.search, PRESENTATION_SLIDES));
      if (requestedRelease === release) return;
      setLoading(true);
      setError("");
      loadReleaseIndex(BASE_URL, requestedRelease)
        .then((nextIndex) => {
          if (!active) return;
          setRegionData(new Map());
          setRelease(requestedRelease);
          setIndex(nextIndex);
        })
        .catch((reason) => {
          if (!active) return;
          console.error("Oil World presentation history restore failed", reason);
          setError(errorMessage(reason));
          setLoading(false);
        });
    };
    window.addEventListener("popstate", onPopState);
    return () => {
      active = false;
      window.removeEventListener("popstate", onPopState);
    };
  }, [release, releases]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.target instanceof HTMLInputElement || event.target instanceof HTMLSelectElement) return;
      if (event.altKey || event.ctrlKey || event.metaKey) return;
      const next = pageIndexForKey(event.key, page, PRESENTATION_SLIDES.length);
      if (next !== page) {
        event.preventDefault();
        changePage(next);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [page]);

  useEffect(() => {
    const onFullscreenChange = () => {
      const active = Boolean(document.fullscreenElement);
      setFullscreen(active);
      setControlsVisible(true);
      if (active) hideControlsLater();
    };
    document.addEventListener("fullscreenchange", onFullscreenChange);
    return () => document.removeEventListener("fullscreenchange", onFullscreenChange);
  }, [hideControlsLater]);

  useEffect(() => () => {
    if (controlsTimer.current) window.clearTimeout(controlsTimer.current);
  }, []);

  async function changeRelease(nextRelease: string) {
    if (nextRelease === release) return;
    setLoading(true);
    setError("");
    try {
      const nextIndex = await loadReleaseIndex(BASE_URL, nextRelease);
      setRegionData(new Map());
      setRelease(nextRelease);
      setIndex(nextIndex);
      window.history.replaceState(null, "", `${window.location.pathname}${searchForRelease(window.location.search, nextRelease)}`);
    } catch (reason) {
      console.error("Oil World presentation release switch failed", reason);
      setError(errorMessage(reason));
      setLoading(false);
    }
  }

  function changePage(nextPage: number) {
    const bounded = Math.max(0, Math.min(PRESENTATION_SLIDES.length - 1, nextPage));
    if (bounded === page) return;
    setPage(bounded);
    const nextSearch = searchForSlide(window.location.search, PRESENTATION_SLIDES[bounded].slideId);
    window.history.pushState(null, "", `${window.location.pathname}${nextSearch}`);
  }

  async function toggleFullscreen() {
    if (document.fullscreenElement) {
      await document.exitFullscreen();
      return;
    }
    if (fullscreen) {
      setFullscreen(false);
      setControlsVisible(true);
      return;
    }
    try {
      await document.querySelector<HTMLElement>(".presentation-viewport")?.requestFullscreen();
    } catch {
      setFullscreen(true);
      setControlsVisible(true);
      hideControlsLater();
    }
  }

  function onEdgePointerMove(event: PointerEvent<HTMLElement>) {
    if (!fullscreen) return;
    const edge = event.clientX < 80
      || event.clientX > window.innerWidth - 80
      || event.clientY < 90
      || event.clientY > window.innerHeight - 90;
    if (edge) {
      setControlsVisible(true);
      hideControlsLater();
    }
  }

  const currentRelease = releases?.releases.find((item) => item.release === release);
  const previousRelease = releases?.releases.find((item) => item.release === currentRelease?.previous_release);
  const releaseLabel = currentRelease ? `${currentRelease.label} · ${currentRelease.release}` : release || "读取中";
  const previousReleaseLabel = previousRelease ? `${previousRelease.label} · ${previousRelease.release}` : "—";
  const showControls = !fullscreen || controlsVisible;

  return (
    <PresentationShell controlsVisible={showControls} onEdgePointerMove={onEdgePointerMove}>
      <PresentationHeader
        slide={slide}
        slides={PRESENTATION_SLIDES}
        currentPage={page}
        onPage={changePage}
        releaseLabel={releaseLabel}
        previousReleaseLabel={previousReleaseLabel}
      />
      <div className="presentation-content" aria-live="polite">
        {loading && regionData.size === 0 && <div className="presentation-status">正在读取{slide.regions.length}个地区的正式发布数据…</div>}
        {error && (
          <div className="presentation-status presentation-status--error" role="alert">
            <strong>演示页面暂时无法打开</strong><span>{error}</span>
            <button type="button" onClick={() => setRetryKey((value) => value + 1)}>重新加载</button>
          </div>
        )}
        {regionData.size === slide.regions.length && <ResearchTableSlide slide={slide} regionData={regionData} />}
      </div>
      <PresentationNavigation
        page={page}
        slides={PRESENTATION_SLIDES}
        release={release}
        releases={releases}
        visible={showControls}
        onPrevious={() => changePage(page - 1)}
        onNext={() => changePage(page + 1)}
        onRelease={(value) => void changeRelease(value)}
        onFullscreen={() => void toggleFullscreen()}
        detailUrl={BASE_URL}
      />
    </PresentationShell>
  );
}
