"""Domestic Basis domain keeps public 29-series and live Async scopes separate."""
from datetime import datetime

from agri_research_agent.market_data.public_basis_current import (
    load_public_basis_current, resolve_public_basis_current_identity,
)
from agri_research_agent.summary_engine.basis import build_basis_summary

from .core import build_message
from .gate import ReadyRun, contained
from .views import async_view, public_identity, status_line, summary_view


def build(run: ReadyRun, **_):
    status = async_view(run.report("domestic_basis"))
    root = contained(run.data_root, "public-market-data/lutou-domestic-basis")
    identity = resolve_public_basis_current_identity(root)
    source = public_identity(identity)
    run.bind("lutou-domestic-basis", source)
    snapshot = load_public_basis_current(root, expected_release_id=identity.release_id,
                                         expected_manifest_sha256=identity.manifest_sha256)
    summary = build_basis_summary(snapshot.records, source_identity=source,
                                  generated_at=datetime.fromisoformat(run.daily["completed_at"].replace("Z", "+00:00")))
    if public_identity(snapshot.identity) != source:
        raise ValueError("BASIS_INPUT_CHANGED_DURING_READ")
    view = summary_view(summary)
    # Upstream headline says 'today'; notification uses the explicit quote date.
    view["headline"] = f"国内基差（报价日期 {summary.source_date}）"
    text = "\n".join([
        f"国内基差通知候选 · {run.mode}", status_line("Domestic Basis", status),
        "本轮状态来自正式 Async evidence；以下为现有报价相对前次有效报价的变化。",
        summary.short_text,
    ])
    return build_message(message_type="domestic_basis", source_run_id=run.source_run_id,
                         source_data_identity=source, business={"summary": view},
                         status={"domestic_basis": status},
                         metadata={"data_ready": "PASS", "data_root_mode": run.mode,
                                   "run_evidence": run.evidence_identity,
                                   "public_series_count": identity.series_count,
                                   "async_required_count": status["summary"]["TOTAL_REQUIRED"],
                                   "freshness_authority": "sealed_async_report"},
                         rendered_content=text)
