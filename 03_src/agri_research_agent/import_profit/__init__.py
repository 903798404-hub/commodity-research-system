"""Pure business kernel for imported commodity profit research."""

from .config import (
    ImportProfitConfigError,
    SoybeanImportProfitConfig,
    SoybeanParameters,
    load_soybean_config,
)
from .contract_mapping import map_soybean_contracts
from .fx import calculate_tenor_months, select_fx
from .models import (
    BusinessKey,
    CalculationStatus,
    CbotContract,
    ContractMappingError,
    DceContract,
    FxCurve,
    FxCurvePoint,
    FxSelection,
    FxSelectionStatus,
    MissingReason,
)
from .soybean import (
    SoybeanCalculationInput,
    SoybeanCalculationOutput,
    calculate_soybean_net_crush_margin,
)

__all__ = [
    "BusinessKey",
    "CalculationStatus",
    "CbotContract",
    "ContractMappingError",
    "DceContract",
    "FxCurve",
    "FxCurvePoint",
    "FxSelection",
    "FxSelectionStatus",
    "ImportProfitConfigError",
    "MissingReason",
    "SoybeanCalculationInput",
    "SoybeanCalculationOutput",
    "SoybeanImportProfitConfig",
    "SoybeanParameters",
    "calculate_soybean_net_crush_margin",
    "calculate_tenor_months",
    "load_soybean_config",
    "map_soybean_contracts",
    "select_fx",
]
