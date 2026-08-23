"""Stable server-side loader targets used after Public Current activation."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from agri_research_agent.pipelines.public_data_delivery import PrewarmTarget


def build_consumer_prewarm_targets(
    *, project_root: str | Path, runtime_root: str | Path
) -> tuple[PrewarmTarget, ...]:
    """Return minimal loader calls for the four deployed Public Current consumers.

    The runtime root is explicit so a staging or default worktree path can never
    be mistaken for the newly activated formal package.
    """

    project = Path(project_root).resolve()
    runtime = Path(runtime_root).resolve()
    apps = project / "05_apps"
    if str(apps) not in sys.path:
        sys.path.insert(0, str(apps))
    os.environ["PUBLIC_MARKET_DATA_RUNTIME_ROOT"] = str(runtime)

    from agri_research_agent.summary_engine.weather_cache import (
        load_weather_current_summary_cached,
    )
    from agri_research_agent.application.domestic_spreads import (
        load_domestic_spread_database,
    )
    from basis_page import load_basis_page_data
    from international_spread_page import load_international_spread_payload

    public = runtime / "public-market-data"
    weather_root = public / "lutou-weather"
    basis_root = public / "lutou-domestic-basis"

    domestic_spread = (
        runtime
        / "consumer-artifacts"
        / "domestic-spread"
        / "historical_spread_database.parquet"
    )

    return (
        PrewarmTarget(
            "international_spread",
            lambda: load_international_spread_payload("palm", project_root=project),
        ),
        PrewarmTarget(
            "weather",
            lambda: load_weather_current_summary_cached(
                weather_root, project / "02_configs" / "soybean_weather_us.yaml"
            ),
        ),
        PrewarmTarget("domestic_basis", lambda: load_basis_page_data(basis_root)),
        PrewarmTarget(
            "domestic_spread",
            lambda: load_domestic_spread_database(domestic_spread),
        ),
    )


def validate_activated_public_currents(runtime_root: str | Path) -> None:
    """Run strict schema/content readers against an activated package data root."""

    from agri_research_agent.pipelines.lutou_domestic_basis import (
        load_domestic_basis_current,
    )
    from agri_research_agent.pipelines.lutou_goal_b import (
        load_current as load_three_oil_current,
    )
    from agri_research_agent.pipelines.lutou_goal_b_soil import load_soil_current
    from agri_research_agent.pipelines.lutou_weather import load_weather_current
    from agri_research_agent.pipelines.tankan_goal_a import (
        load_current as load_tankan_current,
    )

    public = Path(runtime_root).resolve() / "public-market-data"
    validators = {
        "tankan": load_tankan_current,
        "lutou-three-oil": load_three_oil_current,
        "lutou-soil-moisture": load_soil_current,
        "lutou-weather": load_weather_current,
        "lutou-domestic-basis": load_domestic_basis_current,
    }
    found = False
    for dataset, loader in validators.items():
        dataset_root = public / dataset
        if not dataset_root.is_dir():
            continue
        found = True
        if loader(dataset_root) is None:
            raise ValueError(f"activated Public Current is missing: {dataset}")
    if not found:
        raise ValueError("activated package contains no supported Public Current")


__all__ = ["build_consumer_prewarm_targets", "validate_activated_public_currents"]
