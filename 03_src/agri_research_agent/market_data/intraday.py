"""Public exact-quote contracts and immutable session storage (no business formulas)."""
from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date, datetime
from enum import StrEnum
from hashlib import sha256
import json
import math
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .contracts import ContractId, Exchange, parse_standard_instrument
from agri_research_agent.shared.file_identity import identify_file
from agri_research_agent.shared.immutable_candidate import seal_immutable_candidate
from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode, assert_runtime_write

SCHEMA_VERSION = "2"
RELEASES_DIRNAME = "releases"
MANIFEST_FILENAME = "manifest.json"
QUOTES_FILENAME = "quotes.json"


class IntradaySnapshotError(RuntimeError):
    status = "ERROR"


class IntradaySnapshotValidationError(IntradaySnapshotError):
    status = "NOT_READY"


class IntradaySnapshotConflictError(IntradaySnapshotError):
    status = "IMMUTABLE_CONFLICT"


class IntradaySnapshotNotFoundError(IntradaySnapshotError):
    status = "NOT_FOUND"


class IntradaySnapshotLockedError(IntradaySnapshotError):
    status = "LOCKED"


class MarketSession(StrEnum):
    AM = "AM"
    PM = "PM"


class FreshnessStatus(StrEnum):
    FRESH = "FRESH"


class InstrumentAvailabilityStatus(StrEnum):
    CONTRACT_NOT_AVAILABLE = "CONTRACT_NOT_AVAILABLE"


class SealStatus(StrEnum):
    SEALED = "SEALED"
    NO_CHANGE = "NO_CHANGE"


def require_aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise IntradaySnapshotValidationError(f"{field_name} must be timezone-aware")
    return value


def json_value(value):
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise IntradaySnapshotValidationError("JSON keys must be strings")
        return {key: json_value(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(child) for child in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise IntradaySnapshotValidationError("Noncanonical JSON value")


def canonical_json(value) -> bytes:
    return json.dumps(json_value(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def content_hash(value) -> str:
    return sha256(canonical_json(value)).hexdigest()


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    return value


def _provenance(value):
    if not isinstance(value, Mapping) or not value:
        raise IntradaySnapshotValidationError("Provenance is required")
    return _freeze(json.loads(canonical_json(value)))


def validate_exact_identity(instrument_id: str, contract_code: str, exchange: str, product: str):
    if instrument_id == "FX:USD/CNH:SPOT":
        if (contract_code, exchange, product) != ("", "OTC", "USD/CNH"):
            raise IntradaySnapshotValidationError("FX identity mismatch")
        return
    try:
        instrument = parse_standard_instrument(instrument_id)
        if not isinstance(instrument, ContractId) or str(instrument) != instrument_id:
            raise ValueError("Exact canonical contract required")
        prefix = {(Exchange.CBOT, "SOYBEAN"): "", (Exchange.DCE, "SOYMEAL"): "M",
                  (Exchange.DCE, "SOYOIL"): "Y"}[(instrument.exchange, instrument.product)]
        if not 2000 <= instrument.year <= 2099:
            raise ValueError("YYMM year range")
        expected = f"{prefix}{instrument.year % 100:02d}{instrument.month:02d}"
        if (contract_code, exchange, product) != (expected, instrument.exchange.value, instrument.product):
            raise ValueError("Contract identity mismatch")
    except (ValueError, KeyError, TypeError) as exc:
        raise IntradaySnapshotValidationError("Invalid exact identity") from exc


@dataclass(frozen=True, slots=True)
class IntradayQuote:
    business_date: date
    session: MarketSession
    captured_at: datetime
    instrument_id: str
    contract_code: str
    exchange: str
    product: str
    price: float
    quote_type: str
    currency: str
    unit: str
    source_system: str
    source_table: str
    source_updated_at: datetime
    source_trade_date: date | None
    freshness_status: FreshnessStatus
    provenance: Mapping[str, object]
    retrieved_at: datetime | None = None

    def __post_init__(self):
        validate_exact_identity(self.instrument_id, self.contract_code, self.exchange, self.product)
        if type(self.business_date) is not date or not isinstance(self.session, MarketSession):
            raise IntradaySnapshotValidationError("Invalid date/session")
        require_aware(self.captured_at, "captured_at")
        require_aware(self.source_updated_at, "source_updated_at")
        if self.source_updated_at > self.captured_at:
            raise IntradaySnapshotValidationError("Future source timestamp")
        if self.retrieved_at is not None:
            require_aware(self.retrieved_at, "retrieved_at")
            if not self.source_updated_at <= self.retrieved_at <= self.captured_at:
                raise IntradaySnapshotValidationError("Retrieval chronology mismatch")
        if type(self.price) not in (int, float) or not math.isfinite(self.price) or self.price <= 0:
            raise IntradaySnapshotValidationError("Positive finite price required")
        object.__setattr__(self, "price", float(self.price))
        if self.source_trade_date is not None and type(self.source_trade_date) is not date:
            raise IntradaySnapshotValidationError("Invalid source trade date")
        if self.freshness_status is not FreshnessStatus.FRESH:
            raise IntradaySnapshotValidationError("Sealed quote must be fresh")
        for name in ("quote_type", "currency", "unit", "source_system", "source_table"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise IntradaySnapshotValidationError("Quote metadata missing")
        object.__setattr__(self, "provenance", _provenance(self.provenance))

    @property
    def key(self):
        return self.instrument_id, self.contract_code

    def as_dict(self):
        result = {field.name: json_value(getattr(self, field.name)) for field in fields(self)}
        if self.retrieved_at is None:
            result.pop("retrieved_at")  # Preserve verified v1 content bytes.
        return result

    def content_dict(self):
        result = self.as_dict()
        result.pop("captured_at")
        return result


@dataclass(frozen=True, slots=True)
class IntradayUnavailableInstrument:
    instrument_id: str
    contract_code: str
    exchange: str
    product: str
    status: InstrumentAvailabilityStatus
    reason: str
    provenance: Mapping[str, object]

    def __post_init__(self):
        validate_exact_identity(self.instrument_id, self.contract_code, self.exchange, self.product)
        if self.status is not InstrumentAvailabilityStatus.CONTRACT_NOT_AVAILABLE or not self.reason.strip():
            raise IntradaySnapshotValidationError("Unavailable evidence missing")
        object.__setattr__(self, "provenance", _provenance(self.provenance))

    @property
    def key(self):
        return self.instrument_id, self.contract_code

    def as_dict(self):
        return {field.name: json_value(getattr(self, field.name)) for field in fields(self)}


@dataclass(frozen=True, slots=True)
class IntradaySnapshot:
    business_date: date
    session: MarketSession
    captured_at: datetime
    quotes: tuple[IntradayQuote, ...]
    calendar_policy: str
    calendar_source_identity: str
    environment: str = "FORMAL"
    unavailable_instruments: tuple[IntradayUnavailableInstrument, ...] = ()
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self):
        if type(self.business_date) is not date or not isinstance(self.session, MarketSession):
            raise IntradaySnapshotValidationError("Invalid date/session")
        require_aware(self.captured_at, "captured_at")
        if self.schema_version not in {"1", SCHEMA_VERSION}:
            raise IntradaySnapshotValidationError("Unsupported schema")
        if self.environment not in {"FORMAL", "TEST_ISOLATED_NON_PRODUCTION"}:
            raise IntradaySnapshotValidationError("Preview is not a sealed environment")
        if not self.calendar_policy.strip() or not self.calendar_source_identity.strip():
            raise IntradaySnapshotValidationError("Calendar evidence required")
        if not isinstance(self.quotes, tuple) or not self.quotes or not isinstance(self.unavailable_instruments, tuple):
            raise IntradaySnapshotValidationError("Immutable nonempty quote tuple required")
        keys = []
        for quote in self.quotes:
            if not isinstance(quote, IntradayQuote) or (quote.business_date, quote.session, quote.captured_at) != (self.business_date, self.session, self.captured_at):
                raise IntradaySnapshotValidationError("Quote snapshot identity mismatch")
            if self.schema_version == SCHEMA_VERSION and quote.retrieved_at is None:
                raise IntradaySnapshotValidationError("Retrieval evidence required")
            keys.append(quote.key)
        for item in self.unavailable_instruments:
            if not isinstance(item, IntradayUnavailableInstrument):
                raise IntradaySnapshotValidationError("Invalid unavailable evidence")
            keys.append(item.key)
        if len(keys) != len(set(keys)):
            raise IntradaySnapshotValidationError("Duplicate requested identity")

    @property
    def release_id(self):
        return f"{self.business_date.isoformat()}-{self.session.value}"

    @property
    def requested_identities(self):
        return tuple(sorted(item.key for item in (*self.quotes, *self.unavailable_instruments)))

    @property
    def content_sha256(self):
        value = dict(schema_version=self.schema_version, business_date=self.business_date.isoformat(),
                     session=self.session.value, quotes=[q.content_dict() for q in sorted(self.quotes, key=lambda q:q.key)],
                     unavailable_instruments=[q.as_dict() for q in sorted(self.unavailable_instruments, key=lambda q:q.key)],
                     calendar_policy=self.calendar_policy, calendar_source_identity=self.calendar_source_identity,
                     environment=self.environment)
        if self.schema_version == SCHEMA_VERSION:
            value["captured_at"] = self.captured_at.isoformat()
        return content_hash(value)

    @property
    def quotes_sha256(self):
        return content_hash([q.as_dict() for q in sorted(self.quotes, key=lambda q:q.key)])


@dataclass(frozen=True, slots=True)
class IntradaySealResult:
    status: SealStatus
    release_id: str
    content_sha256: str
    release_dir: Path


def _safe_path(path):
    target = Path(path).absolute()
    if any(p.is_symlink() or p.is_junction() for p in (target, *target.parents)):
        raise IntradaySnapshotValidationError("Snapshot links forbidden")
    return target


def _read_json(path):
    path = _safe_path(path)
    if not path.is_file():
        raise IntradaySnapshotNotFoundError(path.name)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise IntradaySnapshotValidationError("Duplicate JSON key")
            result[key] = value
        return result
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(IntradaySnapshotValidationError("Nonfinite JSON")))


def _manifest(snapshot):
    return dict(schema_version=snapshot.schema_version, release_id=snapshot.release_id,
                business_date=snapshot.business_date.isoformat(), session=snapshot.session.value,
                captured_at=snapshot.captured_at.isoformat(), environment=snapshot.environment,
                calendar_policy=snapshot.calendar_policy, calendar_source_identity=snapshot.calendar_source_identity,
                record_count=len(snapshot.quotes), available_instruments=[dict(instrument_id=q.instrument_id, contract_code=q.contract_code) for q in sorted(snapshot.quotes,key=lambda q:q.key)],
                requested_but_unavailable=[q.as_dict() for q in sorted(snapshot.unavailable_instruments,key=lambda q:q.key)],
                content_sha256=snapshot.content_sha256, quotes_sha256=snapshot.quotes_sha256,
                files=[QUOTES_FILENAME], immutable=True, status="SEALED")


def load_intraday_snapshot(root, business_date, session, *, expected_environment=None):
    if type(business_date) is not date:
        raise IntradaySnapshotValidationError("Exact date required")
    session = MarketSession(session)
    release = _safe_path(Path(root) / RELEASES_DIRNAME / f"{business_date.isoformat()}-{session.value}")
    paths = (release / MANIFEST_FILENAME, release / QUOTES_FILENAME)
    before = tuple(identify_file(p) for p in paths) if all(p.is_file() for p in paths) else None
    manifest, rows = (_read_json(p) for p in paths)
    try:
        if manifest["schema_version"] not in {"1", SCHEMA_VERSION} or manifest["immutable"] is not True:
            raise ValueError("Not sealed")
        if manifest.get("status", "SEALED" if manifest["schema_version"] == "1" else None) != "SEALED":
            raise ValueError("Not sealed")
        if (manifest["business_date"],manifest["session"],manifest["release_id"]) != (business_date.isoformat(),session.value,release.name):
            raise ValueError("Release identity mismatch")
        quotes=[]
        for source in rows:
            row=dict(source)
            for key in ("captured_at","source_updated_at","retrieved_at"):
                if row.get(key) is not None:
                    row[key]=datetime.fromisoformat(row[key])
            row["business_date"]=date.fromisoformat(row["business_date"])
            row["source_trade_date"]=date.fromisoformat(row["source_trade_date"]) if row["source_trade_date"] else None
            row["session"]=MarketSession(row["session"])
            row["freshness_status"]=FreshnessStatus(row["freshness_status"])
            quotes.append(IntradayQuote(**row))
        unavailable=tuple(IntradayUnavailableInstrument(**{**r,"status":InstrumentAvailabilityStatus(r["status"])}) for r in manifest.get("requested_but_unavailable",[]))
        snapshot=IntradaySnapshot(business_date,session,datetime.fromisoformat(manifest["captured_at"]),tuple(quotes),
                                 manifest["calendar_policy"],manifest["calendar_source_identity"],manifest["environment"],unavailable,manifest["schema_version"])
        expected=_manifest(snapshot)
        for key in ("record_count","available_instruments","content_sha256","quotes_sha256","files"):
            if manifest.get(key)!=expected[key]:
                raise ValueError("Manifest identity mismatch:"+key)
        if content_hash(rows)!=manifest["quotes_sha256"]:
            raise ValueError("Quote hash mismatch")
        if expected_environment is not None and snapshot.environment!=expected_environment:
            raise ValueError("Snapshot environment mismatch")
        if before!=tuple(identify_file(p) for p in paths):
            raise ValueError("Snapshot changed during read")
        return snapshot
    except (KeyError,TypeError,ValueError) as exc:
        raise IntradaySnapshotValidationError("Invalid sealed snapshot") from exc


def seal_intraday_snapshot(root, snapshot: IntradaySnapshot, *, context: RuntimeContext):
    root=_safe_path(root)
    assert_runtime_write(context,root)
    if context.module_id != "shared-intraday" or snapshot.schema_version!=SCHEMA_VERSION:
        raise IntradaySnapshotValidationError("Shared runtime and new schema required")
    expected="FORMAL" if context.mode is RuntimeMode.PRODUCTION_WRITE else "TEST_ISOLATED_NON_PRODUCTION"
    if snapshot.environment!=expected:
        raise IntradaySnapshotValidationError("Runtime/snapshot environment mismatch")
    releases=assert_runtime_write(context,_safe_path(root/RELEASES_DIRNAME))
    def existing():
        prior=load_intraday_snapshot(root,snapshot.business_date,snapshot.session,expected_environment=expected)
        if prior.content_sha256!=snapshot.content_sha256 or prior.quotes_sha256!=snapshot.quotes_sha256:
            raise IntradaySnapshotConflictError("Immutable identity already has different content")
        return IntradaySealResult(SealStatus.NO_CHANGE,snapshot.release_id,prior.content_sha256,releases/snapshot.release_id)
    if (releases/snapshot.release_id).exists():
        return existing()
    def builder(directory):
        (directory/QUOTES_FILENAME).write_bytes(canonical_json([q.as_dict() for q in sorted(snapshot.quotes,key=lambda q:q.key)])+b"\n")
        (directory/MANIFEST_FILENAME).write_bytes(canonical_json(_manifest(snapshot))+b"\n")
    try:
        final,_=seal_immutable_candidate(releases,snapshot.release_id,builder)
    except FileExistsError:
        return existing()
    load_intraday_snapshot(root,snapshot.business_date,snapshot.session,expected_environment=expected)
    return IntradaySealResult(SealStatus.SEALED,snapshot.release_id,snapshot.content_sha256,final)


def load_latest_intraday_snapshot(root,session,*,expected_environment=None):
    session=MarketSession(session)
    releases=_safe_path(Path(root)/RELEASES_DIRNAME)
    dates=[]
    if releases.is_dir():
        for path in releases.glob(f"????-??-??-{session.value}"):
            dates.append(date.fromisoformat(path.name[:10]))
    if not dates:
        raise IntradaySnapshotNotFoundError("No sealed session")
    return load_intraday_snapshot(root,max(dates),session,expected_environment=expected_environment)


def quote_by_instrument(snapshot,instrument_id,contract_code=""):
    matches=[q for q in snapshot.quotes if q.key==(instrument_id,contract_code)]
    if len(matches)!=1:
        raise IntradaySnapshotNotFoundError("Exact quote unavailable")
    return matches[0]
