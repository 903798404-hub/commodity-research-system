"""Pinned-image table reconstruction and compatibility materialization.

The protected host must not import this pandas/Arrow module. It receives only
small identities from independent validation. A later exact-image materializer
may write matching compatibility files into candidate-owned isolated scratch.
No provider or production path is opened for writing here.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from . import table_increment as codec


def _sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            codec.require(key not in result, "duplicate table transport field")
            result[key] = value
        return result
    def nonfinite(_value):
        raise ValueError("non-finite table transport value")
    return json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=pairs,
                      parse_constant=nonfinite)


def read(path: Path, baseline_path: Path | None, *, baseline_sha256, **options):
    """Read legacy Parquet or resolve sparse transport against its actual SHA.

    Keys, fetched scopes and provenance projection come from the external
    validated domain contract, never from the transport's own declarations.
    """
    path = Path(path)
    codec.require(path.is_file() and not path.is_symlink(), "table input must be a regular file")
    with path.open("rb") as handle:
        magic = handle.read(4)
    if magic == b"PAR1":
        return pd.read_parquet(path), False
    codec.require(path.stat().st_size <= codec.MAX_BYTES, "table transport exceeds byte limit")
    codec.require((baseline_path is None) == (baseline_sha256 is None), "baseline presence differs")
    baseline = None
    if baseline_path is not None:
        baseline_path = Path(baseline_path)
        codec.require(baseline_path.is_file() and not baseline_path.is_symlink(),
                      "table baseline must be a regular file")
        codec.require(_sha(baseline_path) == baseline_sha256, "actual table baseline identity differs")
        baseline = pd.read_parquet(baseline_path)
        codec.require(_sha(baseline_path) == baseline_sha256, "table baseline changed while reading")
    result = codec.resolve(_json(path.read_bytes()), baseline,
                           baseline_sha256=baseline_sha256, **options)
    return result, True


def parquet_bytes(frame, metadata):
    """Deterministic compatibility bytes, called only after semantic change."""
    codec.require(type(metadata) is dict and all(type(k) is str and type(v) is str
                  for k, v in metadata.items()), "invalid compatibility metadata")
    table = pa.Table.from_pandas(frame, preserve_index=False)
    table = table.replace_schema_metadata({**(table.schema.metadata or {}),
        **{key.encode("utf-8"): value.encode("utf-8") for key, value in metadata.items()}})
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink, compression="zstd", version="2.6", row_group_size=65536)
    raw = sink.getvalue().to_pybytes()
    codec.require(len(raw) <= codec.MAX_BYTES, "compatibility table exceeds byte limit")
    return raw


def compatibility(frames, metadata, *, business_changed: bool, output: Path | None = None):
    """Return small identities; optional writes are confined to empty scratch.

    Validation serializes changed candidates in memory. Publication separately
    reconstructs in the same image and writes identical bytes to owned scratch;
    the host must compare identities before controlled exchange into production.
    NO_CHANGE never encodes or writes a complete Parquet table.
    """
    codec.require(type(business_changed) is bool, "semantic comparison must be boolean")
    codec.require(type(frames) is dict and 0 < len(frames) <= 2
                  and set(metadata) == set(frames)
                  and all(type(name) is str and re.fullmatch(r"[a-z0-9_]+\.parquet", name)
                          for name in frames), "invalid compatibility file set")
    if not business_changed:
        codec.require(output is None, "NO_CHANGE cannot materialize compatibility files")
        return {}
    if output is not None:
        output = Path(output)
        codec.require(output.is_dir() and not output.is_symlink() and not any(output.iterdir()),
                      "compatibility scratch must be an empty directory")
    result = {}
    total = 0
    for name in sorted(frames):
        frame = frames[name]
        raw = parquet_bytes(frame, metadata[name])
        total += len(raw)
        codec.require(total <= codec.MAX_BYTES, "compatibility output exceeds total byte limit")
        result[name] = {"sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw),
            "row_count": len(frame), "parquet_schema_sha256": hashlib.sha256(
                pq.read_schema(pa.BufferReader(raw)).remove_metadata().to_string().encode("utf-8")).hexdigest()}
        if output is not None:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            with os.fdopen(os.open(output / name, flags, 0o444), "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            codec.require(_sha(output / name) == result[name]["sha256"], "materialized identity differs")
    return result
