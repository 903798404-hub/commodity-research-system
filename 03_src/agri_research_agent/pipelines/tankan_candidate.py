"""Immutable candidate generation for the two approved Tankan datasets."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Protocol, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from agri_research_agent.data_sources.tankan.fx_adapter import (
    FX_RAW_SCHEMA,
    FxAdapterResult,
    adapt_fx,
    load_fx_config,
)
from agri_research_agent.data_sources.tankan.market_price_adapter import (
    MARKET_RAW_SCHEMA,
    MarketAdapterResult,
    adapt_market_price,
    load_market_config,
)
from agri_research_agent.data_sources.tankan.models import (
    ConnectionProof,
    QueryPlanProof,
    QuerySpec,
    SourceBatch,
)
from agri_research_agent.data_sources.tankan.queries import (
    FX_WINDOW_QUERY,
    MARKET_WINDOW_QUERY,
)
from agri_research_agent.shared.file_identity import FileIdentity, identify_file
from agri_research_agent.shared.immutable_candidate import (
    seal_immutable_candidate,
    validate_candidate_id,
)


class TankanCandidateError(ValueError):
    pass


class PlannedReader(Protocol):
    @property
    def proof(self) -> ConnectionProof: ...

    def plan_stream(
        self,
        query: QuerySpec,
        parameters: Sequence[object],
        *,
        batch_size: int = 10_000,
    ) -> tuple[QueryPlanProof, Iterator[SourceBatch]]: ...


@dataclass(frozen=True, slots=True)
class TankanCandidateResult:
    candidate_directory: Path
    manifest: dict[str, object]


AdapterResult = MarketAdapterResult | FxAdapterResult


def build_market_candidate(
    client: PlannedReader,
    *,
    start_date: date,
    end_date: date,
    candidate_root: str | Path,
    candidate_id: str,
    mapping_config: str | Path,
    batch_size: int = 10_000,
) -> TankanCandidateResult:
    config_path = Path(mapping_config)
    config = load_market_config(config_path)
    return _build_candidate(
        client,
        query=MARKET_WINDOW_QUERY,
        raw_schema=MARKET_RAW_SCHEMA,
        start_date=start_date,
        end_date=end_date,
        candidate_root=candidate_root,
        candidate_id=candidate_id,
        mapping_config=config_path,
        batch_size=batch_size,
        adapter=lambda raw, sha, captured: adapt_market_price(
            raw,
            config,
            snapshot_sha256=sha,
            captured_at=captured,
        ),
        standard_filename="market_candidate.parquet",
    )


def build_fx_candidate(
    client: PlannedReader,
    *,
    start_date: date,
    end_date: date,
    candidate_root: str | Path,
    candidate_id: str,
    mapping_config: str | Path,
    batch_size: int = 10_000,
) -> TankanCandidateResult:
    config_path = Path(mapping_config)
    config = load_fx_config(config_path)
    return _build_candidate(
        client,
        query=FX_WINDOW_QUERY,
        raw_schema=FX_RAW_SCHEMA,
        start_date=start_date,
        end_date=end_date,
        candidate_root=candidate_root,
        candidate_id=candidate_id,
        mapping_config=config_path,
        batch_size=batch_size,
        adapter=lambda raw, sha, captured: adapt_fx(
            raw,
            config,
            snapshot_sha256=sha,
            captured_at=captured,
        ),
        standard_filename="fx_candidate.parquet",
    )


def _build_candidate(
    client: PlannedReader,
    *,
    query: QuerySpec,
    raw_schema: pa.Schema,
    start_date: date,
    end_date: date,
    candidate_root: str | Path,
    candidate_id: str,
    mapping_config: Path,
    batch_size: int,
    adapter: Callable[[pa.Table, str, datetime], AdapterResult],
    standard_filename: str,
) -> TankanCandidateResult:
    root = _candidate_root(candidate_root)
    safe_candidate_id = validate_candidate_id(candidate_id)
    if (root / safe_candidate_id).exists():
        raise FileExistsError(f"candidate already exists: {safe_candidate_id}")
    if type(start_date) is not date or type(end_date) is not date:
        raise TankanCandidateError("candidate window must use exact dates")
    if start_date > end_date or (end_date - start_date).days > query.max_window_days:
        raise TankanCandidateError("candidate window exceeds approved query bounds")
    config_identity = identify_file(mapping_config)
    plan, source_batches = client.plan_stream(
        query,
        (start_date, end_date),
        batch_size=batch_size,
    )
    rows: list[dict[str, object]] = []
    extraction_times: set[datetime] = set()
    for batch in source_batches:
        if not isinstance(batch, SourceBatch):
            raise TankanCandidateError("provider returned an invalid source batch")
        if batch.query != query or batch.plan != plan:
            raise TankanCandidateError("provider batch identity drifted during extraction")
        if len(rows) + len(batch.rows) > query.max_plan_rows:
            raise TankanCandidateError("actual row count exceeds approved query bound")
        rows.extend(dict(item) for item in batch.rows)
        extraction_times.add(batch.extracted_at)
    if len(extraction_times) > 1:
        raise TankanCandidateError("provider extraction timestamp drifted between batches")
    captured_at = (
        next(iter(extraction_times))
        if extraction_times
        else datetime.now(timezone.utc)
    )
    raw = pa.Table.from_pylist(rows, schema=raw_schema)
    if raw.num_rows == 0:
        raise TankanCandidateError("empty input is blocked and will not be sealed")

    def builder(directory: Path) -> dict[str, object]:
        raw_path = directory / "raw.parquet"
        pq.write_table(raw, raw_path, compression="zstd")
        raw_identity = identify_file(raw_path)
        raw_manifest = _raw_manifest(
            query=query,
            plan=plan,
            proof=client.proof,
            start_date=start_date,
            end_date=end_date,
            captured_at=captured_at,
            raw=raw,
            raw_identity=raw_identity,
        )
        raw_manifest_path = directory / "raw_manifest.json"
        _write_json(raw_manifest_path, raw_manifest)
        raw_manifest_identity = identify_file(raw_manifest_path)

        adapted = adapter(raw, raw_identity.sha256, captured_at)
        standard_path = directory / standard_filename
        pq.write_table(adapted.table, standard_path, compression="zstd")
        standard_identity = identify_file(standard_path)
        quality_path = directory / "quality_report.json"
        _write_json(quality_path, adapted.quality_report)
        quality_identity = identify_file(quality_path)
        collision_identity: FileIdentity | None = None
        if isinstance(adapted, MarketAdapterResult):
            collision_path = directory / "collision_report.json"
            _write_json(collision_path, adapted.collision_report)
            collision_identity = identify_file(collision_path)
        manifest = {
            "schema_version": "tankan-candidate-manifest/1",
            "candidate_id": candidate_id,
            "candidate_only": True,
            "promotion_authorized": False,
            **query.identity(),
            "query_parameters": {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
            "query_plan": plan.safe_manifest_fields(),
            "connection_proof": client.proof.safe_manifest_fields(),
            "captured_at": captured_at.astimezone(timezone.utc).isoformat(),
            "raw_file": _identity_fields("raw.parquet", raw_identity),
            "raw_manifest": _identity_fields(
                "raw_manifest.json", raw_manifest_identity
            ),
            "mapping_config": _identity_fields(mapping_config.name, config_identity),
            "standard_file": {
                **_identity_fields(standard_filename, standard_identity),
                "arrow_schema_sha256": _schema_sha(adapted.table.schema),
            },
            "quality_report": _identity_fields("quality_report.json", quality_identity),
            "collision_report": (
                None
                if collision_identity is None
                else _identity_fields("collision_report.json", collision_identity)
            ),
            "standard_row_count": adapted.table.num_rows,
            "provider_series_ids": sorted(
                set(adapted.table.column("provider_series_id").to_pylist())
            ),
            "canonical_series_ids": [],
        }
        _write_json(directory / "manifest.json", manifest)
        return manifest

    final, manifest = seal_immutable_candidate(root, safe_candidate_id, builder)
    return TankanCandidateResult(candidate_directory=final, manifest=manifest)


def _candidate_root(value: str | Path) -> Path:
    root = Path(value).resolve()
    parts = tuple(item.casefold() for item in root.parts)
    if len(parts) < 3 or parts[-3:] != ("01_data", "candidates", "tankan"):
        raise TankanCandidateError(
            "candidate_root must end with 01_data/candidates/tankan"
        )
    forbidden = {"current", "previous", "processed", "runtime", "stable"}
    if forbidden.intersection(parts):
        raise TankanCandidateError("candidate_root contains a promotion path component")
    return root


def _raw_manifest(
    *,
    query: QuerySpec,
    plan: QueryPlanProof,
    proof: ConnectionProof,
    start_date: date,
    end_date: date,
    captured_at: datetime,
    raw: pa.Table,
    raw_identity: FileIdentity,
) -> dict[str, object]:
    dates = raw.column(raw.schema.names[0]).to_pylist()
    return {
        "schema_version": "tankan-raw-manifest/1",
        "candidate_only": True,
        "promotion_authorized": False,
        **query.identity(),
        "query_parameters": {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        },
        "query_plan": plan.safe_manifest_fields(),
        "connection_proof": proof.safe_manifest_fields(),
        "captured_at": captured_at.astimezone(timezone.utc).isoformat(),
        "row_count": raw.num_rows,
        "source_min_date": min(dates).isoformat() if dates else None,
        "source_max_date": max(dates).isoformat() if dates else None,
        "arrow_schema_sha256": _schema_sha(raw.schema),
        "raw_file": _identity_fields("raw.parquet", raw_identity),
    }


def _schema_sha(schema: pa.Schema) -> str:
    return hashlib.sha256(str(schema).encode("utf-8")).hexdigest()


def _identity_fields(filename: str, identity: FileIdentity) -> dict[str, object]:
    return {
        "filename": filename,
        "sha256": identity.sha256,
        "size_bytes": identity.size_bytes,
    }


def _write_json(path: Path, payload: object) -> None:
    _validate_safe_metadata(payload)
    path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _validate_safe_metadata(payload: object) -> None:
    forbidden_keys = {
        "password",
        "passwd",
        "secret",
        "client_secret",
        "private_key",
        "token",
        "access_token",
        "api_key",
        "apikey",
        "dsn",
        "host",
        "user",
        "username",
    }
    absolute_user_path = re.compile(
        r"(?:[A-Za-z]:[\\/]Users[\\/]|/home/[^/]+/|/Users/[^/]+/)",
        re.IGNORECASE,
    )

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).casefold() in forbidden_keys:
                    raise TankanCandidateError("candidate metadata contains a sensitive field")
                visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)
        elif isinstance(value, str) and absolute_user_path.search(value):
            raise TankanCandidateError("candidate metadata contains an absolute user path")

    visit(payload)
