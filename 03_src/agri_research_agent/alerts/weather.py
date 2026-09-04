"""Weather domain: formal current consumer, existing summary, run Async states."""
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

from agri_research_agent.market_data.public_weather_current import (
    load_public_weather_current, resolve_weather_current_identity,
)
from agri_research_agent.summary_engine.weather import build_weather_summary

from .core import build_message
from .gate import ReadyRun, contained, file_sha
from .views import async_view, public_identity, status_line, summary_view


def build(run: ReadyRun, *, weather_config: Path):
    observation = async_view(run.report("weather_observation"))
    forecast = async_view(run.report("weather_forecast"))
    root = contained(run.data_root, "public-market-data/lutou-weather")
    identity = resolve_weather_current_identity(root)
    source = public_identity(identity)
    run.bind("lutou-weather", source)
    config_sha = file_sha(weather_config)
    configs = yaml.safe_load(weather_config.read_text(encoding="utf-8"))
    if not isinstance(configs, dict) or len(configs) != 1:
        raise ValueError("ONE_EXPLICIT_WEATHER_CONFIG_REQUIRED")
    config = next(iter(configs.values()))
    regions = [str(item["key"]) for item in config["regions"]]
    snapshot = load_public_weather_current(
        root, crop=str(config["crop"]), country=str(config["country"]),
        metrics=("precipitation", "temperature_max", "soil_moisture"), regions=regions,
        start_date=(pd.Timestamp(identity.observation_source_max) - pd.DateOffset(years=6)).date(),
        expected_release_id=identity.release_id, expected_manifest_sha256=identity.manifest_sha256,
    )
    summary = build_weather_summary(
        snapshot.records, snapshot.normals, config,
        source_identity={"public_weather": source, "config_sha256": config_sha},
        generated_at=datetime.fromisoformat(run.daily["completed_at"].replace("Z", "+00:00")),
    )
    if file_sha(weather_config) != config_sha or public_identity(snapshot.identity) != source:
        raise ValueError("WEATHER_INPUT_CHANGED_DURING_READ")
    business = {"summary_scope": {"crop": config["crop"], "country": config["country"], "regions": regions},
                "summary": summary_view(summary)}
    missing = observation["nonblocking_missing"] + forecast["nonblocking_missing"]
    text = "\n".join([
        f"天气通知候选 · {run.mode}", status_line("Observation", observation),
        status_line("Forecast", forecast),
        f"非阻断缺失 warning：{len(missing)} 项；未补值。",
        f"业务摘要范围：{config['page_title']}", summary.short_text,
    ])
    return build_message(message_type="weather", source_run_id=run.source_run_id,
                         source_data_identity=source, business=business,
                         status={"weather_observation": observation, "weather_forecast": forecast},
                         metadata={"data_ready": "PASS", "data_root_mode": run.mode,
                                   "run_evidence": run.evidence_identity,
                                   "freshness_authority": "sealed_async_report"},
                         rendered_content=text)
