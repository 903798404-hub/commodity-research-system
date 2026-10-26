import hashlib
import json

import pandas as pd
import pyarrow.parquet as pq
import pytest

from agri_research_agent.shared import table_delivery as delivery, table_increment as codec
from test_table_increment import baseline, fresh, OPTIONS


def transport_fixture(tmp_path):
    path = tmp_path / "baseline.parquet"
    baseline().to_parquet(path, index=False)
    old = pd.read_parquet(path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    payload = codec.build(fresh(), old, baseline_sha256=sha, **OPTIONS)
    incoming = tmp_path / "candidate.parquet"
    incoming.write_bytes(codec.canonical(payload))
    return path, sha, incoming


def test_exact_baseline_read_resolves_sparse_transport_without_modifying_inputs(tmp_path):
    path, sha, incoming = transport_fixture(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    frame, sparse = delivery.read(incoming, path, baseline_sha256=sha, **OPTIONS)
    assert sparse and codec.equivalent(frame, fresh(), OPTIONS["key_columns"])
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_legacy_parquet_consumer_remains_supported(tmp_path):
    path = tmp_path / "legacy.parquet"
    fresh().to_parquet(path, index=False)
    frame, sparse = delivery.read(path, None, baseline_sha256=None, **OPTIONS)
    assert not sparse
    pd.testing.assert_frame_equal(frame, pd.read_parquet(path))


@pytest.mark.parametrize("mutation", ["baseline", "duplicate", "scope", "trailing", "nan"])
def test_untrusted_transport_is_rejected_before_compatibility_creation(tmp_path, mutation):
    path, sha, incoming = transport_fixture(tmp_path)
    if mutation == "baseline":
        baseline().assign(value=99).to_parquet(path, index=False)
    elif mutation == "duplicate":
        raw = incoming.read_bytes()
        incoming.write_bytes(raw.rstrip()[:-1] + b',"row_count":3}')
    elif mutation == "scope":
        value = json.loads(incoming.read_bytes())
        value["scope_values"] = [2025, 2026]
        incoming.write_text(json.dumps(value), encoding="utf-8")
    elif mutation == "trailing":
        incoming.write_bytes(incoming.read_bytes() + b"trailing")
    else:
        incoming.write_bytes(incoming.read_bytes().replace(b'"row_count":3', b'"row_count":NaN'))
    with pytest.raises(ValueError):
        delivery.read(incoming, path, baseline_sha256=sha, **OPTIONS)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["baseline.parquet", "candidate.parquet"]


def test_nochange_does_not_even_serialize_full_parquet(tmp_path, monkeypatch):
    monkeypatch.setattr(delivery, "parquet_bytes", lambda *a, **k: pytest.fail("full table encoded"))
    assert delivery.compatibility({"weekly.parquet": fresh()}, {"weekly.parquet": {}}, business_changed=False) == {}
    with pytest.raises(ValueError, match="NO_CHANGE"):
        delivery.compatibility({"weekly.parquet": fresh()}, {"weekly.parquet": {}},
                               business_changed=False, output=tmp_path)
    assert not list(tmp_path.iterdir())


def test_separate_materializer_matches_inmemory_validation_identity_and_legacy_reader(tmp_path):
    frames = {"weekly.parquet": fresh()}
    metadata = {"weekly.parquet": {"source": "offline-unit-fixture", "raw_snapshot_sha256": "d" * 64}}
    expected = delivery.compatibility(frames, metadata, business_changed=True)
    assert not list(tmp_path.iterdir())
    actual = delivery.compatibility(frames, metadata, business_changed=True, output=tmp_path)
    assert actual == expected
    path = tmp_path / "weekly.parquet"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected["weekly.parquet"]["sha256"]
    pd.testing.assert_frame_equal(pd.read_parquet(path), fresh())
    assert pq.read_metadata(path).metadata[b"raw_snapshot_sha256"] == b"d" * 64
    before = path.read_bytes()
    with pytest.raises(ValueError, match="empty directory"):
        delivery.compatibility(frames, metadata, business_changed=True, output=tmp_path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("name", ["../weekly.parquet", "/weekly.parquet", "weekly.json", "sub/weekly.parquet"])
def test_materializer_refuses_arbitrary_file_names(tmp_path, name):
    with pytest.raises(ValueError, match="file set"):
        delivery.compatibility({name: fresh()}, {name: {}}, business_changed=True, output=tmp_path)
    assert not list(tmp_path.iterdir())
