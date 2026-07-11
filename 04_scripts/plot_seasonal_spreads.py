from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import pandas as pd


RECENT_SEASON_COUNT = 6


def setup_logger(log_file: Path) -> logging.Logger:
    logger = logging.getLogger("plot_seasonal_spreads")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def safe_filename(value: str) -> str:
    return (
        str(value)
        .strip()
        .replace(" ", "_")
        .replace("/", "-")
        .replace("\\", "-")
        .replace(":", "-")
    )


def clear_current_outputs(current_dir: Path) -> list[str]:
    deleted: list[str] = []
    patterns = ["*.png", "plot_summary.xlsx"]
    for pattern in patterns:
        for path in current_dir.glob(pattern):
            if path.is_file():
                path.unlink()
                deleted.append(str(path))
    return deleted


def setup_font() -> str:
    available_fonts = {font.name for font in fm.fontManager.ttflist}
    if "Microsoft YaHei" in available_fonts:
        plt.rcParams["font.sans-serif"] = ["Microsoft YaHei"]
        font_used = "Microsoft YaHei"
    else:
        font_used = "default"
    plt.rcParams["axes.unicode_minus"] = False
    return font_used


def season_sort_key(season: str) -> int:
    try:
        return int(str(season).split("/")[0])
    except (TypeError, ValueError):
        return -1


def latest_seasons(seasons: list[str], count: int = RECENT_SEASON_COUNT) -> list[str]:
    ordered = sorted(seasons, key=season_sort_key)
    return ordered[-count:]


def plot_one_spread(
    *,
    spread_name: str,
    data: pd.DataFrame,
    full_history: bool,
    png_file: Path,
    logger: logging.Logger,
) -> dict[str, object]:
    plot_log: list[dict[str, object]] = []
    try:
        chart_long = data.copy()
        chart_long["calendar_offset"] = pd.to_numeric(chart_long["calendar_offset"], errors="coerce")
        chart_long["spread_value"] = pd.to_numeric(chart_long["spread_value"], errors="coerce")
        chart_long = chart_long.dropna(subset=["calendar_offset", "spread_value"]).sort_values(
            ["season", "calendar_offset"]
        )
        selected_seasons = sorted(chart_long["season"].astype(str).unique().tolist(), key=season_sort_key)
        if not full_history:
            selected_seasons = latest_seasons(selected_seasons)
            chart_long = chart_long[chart_long["season"].astype(str).isin(selected_seasons)].copy()

        if chart_long.empty:
            chart_wide = pd.DataFrame()
            fig, ax = plt.subplots(figsize=(13, 7))
            ax.set_title(spread_name, fontsize=18, pad=14)
            ax.text(0.5, 0.55, "无可用数据", ha="center", va="center", transform=ax.transAxes, fontsize=18)
            ax.text(
                0.5,
                0.46,
                f"{spread_name} 没有 status = success 的记录",
                ha="center",
                va="center",
                transform=ax.transAxes,
                fontsize=11,
                color="#666666",
            )
            ax.set_axis_off()
            fig.tight_layout()
            fig.savefig(png_file, dpi=180)
            plt.close(fig)
            plot_log.append({"event": "no_data", "message": "no success rows after numeric cleanup"})
        else:
            seasons = sorted(chart_long["season"].astype(str).unique().tolist(), key=season_sort_key)
            latest_season = seasons[-1]
            fig, ax = plt.subplots(figsize=(13, 7))
            for season in seasons:
                season_data = chart_long[chart_long["season"].astype(str) == season]
                is_latest = season == latest_season
                line_kwargs = {
                    "label": season,
                    "linewidth": 3.4 if is_latest else 1.5,
                    "alpha": 1.0 if is_latest else 0.8,
                }
                if is_latest:
                    line_kwargs["color"] = "#D62728"
                ax.plot(
                    season_data["calendar_offset"],
                    season_data["spread_value"],
                    **line_kwargs,
                )

            tick_source = (
                chart_long.loc[:, ["calendar_offset", "month_day"]]
                .drop_duplicates()
                .sort_values("calendar_offset")
            )
            tick_source = tick_source[tick_source["calendar_offset"] % 14 == 0]
            if tick_source.empty:
                tick_source = chart_long.loc[:, ["calendar_offset", "month_day"]].drop_duplicates().iloc[::14]

            ax.set_title(spread_name, fontsize=18, pad=14)
            ax.set_xlabel("month_day")
            ax.set_ylabel("spread_value")
            ax.set_xticks(tick_source["calendar_offset"].tolist())
            ax.set_xticklabels(tick_source["month_day"].tolist(), rotation=45, ha="right")
            ax.grid(True, linestyle="--", linewidth=0.7, alpha=0.35)
            ax.legend(title="season")
            fig.tight_layout()
            fig.savefig(png_file, dpi=180)
            plt.close(fig)

            chart_wide = chart_long.pivot_table(
                index=["calendar_offset", "month_day"],
                columns="season",
                values="spread_value",
                aggfunc="last",
            ).reset_index()
            plot_log.append(
                {
                    "event": "plot_success",
                    "message": f"rows={len(chart_long)}; seasons={','.join(seasons)}; full_history={full_history}",
                }
            )

        season_count = chart_long["season"].nunique() if not chart_long.empty else 0
        logger.info("plot generated: spread_name=%s rows=%s seasons=%s", spread_name, len(chart_long), season_count)
        return {
            "spread_name": spread_name,
            "png_path": str(png_file),
            "success_rows": len(chart_long),
            "season_count": int(season_count),
            "status": "success",
            "error": "",
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("plot failed: spread_name=%s error=%s", spread_name, exc)
        return {
            "spread_name": spread_name,
            "png_path": str(png_file),
            "success_rows": 0,
            "season_count": 0,
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    database_file = project_root / "01_data" / "historical_spread_database.xlsx"
    reports_dir = project_root / "06_outputs" / "reports" / "current"
    current_dir = reports_dir / "价差日报图片"
    logs_dir = project_root / "10_logs"
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    log_file = logs_dir / f"plot_seasonal_spreads_{timestamp}.log"
    summary_file = reports_dir / "plot_summary.xlsx"

    reports_dir.mkdir(parents=True, exist_ok=True)
    current_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logger(log_file)
    font_used = setup_font()
    logger.info("font_used=%s", font_used)
    current_deleted = clear_current_outputs(current_dir)
    logger.info("current files deleted=%s", len(current_deleted))

    try:
        spread_long = pd.read_excel(database_file, sheet_name="spread_long")
        config_file = project_root / "01_data" / "historical_spread_config.xlsx"
        try:
            config = pd.read_excel(config_file, sheet_name="spread_config")
            full_history_map = {
                str(row["spread_name"]): str(row.get("full_history", False)).strip().lower() in {"true", "1", "yes", "y"}
                for _, row in config.iterrows()
            }
        except Exception as exc:  # noqa: BLE001
            full_history_map = {}
            logger.warning("could not read full_history config, defaulting to recent seasons: %s", exc)

        success_data = spread_long[spread_long["status"] == "success"].copy()
        spread_names = sorted(spread_long["spread_name"].dropna().astype(str).unique().tolist())
        logger.info("recognized spread count=%s", len(spread_names))

        summary_rows: list[dict[str, object]] = []
        for spread_name in spread_names:
            file_prefix = safe_filename(spread_name)
            png_file = current_dir / f"{file_prefix}.png"
            spread_data = success_data[success_data["spread_name"].astype(str) == spread_name].copy()
            summary_rows.append(
                plot_one_spread(
                    spread_name=spread_name,
                    data=spread_data,
                    full_history=full_history_map.get(spread_name, False),
                    png_file=png_file,
                    logger=logger,
                )
            )

        summary = pd.DataFrame(
            summary_rows,
            columns=[
                "spread_name",
                "png_path",
                "success_rows",
                "season_count",
                "status",
                "error",
            ],
        )
        with pd.ExcelWriter(summary_file, engine="openpyxl") as writer:
            summary.to_excel(writer, sheet_name="plot_summary", index=False)
            pd.DataFrame({"deleted_current_file": current_deleted}).to_excel(
                writer, sheet_name="current_cleanup", index=False
            )

        success_count = int((summary["status"] == "success").sum()) if not summary.empty else 0
        failed_count = int((summary["status"] == "failed").sum()) if not summary.empty else 0
        print(f"recognized_spread_count: {len(spread_names)}")
        print(f"plot_success_count: {success_count}")
        print(f"plot_failed_count: {failed_count}")
        print(f"plot_summary: {summary_file}")
        print(f"png_dir: {current_dir}")
        print(f"plot_seasonal_spreads_log: {log_file}")
        return 0 if failed_count == 0 else 1
    except Exception as exc:  # noqa: BLE001
        logger.exception("plot batch failed: %s", exc)
        print(f"plot_seasonal_spreads failed: {exc}")
        print(f"plot_seasonal_spreads_log: {log_file}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

