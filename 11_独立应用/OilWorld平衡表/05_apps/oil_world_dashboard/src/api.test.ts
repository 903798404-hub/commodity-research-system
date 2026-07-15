import assert from "node:assert/strict";
import test from "node:test";
import { DataLoadError, fetchJsonWithRetry, loadBootstrap, resolveAssetPath } from "./api";

test("根路径和子路径使用同一资源拼接规则", () => {
  assert.equal(resolveAssetPath("data/oil_world/latest.json", "/"), "/data/oil_world/latest.json");
  assert.equal(resolveAssetPath("/data/oil_world/latest.json", "/research/oil-world/"), "/research/oil-world/data/oil_world/latest.json");
});

test("首次latest短暂404后自动恢复，无需人工刷新", async () => {
  const attempts = new Map<string, number>();
  const fetcher: typeof fetch = async (input) => {
    const url = String(input);
    attempts.set(url, (attempts.get(url) ?? 0) + 1);
    if (url.endsWith("latest.json") && attempts.get(url) === 1) {
      return new Response("not ready", { status: 404, headers: { "content-type": "text/plain" } });
    }
    if (url.endsWith("latest.json")) return Response.json({ release: "2026-06" });
    if (url.endsWith("releases.json")) return Response.json({ releases: [{ release: "2026-06", label: "June 2026", available: true }] });
    if (url.endsWith("index.json")) return Response.json({ release: "2026-06", systems: [], files: [] });
    return new Response("not found", { status: 404 });
  };
  const result = await loadBootstrap("/", { fetcher, delays: [0, 0] });
  assert.equal(result.latest.release, "2026-06");
  assert.equal(attempts.get("/data/oil_world/latest.json"), 2);
});

test("永久404在有限重试后显示错误", async () => {
  let attempts = 0;
  const fetcher: typeof fetch = async () => {
    attempts += 1;
    return new Response("missing", { status: 404 });
  };
  await assert.rejects(
    fetchJsonWithRetry("/data/oil_world/latest.json", { fetcher, delays: [0, 0] }),
    DataLoadError,
  );
  assert.equal(attempts, 3);
});

test("HTML回退返回明确格式错误且不重试", async () => {
  let attempts = 0;
  const fetcher: typeof fetch = async () => {
    attempts += 1;
    return new Response("<!doctype html><html></html>", { status: 200, headers: { "content-type": "text/html" } });
  };
  await assert.rejects(
    fetchJsonWithRetry("/data/oil_world/latest.json", { fetcher, delays: [0, 0] }),
    /返回了网页内容/,
  );
  assert.equal(attempts, 1);
});

test("非法JSON返回明确错误", async () => {
  const fetcher: typeof fetch = async () => new Response("{broken", { status: 200, headers: { "content-type": "application/json" } });
  await assert.rejects(fetchJsonWithRetry("/broken.json", { fetcher, delays: [] }), /不是有效JSON/);
});
