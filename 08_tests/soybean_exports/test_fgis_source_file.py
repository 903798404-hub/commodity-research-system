from __future__ import annotations

import csv
import io
import json
import runpy
import sys
from pathlib import Path
from typing import Any

import pytest

from agri_research_agent.soybean_exports.common import sha256_bytes
from agri_research_agent.soybean_exports.fgis import (
    FGIS_YEARLY_BASE_URL,
    FGIS_YEARLY_SOURCE_CHANNEL,
    FgisAdapterError,
    FgisYearlyFileAdapter,
    fgis_paths,
    run_fgis_pipeline,
)


REPOSITORY = Path(__file__).resolve().parents[2]
FGIS_CLI = REPOSITORY / "04_scripts/soybean_exports/run_fgis_export_inspections.py"
GIT_HEAD = "e" * 40
FIELDS = ("Thursday", "Cert Date", "Grain", "Destination", "Metric Ton", "Pounds")


def source_bytes(
    *,
    year: int = 2026,
    grain: str = "SOYBEANS",
    include_metric_ton: bool = True,
) -> bytes:
    fields = FIELDS if include_metric_ton else tuple(x for x in FIELDS if x != "Metric Ton")
    values = {
        "Thursday": f"{year}0730",
        "Cert Date": f"{year}0730",
        "Grain": grain,
        "Destination": "CHINA",
        "Metric Ton": "10",
        "Pounds": "22046",
    }
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerow({name: values[name] for name in fields})
    return stream.getvalue().encode("utf-8")


def write_source(
    root: Path,
    *,
    content: bytes | None = None,
    year: int = 2026,
    metadata_changes: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    body = source_bytes(year=year) if content is None else content
    source = root / f"CY{year}.csv"
    source.write_bytes(body)
    observed = {
        "content_length": len(body),
        "last_modified": "Mon, 03 Aug 2026 15:00:25 GMT",
        "etag": '"backend-reference"',
        "accept_ranges": "bytes",
    }
    metadata = {
        "schema_version": 1,
        "source_authority": "USDA FGIS",
        "source_channel": FGIS_YEARLY_SOURCE_CHANNEL,
        "calendar_year": year,
        "source_file": source.name,
        "official_url": f"{FGIS_YEARLY_BASE_URL}/{source.name}",
        "source_sha256": sha256_bytes(body),
        "source_size": len(body),
        "content_length": len(body),
        "accept_ranges": "bytes",
        "last_modified": observed["last_modified"],
        "etag": observed["etag"],
        "download_completed_at_utc": "2026-08-08T01:02:03Z",
        "metadata_before": observed,
        "metadata_after": observed,
    }
    metadata.update(metadata_changes or {})
    sidecar = root / f"{source.name}.metadata.json"
    sidecar.write_text(json.dumps(metadata), encoding="utf-8")
    return source, sidecar


def test_source_file_adapter_revalidates_identity_schema_and_provenance(tmp_path: Path) -> None:
    source, sidecar = write_source(tmp_path)
    result = FgisYearlyFileAdapter(
        source_path=source, metadata_path=sidecar
    ).fetch_soybeans()

    assert len(result.records) == 1
    assert result.records[0]["grain"] == "SOYBEANS"
    assert result.source_sha256 == sha256_bytes(source.read_bytes())
    assert result.content_length == source.stat().st_size
    assert result.query_scope["source_size"] == source.stat().st_size
    assert result.query_scope["delivery"] == "local_source_file"
    assert result.query_scope["metadata_file"] == sidecar.name
    assert result.source_url == f"{FGIS_YEARLY_BASE_URL}/CY2026.csv"


@pytest.mark.parametrize("case", ["missing", "directory", "empty"])
def test_source_file_adapter_rejects_missing_non_file_and_empty(
    tmp_path: Path, case: str
) -> None:
    source, sidecar = write_source(tmp_path)
    if case == "missing":
        source.unlink()
    elif case == "directory":
        source.unlink()
        source.mkdir()
    else:
        source.write_bytes(b"")
    with pytest.raises(FgisAdapterError):
        FgisYearlyFileAdapter(source_path=source, metadata_path=sidecar).fetch_soybeans()


@pytest.mark.parametrize(
    ("content", "metadata_changes", "message"),
    [
        (source_bytes(include_metric_ton=False), {}, "missing required fields"),
        (
            source_bytes(year=2025).replace(b"20250730", b"20250731"),
            {},
            "Cert Date does not match",
        ),
        (source_bytes(grain="soybeans"), {}, "no exact SOYBEANS rows"),
        (source_bytes(), {"source_sha256": "0" * 64}, "SHA-256 identity mismatch"),
        (source_bytes(), {"source_size": 1}, "size identity mismatch"),
        (source_bytes(), {"content_length": 1}, "Content-Length identity mismatch"),
    ],
)
def test_source_file_adapter_fails_closed_on_business_and_delivery_identity(
    tmp_path: Path,
    content: bytes,
    metadata_changes: dict[str, Any],
    message: str,
) -> None:
    source, sidecar = write_source(
        tmp_path, content=content, metadata_changes=metadata_changes
    )
    with pytest.raises(FgisAdapterError, match=message):
        FgisYearlyFileAdapter(source_path=source, metadata_path=sidecar).fetch_soybeans()


def test_source_file_cli_never_constructs_network_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, sidecar = write_source(tmp_path)
    namespace = runpy.run_path(str(FGIS_CLI))
    script_globals = namespace["main"].__globals__
    observed: dict[str, Any] = {}

    class NetworkAdapter:
        def __init__(self, **_kwargs: Any) -> None:
            raise AssertionError("HTTP Yearly adapter must not be constructed")

    class FileAdapter:
        def __init__(self, **kwargs: Any) -> None:
            observed["adapter"] = kwargs

    def pipeline(**kwargs: Any) -> dict[str, Any]:
        observed["pipeline"] = kwargs
        return {"status": "candidate_only"}

    script_globals["resolve_runtime_git_head"] = lambda **_kwargs: GIT_HEAD
    script_globals["FgisYearlyAdapter"] = NetworkAdapter
    script_globals["FgisYearlyFileAdapter"] = FileAdapter
    script_globals["run_fgis_pipeline"] = pipeline
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(FGIS_CLI),
            "--runtime-root",
            str(tmp_path / "runtime"),
            "--source",
            "yearly",
            "--source-file",
            str(source),
            "--source-metadata-file",
            str(sidecar),
            "--candidate-only",
        ],
    )

    assert namespace["main"]() == 0
    assert observed["adapter"] == {
        "source_path": source.resolve(),
        "metadata_path": sidecar.resolve(),
    }
    assert observed["pipeline"]["adapter"].__class__ is FileAdapter


def test_source_file_sha_no_change_preserves_stable_bytes_and_mtime(tmp_path: Path) -> None:
    source, sidecar = write_source(tmp_path)
    runtime = tmp_path / "runtime"
    first = run_fgis_pipeline(
        runtime_root=runtime,
        adapter=FgisYearlyFileAdapter(source_path=source, metadata_path=sidecar),
        git_head=GIT_HEAD,
        batch_id="source-first",
    )
    paths = fgis_paths(runtime, "unused")
    stable_bytes = paths["stable"].read_bytes()
    stable_mtime = paths["stable"].stat().st_mtime_ns

    second = run_fgis_pipeline(
        runtime_root=runtime,
        adapter=FgisYearlyFileAdapter(source_path=source, metadata_path=sidecar),
        git_head=GIT_HEAD,
        batch_id="source-same",
    )

    assert first["published"] is True
    assert second["status"] == "no_change"
    assert paths["stable"].read_bytes() == stable_bytes
    assert paths["stable"].stat().st_mtime_ns == stable_mtime
    assert not (runtime / "01_data/raw/soybean_export_inspections/source-same").exists()


def test_source_metadata_sidecar_is_mandatory(tmp_path: Path) -> None:
    source, sidecar = write_source(tmp_path)
    sidecar.unlink()
    with pytest.raises(FgisAdapterError, match="metadata file does not exist"):
        FgisYearlyFileAdapter(source_path=source, metadata_path=sidecar).fetch_soybeans()


def test_source_metadata_allows_backend_etag_difference_after_download(
    tmp_path: Path,
) -> None:
    source, sidecar = write_source(tmp_path)
    metadata = json.loads(sidecar.read_text(encoding="utf-8"))
    metadata["metadata_after"] = {
        **metadata["metadata_after"],
        "etag": '"different-backend-reference"',
    }
    sidecar.write_text(json.dumps(metadata), encoding="utf-8")

    result = FgisYearlyFileAdapter(
        source_path=source, metadata_path=sidecar
    ).fetch_soybeans()
    assert result.source_sha256 == sha256_bytes(source.read_bytes())
