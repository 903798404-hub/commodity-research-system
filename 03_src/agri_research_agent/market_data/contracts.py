"""Immutable identities for exchange-traded instruments."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypeAlias


class Exchange(StrEnum):
    DCE = "DCE"
    CZCE = "CZCE"
    CBOT = "CBOT"
    BMD = "BMD"
    ICE = "ICE"
    EURONEXT = "EURONEXT"


class InstrumentType(StrEnum):
    DELIVERY_CONTRACT = "DELIVERY_CONTRACT"
    CONTINUOUS_MAIN = "CONTINUOUS_MAIN"


def _canonical_product(value: str) -> str:
    product = value.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", product):
        raise ValueError(f"invalid product code: {value!r}")
    return product


@dataclass(frozen=True, slots=True)
class ContractId:
    exchange: Exchange
    product: str
    year: int
    month: int

    def __post_init__(self) -> None:
        if not isinstance(self.exchange, Exchange):
            raise TypeError("exchange must be an Exchange")
        if type(self.year) is not int or not 1000 <= self.year <= 9999:
            raise ValueError("year must be a four-digit full year")
        if type(self.month) is not int or not 1 <= self.month <= 12:
            raise ValueError("month must be between 1 and 12")
        object.__setattr__(self, "product", _canonical_product(self.product))

    @property
    def instrument_type(self) -> InstrumentType:
        return InstrumentType.DELIVERY_CONTRACT

    def __str__(self) -> str:
        return f"{self.exchange}:{self.product}:{self.year:04d}-{self.month:02d}"


@dataclass(frozen=True, slots=True)
class ContinuousInstrumentId:
    exchange: Exchange
    product: str

    def __post_init__(self) -> None:
        if not isinstance(self.exchange, Exchange):
            raise TypeError("exchange must be an Exchange")
        object.__setattr__(self, "product", _canonical_product(self.product))

    @property
    def instrument_type(self) -> InstrumentType:
        return InstrumentType.CONTINUOUS_MAIN

    def __str__(self) -> str:
        return f"{self.exchange}:{self.product}:CONTINUOUS_MAIN"


InstrumentId: TypeAlias = ContractId | ContinuousInstrumentId


@dataclass(frozen=True, slots=True)
class ParsedInstrument:
    """Parser evidence; ``raw_code`` is deliberately not the instrument identity."""

    instrument: InstrumentId
    raw_code: str
    parser_name: str


class InstrumentParser(Protocol):
    def parse(self, raw_code: str, exchange: Exchange) -> ParsedInstrument: ...


class InstrumentFormatter(Protocol):
    def format(self, instrument: InstrumentId) -> str: ...


class StandardInstrumentFormatter:
    def format(self, instrument: InstrumentId) -> str:
        return str(instrument)


class ChinaFuturesSymbolParser:
    """Strict parser for four-digit delivery symbols and the ``0`` main symbol."""

    name = "china-futures-symbol-v1"
    _pattern = re.compile(r"(?P<product>[A-Za-z]+)(?P<suffix>0|\d{4})")

    def parse(self, raw_code: str, exchange: Exchange) -> ParsedInstrument:
        if exchange not in {Exchange.DCE, Exchange.CZCE}:
            raise ValueError("China futures symbols require DCE or CZCE")
        match = self._pattern.fullmatch(raw_code.strip())
        if match is None:
            raise ValueError(f"unsupported raw symbol: {raw_code!r}")
        product = match.group("product")
        suffix = match.group("suffix")
        if suffix == "0":
            instrument: InstrumentId = ContinuousInstrumentId(exchange, product)
        else:
            year = 2000 + int(suffix[:2])
            month = int(suffix[2:])
            instrument = ContractId(exchange, product, year, month)
        return ParsedInstrument(instrument=instrument, raw_code=raw_code, parser_name=self.name)


def parse_standard_instrument(value: str) -> InstrumentId:
    parts = value.split(":")
    if len(parts) != 3:
        raise ValueError(f"invalid standard instrument: {value!r}")
    exchange = Exchange(parts[0])
    product = parts[1]
    suffix = parts[2]
    if suffix == InstrumentType.CONTINUOUS_MAIN:
        return ContinuousInstrumentId(exchange, product)
    match = re.fullmatch(r"(\d{4})-(\d{2})", suffix)
    if match is None:
        raise ValueError(f"invalid standard instrument: {value!r}")
    return ContractId(exchange, product, int(match.group(1)), int(match.group(2)))
