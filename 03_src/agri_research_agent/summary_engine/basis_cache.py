"""Shared, version-bound Basis reads and summaries for website consumers.

The original Public Current reader remains the data authority. Pages keep
their own views; only identical observations, identities, rules and UTC day
share a computed result. This module never collects, promotes or writes data.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping

import pandas as pd
import streamlit as st
import yaml

from agri_research_agent.market_data.public_basis_current import (
    PublicBasisCurrentError,
    PublicBasisCurrentErrorCode,
    load_public_basis_current,
    resolve_public_basis_current_identity,
)

from .basis import build_basis_summary
from .rules import DEFAULT_CONFIG
from .schema import Summary


@st.cache_data(show_spinner=False, max_entries=8)
def _load_basis_versioned(
    root: str, release_id: str, manifest_sha256: str,
) -> tuple[pd.DataFrame, dict[str, object]]:
    snapshot = load_public_basis_current(
        root, expected_release_id=release_id,
        expected_manifest_sha256=manifest_sha256,
    )
    return snapshot.records, snapshot.source_identity


def load_shared_basis_current(
    public_current_root: str | Path,
) -> tuple[pd.DataFrame, dict[str, object]]:
    """Share one verified version, including on a cross-page cache hit."""
    root = str(Path(public_current_root).resolve())
    before = resolve_public_basis_current_identity(root)
    key = before.release_id, before.manifest_sha256
    records, source = _load_basis_versioned(root, *key)
    after = resolve_public_basis_current_identity(root)
    if (after.release_id, after.manifest_sha256) != key:
        raise PublicBasisCurrentError(
            PublicBasisCurrentErrorCode.CURRENT_IDENTITY_MISMATCH,
            "Domestic Basis Current changed during shared read",
        )
    return records, source


def _records_sha256(records: pd.DataFrame) -> str:
    # Streamlit samples large DataFrames by default. Hash every observation so
    # a revision outside that sample cannot reuse an old calculation. Column
    # order, types and index also distinguish the original page view scopes.
    schema = [(str(name), str(dtype)) for name, dtype in zip(records.columns, records.dtypes)]
    digest = sha256(json.dumps(schema, ensure_ascii=False).encode('utf-8'))
    digest.update(pd.util.hash_pandas_object(records, index=True).to_numpy().tobytes())
    return digest.hexdigest()


@st.cache_data(show_spinner=False, max_entries=64)
def _summarize_basis_versioned(
    records_sha256: str,
    source_identity: dict[str, Any],
    source_dataset: str,
    rules_sha256: str,
    as_of_utc_day: str,
    _records: pd.DataFrame,
    _rules: Mapping[str, Any],
) -> Summary:
    # Underscored inputs are excluded only because the public wrapper hashes
    # their full content above; caller-owned mutable frames are never cached.
    del records_sha256, rules_sha256
    return build_basis_summary(
        _records, source_identity=source_identity, source_dataset=source_dataset,
        rules=_rules, today=date.fromisoformat(as_of_utc_day),
    )


def build_shared_basis_summary(
    records: pd.DataFrame, *, source_identity: Mapping[str, Any],
    source_dataset: str = 'public_domestic_basis_current',
    rules_path: str | Path = DEFAULT_CONFIG,
    today: date | None = None,
) -> Summary:
    """Keep existing calculations and freshness policy with exact dependencies.

    The Basis page passes its existing filtered display frame; the overview
    passes the full Public frame. Their different scopes cannot share results.
    UTC dates retain the original summary engine's freshness day boundary.
    """
    body = Path(rules_path).read_bytes()
    rules = yaml.safe_load(body.decode('utf-8', 'strict'))
    if rules.get('schema_version') != 1 or not rules.get('rule_version'):
        raise ValueError('Summary规则配置版本无效')
    return _summarize_basis_versioned(
        _records_sha256(records), dict(source_identity), source_dataset,
        sha256(body).hexdigest(), (today or datetime.now(timezone.utc).date()).isoformat(),
        _records=records, _rules=rules,
    )


def clear_shared_basis_cache() -> None:
    _load_basis_versioned.clear()
    _summarize_basis_versioned.clear()
