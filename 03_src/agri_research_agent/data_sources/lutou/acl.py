"""Thin ACL that routes verified Lutou snapshots to mature domain adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable, Generic, Sequence, TypeVar

from .snapshot import LutouSnapshotError, SnapshotCapture


T = TypeVar("T")


class LutouAclRoute(StrEnum):
    WEATHER_IMPORT = "weather_import"
    BASIS = "basis"
    IMPORT_PROFIT_REUTERS = "import_profit_reuters"
    IMPORT_PROFIT_HISTORICAL_DCE = "import_profit_historical_dce"


@dataclass(frozen=True, slots=True)
class AclResult(Generic[T]):
    route: LutouAclRoute
    result: T
    captures: tuple[SnapshotCapture, ...]


_EXACT_PROVIDER_IDS = {
    LutouAclRoute.BASIS: frozenset({"lutou:oils:basis_price"}),
    LutouAclRoute.IMPORT_PROFIT_REUTERS: frozenset(
        {
            "lutou:oils:us_cbot_soybean",
            "lutou:oils:美元兑人民币历史汇率",
        }
    ),
    LutouAclRoute.IMPORT_PROFIT_HISTORICAL_DCE: frozenset(
        {"lutou:oils:内盘期货价格_收盘"}
    ),
}


class LutouSnapshotAcl:
    """Validate source identity and delegate parsing without changing its result."""

    def execute(
        self,
        route: LutouAclRoute,
        captures: Sequence[SnapshotCapture],
        adapter: Callable[[Path], T],
    ) -> AclResult[T]:
        if not isinstance(route, LutouAclRoute):
            raise TypeError("route must be LutouAclRoute")
        captured = tuple(captures)
        if not captured:
            raise LutouSnapshotError("ACL requires at least one verified capture")
        paths = {item.source_path.resolve() for item in captured}
        hashes = {item.file_identity.sha256.lower() for item in captured}
        if len(paths) != 1 or len(hashes) != 1:
            raise LutouSnapshotError("ACL captures must refer to one snapshot")
        provider_ids = {
            str(item.provenance.provider.provider_dataset_id) for item in captured
        }
        if route is LutouAclRoute.WEATHER_IMPORT:
            if any(item.asset.domain != "weather" for item in captured):
                raise LutouSnapshotError("weather ACL received a non-weather dataset")
        elif provider_ids != _EXACT_PROVIDER_IDS[route]:
            raise LutouSnapshotError("ACL datasets do not match the selected route")
        result = adapter(next(iter(paths)))
        return AclResult(route=route, result=result, captures=captured)
