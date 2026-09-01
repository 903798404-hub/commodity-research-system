"""Formal runtime Release entry for the imported-soybean profit page."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Callable
import uuid

import streamlit as st

from agri_research_agent.import_profit import (
    ImportProfitConfigError,
    ParameterSnapshotError,
    build_parameter_snapshot,
    config_with_parameter_provenance,
    load_soybean_config,
)
from agri_research_agent.import_profit.mapping_snapshot import (
    MappingSnapshotError,
    build_mapping_snapshot,
    config_with_mapping_provenance,
)
from agri_research_agent.import_profit.override_snapshot import (
    ContractOverrideSnapshotError,
    build_contract_override_snapshot,
    config_with_contract_override_provenance,
)
from agri_research_agent.import_profit.query import (
    ImportProfitQueryError,
    SoybeanQueryDataset,
    load_soybean_query_dataset,
)
from agri_research_agent.import_profit.runtime_store import (
    BUSINESS_KEYS_FILENAME,
    RELEASES_DIRNAME,
    RESULTS_FILENAME,
    SNAPSHOTS_FILENAME,
    RuntimeConcurrentUpdateError,
    RuntimeLockedError,
    RuntimeReleaseValidationError,
    RuntimeStoreError,
    RuntimeWriteError,
    resolve_current_runtime_release,
)
from agri_research_agent.pipelines.import_profit_runtime import (
    RuntimeCnfUpdate,
    RuntimePipelineError,
    update_runtime_cnf_quotes,
)
from import_profit_components import (
    CnfEditDifference,
    PageRuntimeIdentity,
    RuntimeEditorContext,
    RuntimeSaveFeedback,
)
from import_profit_page import (
    PAGE_TITLE,
    _load_config_cached,
    render_import_profit_page_from_dataset,
)
from ui_theme import inject_workspace_theme


Clock = Callable[[], datetime]
IdFactory = Callable[
    [datetime, tuple[CnfEditDifference, ...]], tuple[str, str]
]


@st.cache_resource(show_spinner=False)
def _load_runtime_dataset_cached(
    runtime_root: str,
    release_id: str,
    generation: int,
    index_sha256: str,
    business_keys_sha256: str,
    business_keys_size: int,
    business_keys_mtime_ns: int,
    snapshots_sha256: str,
    snapshots_size: int,
    snapshots_mtime_ns: int,
    results_sha256: str,
    results_size: int,
    results_mtime_ns: int,
    manual_cnf_sha256: str | None,
) -> SoybeanQueryDataset:
    """Cache only one immutable Release identity, never runtime_root alone."""

    del (
        generation,
        index_sha256,
        business_keys_sha256,
        business_keys_size,
        business_keys_mtime_ns,
        snapshots_sha256,
        snapshots_size,
        snapshots_mtime_ns,
        results_sha256,
        results_size,
        results_mtime_ns,
        manual_cnf_sha256,
    )
    release_dir = (
        Path(runtime_root) / RELEASES_DIRNAME / release_id
    )
    return load_soybean_query_dataset(
        release_dir / BUSINESS_KEYS_FILENAME,
        release_dir / SNAPSHOTS_FILENAME,
        release_dir / RESULTS_FILENAME,
    )


def render_import_profit_runtime_page(
    runtime_root: str | Path | None,
    *,
    config_path: str | Path,
    allow_cnf_save: bool = True,
    clock: Clock | None = None,
    id_factory: IdFactory | None = None,
) -> None:
    """Resolve Current once, cache its exact dataset, and render formal mode."""

    if runtime_root is None or not str(runtime_root).strip():
        _render_unavailable("运行数据尚未配置。")
        return
    root = Path(runtime_root)
    if not root.is_dir():
        _render_unavailable("进口利润运行目录不存在或不可读取。", error=True)
        return
    try:
        resolve_started = perf_counter()
        resolved = resolve_current_runtime_release(root)
        resolve_seconds = perf_counter() - resolve_started
        file_map = {item.filename: item for item in resolved.files}
        signatures = tuple(
            (
                file_map[filename].sha256,
                file_map[filename].size_bytes,
                (resolved.release_dir / filename).stat().st_mtime_ns,
            )
            for filename in (
                BUSINESS_KEYS_FILENAME,
                SNAPSHOTS_FILENAME,
                RESULTS_FILENAME,
            )
        )
        load_started = perf_counter()
        dataset = _load_runtime_dataset_cached(
            str(root),
            resolved.release_id,
            resolved.generation,
            resolved.identity.index_sha256,
            *signatures[0],
            *signatures[1],
            *signatures[2],
            resolved.identity.manual_cnf_sha256,
        )
        load_seconds = perf_counter() - load_started
        if (
            dataset.business_key_count
            != resolved.manifest["record_count"]
            or dataset.success_count
            != resolved.manifest["success_count"]
            or dataset.incomplete_count
            != resolved.manifest["incomplete_count"]
        ):
            raise RuntimeReleaseValidationError(
                "runtime dataset statistics do not match Manifest"
            )
        config_file = Path(config_path)
        config_stat = config_file.stat()
        current_config = _load_config_cached(
            str(config_file),
            config_stat.st_mtime_ns,
            config_stat.st_size,
        )
        release_parameters_available = (
            resolved.parameter_provenance.available
        )
        release_mapping_available = resolved.mapping_provenance.available
        release_contract_override_available = (
            resolved.contract_override_provenance.available
        )
        config_matches_release = (
            release_parameters_available
            and release_mapping_available
            and release_contract_override_available
            and build_parameter_snapshot(current_config).parameter_hash
            == resolved.parameter_provenance.parameter_hash
            and build_mapping_snapshot(current_config).mapping_hash
            == resolved.mapping_provenance.mapping_hash
            and build_contract_override_snapshot(
                current_config
            ).contract_override_hash
            == resolved.contract_override_provenance.contract_override_hash
        )
        parameter_config = (
            config_with_parameter_provenance(
                current_config, resolved.parameter_provenance
            )
            if release_parameters_available
            else current_config
        )
        config = (
            config_with_mapping_provenance(
                parameter_config, resolved.mapping_provenance
            )
            if release_mapping_available
            else parameter_config
        )
        config = (
            config_with_contract_override_provenance(
                config, resolved.contract_override_provenance
            )
            if release_contract_override_available
            else config
        )
    except FileNotFoundError:
        _render_unavailable("正式配置文件不存在或运行文件缺失。", error=True)
        return
    except (
        RuntimeStoreError,
        ImportProfitQueryError,
        ImportProfitConfigError,
        ParameterSnapshotError,
        MappingSnapshotError,
        ContractOverrideSnapshotError,
        OSError,
    ):
        _render_unavailable(
            "当前Release结构校验失败，未自动回退Previous。",
            error=True,
        )
        return
    identity = PageRuntimeIdentity(
        release_id=resolved.release_id,
        generation=resolved.generation,
        previous_release_id=resolved.previous_release_id,
        index_sha256=resolved.identity.index_sha256,
        manual_cnf_sha256=resolved.identity.manual_cnf_sha256,
        release_created_at=str(resolved.manifest["created_at"]),
        manual_cnf_record_count=int(
            resolved.manifest["manual_cnf_record_count"]
        ),
    )

    def save_handler(
        context: RuntimeEditorContext,
        differences: tuple[CnfEditDifference, ...],
    ) -> RuntimeSaveFeedback:
        return save_runtime_cnf_differences(
            root,
            config_path=config_file,
            config=config,
            context=context,
            differences=differences,
            previous_success_count=dataset.success_count,
            previous_incomplete_count=dataset.incomplete_count,
            clock=clock,
            id_factory=id_factory,
        )

    render_import_profit_page_from_dataset(
        dataset,
        config=config,
        runtime_identity=identity,
        allow_cnf_save=allow_cnf_save and config_matches_release,
        release_parameters_available=release_parameters_available,
        release_mapping_available=release_mapping_available,
        release_contract_override_available=(
            release_contract_override_available
        ),
        parameter_provenance=resolved.parameter_provenance,
        runtime_save_handler=save_handler,
    )
    st.caption(
        "运行Release解析耗时："
        f"{resolve_seconds:.3f}秒｜数据集加载耗时：{load_seconds:.3f}秒"
    )


def save_runtime_cnf_differences(
    runtime_root: str | Path,
    *,
    config_path: str | Path,
    config,
    context: RuntimeEditorContext,
    differences: tuple[CnfEditDifference, ...],
    previous_success_count: int,
    previous_incomplete_count: int,
    clock: Clock | None = None,
    id_factory: IdFactory | None = None,
) -> RuntimeSaveFeedback:
    """Execute at most one Stage-13 transaction for one page click."""

    if not differences:
        return RuntimeSaveFeedback(
            "no_change", "没有需要写入的业务变化。"
        )
    now = (clock or _utc_now)()
    factory = id_factory or _default_ids
    try:
        batch_id, release_id = factory(now, differences)
        updates = tuple(
            RuntimeCnfUpdate(
                business_key=item.business_key,
                cnf_cents_per_bushel=item.new_cnf,
                updated_at=now,
                batch_id=batch_id,
            )
            for item in differences
        )
        result = update_runtime_cnf_quotes(
            runtime_root,
            updates,
            config=config,
            config_path=config_path,
            expected_release_id=context.loaded_release_id,
            expected_index_sha256=context.loaded_index_sha256,
            expected_manual_cnf_sha256=(
                context.loaded_manual_cnf_sha256
            ),
            calculated_at=now,
            batch_id=batch_id,
            release_id=release_id,
            lock_timeout_seconds=10.0,
        )
        if result.status == "no_change":
            return RuntimeSaveFeedback(
                "no_change", "没有需要写入的业务变化。"
            )
        current = resolve_current_runtime_release(runtime_root)
        new_dataset = _load_runtime_dataset_cached(
            str(Path(runtime_root)),
            current.release_id,
            current.generation,
            current.identity.index_sha256,
            *(
                value
                for filename in (
                    BUSINESS_KEYS_FILENAME,
                    SNAPSHOTS_FILENAME,
                    RESULTS_FILENAME,
                )
                for value in _resolved_file_signature(current, filename)
            ),
            current.identity.manual_cnf_sha256,
        )
        return RuntimeSaveFeedback(
            "success",
            (
                f"正式更新完成：新Release {result.release_id}｜"
                f"generation {result.generation}｜"
                f"实际更新 {result.changed_count} 个业务键｜"
                f"success {previous_success_count:,}→"
                f"{new_dataset.success_count:,}｜"
                f"incomplete {previous_incomplete_count:,}→"
                f"{new_dataset.incomplete_count:,}"
            ),
        )
    except RuntimeConcurrentUpdateError:
        return RuntimeSaveFeedback("concurrent_conflict", "")
    except RuntimeLockedError:
        return RuntimeSaveFeedback("locked", "")
    except (RuntimePipelineError, ValueError):
        return RuntimeSaveFeedback(
            "validation_failed",
            "正式更新请求校验失败，请检查CNF值、业务键和保存身份。",
        )
    except (
        RuntimeWriteError,
        RuntimeReleaseValidationError,
        RuntimeStoreError,
        OSError,
    ):
        return RuntimeSaveFeedback("failed", "")
    except Exception:
        return RuntimeSaveFeedback("failed", "")


def _resolved_file_signature(
    resolved,
    filename: str,
) -> tuple[str, int, int]:
    identity = next(
        item for item in resolved.files if item.filename == filename
    )
    path = resolved.release_dir / filename
    return identity.sha256, identity.size_bytes, path.stat().st_mtime_ns


def _default_ids(
    now: datetime,
    differences: tuple[CnfEditDifference, ...],
) -> tuple[str, str]:
    if now.tzinfo is None:
        raise ValueError("保存时间必须包含UTC时区")
    timestamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    token = uuid.uuid4().hex[:10]
    return (
        f"ui-{timestamp}-{token}",
        f"{timestamp}-ui-{len(differences)}-{token}",
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _render_unavailable(message: str, *, error: bool = False) -> None:
    inject_workspace_theme()
    st.title(PAGE_TITLE)
    st.caption("正式运行模式")
    (st.error if error else st.info)(message)
