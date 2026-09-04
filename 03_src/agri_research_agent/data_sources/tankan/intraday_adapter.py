"""Bounded public read_live adapter; never opens private cursors or changes allowlists."""
from dataclasses import dataclass
from datetime import date, datetime
from typing import Mapping

from .client import TankanClient
from .queries import (CBOT_SOYBEAN_LIVE_QUERY, DCE_SOYMEAL_LIVE_QUERY,
                      DCE_SOYOIL_LIVE_QUERY, USD_CNH_SPOT_LIVE_QUERY)
from agri_research_agent.market_data.intraday import (
    InstrumentAvailabilityStatus, IntradayUnavailableInstrument,
    IntradaySnapshotValidationError, validate_exact_identity, require_aware, _provenance,
)

FAMILIES = {
    ("CBOT","SOYBEAN"): (CBOT_SOYBEAN_LIVE_QUERY,"LAST","USD","US_CENTS_PER_BUSHEL","market.foreign_futures_live"),
    ("DCE","SOYMEAL"): (DCE_SOYMEAL_LIVE_QUERY,"LAST","CNY","CNY_PER_METRIC_TONNE","market.futures_live"),
    ("DCE","SOYOIL"): (DCE_SOYOIL_LIVE_QUERY,"LAST","CNY","CNY_PER_METRIC_TONNE","market.futures_live"),
    ("OTC","USD/CNH"): (USD_CNH_SPOT_LIVE_QUERY,"MID","CNH","CNH_PER_USD","market.exchange_rate_live"),
}


@dataclass(frozen=True,slots=True)
class ExactRequest:
    instrument_id: str
    contract_code: str
    exchange: str
    product: str

    def __post_init__(self):
        validate_exact_identity(self.instrument_id,self.contract_code,self.exchange,self.product)

    @property
    def key(self):
        return self.instrument_id,self.contract_code

    @property
    def source_key(self):
        return self.contract_code if self.contract_code else "USD/CNH:SPOT"


@dataclass(frozen=True,slots=True)
class SourceQuote:
    request: ExactRequest
    price: float
    source_updated_at: datetime
    retrieved_at: datetime
    source_trade_date: date | None
    quote_type: str
    currency: str
    unit: str
    source_table: str
    provenance: Mapping


@dataclass(frozen=True,slots=True)
class IntradayRead:
    requested: tuple[ExactRequest,...]
    available: tuple[SourceQuote,...]
    unavailable: tuple[IntradayUnavailableInstrument,...]
    query_evidence: tuple[Mapping,...]


def read_intraday(client: TankanClient, requested: tuple[ExactRequest,...]) -> IntradayRead:
    if not isinstance(requested,tuple) or not requested or any(not isinstance(r,ExactRequest) for r in requested):
        raise IntradaySnapshotValidationError("Explicit immutable exact requests required")
    if len({r.key for r in requested})!=len(requested):
        raise IntradaySnapshotValidationError("Duplicate requested identity")
    available,unavailable,evidence=[],[],[]
    for family,spec in FAMILIES.items():
        group=tuple(sorted((r for r in requested if (r.exchange,r.product)==family),key=lambda r:r.key))
        if not group:
            continue
        query,quote_type,currency,unit,table=spec
        result=client.read_live(query,tuple(r.source_key for r in group))
        if result.query!=query or result.plan.query_sha256!=query.sha256 or result.requested!=tuple(r.source_key for r in group):
            raise IntradaySnapshotValidationError("Provider request/proof identity mismatch")
        proof=_provenance({"query":query.identity(),"connection":result.connection_proof.safe_manifest_fields(),
                           "plan":result.plan.safe_manifest_fields(),"retrieved_at":require_aware(result.retrieved_at,"retrieved_at")})
        evidence.append(proof)
        by_key={r.source_key:r for r in group}
        seen=set()
        for row,is_available in [(r,True) for r in result.available]+[(r,False) for r in result.unavailable]:
            key=row.get("requested_identity")
            if key not in by_key or key in seen:
                raise IntradaySnapshotValidationError("Provider duplicate/unrequested identity")
            seen.add(key)
            request=by_key[key]
            if not is_available:
                if row.get("price") is not None or not row.get("reason"):
                    raise IntradaySnapshotValidationError("Unavailable price/evidence invalid")
                unavailable.append(IntradayUnavailableInstrument(request.instrument_id,request.contract_code,request.exchange,request.product,
                                   InstrumentAvailabilityStatus.CONTRACT_NOT_AVAILABLE,str(row["reason"]),proof))
                continue
            stamp=require_aware(row["source_updated_at"],"source_updated_at")
            if stamp!=row.get("update_time") or stamp>result.retrieved_at:
                raise IntradaySnapshotValidationError("Source timestamp mismatch")
            source_date=row.get("value_date") if request.exchange=="OTC" else None
            if source_date is not None and type(source_date) is not date:
                raise IntradaySnapshotValidationError("Invalid source date")
            provenance=_provenance({**proof,"ric":row.get("ric"),"source_contract":row.get("contract"),
                                    "source_trade_date_status":"PROVIDED" if source_date else "NOT_PROVIDED_BY_LIVE_TABLE"})
            available.append(SourceQuote(request,float(row["price"]),stamp,result.retrieved_at,source_date,
                                         quote_type,currency,unit,table,provenance))
        if seen!=set(by_key):
            raise IntradaySnapshotValidationError("Provider request accounting incomplete")
    return IntradayRead(tuple(sorted(requested,key=lambda r:r.key)),tuple(available),tuple(unavailable),tuple(evidence))
