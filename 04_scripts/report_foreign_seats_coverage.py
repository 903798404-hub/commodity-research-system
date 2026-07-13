from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def coverage(data: pd.DataFrame, start: str, end: str | None = None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    data = data.copy(); data["trade_date"] = pd.to_datetime(data["trade_date"])
    data = data[data.trade_date >= pd.Timestamp(start)]
    if end: data = data[data.trade_date <= pd.Timestamp(end)]
    rows = []
    for (variety, seat), group in data.groupby(["variety", "seat_name_normalized"]):
        successful_days = group.loc[group.source_status.eq("success"), "trade_date"].nunique()
        long_days, short_days = group.long_position.notna().sum(), group.short_position.notna().sum()
        strict_days = group.net_position.notna().sum()
        rows.append({"variety": variety, "seat_name_normalized": seat, "successful_trade_dates": successful_days, "long_rank_dates": long_days, "short_rank_dates": short_days, "both_rank_dates": strict_days, "strict_net_coverage": strict_days / successful_days if successful_days else 0.0})
    failures = data[data.data_status.eq("source_error")][["trade_date", "variety", "source_name", "source_method", "error_message"]].drop_duplicates().sort_values(["trade_date", "variety"])
    # Not an indicator: this is a transparent counterfactual assessment only.
    approximation = data[data.source_status.eq("success")].copy()
    approximation["visible_approx_net"] = approximation.long_position.fillna(0) - approximation.short_position.fillna(0)
    assessment = approximation.groupby("variety").agg(total_seat_days=("trade_date", "count"), strict_seat_days=("net_position", lambda s: s.notna().sum()), approximate_seat_days=("visible_approx_net", lambda s: s.notna().sum())).reset_index()
    assessment["continuity_gain_seat_days"] = assessment.approximate_seat_days - assessment.strict_seat_days
    assessment["continuity_gain_ratio"] = assessment.continuity_gain_seat_days / assessment.total_seat_days.replace(0, pd.NA)
    return pd.DataFrame(rows), failures, assessment


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--start", default="2026-06-01"); parser.add_argument("--end")
    args = parser.parse_args(); input_file = ROOT / "01_data" / "database" / "foreign_seats" / "foreign_seat_positions.parquet"
    output_dir = ROOT / "06_outputs" / "foreign_seats"; output_dir.mkdir(parents=True, exist_ok=True)
    summary, failures, assessment = coverage(pd.read_parquet(input_file), args.start, args.end)
    summary.to_csv(output_dir / "foreign_seats_coverage.csv", index=False, encoding="utf-8-sig")
    failures.to_csv(output_dir / "foreign_seats_failures.csv", index=False, encoding="utf-8-sig")
    assessment.to_csv(output_dir / "foreign_seats_approximation_assessment.csv", index=False, encoding="utf-8-sig")
    latest = args.end or pd.read_parquet(input_file)["trade_date"].max()
    markdown = [f"# 外资与重点席位数据覆盖率\n\n统计区间：{args.start} 至 {latest}\n", "## 严格口径覆盖率\n", summary.to_markdown(index=False), "\n## 数据源失败\n", failures.to_markdown(index=False) if not failures.empty else "无", "\n## 公开可见近似口径评估（未上线）\n", assessment.to_markdown(index=False), "\n说明：近似口径只在成功取得公开排名的席位日计算，未上榜一侧按 0。它会把“未公开”误读为零持仓；严格双边上榜时两者差异为 0，缺边日期没有真实值，无法量化实际误差或选出严格差异最大的日期。因此不建议作为正式或交易净持仓指标，只能在未来另行明确标注后作为辅助观察。"]
    (output_dir / "foreign_seats_coverage.md").write_text("\n".join(markdown), encoding="utf-8")
    return 0

if __name__ == "__main__": raise SystemExit(main())
