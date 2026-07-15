import type { MetricData } from "../../model";

export function SourceFooter({
  metrics,
  note,
  periodDetails = [],
  ratioNote,
}: {
  metrics: readonly MetricData[];
  note?: string;
  periodDetails?: readonly string[];
  ratioNote?: string;
}) {
  const reportCount = new Set(metrics.flatMap((metric) => metric.source_report_id).filter(Boolean)).size;
  return (
    <footer className="presentation-source-footer">
      <div>
        <span><strong>数据来源：</strong>Oil World正式发布数据{reportCount ? ` · ${reportCount}份原始报表` : ""}</span>
        <span>{note ?? "正式发布数据；缺失、冲突或不可比值统一显示为—"}</span>
      </div>
      {periodDetails.length > 0 && <p><strong>口径审计：</strong>{periodDetails.join(" ")}</p>}
      {ratioNote && <p><strong>库存/使用比：</strong>{ratioNote}</p>}
    </footer>
  );
}
