"""Pure business kernel for imported commodity profit research."""

from .config import (
    ContractOverrideConfig,
    ContractOverrideRule,
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
from .contract_override import (
    ContractSelectionMode,
    select_soybean_contracts,
)
from .parameter_snapshot import (
    ParameterProvenance,
    ParameterSnapshotError,
    build_parameter_snapshot,
    config_with_parameter_provenance,
    read_parameter_provenance,
)
from .mapping_snapshot import (
    MappingProvenance,
    MappingSnapshotError,
    build_mapping_snapshot,
    config_with_mapping_provenance,
    read_mapping_provenance,
)
from .override_snapshot import (
    ContractOverrideProvenance,
    ContractOverrideSnapshotError,
    build_contract_override_snapshot,
    config_with_contract_override_provenance,
    read_contract_override_provenance,
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
    "ContractOverrideConfig",
    "ContractOverrideProvenance",
    "ContractOverrideRule",
    "ContractOverrideSnapshotError",
    "ContractSelectionMode",
    "DceContract",
    "FxCurve",
    "FxCurvePoint",
    "FxSelection",
    "FxSelectionStatus",
    "ImportProfitConfigError",
    "MissingReason",
    "MappingProvenance",
    "MappingSnapshotError",
    "ParameterProvenance",
    "ParameterSnapshotError",
    "SoybeanCalculationInput",
    "SoybeanCalculationOutput",
    "SoybeanImportProfitConfig",
    "SoybeanParameters",
    "calculate_soybean_net_crush_margin",
    "build_parameter_snapshot",
    "build_mapping_snapshot",
    "build_contract_override_snapshot",
    "config_with_contract_override_provenance",
    "config_with_mapping_provenance",
    "config_with_parameter_provenance",
    "calculate_tenor_months",
    "load_soybean_config",
    "map_soybean_contracts",
    "read_parameter_provenance",
    "read_mapping_provenance",
    "read_contract_override_provenance",
    "select_soybean_contracts",
    "select_fx",
]
