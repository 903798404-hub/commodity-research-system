import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const appSource = fs.readFileSync(fileURLToPath(new URL("./App.tsx", import.meta.url)), "utf8");
const cssSource = fs.readFileSync(fileURLToPath(new URL("./styles.css", import.meta.url)), "utf8");
const projectRoot = fileURLToPath(new URL("../../../", import.meta.url));

function treeHash(root: string) {
  const files: string[] = [];
  const visit = (directory: string) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const absolute = path.join(directory, entry.name);
      if (entry.isDirectory()) visit(absolute);
      if (entry.isFile()) files.push(absolute);
    }
  };
  visit(root);
  files.sort((left, right) => {
    const leftRelative = path.relative(root, left).replaceAll("\\", "/");
    const rightRelative = path.relative(root, right).replaceAll("\\", "/");
    return leftRelative < rightRelative ? -1 : leftRelative > rightRelative ? 1 : 0;
  });
  const digest = crypto.createHash("sha256");
  for (const file of files) {
    digest.update(path.relative(root, file).replaceAll("\\", "/"));
    digest.update("\0");
    digest.update(crypto.createHash("sha256").update(fs.readFileSync(file)).digest());
  }
  return digest.digest("hex");
}

test("表格列顺序为指标、单位、季度修正、年度变化、市场年度", () => {
  const header = appSource.slice(appSource.indexOf("<thead>"), appSource.indexOf("</thead>"));
  const indicator = header.indexOf("<th>指标</th>");
  const unit = header.indexOf("<th>单位</th>");
  const quarter = header.indexOf("季度修正");
  const annual = header.indexOf("年度变化");
  const periods = header.indexOf("data.periods.map");
  assert.ok(indicator < unit && unit < quarter && quarter < annual && annual < periods);
});

test("变化列具有统一深红强调且空值使用弱样式", () => {
  assert.match(appSource, /className="change-header">季度修正/);
  assert.match(appSource, /className="change-header">年度变化/);
  assert.match(appSource, /change-cell--value/);
  assert.match(appSource, /change-cell--muted/);
  assert.match(cssSource, /--change-accent:\s*#8b3f48/);
  assert.match(cssSource, /\.change-cell--value\s*\{\s*color:\s*var\(--change-accent\)/);
  assert.match(cssSource, /\.change-cell--muted\s*\{\s*color:\s*var\(--change-muted\)/);
});

test("正负变化共用同一颜色类且无方向色切换", () => {
  const annualCell = appSource.slice(appSource.indexOf("metric.annual_change ? \"change-cell--value\""), appSource.indexOf("data.periods.map", appSource.indexOf("metric.annual_change ? \"change-cell--value\"")));
  assert.match(annualCell, /formatSignedChange/);
  assert.doesNotMatch(annualCell, /positive|negative|green|red/);
});

test("发布数据和计算结果保持原样", () => {
  const internal = path.join(projectRoot, "01_data", "releases", "2026-06");
  const published = path.join(projectRoot, "public", "data", "oil_world", "releases", "2026-06");
  const expected = "cf0e890c6b0f067a4e733aac55362709739a9b6f6df7a10c2a752fb0b0979930";
  assert.equal(treeHash(internal), expected);
  assert.equal(treeHash(published), expected);
  const soybeanGlobal = JSON.parse(fs.readFileSync(path.join(published, "combinations", "soybeans__global.json"), "utf8"));
  const production = soybeanGlobal.metrics.find((metric: { metric: string }) => metric.metric === "Production");
  assert.equal(production.annual_change.value, 11970);
  assert.equal(production.values["2026/27"], 441180);
});
