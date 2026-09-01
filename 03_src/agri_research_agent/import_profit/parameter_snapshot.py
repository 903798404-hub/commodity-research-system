"""Canonical, self-contained calculation parameters for soybean Releases."""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
import hashlib
import json
from math import isfinite
from typing import Mapping

from .config import (
    ParameterOverride,
    SoybeanImportProfitConfig,
    SoybeanParameters,
)


PARAMETER_SNAPSHOT_SCHEMA = "soybean-import-profit-parameters/1"
PARAMETER_FIELDS = tuple(item.name for item in fields(SoybeanParameters))


class ParameterSnapshotError(ValueError):
    """Raised when persisted calculation-parameter provenance is invalid."""


@dataclass(frozen=True, slots=True)
class ParameterProvenance:
    status: str
    parameter_hash: str | None
    snapshot: Mapping[str, object] | None

    @property
    def available(self) -> bool:
        return self.status == "available"

    def parameters_for_origin(self, origin: str) -> SoybeanParameters:
        if not self.available or self.snapshot is None:
            raise ParameterSnapshotError(
                "legacy parameter snapshot is unavailable"
            )
        origins = self.snapshot["parameters_by_origin"]
        if not isinstance(origins, Mapping) or origin not in origins:
            raise ParameterSnapshotError(
                f"parameter snapshot does not contain origin: {origin}"
            )
        values = origins[origin]
        if not isinstance(values, Mapping):
            raise ParameterSnapshotError("origin parameters are invalid")
        return SoybeanParameters(**dict(values))


def build_parameter_snapshot(
    config: SoybeanImportProfitConfig,
) -> ParameterProvenance:
    """Freeze every effective parameter that can change calculated margin."""

    if not isinstance(config, SoybeanImportProfitConfig):
        raise ParameterSnapshotError(
            "config must be a SoybeanImportProfitConfig"
        )
    snapshot = {
        "snapshot_schema": PARAMETER_SNAPSHOT_SCHEMA,
        "commodity": config.commodity,
        "default_parameters": _parameter_payload(
            config.default_parameters
        ),
        "parameters_by_origin": {
            origin: _parameter_payload(config.resolve_parameters(origin))
            for origin in config.origin_codes
        },
    }
    validated = validate_parameter_snapshot(snapshot)
    return ParameterProvenance(
        status="available",
        parameter_hash=parameter_hash(validated),
        snapshot=validated,
    )


def canonical_parameter_bytes(snapshot: Mapping[str, object]) -> bytes:
    validated = validate_parameter_snapshot(snapshot)
    return json.dumps(
        validated,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def parameter_hash(snapshot: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_parameter_bytes(snapshot)).hexdigest().upper()


def read_parameter_provenance(
    manifest: Mapping[str, object],
) -> ParameterProvenance:
    """Read new provenance or explicitly classify a legacy Manifest."""

    if not isinstance(manifest, Mapping):
        raise ParameterSnapshotError("Manifest must be an object")
    has_snapshot = "parameter_snapshot" in manifest
    has_hash = "parameter_hash" in manifest
    if not has_snapshot and not has_hash:
        return ParameterProvenance(
            status="legacy_unavailable",
            parameter_hash=None,
            snapshot=None,
        )
    if not has_snapshot or not has_hash:
        raise ParameterSnapshotError(
            "parameter snapshot and hash must be present together"
        )
    snapshot = manifest["parameter_snapshot"]
    expected_hash = manifest["parameter_hash"]
    if not isinstance(snapshot, Mapping):
        raise ParameterSnapshotError("parameter snapshot must be an object")
    validated = validate_parameter_snapshot(snapshot)
    actual_hash = parameter_hash(validated)
    if expected_hash != actual_hash:
        raise ParameterSnapshotError("parameter snapshot hash mismatch")
    return ParameterProvenance(
        status="available",
        parameter_hash=actual_hash,
        snapshot=validated,
    )


def config_with_parameter_provenance(
    config: SoybeanImportProfitConfig,
    provenance: ParameterProvenance,
) -> SoybeanImportProfitConfig:
    """Use Release parameters with the current non-parameter contract."""

    if not provenance.available or provenance.snapshot is None:
        raise ParameterSnapshotError(
            "legacy parameter snapshot is unavailable"
        )
    origins = provenance.snapshot["parameters_by_origin"]
    if not isinstance(origins, Mapping) or set(origins) != set(
        config.origin_codes
    ):
        raise ParameterSnapshotError(
            "Release parameter origins do not match configuration"
        )
    default_values = provenance.snapshot["default_parameters"]
    if not isinstance(default_values, Mapping):
        raise ParameterSnapshotError("default parameters are invalid")
    default = SoybeanParameters(**dict(default_values))
    overrides = tuple(
        (
            origin,
            ParameterOverride(
                tuple(
                    (name, value)
                    for name, value in dict(origins[origin]).items()
                    if value != getattr(default, name)
                )
            ),
        )
        for origin in config.origin_codes
        if dict(origins[origin]) != _parameter_payload(default)
    )
    restored = replace(
        config,
        default_parameters=default,
        origin_overrides=overrides,
    )
    if build_parameter_snapshot(restored).parameter_hash != provenance.parameter_hash:
        raise ParameterSnapshotError(
            "Release parameters could not be restored exactly"
        )
    return restored


def validate_parameter_snapshot(
    snapshot: Mapping[str, object],
) -> Mapping[str, object]:
    expected = (
        "snapshot_schema",
        "commodity",
        "default_parameters",
        "parameters_by_origin",
    )
    if not isinstance(snapshot, Mapping) or set(snapshot) != set(expected):
        raise ParameterSnapshotError(
            "parameter snapshot fields are missing or unknown"
        )
    if snapshot["snapshot_schema"] != PARAMETER_SNAPSHOT_SCHEMA:
        raise ParameterSnapshotError("parameter snapshot schema is unsupported")
    if snapshot["commodity"] != "soybean":
        raise ParameterSnapshotError("parameter snapshot commodity is invalid")
    default = _validate_parameter_mapping(
        snapshot["default_parameters"], "default parameters"
    )
    origins = snapshot["parameters_by_origin"]
    if not isinstance(origins, Mapping) or not origins:
        raise ParameterSnapshotError(
            "parameter snapshot origins must be a non-empty object"
        )
    normalized_origins: dict[str, Mapping[str, float]] = {}
    for origin in sorted(origins):
        if not isinstance(origin, str) or not origin:
            raise ParameterSnapshotError("parameter snapshot origin is invalid")
        normalized_origins[origin] = _validate_parameter_mapping(
            origins[origin], f"parameters for {origin}"
        )
    return {
        "snapshot_schema": PARAMETER_SNAPSHOT_SCHEMA,
        "commodity": "soybean",
        "default_parameters": dict(default),
        "parameters_by_origin": {
            origin: dict(values)
            for origin, values in normalized_origins.items()
        },
    }


def _parameter_payload(parameters: SoybeanParameters) -> dict[str, float]:
    return {
        name: float(getattr(parameters, name))
        for name in PARAMETER_FIELDS
    }


def _validate_parameter_mapping(
    value: object, label: str
) -> Mapping[str, float]:
    if not isinstance(value, Mapping) or set(value) != set(PARAMETER_FIELDS):
        raise ParameterSnapshotError(f"{label} fields are invalid")
    normalized: dict[str, float] = {}
    for name in PARAMETER_FIELDS:
        item = value[name]
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not isfinite(float(item))
        ):
            raise ParameterSnapshotError(f"{label}.{name} must be finite")
        normalized[name] = float(item)
    try:
        SoybeanParameters(**normalized)
    except Exception as exc:
        raise ParameterSnapshotError(f"{label} is invalid") from exc
    return normalized
