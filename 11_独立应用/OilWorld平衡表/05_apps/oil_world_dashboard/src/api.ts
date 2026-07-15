import type {
  CombinationComparison,
  CombinationData,
  ComparisonIndex,
  ReleaseIndex,
  ReleaseList,
} from "./model";

export class DataLoadError extends Error {
  constructor(message: string, readonly detail?: string) {
    super(message);
    this.name = "DataLoadError";
  }
}

interface RetryOptions {
  delays?: number[];
  fetcher?: typeof fetch;
}

const RETRYABLE_STATUS = new Set([404, 502, 503, 504]);

export function resolveAssetPath(path: string, baseUrl = "/"): string {
  const cleanPath = path.replace(/^\/+/, "");
  const cleanBase = baseUrl === "/" ? "/" : `/${baseUrl.replace(/^\/+|\/+$/g, "")}/`;
  return `${cleanBase}${cleanPath}`.replace(/\/{2,}/g, "/");
}

export async function fetchJsonWithRetry<T>(url: string, options: RetryOptions = {}): Promise<T> {
  const delays = options.delays ?? [120, 280];
  const fetcher = options.fetcher ?? fetch;
  let lastError: unknown;
  for (let attempt = 0; attempt <= delays.length; attempt += 1) {
    try {
      const response = await fetcher(url, { cache: "no-store" });
      const body = await response.text();
      if (!response.ok) {
        const error = new DataLoadError(`数据请求失败（HTTP ${response.status}）`, `${url}: ${body.slice(0, 200)}`);
        if (!RETRYABLE_STATUS.has(response.status) || attempt === delays.length) throw error;
        lastError = error;
      } else {
        const contentType = response.headers.get("content-type") ?? "";
        if (contentType.includes("text/html") || /^\s*<!doctype html|^\s*<html/i.test(body)) {
          throw new DataLoadError("数据文件返回了网页内容，请检查部署路径。", `${url}: ${body.slice(0, 200)}`);
        }
        try {
          return JSON.parse(body) as T;
        } catch (error) {
          throw new DataLoadError("数据文件不是有效JSON。", `${url}: ${String(error)}; ${body.slice(0, 200)}`);
        }
      }
    } catch (error) {
      if (error instanceof DataLoadError && !RETRYABLE_STATUS.has(Number(error.message.match(/HTTP (\d+)/)?.[1]))) {
        throw error;
      }
      if (error instanceof DOMException && error.name === "AbortError") throw error;
      lastError = error;
      if (attempt === delays.length) break;
    }
    await new Promise((resolve) => setTimeout(resolve, delays[attempt]));
  }
  if (lastError instanceof DataLoadError) throw lastError;
  throw new DataLoadError("暂时无法连接数据文件，已自动重试。", String(lastError));
}

export async function loadBootstrap(baseUrl: string, options: RetryOptions = {}) {
  const latest = await fetchJsonWithRetry<{ release: string }>(
    resolveAssetPath("data/oil_world/latest.json", baseUrl),
    options,
  );
  const releases = await fetchJsonWithRetry<ReleaseList>(
    resolveAssetPath("data/oil_world/releases.json", baseUrl),
    options,
  );
  const index = await loadReleaseIndex(baseUrl, latest.release, options);
  return { latest, releases, index };
}

export function loadReleaseIndex(baseUrl: string, release: string, options: RetryOptions = {}) {
  return fetchJsonWithRetry<ReleaseIndex>(
    resolveAssetPath(`data/oil_world/releases/${release}/index.json`, baseUrl),
    options,
  );
}

export function loadCombination(
  baseUrl: string,
  release: string,
  path: string,
  options: RetryOptions = {},
) {
  return fetchJsonWithRetry<CombinationData>(
    resolveAssetPath(`data/oil_world/releases/${release}/${path}`, baseUrl),
    options,
  );
}

export async function loadCombinationComparison(
  baseUrl: string,
  previousRelease: string,
  currentRelease: string,
  system: string,
  product: string,
  region: string,
  options: RetryOptions = {},
) {
  const pair = `${previousRelease}_to_${currentRelease}`;
  const index = await fetchJsonWithRetry<ComparisonIndex>(
    resolveAssetPath(`data/oil_world/comparisons/${pair}/index.json`, baseUrl),
    options,
  );
  const file = index.files.find(
    (item) => item.system === system && item.product === product && item.region === region,
  );
  if (!file) {
    throw new DataLoadError("相邻发布期比较清单中没有当前商品和地区。", pair);
  }
  return fetchJsonWithRetry<CombinationComparison>(
    resolveAssetPath(`data/oil_world/comparisons/${pair}/${file.path}`, baseUrl),
    options,
  );
}
