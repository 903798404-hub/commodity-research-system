import type { CombinationComparison, CombinationData } from "./model";

function comparisonKey(metric: string, period: string, periodFamily?: string, sourceRole?: string) {
  return [metric, periodFamily ?? "legacy", sourceRole ?? "legacy", period].join("\u0000");
}

export function applyQuarterRevisions(
  data: CombinationData,
  comparison: CombinationComparison | null,
): CombinationData {
  const records = new Map(
    (comparison?.records ?? []).map((record) => [
      comparisonKey(record.metric, record.period, record.period_family, record.source_role),
      record,
    ]),
  );
  return {
    ...data,
    metrics: data.metrics.map((metric) => {
      const latestPeriod = metric.periods.find((period) => metric.values[period] !== null) ?? metric.periods[0];
      const record = latestPeriod
        ? records.get(comparisonKey(metric.metric, latestPeriod, metric.period_family, metric.source_role))
        : undefined;
      if (!record || record.quarter_revision === null) {
        return {
          ...metric,
          quarter_revision: null,
          quarter_revision_note: comparison ? record?.quality_note || "该年度暂无可比修正。" : "暂无上一期",
        };
      }
      return {
        ...metric,
        quarter_revision: {
          period: record.period,
          value: record.quarter_revision,
          unit: record.unit,
        },
        quarter_revision_note: "",
      };
    }),
  };
}
