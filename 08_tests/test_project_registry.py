"""Registry and startup boundaries using isolated Git fixtures, never production."""
from __future__ import annotations

import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from quality import audit_changed_scope as scope
from quality import project_registry as registry
from quality import start_project

ROOT = Path(__file__).resolve().parents[1]

# Phase 1A shared governance authorization: exact files only, no .github subtree.
ADMISSION_GOVERNANCE_FILES = [
    '.github/workflows/trusted-main-admission.yml',
    '02_configs/main_admission_result.schema.json',
    '08_tests/test_main_admission.py',
]
ADMISSION_GOVERNANCE_TESTS = ['08_tests/test_main_admission.py']

# Independently authorized Goal B registration; exact files, no directory grants.
PRODUCTION_INFRA_REGISTRATION = {'project_id': 'shared-production-infrastructure',
 'change_class': 'shared',
 'status': 'ready',
 'runtime_target': 'library_only',
 'owned_paths': ['03_src/agri_research_agent/shared/runtime_context.py',
                 '08_tests/shared/test_runtime_context.py'],
 'future_owned_paths': ['03_src/agri_research_agent/shared/production_identity.py',
                        '02_configs/production_runtime_trust.json',
                        '09_deploy/runtime_identity/说明.md',
                        '09_deploy/runtime_identity/host_authorization.py',
                        '09_deploy/runtime_identity/production_authorization.schema.json',
                        '07_docs/projects/production-runtime-v2/说明.md',
                        '07_docs/projects/production-runtime-v2/生产执行身份合同.md',
                        '08_tests/shared/test_production_identity.py',
                        '08_tests/test_host_runtime_authorization.py'],
 'shared_dependencies': ['03_src/agri_research_agent/shared/file_identity.py',
                         '03_src/agri_research_agent/automation/full_daily_windows.py',
                         '04_scripts/automation/full_daily_windows_bootstrap.py',
                         '09_deploy/spread_release/release_contract.py',
                         '04_scripts/quality/target_runtime_gate.py'],
 'forbidden_paths': ['02_configs/project_registry.json',
                     '03_src/agri_research_agent/automation',
                     '03_src/agri_research_agent/data_sources',
                     '03_src/agri_research_agent/market_data',
                     '03_src/agri_research_agent/pipelines',
                     '03_src/agri_research_agent/import_profit',
                     '03_src/agri_research_agent/summary_engine',
                     '03_src/agri_research_agent/alerts',
                     '04_scripts',
                     '05_apps',
                     '09_deploy/spread_release',
                     'Dockerfile',
                     '.dockerignore',
                     'docker-compose.yml',
                     'requirements.txt',
                     'requirements.in',
                     'AGENTS.md'],
 'required_tests': ['08_tests/test_project_registry.py',
                    '08_tests/test_quality_controls.py',
                    '08_tests/test_documentation_contract.py',
                    '08_tests/shared/test_runtime_context.py',
                    '08_tests/pipelines/test_full_daily_windows_wrapper.py'],
 'future_required_tests': ['08_tests/shared/test_production_identity.py',
                           '08_tests/test_host_runtime_authorization.py'],
 'capabilities': ['versioned explicit git_worktree / oci_container execution identity and Approved '
                  'Commit/Tree binding',
                  'trusted host authorization verifier, signed evidence and protected read-only '
                  'injection contract',
                  'distinct artifact origin, deployment role and production write grant; '
                  'candidate-validation isolation',
                  'additive RuntimeContext integration preserving runtime marker, readonly and path '
                  'boundaries'],
 'boundary_notes': 'PROD-RUNTIME-V2 Goal '
                   'B明确授权。library_only表示本项目交付跨Git/OCI复用的身份库及宿主验证工具，不是可独立部署的业务容器；真实container '
                   'deployability必须由后续Goal '
                   'D实现和验证，本登记不宣称容器PASS。只分配精确文件，没有已有owner转移或目录扩权。当前RuntimeContext及其测试此前无project '
                   'owner；global protected保持。trust配置仅允许公开verification keys/key identity，不允许private '
                   'key或secret。业务进程不能签发production grant；禁止Docker socket、伪造.git或identity '
                   'fallback。当前root/writable-rootfs生产容器不自动取得v2写权限；容器hardening和正式接线在后续独立项目完成。FULL DAILY '
                   'bootstrap/Wrapper只读，行为不得削弱；Soybean/Shared '
                   'Intraday/Tankan/Weather/Basis/Notification业务逻辑、旧Wiring '
                   'worktree、旧SEALED和生产数据禁止修改。不部署、不capture、不新增schedule，不提升Approved identity。'}

# Exact Goal C registration from the reviewed external record, embedded so
# tests do not depend on a machine-local attachment.
RUNTIME_MANIFEST_REGISTRATION = {
 'project_id':'shared-runtime-manifest','change_class':'shared','status':'ready','runtime_target':'library_only','owned_paths':[],
 'future_owned_paths':['03_src/agri_research_agent/shared/runtime_manifest.py','02_configs/runtime_manifest.schema.json','08_tests/shared/test_runtime_manifest.py','07_docs/projects/生产RuntimeManifest合同.md'],
 'shared_dependencies':['04_scripts/quality/target_runtime_gate.py','03_src/agri_research_agent/shared/production_identity.py','09_deploy/runtime_identity/host_authorization.py'],
 'forbidden_paths':['02_configs/project_registry.json','02_configs/production_runtime_trust.json','03_src/agri_research_agent/shared/runtime_context.py','03_src/agri_research_agent/shared/production_identity.py','03_src/agri_research_agent/automation','03_src/agri_research_agent/data_sources','03_src/agri_research_agent/market_data','03_src/agri_research_agent/pipelines','03_src/agri_research_agent/import_profit','03_src/agri_research_agent/summary_engine','03_src/agri_research_agent/alerts','04_scripts','05_apps','09_deploy','Dockerfile','.dockerignore','docker-compose.yml','requirements.txt','requirements.in','AGENTS.md','07_docs/00_文档索引与适用范围.md'],
 'required_tests':['08_tests/test_project_registry.py','08_tests/test_quality_controls.py','08_tests/test_documentation_contract.py'],'future_required_tests':['08_tests/shared/test_runtime_manifest.py'],
 'capabilities':['versioned machine-readable source runtime manifest schema and shared validator','explicit production entrypoint, initialization commands and controlled source inputs','runtime path roles, identity-root binding, environment/secret references and dependency requirements','production/preview policy and deployment-instance/source separation'],
 'boundary_notes':'PROD-RUNTIME-V2 Goal C独立授权。library_only表示通用schema/validator源码交付，不是独立业务容器；不宣称真实container deployability。仅新增四个精确文件，没有ownership转移或目录扩权。源码manifest不得存放host绝对路径、当前container/image identity或secret值；实例值由受保护deployment evidence提供。明确区分实际entrypoint与初始化验证命令，不把--help或单纯import作为完整部署证明。版本演进必须明确，旧合同不得被隐式重解释；quality gate与host identity消费者的适配由各自合法owner在后续独立阶段实施。当前只交付清单，不修改治理入口、身份库、Docker/Compose、信任公钥或任何业务模块。FULL DAILY/Wrapper只读不改，旧Wiring保持PARKED；不部署、不capture、不修改旧SEALED、不启用Notification或任何schedule、不提升Approved identity。'
}

# Goal B grant-contract extension: preserve the original B registration and
# apply only the two exact future files, one future test, and two read-only
# dependencies authorized by the review record.
GRANT_CONTRACT_REGISTRATION = copy.deepcopy(PRODUCTION_INFRA_REGISTRATION)
GRANT_CONTRACT_REGISTRATION['future_owned_paths'] += [
    '03_src/agri_research_agent/shared/production_grant.py',
    '08_tests/shared/test_production_grant.py']
GRANT_CONTRACT_REGISTRATION['shared_dependencies'] += [
    '03_src/agri_research_agent/shared/runtime_manifest.py',
    '02_configs/runtime_manifest.schema.json']
GRANT_CONTRACT_REGISTRATION['future_required_tests'] += ['08_tests/shared/test_production_grant.py']
GRANT_CONTRACT_REGISTRATION['boundary_notes'] += ' 后续独立v2 bridge仅增加production_grant结构解析器和测试两个精确文件；既有runtime_manifest解析器及Schema为只读依赖，无ownership转移。宿主与容器共用完整grant字段/类型/时间/角色结构校验；保留JSON Schema作为一致性测试，不因宿主缺少jsonschema而删除任何校验。新host policy/grant版本必须显式分派，候选临时性以受保护host scope和实际mount来源证明，不能依赖容器路径名或fallback。既有trust配置仅可登记受保护宿主生成的公开验证密钥，私钥不得进入Git或镜像；登记不等于已配置或签发production授权。'

# Goal D deployability-engine registration is an exact extension of the
# reviewed B grant contract. Removing the broad scripts prohibition is only
# necessary because ownership remains an exact future file, never a directory.
DEPLOYABILITY_ENGINE_REGISTRATION = copy.deepcopy(GRANT_CONTRACT_REGISTRATION)
DEPLOYABILITY_ENGINE_REGISTRATION['future_owned_paths'] += [
    '04_scripts/runtime/validate_target_runtime.py',
    '08_tests/test_target_runtime_validator.py',
    '07_docs/projects/production-runtime-v2/目标RuntimeDeployabilityGate.md']
DEPLOYABILITY_ENGINE_REGISTRATION['future_required_tests'] += [
    '08_tests/test_target_runtime_validator.py']
DEPLOYABILITY_ENGINE_REGISTRATION['forbidden_paths'].remove('04_scripts')
marker = DEPLOYABILITY_ENGINE_REGISTRATION['forbidden_paths'].index('03_src/agri_research_agent/automation') + 1
DEPLOYABILITY_ENGINE_REGISTRATION['forbidden_paths'][marker:marker] = [
    '04_scripts/automation', '04_scripts/environment', '04_scripts/import_profit',
    '04_scripts/notifications', '04_scripts/quality', '04_scripts/soybean_crop_progress',
    '04_scripts/soybean_exports', '04_scripts/weather']
DEPLOYABILITY_ENGINE_REGISTRATION['capabilities'] += [
    'candidate-bound static and actual target runtime validation with machine evidence',
    'isolated Linux exact-image Compose and negative deployability probes']
DEPLOYABILITY_ENGINE_REGISTRATION['boundary_notes'] += ' Goal D独立Deployability Engine仅分配三个精确未来文件，并为该精确测试增加Completion要求；移除04_scripts目录级forbidden仅为容纳已列明的单个engine文件，不授予任何其他脚本ownership。Engine必须从clean committed candidate重新建立source/image/Compose绑定，在真实隔离Linux Docker执行全部13项probe，验证.git缺失、不可变Image ID、OCI revision/tree、实际Compose/mount/env/dependency与candidate-only scope；禁止production volume/secret、业务Docker socket、caller evidence、mutable tag或dev worktree输入。BLOCKED只允许真实LINUX_BUILDER_UNAVAILABLE，任何检查缺失、错误镜像、错误Commit/Tree/service/manifest、Preview写入或release不一致均FAIL。该登记不创建业务runtime contract、不修改Docker/Compose/业务模块、不签发production grant、不部署或提升Approved identity。'

# Goal D pre-release extension: five exact future files and two completion tests.
PRE_RELEASE_REGISTRATION = copy.deepcopy(DEPLOYABILITY_ENGINE_REGISTRATION)
PRE_RELEASE_REGISTRATION['future_owned_paths'] += [
    '09_deploy/runtime_identity/candidate_validation_record.py',
    '04_scripts/runtime/pre_release_runtime.py',
    '08_tests/test_candidate_validation_record.py',
    '08_tests/test_pre_release_runtime.py']
PRE_RELEASE_REGISTRATION['future_required_tests'] += [
    '08_tests/test_candidate_validation_record.py',
    '08_tests/test_pre_release_runtime.py']
PRE_RELEASE_REGISTRATION['capabilities'] += [
    'typed candidate validation record and fail-closed pre-release same-image revalidation']
PRE_RELEASE_REGISTRATION['boundary_notes'] += ' Goal D pre-release extension adds only four exact future files and two completion tests; extend the existing target runtime deployability contract document rather than creating a parallel project contract; existing ownership, protected paths, forbidden paths and other projects remain unchanged. The candidate_validation_record parser verifies a typed signed validation artifact, never caller authority. The root-only pre_release_runtime entry must itself run the exact protected clean candidate validator and sign only its successful observed result with the existing candidate-validation trust domain; no CLI imports caller-crafted raw evidence or claims Image ID/probe results. Pre-release consumes that record, revalidates Approved Commit/Tree and source bindings, observes the same immutable image and actual production Compose/environment/mounts, and fails on undeclared differences or drift. Candidate and production rendered Compose identities are recorded separately and checked against the same manifest roles, not equated by pathname or ignored. Reuse existing protected host policy as Approved object and existing production key for execution grants; no new approval-key hierarchy. Production manifest/2 requires policy/3 with validated candidate record; policy/2 remains candidate-only, legacy policy/1 and Git/FULL DAILY remain compatible, execution grant/2 remains unchanged. Host signing rechecks record and actual instance before and after authorization. Record parsing rejects duplicate keys, non-finite values, wrong purpose/domain/signature, revocation, expiry and identity mismatch; protected outputs are exclusive, never overwritten. Registration is not runtime implementation or production approval: no production grant is issued, no deployment/start/capture, no secret access, no business or SEALED mutation, no schedule, FULL DAILY integration or Notification activation. Any later implementation is a new independent shared branch/worktree from this registration on authoritative main.'

# Exact reviewed PM registration delta; no directory or shared ownership grant.
CONTRACT_SOURCE_ALLOCATION = copy.deepcopy(PRE_RELEASE_REGISTRATION)
CONTRACT_SOURCE_ALLOCATION['future_owned_paths'] += [
    '02_configs/runtime_contracts/spread-production-runtime.json']
CONTRACT_SOURCE_ALLOCATION['boundary_notes'] += ' Production runtime contract bootstrap allocates only 02_configs/runtime_contracts/spread-production-runtime.json as an exact future source-contract file. In a subsequent independent infrastructure branch from approved main, create a valid runtime-manifest/3 for project_id spread-production-runtime-wiring using existing tracked deployment/source inputs. This stage does not own or modify Dockerfile, Compose, business sources or production data and does not claim target container deployability. The infrastructure project remains a library and host-tool project; the future wiring project is not registered under a false runtime target. Before wiring development, a separate dev-governance change must atomically transfer this manifest ownership and register the actual production_container project with its existing valid contract and exact deployment paths. No pending target, missing-contract exception, production approval, deployment, capture, schedule, FULL DAILY integration or Notification activation is authorized by this allocation.'

# The bootstrap allocation remains historical evidence.  The wiring registration
# transfers its one exact manifest and leaves all infrastructure tests intact.
POST_TRANSFER_INFRA_REGISTRATION = copy.deepcopy(CONTRACT_SOURCE_ALLOCATION)
POST_TRANSFER_INFRA_REGISTRATION['future_owned_paths'].remove(
    '02_configs/runtime_contracts/spread-production-runtime.json')
POST_TRANSFER_INFRA_REGISTRATION['boundary_notes'] += ' The spread source-contract bootstrap is complete. Ownership of its one exact manifest is transferred to spread-production-runtime-wiring by an independent governance stage; all remaining infrastructure ownership and required tests stay unchanged. The new project carries the real production_container target and must pass its own target deployability gate.'

SPREAD_RUNTIME_WIRING_REGISTRATION = {
 'project_id': 'spread-production-runtime-wiring', 'change_class': 'shared',
 'status': 'ready', 'runtime_target': 'production_container',
 'runtime_contract': '02_configs/runtime_contracts/spread-production-runtime.json',
 'owned_paths': [],
 'future_owned_paths': ['02_configs/runtime_contracts/spread-production-runtime.json',
                        '04_scripts/capture_public_intraday.py',
                        '09_deploy/spread_runtime/Dockerfile.spread-runtime',
                        '09_deploy/spread_runtime/compose.yml',
                        '09_deploy/spread_runtime/说明.md',
                        '04_scripts/runtime/spread_runtime_preflight.py',
                        '08_tests/test_spread_runtime_contract.py',
                        '08_tests/fixtures/spread_runtime/public_current.json',
                        '08_tests/fixtures/spread_runtime/public_release_manifest.json',
                        '08_tests/fixtures/spread_runtime/domestic_spread_database.parquet',
                        '08_tests/fixtures/spread_runtime/soybean_runtime_release_index.json',
                        '08_tests/fixtures/spread_runtime/soybean_runtime_release_manifest.json',
                        '08_tests/fixtures/spread_runtime/soybean_runtime_business_keys.parquet',
                        '08_tests/fixtures/spread_runtime/soybean_runtime_market_snapshots.parquet',
                        '08_tests/fixtures/spread_runtime/soybean_runtime_net_crush_results.parquet',
                        '08_tests/fixtures/spread_runtime/soybean_runtime_quality_report.json',
                        '08_tests/fixtures/spread_runtime/soybean_runtime_manual_cnf_quotes.parquet',
                        '08_tests/fixtures/spread_runtime/historical_cnf_cache.parquet',
                        '08_tests/fixtures/spread_runtime/shared_intraday_snapshot_manifest.json',
                        '08_tests/fixtures/spread_runtime/shared_intraday_snapshot_quotes.json'],
 'shared_dependencies': ['Dockerfile', 'docker-compose.yml', '.dockerignore', 'requirements.txt',
                         '05_apps/streamlit_app.py', '05_apps/import_profit_intraday_runtime_page.py',
                         '05_apps/import_profit_intraday_page.py',
                         '03_src/agri_research_agent/shared/runtime_context.py',
                         '03_src/agri_research_agent/shared/production_identity.py',
                         '03_src/agri_research_agent/shared/production_grant.py',
                         '03_src/agri_research_agent/shared/runtime_manifest.py',
                         '02_configs/runtime_manifest.schema.json', '02_configs/production_runtime_trust.json',
                         '02_configs/import_profit_soybean.yaml',
                         '03_src/agri_research_agent/market_data/activated_runtime.py',
                         '03_src/agri_research_agent/application/domestic_spreads.py',
                         '03_src/agri_research_agent/market_data/intraday.py',
                         '03_src/agri_research_agent/market_data/calendars.py',
                         '03_src/agri_research_agent/import_profit/runtime_store.py',
                         '03_src/agri_research_agent/import_profit/cnf_store.py',
                         '03_src/agri_research_agent/import_profit/intraday_store.py',
                         '03_src/agri_research_agent/pipelines/public_intraday.py',
                         '04_scripts/runtime/validate_target_runtime.py',
                         '04_scripts/runtime/pre_release_runtime.py',
                         '09_deploy/runtime_identity/host_authorization.py'],
 'forbidden_paths': ['02_configs/project_registry.json', '03_src', '05_apps',
                     '04_scripts/automation', '04_scripts/environment', '04_scripts/quality',
                     '04_scripts/import_profit', '04_scripts/notifications',
                     '04_scripts/refresh_public_data.py', '09_deploy/runtime_identity',
                     '09_deploy/spread_release', '09_deploy/public_intraday_runtime',
                     'Dockerfile', 'docker-compose.yml', '.dockerignore', 'requirements.txt',
                     'requirements.in'],
 'required_tests': ['08_tests/test_project_registry.py', '08_tests/test_quality_controls.py',
                    '08_tests/test_documentation_contract.py',
                    '08_tests/shared/test_runtime_context.py',
                    '08_tests/shared/test_production_identity.py',
                    '08_tests/test_target_runtime_validator.py',
                    '08_tests/test_public_intraday_capture.py',
                    '08_tests/market_data/test_intraday.py',
                    '08_tests/pipelines/test_public_intraday.py',
                    '08_tests/test_import_profit_intraday_page.py',
                    '08_tests/pipelines/test_full_daily_windows_wrapper.py'],
 'future_required_tests': ['08_tests/test_spread_runtime_contract.py'],
 'capabilities': ['full spread-dashboard production runtime manifest and exact deployment source wiring',
                  'explicit Git/OCI capture CLI identity initialization without changing capture semantics',
                  'candidate-only immutable fixtures and real readonly Domestic Spread, Shared Intraday consumer and Soybean STRICT_RUNTIME initialization',
                  'candidate-bound Linux container deployability before closure and same-image production promotion'],
 'boundary_notes': 'PROD-RUNTIME-V2 Goals E/F authorize an independent shared wiring project from the source-contract stage on authoritative main. Atomically transfer only the existing spread manifest and capture CLI; all new ownership is exact files, without directory or reserved namespace authority. Keep the full 05_apps/streamlit_app.py entrypoint and service spread-dashboard; no fourth or narrowed production service. Root Dockerfile/Compose remain read-only historical deployment sources; the wiring stage must bind the manifest to its new exact Dockerfile.spread-runtime and compose.yml and actual rendered deployment configuration. Registration is not runtime implementation, container PASS, Production Approval or deployment. New fixtures are synthetic Git-tracked candidate-only inputs, excluded from image COPY, hash-bound and seeded only into protected temporary read-only child mounts; never promote them to production data. Production roots require real provenance and physical/identity separation from Preview, dev/feature/old PM worktrees and temporary fixtures. Keep PAGE_MODE=STRICT_RUNTIME, ENVIRONMENT=FORMAL, current business-date behavior and Tankan secret-file contract; do not enable CNF save or configure write authorization just for smoke. Capture CLI changes are limited to explicit execution identity selection and initialization; no fallback, query/model/formula/snapshot changes. Test real CLI identity initialization, strict page and immutable snapshot consumer, dependencies, mounts, missing/mismatched identities and Preview write rejection in the actual clean Linux target image before code closure; no import-only or empty-directory substitute. Temporal capture is not a deployability prerequisite and no historical recapture or old SEALED mutation is allowed. AM_PM_AUTO_EXECUTION=NO; INTRADAY_FULL_DAILY_DEPENDENCY=NONE; NOTIFICATION_AUTO_EXECUTION=NO; REAL_AM_TEMPORAL_ACCEPTANCE=DEFERRED; REAL_PM_TEMPORAL_ACCEPTANCE=DEFERRED. All production-container Completion and pre-release evidence binding remain mandatory. Registry grants no production data writes or scheduling; production release follows the separately authorized whole-main audit and same validated Image ID workflow.'}


PM_EXISTING_ADDITIONS = [
    '03_src/agri_research_agent/pipelines/import_profit_daily.py',
    '03_src/agri_research_agent/pipelines/import_profit_results.py',
    '03_src/agri_research_agent/pipelines/import_profit_runtime.py',
    '07_docs/projects/进口商品利润研究框架契约.md',
]
PM_FUTURE_TESTS = ['08_tests/test_import_profit_' + name + '.py' for name in (
    'contract_override', 'historical_margin', 'intraday', 'intraday_history',
    'intraday_page', 'mapping_snapshot', 'parameter_snapshot',
    'public_market_input_adapter', 'scenario')]
PM_FUTURE_PATHS = [
    '03_src/agri_research_agent/pipelines/soybean_intraday.py',
    '05_apps/import_profit_intraday_page.py',
    '05_apps/import_profit_intraday_runtime_page.py',
] + PM_FUTURE_TESTS
PM_EXISTING_TEST_ADDITIONS = ['08_tests/test_import_profit_' + name + '.py' for name in (
    'cnf_repricing', 'components', 'config', 'contract', 'contract_mapping',
    'daily_increment', 'daily_pipeline', 'dce_candidate', 'dce_daily',
    'historical_candidate', 'historical_dce_adapter', 'historical_recalculation',
    'historical_results_candidate', 'morning_external_inputs', 'page', 'query',
    'recalculation', 'runtime_pipeline', 'runtime_store', 'soybean', 'standard_io')]


PUBLIC_INTRADAY_RUNTIME_REGISTRATION = {'project_id': 'public-intraday-runtime',
 'change_class': 'shared',
 'status': 'ready',
 'runtime_target': 'none',
 'owned_paths': [],
 'future_owned_paths': ['02_configs/runtime_contracts/public-intraday-runtime.json',
                        '04_scripts/runtime/public_intraday_runtime.py',
                        '09_deploy/public_intraday_runtime/Dockerfile.public-intraday',
                        '09_deploy/public_intraday_runtime/compose.yml',
                        '08_tests/test_public_intraday_runtime_v2.py',
                        '07_docs/projects/public-intraday-runtime/运行合同.md'],
 'shared_dependencies': ['.dockerignore',
                         'requirements.txt',
                         '04_scripts/capture_public_intraday.py',
                         '03_src/agri_research_agent/market_data/intraday.py',
                         '03_src/agri_research_agent/market_data/calendars.py',
                         '03_src/agri_research_agent/data_sources/tankan/client.py',
                         '03_src/agri_research_agent/data_sources/tankan/intraday_adapter.py',
                         '03_src/agri_research_agent/pipelines/public_intraday.py',
                         '03_src/agri_research_agent/shared/runtime_context.py',
                         '03_src/agri_research_agent/shared/production_identity.py',
                         '03_src/agri_research_agent/shared/production_grant.py',
                         '03_src/agri_research_agent/shared/runtime_manifest.py',
                         '02_configs/runtime_manifest.schema.json',
                         '04_scripts/runtime/validate_target_runtime.py',
                         '09_deploy/runtime_identity/host_authorization.py'],
 'forbidden_paths': ['02_configs/project_registry.json',
                     'Dockerfile',
                     'docker-compose.yml',
                     '03_src/agri_research_agent/import_profit',
                     '03_src/agri_research_agent/automation',
                     '03_src/agri_research_agent/summary_engine',
                     '04_scripts/import_profit',
                     '04_scripts/automation',
                     '05_apps'],
 'required_tests': ['08_tests/pipelines/test_public_intraday.py',
                    '08_tests/test_public_intraday_capture.py'],
 'future_required_tests': ['08_tests/test_public_intraday_runtime_v2.py'],
 'capabilities': ['explicit OCI production identity wiring for the shared-intraday module',
                  'candidate-safe read-only initialization without Tankan secret access or capture',
                  'separate snapshot, input, identity and evidence runtime roots',
                  'manual explicit-date AM/PM capture entrypoint with no scheduler or FULL DAILY '
                  'integration'],
 'boundary_notes': 'PROD-RUNTIME-V2 Goal E/F staged registration. runtime_target=none is a '
                   'temporary governance state while exact future files are absent; it grants no '
                   'deployment or production authority. A later independent dev-governance phase '
                   'may promote this record to production_container only after the manifest and '
                   'all bound inputs exist and the exact candidate passes isolated Linux target '
                   'validation. The project may add only the listed exact wiring files and treats '
                   'existing Shared Intraday, Tankan and identity code as read-only dependencies. '
                   'It must not change business logic, use the parked Soybean worktree, fix a '
                   'business date, read a production secret during candidate validation, capture '
                   'data, mutate old SEALED snapshots, add schedules, join FULL DAILY/Production '
                   'Wrapper/Daily Summary, activate Notification, issue a production grant or '
                   'deploy. AM_PM_AUTO_EXECUTION=NO; INTRADAY_FULL_DAILY_DEPENDENCY=NONE; '
                   'REAL_AM_TEMPORAL_ACCEPTANCE=DEFERRED; REAL_PM_TEMPORAL_ACCEPTANCE=DEFERRED.'}

PUBLIC_INTRADAY_RUNTIME_PRODUCTION_REGISTRATION = copy.deepcopy(
    PUBLIC_INTRADAY_RUNTIME_REGISTRATION)
PUBLIC_INTRADAY_RUNTIME_PRODUCTION_REGISTRATION.update(
    runtime_target='production_container',
    runtime_contract='02_configs/runtime_contracts/public-intraday-runtime.json',
    owned_paths=PUBLIC_INTRADAY_RUNTIME_REGISTRATION['future_owned_paths'],
    future_owned_paths=[],
    required_tests=[*PUBLIC_INTRADAY_RUNTIME_REGISTRATION['required_tests'],
                    '08_tests/test_public_intraday_runtime_v2.py'],
    future_required_tests=[],
    boundary_notes='PROD-RUNTIME-V2 Goal E/F production-container promotion. The exact manifest, '
                   'wrapper, Dockerfile, Compose, test and runtime documentation are closed source '
                   'inputs and project-owned. Promotion requires isolated Linux target-runtime '
                   'validation of the exact candidate before main. It grants no automatic execution, '
                   'deployment or capture authority. Existing Shared Intraday, Tankan and identity '
                   'code remain read-only dependencies. It must not change business logic, use the '
                   'parked Soybean worktree, fix a business date, read a production secret during '
                   'candidate validation, capture data, mutate old SEALED snapshots, add schedules, '
                   'join FULL DAILY/Production Wrapper/Daily Summary, activate Notification or issue '
                   'a production grant. AM_PM_AUTO_EXECUTION=NO; '
                   'INTRADAY_FULL_DAILY_DEPENDENCY=NONE; '
                   'REAL_AM_TEMPORAL_ACCEPTANCE=DEFERRED; REAL_PM_TEMPORAL_ACCEPTANCE=DEFERRED.')

PRODUCTION_INPUT_AUTHORITY_REGISTRATION = {'project_id': 'soybean-production-input-authority',
 'change_class': 'shared',
 'status': 'ready',
 'runtime_target': 'library_only',
 'owned_paths': [],
 'future_owned_paths': ['09_deploy/production_inputs/asset_tool.py',
                        '09_deploy/production_inputs/historical_asset.schema.json',
                        '09_deploy/production_inputs/cnf_asset.schema.json',
                        '09_deploy/production_inputs/approval.schema.json',
                        '09_deploy/production_inputs/README.md',
                        '08_tests/test_production_input_assets.py'],
 'shared_dependencies': ['03_src/agri_research_agent/import_profit/runtime_store.py',
                         '03_src/agri_research_agent/import_profit/cnf_store.py',
                         '03_src/agri_research_agent/data_sources/tankan/client.py',
                         '04_scripts/import_profit/build_final_ui_preview_cnf_cache.py',
                         '02_configs/runtime_contracts/spread-production-runtime.json'],
 'forbidden_paths': ['02_configs/project_registry.json',
                     '03_src',
                     '05_apps',
                     '04_scripts',
                     '09_deploy/runtime_identity',
                     '09_deploy/spread_runtime',
                     '09_deploy/spread_release',
                     'Dockerfile',
                     'docker-compose.yml',
                     '.dockerignore',
                     'requirements.txt'],
 'required_tests': [],
 'future_required_tests': ['08_tests/test_production_input_assets.py'],
 'capabilities': ['byte-preserving formal historical runtime initialization from explicitly '
                  'approved immutable source',
                  'production Tankan historical CNF extraction and asset provenance validation',
                  'protected host-side approval and publication with separate extraction and asset '
                  'approval evidence'],
 'boundary_notes': 'User Goal SOYBEAN-PRODUCTION-INPUT-AUTHORITY authorizes this independent '
                   'host-tool project, not a production-container application or a Runtime V2 '
                   'change. Own only six exact new files; no directory/reserved ownership or '
                   'transfer. Preserve existing stage-a source bytes and historical values; '
                   'initialize a new dated formal release without retroactive approval or '
                   'recalculation. Keep Preview producer, existing cache schema/natural key, '
                   'manual_ui BusinessKey, NULL versus zero, AM/PM CNF identity, page/API/Compose '
                   'interface and all business code unchanged. Reuse immutable validated '
                   'application image 47fe35a; host producer Commit/Tree is separately identified. '
                   'Extraction approval and exact resulting asset approval are distinct protected '
                   'evidence states; never treat extracted or Preview/synthetic assets as approved '
                   'production. Root-controlled exclusive candidate/publication paths, '
                   'source/manifest/payload SHA, explicit approved destinations and readonly '
                   'consumers are mandatory. Only direct asset/schema/production-preview rejection '
                   'and consumer compatibility tests; no automatic full regression, image rebuild, '
                   'AM/PM capture, SEALED mutation, FULL DAILY integration or Notification '
                   'activation. Registration does not publish assets or issue runtime production '
                   'grants; actual operations require the separate user task authorization and '
                   'explicit result approval evidence.'}


XIAORAN_PRODUCTION_DATA_DELIVERY_REGISTRATION = {
    'project_id': 'xiaoran-production-data-delivery', 'change_class': 'shared', 'status': 'ready',
    'owned_paths': [],
    'future_owned_paths': [
        '03_src/agri_research_agent/automation/production_data_delta.py',
        '04_scripts/automation/run_production_data_delta_windows.py',
        '09_deploy/production_data_delivery/delta_contract.schema.json',
        '09_deploy/production_data_delivery/activate_production_data_delta.py',
        '09_deploy/production_data_delivery/README.md',
        '08_tests/test_production_data_delta.py',
        '08_tests/test_production_data_delta_activation.py'],
    'shared_dependencies': [
        '04_scripts/server_update_spreads.py', '04_scripts/update_price_long_from_akshare.py',
        '04_scripts/calculate_historical_spreads.py',
        '04_scripts/soybean_crop_progress/update_soybeans_crop_weekly.py',
        '04_scripts/soybean_exports/run_fas_export_sales.py', '04_scripts/transfer_public_data_package.py',
        '03_src/agri_research_agent/pipelines/public_data_delivery.py',
        '03_src/agri_research_agent/automation/full_daily_windows.py'],
    'forbidden_paths': [
        '02_configs/project_registry.json', '05_apps', '04_scripts/refresh_public_data.py',
        '04_scripts/capture_public_intraday.py', '09_deploy/runtime_identity',
        '03_src/agri_research_agent/shared', '03_src/agri_research_agent/import_profit',
        '03_src/agri_research_agent/pipelines/public_data_daily.py',
        '03_src/agri_research_agent/pipelines/public_data_refresh.py'],
    'required_tests': ['08_tests/test_project_registry.py', '08_tests/test_quality_controls.py',
                       '08_tests/test_documentation_contract.py'],
    'future_required_tests': ['08_tests/test_production_data_delta.py',
                              '08_tests/test_production_data_delta_activation.py'],
    'capabilities': [
        'Xiaoran execution of approved unchanged AkShare, crop progress and FAS producers',
        'Read-only production baseline transfer and exact producer Commit/Tree and data provenance',
        'Existing public-package publication for the domestic-spread consumer artifact',
        'Strict host-validated crop and FAS delta publication with backup and rollback',
        'Auditable local scheduling replacing explicitly selected legacy host cron entries'],
    'runtime_target': 'windows_git_worktree',
    'boundary_notes': 'User explicitly authorized deployment-contract repair, a new validated image and migration of the affected legacy updates to Xiaoran. Only these exact infrastructure files are owned. Run unchanged business producers from an independent approved clean detached local checkout with explicitly hashed baseline replicas; never use canonical main, dev/Preview assets or old runtime worktrees. AkShare must publish the domestic-spread artifact through the existing public package contract while preserving every current public dataset; legacy root-file writes alone are not consumer activation. Crop and FAS use an independent strict delta contract, host policy, exact path allowlists, byte/schema validation, baseline drift checks and rollback; no server provider execution or Tailscale. Credentials remain local secret files and never enter manifests or transfers. New scheduling and old-cron replacement must bind approved code, allocation and rollback evidence under the explicit migration authorization. Do not change business formulas, CNF/manual-ui keys, FULL DAILY code or source approval, capture AM/PM, existing SEALED, notifications or USDA/Oil World services. Registration does not claim implementation, data validity, tested scheduling or deployment completion.'}


WINDOWS_WRAPPER_PLATFORM_REGISTRATION = {'project_id': 'windows-wrapper-platform',
 'change_class': 'shared',
 'status': 'ready',
 'runtime_target': 'windows_git_worktree',
 'owned_paths': ['03_src/agri_research_agent/automation/full_daily_windows.py',
                 '08_tests/pipelines/test_full_daily_windows_wrapper.py'],
 'shared_dependencies': ['04_scripts/automation/full_daily_windows_bootstrap.py',
                         '04_scripts/automation/run_full_daily_windows.py',
                         '04_scripts/automation/run_full_daily_windows.ps1',
                         '04_scripts/quality/main_admission.py',
                         '04_scripts/quality/platform_test_plan.py',
                         '04_scripts/quality/test_platforms.json'],
 'forbidden_paths': ['02_configs/project_registry.json', '04_scripts/quality', '05_apps', '09_deploy'],
 'required_tests': ['08_tests/pipelines/test_full_daily_windows_wrapper.py',
                    '08_tests/test_project_registry.py',
                    '08_tests/test_quality_controls.py',
                    '08_tests/test_documentation_contract.py'],
 'capabilities': ['Windows Wrapper source and platform-correct required test fixtures; no deployment or '
                  'production update authorization'],
 'boundary_notes': 'Shared ownership registration only. The exact existing Wrapper source and test were '
                   'previously unowned. Runtime target is Windows; registration does not execute FULL DAILY, '
                   'install tools, change production identity, or authorize production writes. Hosted '
                   'planner, workflow and test-platform policy remain with dev-governance. Test transport '
                   'fixtures must isolate tool discovery; genuine Windows filesystem/path/lock tests remain '
                   'required on official Windows hosted runners. No required test removal or '
                   'skip-as-success. All data/output/log paths are outside these two exact owned files and '
                   'remain unauthorized, including Git-ignored 01_data, 06_outputs and 10_logs; no runtime '
                   'directories are created for registration.'}

RELEASE_REFRESH_REGISTRATION = {
    'project_id': 'release-refresh',
    'change_class': 'shared', 'status': 'ready', 'runtime_target': 'library_only',
    'owned_paths': ['08_tests/test_release_refresh.py'],
    'shared_dependencies': [
        '04_scripts/runtime/pre_release_runtime.py',
        '04_scripts/runtime/validate_target_runtime.py',
        '09_deploy/runtime_identity/candidate_validation_record.py',
        '09_deploy/spread_release/high_risk_execution.py',
        '09_deploy/spread_release/create_deployment_plan.py',
        '08_tests/shared/high_risk_execution_docker_e2e.py',
        '08_tests/shared/host_release_timestamp_compatibility.py',
        '.github/workflows/trusted-main-admission.yml',
    ],
    'forbidden_paths': [
        '03_src', '05_apps', '01_data', '06_outputs', '10_logs',
        '02_configs/production_runtime_trust.json', '02_configs/runtime_contracts',
        '09_deploy/runtime_identity/production_authorization.schema.json',
    ],
    'required_tests': [
        '08_tests/test_release_refresh.py',
        '08_tests/test_candidate_validation_record.py',
        '08_tests/test_target_runtime_validator.py',
        '08_tests/test_pre_release_runtime.py',
        '08_tests/test_high_risk_execution.py',
        '08_tests/test_routine_release.py',
        '08_tests/test_host_runtime_authorization.py',
        '08_tests/test_recovery_sandbox.py',
        '08_tests/test_runtime_release_contract_repair.py',
        '08_tests/shared/test_host_release_timestamps.py',
        '08_tests/shared/test_production_identity.py',
        '08_tests/shared/test_production_grant.py',
        '08_tests/shared/test_application_service_identity.py',
        '08_tests/pipelines/test_application_runtime_readability.py',
        '08_tests/pipelines/test_full_daily_windows_wrapper.py',
        '08_tests/test_project_registry.py',
        '08_tests/test_quality_controls.py',
        '08_tests/test_documentation_contract.py',
    ],
    'capabilities': [
        'Exact existing-image validation through the same formal probe and signed record producer',
        'Bounded preparation refresh, immutable downstream references and pre-stop time window checks',
        'Synthetic-trust Hosted same-image revalidation and real executor success/rollback',
    ],
    'boundary_notes': 'Explicit GOAL RELEASE-REFRESH shared host-tool task. Existing ownership is retained; metadata does not replace user authorization or main admission. No production connections, private keys, image rebuild, trust/schema/identity changes, human Review policy changes, main integration or business writes. Expired signed records still reject. Default record TTL is unchanged. Scope includes necessary tests, interpreter compatibility and implementation/manual draft. Hosted evidence is not production acceptance. DRAFT_PENDING_PRODUCTION.',
}


def registration_baseline():
    """Permit only the approved PM delta before commit; keep other invariants."""
    baseline = json.loads(registry.git(ROOT, 'show', f'HEAD:{registry.REGISTRY_PATH}'))
    old = next(p for p in baseline['projects'] if p['project_id'] == 'soybean-pm')
    if old['status'] == 'frozen':
        current = registry.select_project(ROOT, 'soybean-pm')[1]
        expected = copy.deepcopy(old)
        expected.update(status='ready',
                        owned_paths=old['owned_paths'] + PM_EXISTING_ADDITIONS,
                        future_owned_paths=PM_FUTURE_PATHS,
                        required_tests=old['required_tests'] + PM_EXISTING_TEST_ADDITIONS,
                        future_required_tests=PM_FUTURE_TESTS,
                        boundary_notes=current['boundary_notes'])
        assert current == expected
        old.update(expected)
    current = json.loads((ROOT / registry.REGISTRY_PATH).read_text(encoding='utf-8'))
    if baseline['schema_version'] == 'project-registry/3' and current['schema_version'] == 'project-registry/4':
        expected = copy.deepcopy(baseline)
        expected['schema_version'] = 'project-registry/4'
        expected['legacy_registry_commit'] = '0839be9b57674b3e4ad41a7accffe502d918ff33'
        next(p for p in expected['projects'] if p['project_id'] == 'dev-governance')['runtime_target'] = 'none'
        expected['projects'].append(PRODUCTION_INFRA_REGISTRATION)
        assert current == expected
        baseline = expected
    if baseline['schema_version'] == 'project-registry/4' and 'shared-runtime-manifest' not in {p['project_id'] for p in baseline['projects']}:
        expected = copy.deepcopy(baseline)
        expected['projects'].append(RUNTIME_MANIFEST_REGISTRATION)
        assert current == expected
        baseline = expected
    if baseline['schema_version'] == 'project-registry/4':
        baseline_by_id = {p['project_id']: p for p in baseline['projects']}
        if baseline_by_id.get('shared-production-infrastructure') == PRODUCTION_INFRA_REGISTRATION:
            expected = copy.deepcopy(baseline)
            expected['projects'] = [GRANT_CONTRACT_REGISTRATION if p['project_id'] == 'shared-production-infrastructure' else p for p in expected['projects']]
            assert current == expected
            baseline = expected
        baseline_by_id = {p['project_id']: p for p in baseline['projects']}
        if baseline_by_id.get('shared-production-infrastructure') == GRANT_CONTRACT_REGISTRATION:
            expected = copy.deepcopy(baseline)
            expected['projects'] = [DEPLOYABILITY_ENGINE_REGISTRATION if p['project_id'] == 'shared-production-infrastructure' else p for p in expected['projects']]
            assert current == expected
            baseline = expected
        baseline_by_id = {p['project_id']: p for p in baseline['projects']}
        if baseline_by_id.get('shared-production-infrastructure') == DEPLOYABILITY_ENGINE_REGISTRATION:
            expected = copy.deepcopy(baseline)
            expected['projects'] = [PRE_RELEASE_REGISTRATION if p['project_id'] == 'shared-production-infrastructure' else p for p in expected['projects']]
            assert current == expected
            baseline = expected
        baseline_ids = {p['project_id'] for p in baseline['projects']}
        if 'public-intraday-runtime' not in baseline_ids:
            expected = copy.deepcopy(baseline)
            expected['projects'].append(PUBLIC_INTRADAY_RUNTIME_REGISTRATION)
            assert current == expected
            baseline = expected
        baseline_by_id = {p['project_id']: p for p in baseline['projects']}
        if baseline_by_id.get('public-intraday-runtime') == PUBLIC_INTRADAY_RUNTIME_REGISTRATION:
            expected = copy.deepcopy(baseline)
            expected['projects'] = [
                PUBLIC_INTRADAY_RUNTIME_PRODUCTION_REGISTRATION
                if p['project_id'] == 'public-intraday-runtime' else p
                for p in expected['projects']
            ]
            assert current == expected
            baseline = expected
        baseline_by_id = {p['project_id']: p for p in baseline['projects']}
        if baseline_by_id.get('shared-production-infrastructure') == PRE_RELEASE_REGISTRATION:
            expected = copy.deepcopy(baseline)
            expected['projects'] = [CONTRACT_SOURCE_ALLOCATION if p['project_id'] == 'shared-production-infrastructure' else p for p in expected['projects']]
            assert current == expected
            baseline = expected
        baseline_by_id = {p['project_id']: p for p in baseline['projects']}
        if baseline_by_id.get('shared-production-infrastructure') == CONTRACT_SOURCE_ALLOCATION:
            expected = copy.deepcopy(baseline)
            shared_intraday = next(p for p in expected['projects'] if p['project_id'] == 'shared-intraday')
            shared_intraday['future_owned_paths'].remove('04_scripts/capture_public_intraday.py')
            shared_intraday['shared_dependencies'].append('04_scripts/capture_public_intraday.py')
            shared_intraday['runtime_target'] = 'library_only'
            shared_intraday['boundary_notes'] += ' PROD-RUNTIME-V2 transfers only 04_scripts/capture_public_intraday.py to spread-production-runtime-wiring for explicit Git/OCI identity initialization and argument wiring. Remaining Shared Intraday schema, providers, pipelines and tests are library_only and retain their existing boundaries. The transferred CLI remains a read-only dependency here; this project cannot change it or claim container deployability. Its capture semantics, exact-contract queries, dates, secret-file contract and immutable SEALED behavior must remain unchanged.'
            expected['projects'] = [
                POST_TRANSFER_INFRA_REGISTRATION if p['project_id'] == 'shared-production-infrastructure'
                else shared_intraday if p['project_id'] == 'shared-intraday' else p
                for p in expected['projects']]
            expected['projects'].append(SPREAD_RUNTIME_WIRING_REGISTRATION)
            assert current == expected
            baseline = expected
    if not any(p['project_id'] == 'soybean-production-input-authority' for p in baseline['projects']):
        expected = copy.deepcopy(baseline)
        expected['projects'].append(PRODUCTION_INPUT_AUTHORITY_REGISTRATION)
        assert current == expected
        baseline = expected
    if not any(p['project_id'] == 'xiaoran-production-data-delivery' for p in baseline['projects']):
        expected = copy.deepcopy(baseline)
        expected['projects'].append(XIAORAN_PRODUCTION_DATA_DELIVERY_REGISTRATION)
        assert current == expected
        baseline = expected
    governance = next(p for p in baseline['projects'] if p['project_id'] == 'dev-governance')
    if not governance.get('future_owned_paths'):
        expected = copy.deepcopy(baseline)
        target = next(p for p in expected['projects'] if p['project_id'] == 'dev-governance')
        target['future_owned_paths'] = ADMISSION_GOVERNANCE_FILES
        target['future_required_tests'] = ADMISSION_GOVERNANCE_TESTS
        assert current == expected
        baseline = expected
    if not any(p['project_id'] == 'windows-wrapper-platform' for p in baseline['projects']):
        expected = copy.deepcopy(baseline)
        expected['projects'].append(WINDOWS_WRAPPER_PLATFORM_REGISTRATION)
        assert current == expected
        baseline = expected
    if not any(p['project_id'] == 'release-refresh' for p in baseline['projects']):
        expected = copy.deepcopy(baseline)
        expected['projects'].append(RELEASE_REFRESH_REGISTRATION)
        assert current == expected
        baseline = expected
    if not any(p['project_id'] == 'release-reentry' for p in baseline['projects']):
        expected = copy.deepcopy(baseline)
        expected['projects'].append(registry.select_project(ROOT, 'release-reentry')[1])
        assert current == expected  # Only the explicitly authorized additive metadata delta.
        baseline = expected
    return baseline


def test_release_reentry_registration_keeps_existing_owners_and_production_boundaries():
    _, project = registry.select_project(ROOT, 'release-reentry')
    assert project['runtime_target'] == 'library_only' and project['change_class'] == 'shared'
    assert project['owned_paths'] == ['08_tests/test_release_reentry.py']
    assert set(project['shared_dependencies']) == {
        '04_scripts/runtime/historical_primary_rollback.py', '04_scripts/runtime/pre_release_runtime.py',
        '04_scripts/runtime/validate_target_runtime.py', '09_deploy/spread_release/high_risk_execution.py',
        '08_tests/shared/high_risk_execution_docker_e2e.py', '08_tests/test_historical_primary_rollback.py',
        '08_tests/test_target_runtime_validator.py', '08_tests/test_project_registry.py', '04_scripts/runtime/说明.md'}
    assert {'03_src', '05_apps', '01_data', '06_outputs', '10_logs', '02_configs/production_runtime_trust.json',
        '02_configs/runtime_contracts', '09_deploy/runtime_identity/production_authorization.schema.json',
        '09_deploy/runtime_identity/candidate_validation_record.py'} == set(project['forbidden_paths'])
    assert {'08_tests/test_release_reentry.py', '08_tests/test_historical_primary_rollback.py',
        '08_tests/test_target_runtime_validator.py', '08_tests/test_pre_release_runtime.py',
        '08_tests/test_host_runtime_authorization.py', '08_tests/test_recovery_sandbox.py',
        '08_tests/shared/test_production_identity.py', '08_tests/test_candidate_validation_record.py',
        '08_tests/pipelines/test_full_daily_windows_wrapper.py'} <= set(project['required_tests'])
    assert not project['future_required_tests']


def minimal_registry():
    return {"schema_version":"project-registry/1", "protected_paths":["shared"], "projects":[{
        "project_id":"demo", "change_class":"business", "status":"ready",
        "owned_paths":["feature"], "shared_dependencies":["shared"], "forbidden_paths":["other"],
        "required_tests":["tests/test_feature.py"], "capabilities":["demo"], "boundary_notes":"fixture only"}]}


def runtime_v4_fixture(root, *, target="production_container"):
    """Versioned fixture with only repository-contained build inputs."""
    from quality import target_runtime_gate as gate
    data = minimal_registry()
    data['schema_version'] = 'project-registry/4'
    project = data['projects'][0]
    project['runtime_target'] = target
    if target == 'production_container':
        project['runtime_contract'] = 'feature/runtime.json'
        contract = {'schema_version': 'runtime-manifest/1', 'project_id': 'demo',
                    'runtime_target': target, 'identity_kind': 'oci_container',
                    'module_id': 'demo', 'service_id': 'demo', 'entrypoint': ['python', 'feature/code.py'],
                    'working_directory': '/app',
                    'runtime_roots': [{'role': 'state', 'container_path': '/runtime/state', 'access': 'rw'}],
                    'required_mounts': [{'role': 'state', 'container_path': '/runtime/state', 'read_only': False}],
                    'required_environment': [], 'secret_references': [], 'required_executables': ['python'],
                    'required_python_modules': [], 'validation_probes': sorted(gate.REQUIRED_PROBES),
                    'production_policy': {'deployment_role': 'production', 'write_grant_required': True},
                    'preview_policy': {'production_write': False, 'production_rw_mounts': False},
                    'build': {'dockerfile': 'Dockerfile', 'dockerignore': '.dockerignore',
                              'dependency_contracts': ['requirements.txt'],
                              'compose_sources': ['compose.yml']}}
        for name, content in {'Dockerfile': 'FROM scratch\n', '.dockerignore': '.git\n',
                              'requirements.txt': '# fixture\n', 'compose.yml': 'services: {}\n',
                              gate.ENGINE: '# engine transport fixture\n',
                              'feature/runtime.json': json.dumps(contract)}.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
    return data


def approved_runtime_fixture(root):
    data = runtime_v4_fixture(root)
    (root/'tests/test_feature.py').write_text('def test_required(): assert True\n', encoding='utf-8')
    write_registry(root, data)
    registry.git(root, 'add', '.')
    registry.git(root, '-c', 'commit.gpgsign=false', 'commit', '-m', 'runtime fixture')
    registry.git(root, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    return data['projects'][0]


def approved_candidate_record_fixture(root, *, trust_mutation=None):
    """Commit the real record parser and its test signing trust on approved main."""
    data = runtime_v4_fixture(root)
    project = data['projects'][0]
    parser = root / '09_deploy/runtime_identity/candidate_validation_record.py'
    parser.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / '09_deploy/runtime_identity/candidate_validation_record.py', parser)
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(
        private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode('ascii')
    trust = {
        'schema_version': 'production-runtime-trust/1',
        'keys': [{
            'key_id': 'candidate-record-fixture',
            'domain': 'candidate_validation',
            'algorithm': 'ed25519',
            'public_key_base64': public,
        }],
        'revoked_key_ids': [],
        'revoked_grant_ids': [],
    }
    if trust_mutation == 'wrongdomain':
        trust['keys'][0]['domain'] = 'production'
    elif trust_mutation == 'revoked':
        trust['revoked_key_ids'] = ['candidate-record-fixture']
    (root / '02_configs/production_runtime_trust.json').write_text(
        json.dumps(trust), encoding='utf-8'
    )
    (root / 'tests/test_feature.py').write_text(
        'def test_required(): assert True\n', encoding='utf-8'
    )
    write_registry(root, data)
    registry.git(root, 'add', '.')
    registry.git(root, '-c', 'commit.gpgsign=false', 'commit', '-m', 'approved record authority')
    remote = root.parent / 'approved-origin.git'
    registry.git(root, 'init', '--bare', str(remote))
    registry.git(root, 'remote', 'add', 'origin', str(remote))
    registry.git(root, 'push', 'origin', 'HEAD:refs/heads/main')
    registry.git(root, 'fetch', 'origin', 'main')
    return project, private, remote


def allow_fixture_record_remote(gate, monkeypatch, remote):
    monkeypatch.setattr(gate, 'APPROVED_RECORD_REMOTES', frozenset({str(remote)}))


def signed_candidate_record(root, project, private, *, mutation=None):
    """Create an external signed record through the parser copied into approved main."""
    from quality import target_runtime_gate as gate

    spec = importlib.util.spec_from_file_location(
        'fixture_candidate_record_parser', root / gate.RECORD_PARSER
    )
    assert spec and spec.loader
    parser = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(parser)
    evidence = runtime_pass_evidence(gate.candidate_binding(root, project))
    if mutation == 'wrongcommit':
        evidence['binding']['commit'] = '0' * 40
    elif mutation == 'probeFAIL':
        evidence['probes']['dependencies'] = 'FAIL'
    payload = {
        'record_id': '1' * 32,
        'purpose': 'target-runtime-validation',
        'authorization_role': 'candidate_validation',
        'issued_at': (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        'expires_at': (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        'evidence': evidence,
    }
    if mutation == 'expired':
        payload['issued_at'] = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        payload['expires_at'] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    payload['evidence_sha256'] = hashlib.sha256(parser.canonical(evidence)).hexdigest()
    unsigned = {
        'schema_version': 'candidate-validation-record/1',
        'algorithm': 'ed25519',
        'key_id': 'candidate-record-fixture',
        'payload': payload,
    }
    signature_key = Ed25519PrivateKey.generate() if mutation == 'badsig' else private
    envelope = {
        **unsigned,
        'signature': base64.b64encode(signature_key.sign(parser.canonical(unsigned))).decode('ascii'),
    }
    if mutation == 'unsigned':
        del envelope['signature']
    path = root.parent / 'candidate-validation-record.json'
    path.write_bytes(json.dumps(envelope, sort_keys=True, separators=(',', ':')).encode('utf-8'))
    return path


def runtime_manifest_v2_fixture(root):
    """A production target using the v2 manifest source-input contract."""
    from quality import target_runtime_gate as gate
    data = runtime_v4_fixture(root)
    project = data['projects'][0]
    project['runtime_contract'] = 'feature/runtime.json'
    contract = json.loads((root / project['runtime_contract']).read_text(encoding='utf-8'))
    contract['schema_version'] = 'runtime-manifest/2'
    contract['identity_root_role'] = 'marker'
    contract['runtime_roots'].insert(0, {'role': 'marker', 'container_path': '/runtime', 'access': 'ro'})
    contract['required_mounts'].insert(0, {'role': 'marker', 'container_path': '/runtime', 'read_only': True})
    contract['initialization_commands'] = [{'name': 'initialize', 'argv': ['python', 'init.py']}]
    contract['source_inputs'] = [
        {'path': 'app.py', 'role': 'entrypoint'},
        {'path': 'init.py', 'role': 'initialization'},
        {'path': 'config.json', 'role': 'runtime_configuration'},
    ]
    for name, content in {'app.py': 'print("fixture")\n', 'init.py': 'print("init")\n',
                          'config.json': '{"mode":"fixture"}\n'}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    manifest_path = root / '03_src/agri_research_agent/shared/runtime_manifest.py'
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / '03_src/agri_research_agent/shared/runtime_manifest.py', manifest_path)
    schema_path = root / '02_configs/runtime_manifest.schema.json'
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / '02_configs/runtime_manifest.schema.json', schema_path)
    (root / project['runtime_contract']).write_text(json.dumps(contract), encoding='utf-8')
    return data, project


def approved_runtime_manifest_v2_fixture(root):
    data, project = runtime_manifest_v2_fixture(root)
    (root / 'tests/test_feature.py').write_text('def test_required(): assert True\n', encoding='utf-8')
    write_registry(root, data)
    registry.git(root, 'add', '.')
    registry.git(root, '-c', 'commit.gpgsign=false', 'commit', '-m', 'runtime manifest v2 fixture')
    registry.git(root, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    return project


def approved_runtime_manifest_v3_fixture(root):
    data, project = runtime_manifest_v2_fixture(root)
    path = root / project['runtime_contract']
    contract = json.loads(path.read_text(encoding='utf-8'))
    contract.update(schema_version='runtime-manifest/3',
        required_environment=['MODE', 'HISTORY_ROOT', 'MARKET_DATA_EXECUTION_GRANT'],
        environment_bindings=[
            {'name': 'MODE', 'kind': 'literal', 'value': 'STRICT_RUNTIME'},
            {'name': 'HISTORY_ROOT', 'kind': 'runtime_path', 'role': 'history', 'relative_path': ''},
            {'name': 'MARKET_DATA_EXECUTION_GRANT', 'kind': 'execution_grant'}],
        forbidden_environment=['ENABLE_WRITES'])
    contract['runtime_roots'].append({'role': 'history', 'container_path': '/runtime/history', 'access': 'ro'})
    contract['required_mounts'].append({'role': 'history', 'container_path': '/runtime/history', 'read_only': True})
    seed = root / '08_tests/fixtures/runtime/history.json'
    seed.parent.mkdir(parents=True, exist_ok=True)
    seed.write_bytes(b'{"fixture":true}\n')
    contract['candidate_runtime_inputs'] = [{'source_path': seed.relative_to(root).as_posix(),
        'sha256': hashlib.sha256(seed.read_bytes()).hexdigest(), 'role': 'history', 'relative_path': 'current.json'}]
    path.write_text(json.dumps(contract), encoding='utf-8')
    (root / 'tests/test_feature.py').write_text('def test_required(): assert True\n', encoding='utf-8')
    write_registry(root, data)
    registry.git(root, 'add', '.')
    registry.git(root, '-c', 'commit.gpgsign=false', 'commit', '-m', 'runtime manifest v3 fixture')
    registry.git(root, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    return project, seed


def test_runtime_manifest_v3_governance_binds_fixture_and_requires_new_validator(repository):
    from quality import target_runtime_gate as gate
    _, root = repository
    project, seed = approved_runtime_manifest_v3_fixture(root)
    binding = gate.candidate_binding(root, project)
    assert binding['validator_version'] == 'target-runtime-validator/2'
    assert binding['source_sha256'][seed.relative_to(root).as_posix()] == hashlib.sha256(seed.read_bytes()).hexdigest()
    assert {gate.MANIFEST_PARSER, gate.MANIFEST_SCHEMA, gate.ENGINE}.issubset(binding['source_sha256'])
    old_binding = copy.deepcopy(binding)
    old_binding['validator_version'] = 'target-runtime-validator/1'
    with pytest.raises(ValueError, match='binding mismatch'):
        gate.validate_evidence(runtime_pass_evidence(old_binding), binding, 0)


@pytest.mark.parametrize('mutation', ['missing', 'ignored', 'untracked', 'wrong_hash', 'case_alias', 'hidden_drift'])
def test_runtime_manifest_v3_governance_rejects_uncontrolled_fixture(repository, mutation):
    from quality import target_runtime_gate as gate
    _, root = repository
    project, seed = approved_runtime_manifest_v3_fixture(root)
    relative = seed.relative_to(root).as_posix()
    if mutation == 'missing':
        seed.unlink()
        registry.git(root, 'add', '-u')
    elif mutation in ('ignored', 'untracked'):
        registry.git(root, 'rm', '--cached', '--', relative)
        if mutation == 'ignored':
            (root / '.gitignore').write_text(relative + '\n', encoding='utf-8')
            registry.git(root, 'add', '.gitignore')
    elif mutation == 'hidden_drift':
        registry.git(root, 'update-index', '--assume-unchanged', relative)
        seed.write_bytes(b'{"tampered":true}\n')
        assert registry.git(root, 'status', '--porcelain') == ''
    else:
        path = root / project['runtime_contract']
        contract = json.loads(path.read_text(encoding='utf-8'))
        if mutation == 'wrong_hash':
            contract['candidate_runtime_inputs'][0]['sha256'] = 'b' * 64
        else:
            contract['candidate_runtime_inputs'][0]['source_path'] = '08_tests/fixtures/runtime/HISTORY.json'
        path.write_text(json.dumps(contract), encoding='utf-8')
        registry.git(root, 'add', project['runtime_contract'])
    if mutation != 'hidden_drift':
        registry.git(root, '-c', 'commit.gpgsign=false', 'commit', '-m', 'unusable candidate fixture')
    with pytest.raises((ValueError, OSError)):
        gate.candidate_binding(root, project)


def test_runtime_manifest_v3_governance_uses_git_fixture_bytes_across_checkout_crlf(repository):
    from quality import target_runtime_gate as gate
    _, root = repository
    (root / '.gitattributes').write_text('08_tests/fixtures/runtime/history.json text eol=crlf\n', encoding='utf-8')
    project, seed = approved_runtime_manifest_v3_fixture(root)
    canonical = subprocess.check_output(['git', '-C', str(root), 'show', 'HEAD:08_tests/fixtures/runtime/history.json'])
    seed.unlink()
    registry.git(root, 'checkout', '--', '08_tests/fixtures/runtime/history.json')
    assert seed.read_bytes() == canonical.replace(b'\n', b'\r\n')
    assert registry.git(root, 'status', '--porcelain') == ''
    binding = gate.candidate_binding(root, project)
    assert binding['source_sha256']['08_tests/fixtures/runtime/history.json'] == hashlib.sha256(canonical).hexdigest()
    assert binding['source_sha256']['08_tests/fixtures/runtime/history.json'] != hashlib.sha256(seed.read_bytes()).hexdigest()


def test_runtime_manifest_v3_governance_fixture_change_invalidates_previous_evidence(repository):
    from quality import target_runtime_gate as gate
    _, root = repository
    project, seed = approved_runtime_manifest_v3_fixture(root)
    old = runtime_pass_evidence(gate.candidate_binding(root, project))
    seed.write_bytes(b'{"fixture":"changed"}\n')
    path = root / project['runtime_contract']
    contract = json.loads(path.read_text(encoding='utf-8'))
    contract['candidate_runtime_inputs'][0]['sha256'] = hashlib.sha256(seed.read_bytes()).hexdigest()
    path.write_text(json.dumps(contract), encoding='utf-8')
    registry.git(root, 'add', '.')
    registry.git(root, '-c', 'commit.gpgsign=false', 'commit', '-m', 'changed controlled fixture')
    with pytest.raises(ValueError, match='binding mismatch'):
        gate.validate_evidence(old, gate.candidate_binding(root, project), 0)


@pytest.mark.parametrize('mutation', ['duplicate_schema', 'missing_binding', 'seed_image_overlap'])
def test_runtime_manifest_v3_governance_never_bypasses_shared_parser(repository, mutation):
    from quality import target_runtime_gate as gate
    _, root = repository
    project, seed = approved_runtime_manifest_v3_fixture(root)
    path = root / project['runtime_contract']
    raw = path.read_text(encoding='utf-8')
    if mutation == 'duplicate_schema':
        path.write_text(raw.replace('"schema_version":', '"schema_version":"runtime-manifest/2","schema_version":'), encoding='utf-8')
    else:
        contract = json.loads(raw)
        if mutation == 'missing_binding':
            contract['environment_bindings'].pop()
        else:
            contract['source_inputs'].append({'path': seed.relative_to(root).as_posix(), 'role': 'initialization'})
        path.write_text(json.dumps(contract), encoding='utf-8')
    with pytest.raises(ValueError):
        gate.read_contract(root, project)


def test_runtime_manifest_v3_governance_does_not_accept_old_engine_claim(repository, monkeypatch):
    from quality import target_runtime_gate as gate
    _, root = repository
    project, _ = approved_runtime_manifest_v3_fixture(root)
    def old_engine(candidate, selected, output):
        binding = gate.candidate_binding(candidate, selected)
        binding['validator_version'] = 'target-runtime-validator/1'
        output.write_text(json.dumps(runtime_pass_evidence(binding)), encoding='utf-8')
        return 0
    monkeypatch.setattr(gate, 'execute_engine', old_engine)
    with pytest.raises(ValueError, match='binding mismatch'):
        gate.validate_target(root, project)


def test_runtime_manifest_v2_bridge_accepts_source_inputs_and_binds_all_sources(repository):
    from quality import target_runtime_gate as gate
    _, root = repository
    project = approved_runtime_manifest_v2_fixture(root)
    contract = gate.read_contract(root, project)
    assert contract['schema_version'] == 'runtime-manifest/2'
    binding = gate.candidate_binding(root, project)
    assert binding['validator_version'] == 'target-runtime-validator/1'
    for path in ['app.py', 'init.py', 'config.json', '03_src/agri_research_agent/shared/runtime_manifest.py',
                 '02_configs/runtime_manifest.schema.json']:
        assert path in binding['source_sha256']


@pytest.mark.parametrize('mutation', ['missing_source', 'ignored_source', 'logical_role', 'manifest_overlap'])
def test_runtime_manifest_v2_bridge_rejects_invalid_source_contract(repository, mutation):
    from quality import target_runtime_gate as gate
    _, root = repository
    project = approved_runtime_manifest_v2_fixture(root)
    path = root / project['runtime_contract']
    contract = json.loads(path.read_text(encoding='utf-8'))
    if mutation == 'missing_source':
        (root / 'init.py').unlink()
        registry.git(root, 'add', '-u')
        registry.git(root, '-c', 'commit.gpgsign=false', 'commit', '-m', 'missing declared input')
    elif mutation == 'ignored_source':
        registry.git(root, 'rm', '--cached', 'init.py')
        (root / '.gitignore').write_text('init.py\n', encoding='utf-8')
        registry.git(root, 'add', '.gitignore')
        registry.git(root, '-c', 'commit.gpgsign=false', 'commit', '-m', 'ignored-only declared input')
        assert (root / 'init.py').is_file()
    elif mutation == 'logical_role':
        contract['identity_root_role'] = 'unknown'
        path.write_text(json.dumps(contract), encoding='utf-8')
    else:
        contract['source_inputs'][0]['path'] = project['runtime_contract']
        path.write_text(json.dumps(contract), encoding='utf-8')
    if mutation in ('missing_source', 'ignored_source'):
        assert registry.git(root, 'status', '--porcelain=v1') == ''
    with pytest.raises((ValueError, OSError)):
        gate.candidate_binding(root, project)


def test_runtime_manifest_v2_source_change_invalidates_old_binding(repository):
    from quality import target_runtime_gate as gate
    _, root = repository
    project = approved_runtime_manifest_v2_fixture(root)
    old = gate.candidate_binding(root, project)
    (root / 'init.py').write_text('changed\n', encoding='utf-8')
    registry.git(root, 'add', 'init.py')
    registry.git(root, '-c', 'commit.gpgsign=false', 'commit', '-m', 'changed manifest source input')
    with pytest.raises(ValueError, match='binding'):
        gate.validate_evidence(runtime_pass_evidence(old),
                               gate.candidate_binding(root, project), 0)


@pytest.mark.parametrize('version', ['runtime-manifest/1', 'runtime-manifest/2'])
def test_runtime_manifest_v2_bridge_duplicate_json_key_never_downgrades(repository, version):
    from quality import target_runtime_gate as gate
    _, root = repository
    data, project = runtime_manifest_v2_fixture(root)
    project['runtime_contract'] = 'feature/runtime.json'
    path = root / project['runtime_contract']
    contract = json.loads(path.read_text(encoding='utf-8'))
    contract['schema_version'] = version
    if version == 'runtime-manifest/1':
        for key in ('identity_root_role', 'initialization_commands', 'source_inputs'):
            del contract[key]
    other = 'runtime-manifest/2' if version == 'runtime-manifest/1' else 'runtime-manifest/1'
    path.write_text('{"schema_version":' + json.dumps(other) + ',' + json.dumps(contract)[1:], encoding='utf-8')
    with pytest.raises(ValueError, match='Invalid runtime contract JSON'):
        gate.read_contract(root, project)


def runtime_pass_evidence(binding):
    from quality import target_runtime_gate as gate
    return {'schema_version': gate.EVIDENCE_SCHEMA, 'binding': copy.deepcopy(binding),
            'TARGET_RUNTIME_STATIC_VALIDATION': 'PASS', 'TARGET_RUNTIME_CONTAINER_VALIDATION': 'PASS',
            'image_id': 'sha256:' + 'a'*64, 'rendered_compose_sha256': 'b'*64,
            'builder': {'builder_id': 'test-transport-fixture', 'os': 'linux', 'execution': 'isolated'},
            'observed_identity': {'image_id': 'sha256:' + 'a'*64, 'oci_revision': binding['commit'],
                                  'git_tree': binding['tree'], 'source_sha256': copy.deepcopy(binding['source_sha256']),
                                  'rendered_compose_sha256': 'b'*64, 'authorization_role': 'candidate_validation',
                                  'git_metadata_present': False, 'production_volumes_mounted': False},
            'probes': {name: 'PASS' for name in gate.REQUIRED_PROBES}}


@pytest.mark.parametrize('target', ['none', 'library_only', 'windows_git_worktree', 'production_container'])
def test_v4_explicit_runtime_intents(repository, target):
    _, root = repository
    registry.validate_registry(runtime_v4_fixture(root, target=target), root)


@pytest.mark.parametrize('target', [None, '', 'docker', 'unknown_production', 4])
def test_v4_missing_or_unknown_runtime_intent_rejected(repository, target):
    _, root = repository
    data = runtime_v4_fixture(root)
    if target is None:
        del data['projects'][0]['runtime_target']
    else:
        data['projects'][0]['runtime_target'] = target
    with pytest.raises(ValueError, match='runtime_target'):
        registry.validate_registry(data, root)


@pytest.mark.parametrize('path', [None, 'missing.json', '../outside.json', '/tmp/contract.json',
                                'feature/*.json', 'feature', 'C:/contract.json', 'feature\\runtime.json'])
def test_v4_invalid_or_missing_runtime_contract_rejected(repository, path):
    _, root = repository
    data = runtime_v4_fixture(root)
    if path is None:
        del data['projects'][0]['runtime_contract']
    else:
        data['projects'][0]['runtime_contract'] = path
    with pytest.raises(ValueError):
        registry.validate_registry(data, root)


@pytest.mark.parametrize('field,value', [('schema_version', 'runtime-manifest/999'),
                                      ('project_id', 'other'), ('identity_kind', 'git_worktree'),
                                      ('runtime_target', 'none'), ('build', {})])
def test_v4_runtime_contract_identity_rejected(repository, field, value):
    _, root = repository
    data = runtime_v4_fixture(root)
    path = root/'feature/runtime.json'
    contract = json.loads(path.read_text())
    contract[field] = value
    path.write_text(json.dumps(contract), encoding='utf-8')
    with pytest.raises(ValueError):
        registry.validate_registry(data, root)


@pytest.mark.parametrize('version', ['project-registry/1', 'project-registry/2', 'project-registry/3'])
def test_legacy_records_need_no_runtime_declaration(repository, version):
    from quality import target_runtime_gate as gate
    _, root = repository
    data = minimal_registry()
    data['schema_version'] = version
    registry.validate_registry(data, root)
    assert gate.validate_target(root, data['projects'][0]) == {'TARGET_RUNTIME_VALIDATION': 'NOT_REQUIRED'}
    data['projects'][0]['runtime_target'] = 'production_container'
    with pytest.raises(ValueError, match='fields'):
        registry.validate_registry(data, root)


@pytest.mark.parametrize('target', ['none', 'library_only', 'windows_git_worktree'])
def test_noncontainer_completion_does_not_invoke_engine(repository, monkeypatch, target):
    from quality import complete_project, target_runtime_gate as gate
    _, root = repository
    data = runtime_v4_fixture(root, target=target)
    (root/'tests/test_feature.py').write_text('def test_required(): assert True\n')
    write_registry(root, data)
    registry.git(root, 'add', '.')
    registry.git(root, '-c', 'commit.gpgsign=false', 'commit', '-m', 'noncontainer')
    registry.git(root, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    def forbidden(*args):
        raise AssertionError('Noncontainer projects must not require Docker')
    monkeypatch.setattr(gate, 'execute_engine', forbidden)
    assert complete_project.complete(root, 'demo')['PROJECT_COMPLETION'] == 'PASS'


def test_container_completion_executes_validator_and_binds_evidence(repository, monkeypatch):
    from quality import complete_project, target_runtime_gate as gate
    _, root = repository
    project = approved_runtime_fixture(root)
    calls = []
    def engine(candidate, selected, output):
        calls.append((candidate, selected['project_id']))
        output.write_text(json.dumps(runtime_pass_evidence(gate.candidate_binding(candidate, selected))), encoding='utf-8')
        return 0
    monkeypatch.setattr(gate, 'execute_engine', engine)
    result = complete_project.complete(root, 'demo')
    assert calls == [(root, 'demo')]
    assert result['target_runtime_evidence']['binding'] == gate.candidate_binding(root, project)


def test_signed_candidate_record_completes_without_rebuilding_and_preserves_record(repository, monkeypatch):
    from quality import complete_project, target_runtime_gate as gate
    _, root = repository
    project, private, remote = approved_candidate_record_fixture(root)
    allow_fixture_record_remote(gate, monkeypatch, remote)
    record_path = signed_candidate_record(root, project, private)
    original = record_path.read_bytes()
    monkeypatch.setattr(
        gate, 'execute_engine',
        lambda *_: pytest.fail('authenticated record must not rebuild the candidate'),
    )
    result = complete_project.complete(root, 'demo', candidate_record=record_path)
    assert result['PROJECT_COMPLETION'] == 'PASS'
    assert result['executed_tests'] == [{
        'path': 'tests/test_feature.py', 'passed': 1,
        'sha256': hashlib.sha256((root / 'tests/test_feature.py').read_bytes()).hexdigest(),
    }]
    assert result['authenticated_candidate_record']['record_id'] == '1' * 32
    assert record_path.read_bytes() == original


@pytest.mark.parametrize('mutation', ['unsigned', 'badsig', 'wrongcommit', 'probeFAIL', 'expired'])
def test_signed_candidate_record_rejects_untrusted_or_nonpass_facts(repository, monkeypatch, mutation):
    from quality import target_runtime_gate as gate
    _, root = repository
    project, private, remote = approved_candidate_record_fixture(root)
    allow_fixture_record_remote(gate, monkeypatch, remote)
    record_path = signed_candidate_record(root, project, private, mutation=mutation)
    with pytest.raises(ValueError):
        gate.validate_target(root, project, candidate_record=record_path)


@pytest.mark.parametrize('trust_mutation', ['wrongdomain', 'revoked'])
def test_signed_candidate_record_rejects_unapproved_trust_domain_or_revocation(repository, monkeypatch, trust_mutation):
    from quality import target_runtime_gate as gate
    _, root = repository
    project, private, remote = approved_candidate_record_fixture(root, trust_mutation=trust_mutation)
    allow_fixture_record_remote(gate, monkeypatch, remote)
    record_path = signed_candidate_record(root, project, private)
    with pytest.raises(ValueError):
        gate.validate_target(root, project, candidate_record=record_path)


@pytest.mark.parametrize('changed', ['parser', 'trust'])
def test_signed_candidate_record_requires_candidate_parser_and_trust_equal_approved_main(repository, monkeypatch, changed):
    from quality import target_runtime_gate as gate
    _, root = repository
    project, private, remote = approved_candidate_record_fixture(root)
    allow_fixture_record_remote(gate, monkeypatch, remote)
    record_path = signed_candidate_record(root, project, private)
    registry.git(root, 'checkout', '-b', 'feat/record-drift')
    target = root / (gate.RECORD_PARSER if changed == 'parser' else gate.RECORD_TRUST)
    target.write_text(target.read_text(encoding='utf-8') + '\n', encoding='utf-8')
    registry.git(root, 'add', target.relative_to(root).as_posix())
    registry.git(root, '-c', 'commit.gpgsign=false', 'commit', '-m', f'candidate {changed} change')
    with pytest.raises(ValueError, match='parser/trust'):
        gate.validate_target(root, project, candidate_record=record_path)


def test_signed_candidate_record_detects_external_record_drift(repository, monkeypatch):
    from quality import target_runtime_gate as gate
    _, root = repository
    project, private, remote = approved_candidate_record_fixture(root)
    allow_fixture_record_remote(gate, monkeypatch, remote)
    record_path = signed_candidate_record(root, project, private)
    original_authority = gate._approved_record_authority
    calls = 0

    def authority(candidate):
        nonlocal calls
        calls += 1
        value = original_authority(candidate)
        if calls == 1:
            record_path.write_bytes(record_path.read_bytes() + b' ')
        return value

    monkeypatch.setattr(gate, '_approved_record_authority', authority)
    with pytest.raises(ValueError, match='changed'):
        gate.validate_target(root, project, candidate_record=record_path)


def test_signed_candidate_record_rejects_unapproved_origin_remote(repository, monkeypatch):
    from quality import target_runtime_gate as gate
    _, root = repository
    project, private, remote = approved_candidate_record_fixture(root)
    allow_fixture_record_remote(gate, monkeypatch, remote)
    record_path = signed_candidate_record(root, project, private)
    registry.git(root, 'remote', 'set-url', 'origin', str(root.parent / 'unapproved-origin.git'))
    with pytest.raises(ValueError, match='approved GitHub repository'):
        gate.validate_target(root, project, candidate_record=record_path)


def test_completion_rejects_record_drift_during_required_tests(repository, monkeypatch):
    from quality import complete_project, target_runtime_gate as gate
    _, root = repository
    project, private, remote = approved_candidate_record_fixture(root)
    allow_fixture_record_remote(gate, monkeypatch, remote)
    record_path = root.parent / 'candidate-validation-record.json'
    registry.git(root, 'checkout', '-b', 'feat/record-completion-drift')
    (root / 'tests/test_feature.py').write_text(
        "from pathlib import Path\n\n"
        "def test_required():\n"
        f"    target = Path({str(record_path)!r})\n"
        "    target.write_bytes(target.read_bytes() + b' ')\n"
        "    assert True\n",
        encoding='utf-8',
    )
    registry.git(root, 'add', 'tests/test_feature.py')
    registry.git(root, '-c', 'commit.gpgsign=false', 'commit', '-m', 'record drift test')
    signed_candidate_record(root, project, private)
    monkeypatch.setattr(
        gate, 'execute_engine',
        lambda *_: pytest.fail('authenticated record must not rebuild the candidate'),
    )
    with pytest.raises(ValueError, match='Candidate record or authority changed during completion'):
        complete_project.complete(root, 'demo', candidate_record=record_path)


def test_candidate_record_is_rejected_for_noncontainer_project(repository):
    from quality import target_runtime_gate as gate
    _, root = repository
    project = runtime_v4_fixture(root, target='library_only')['projects'][0]
    record_path = root.parent / 'ordinary-record.json'
    record_path.write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='production_container'):
        gate.validate_target(root, project, candidate_record=record_path)


@pytest.mark.parametrize('mutation', ['static_missing', 'container_missing', 'container_failed',
                                    'commit', 'tree', 'manifest', 'validator', 'image', 'compose', 'exit'])
def test_container_completion_rejects_invalid_evidence(repository, monkeypatch, mutation):
    from quality import complete_project, target_runtime_gate as gate
    _, root = repository
    approved_runtime_fixture(root)
    def engine(candidate, selected, output):
        evidence = runtime_pass_evidence(gate.candidate_binding(candidate, selected))
        if mutation == 'static_missing': del evidence['TARGET_RUNTIME_STATIC_VALIDATION']
        elif mutation == 'container_missing': del evidence['TARGET_RUNTIME_CONTAINER_VALIDATION']
        elif mutation == 'container_failed': evidence['TARGET_RUNTIME_CONTAINER_VALIDATION'] = 'FAIL'
        elif mutation in ('commit', 'tree'): evidence['binding'][mutation] = '0'*40
        elif mutation == 'manifest': evidence['binding']['source_sha256'][selected['runtime_contract']] = '0'*64
        elif mutation == 'validator': evidence['binding']['validator_version'] = 'old'
        elif mutation == 'image': evidence['image_id'] = 'latest'
        elif mutation == 'compose': del evidence['rendered_compose_sha256']
        output.write_text(json.dumps(evidence), encoding='utf-8')
        return 1 if mutation == 'exit' else 0
    monkeypatch.setattr(gate, 'execute_engine', engine)
    with pytest.raises(ValueError):
        complete_project.complete(root, 'demo')


@pytest.mark.parametrize('name', ['feature/runtime.json', 'Dockerfile', '.dockerignore',
                                'requirements.txt', 'compose.yml'])
def test_committed_input_change_invalidates_runtime_evidence(repository, name):
    from quality import target_runtime_gate as gate
    _, root = repository
    project = approved_runtime_fixture(root)
    old = runtime_pass_evidence(gate.candidate_binding(root, project))
    with (root/name).open('a', encoding='utf-8') as stream: stream.write('\n')
    with pytest.raises(ValueError, match='clean'):
        gate.candidate_binding(root, project)
    registry.git(root, 'add', '.')
    registry.git(root, '-c', 'commit.gpgsign=false', 'commit', '-m', 'changed runtime input')
    with pytest.raises(ValueError, match='binding'):
        gate.validate_evidence(old, gate.candidate_binding(root, project), 0)


def test_container_missing_engine_is_blocked(repository):
    from quality import target_runtime_gate as gate
    _, root = repository
    project = runtime_v4_fixture(root)['projects'][0]
    (root/gate.ENGINE).unlink()
    with pytest.raises(gate.RuntimeValidationBlocked, match='engine unavailable'):
        gate.validate_target(root, project)


def test_container_builder_unavailable_is_blocked_not_pass(repository, monkeypatch):
    from quality import complete_project, target_runtime_gate as gate
    _, root = repository
    approved_runtime_fixture(root)
    def engine(candidate, project, output):
        evidence = runtime_pass_evidence(gate.candidate_binding(candidate, project))
        evidence.update(TARGET_RUNTIME_CONTAINER_VALIDATION='BLOCKED', blocked_reason='LINUX_BUILDER_UNAVAILABLE')
        output.write_text(json.dumps(evidence), encoding='utf-8')
        return 3
    monkeypatch.setattr(gate, 'execute_engine', engine)
    with pytest.raises(gate.RuntimeValidationBlocked, match='builder unavailable'):
        complete_project.complete(root, 'demo')


def test_container_no_output_or_mutation_cannot_complete(repository, monkeypatch):
    from quality import target_runtime_gate as gate
    _, root = repository
    project = approved_runtime_fixture(root)
    monkeypatch.setattr(gate, 'execute_engine', lambda *args: 0)
    with pytest.raises(ValueError, match='no machine evidence'):
        gate.validate_target(root, project)
    def engine(candidate, selected, output):
        output.write_text(json.dumps(runtime_pass_evidence(gate.candidate_binding(candidate, selected))), encoding='utf-8')
        (candidate/'Dockerfile').write_text('changed\n')
        return 0
    monkeypatch.setattr(gate, 'execute_engine', engine)
    with pytest.raises(ValueError, match='clean|changed'):
        gate.validate_target(root, project)


@pytest.mark.parametrize('raw', [
    '{"schema_version":"target-runtime-evidence/1","schema_version":"forged"}',
    '{"schema_version":"target-runtime-evidence/1","image_id":NaN}',
    '[]',
])
def test_container_evidence_json_is_strict_and_object(repository, monkeypatch, raw):
    from quality import target_runtime_gate as gate
    _, root = repository
    project = approved_runtime_fixture(root)
    def engine(candidate, selected, output):
        output.write_text(raw, encoding='utf-8')
        return 0
    monkeypatch.setattr(gate, 'execute_engine', engine)
    with pytest.raises(ValueError, match='evidence JSON'):
        gate.validate_target(root, project)


@pytest.mark.parametrize('mutation', ['windows', 'builder_missing', 'wrong_observed_image',
                                    'wrong_observed_commit', 'wrong_observed_compose', 'git_present',
                                    'production_mount', 'production_role', 'missing_probe', 'failed_probe'])
def test_runtime_observation_and_probe_contract_rejects_false_pass(mutation):
    from quality import target_runtime_gate as gate
    binding = {'commit': 'c'*40, 'tree': 'd'*40, 'source_sha256': {'manifest.json': 'e'*64}}
    evidence = runtime_pass_evidence(binding)
    if mutation == 'windows': evidence['builder']['os'] = 'windows'
    elif mutation == 'builder_missing': del evidence['builder']
    elif mutation == 'wrong_observed_image': evidence['observed_identity']['image_id'] = 'sha256:' + 'f'*64
    elif mutation == 'wrong_observed_commit': evidence['observed_identity']['oci_revision'] = 'f'*40
    elif mutation == 'wrong_observed_compose': evidence['observed_identity']['rendered_compose_sha256'] = 'f'*64
    elif mutation == 'git_present': evidence['observed_identity']['git_metadata_present'] = True
    elif mutation == 'production_mount': evidence['observed_identity']['production_volumes_mounted'] = True
    elif mutation == 'production_role': evidence['observed_identity']['authorization_role'] = 'production'
    elif mutation == 'missing_probe': del evidence['probes']['preview_write_rejected']
    else: evidence['probes']['wrong_image_rejected'] = 'FAIL'
    with pytest.raises(ValueError): gate.validate_evidence(evidence, binding, 0)


@pytest.mark.parametrize('static,code,reason', [('PASS', 0, 'LINUX_BUILDER_UNAVAILABLE'),
                                            ('FAIL', 3, 'LINUX_BUILDER_UNAVAILABLE'),
                                            ('PASS', 3, 'anything_else')])
def test_arbitrary_blocked_claim_is_failure(static, code, reason):
    from quality import target_runtime_gate as gate
    binding = {'commit': 'c'*40, 'tree': 'd'*40, 'source_sha256': {}}
    evidence = runtime_pass_evidence(binding)
    evidence.update(TARGET_RUNTIME_STATIC_VALIDATION=static, TARGET_RUNTIME_CONTAINER_VALIDATION='BLOCKED',
                    blocked_reason=reason)
    with pytest.raises(ValueError) as caught: gate.validate_evidence(evidence, binding, code)
    assert not isinstance(caught.value, gate.RuntimeValidationBlocked)


def test_completion_unknown_and_frozen_still_fail_closed(repository):
    from quality import complete_project
    _, root = repository
    with pytest.raises(ValueError, match='Unknown'):
        complete_project.complete(root, 'unknown')
    data = runtime_v4_fixture(root, target='library_only')
    data['projects'][0]['status'] = 'frozen'
    write_registry(root, data)
    with pytest.raises(ValueError, match='not ready'):
        complete_project.complete(root, 'demo')


def test_runtime_contract_unknown_fields_and_unsafe_policy_rejected(repository):
    _, root = repository
    data = runtime_v4_fixture(root)
    path = root/'feature/runtime.json'
    original = json.loads(path.read_text())
    for patch in [{'host_path': '/home/ubuntu/production'}, {'production_policy': {}},
                  {'preview_policy': {'production_write': True, 'production_rw_mounts': True}},
                  {'working_directory': '/app/../production'}, {'runtime_roots': []},
                  {'required_mounts': []}, {'validation_probes': ['static_only']}, {'entrypoint': []}]:
        path.write_text(json.dumps({**original, **patch}), encoding='utf-8')
        with pytest.raises(ValueError): registry.validate_registry(data, root)


def test_v4_legacy_provenance_preserves_only_exact_current_records(repository):
    _, root = repository
    legacy = minimal_registry()
    old_commit = registry.git(root, 'rev-parse', 'origin/main')
    mixed = {**copy.deepcopy(legacy), 'schema_version': 'project-registry/4', 'legacy_registry_commit': old_commit}
    registry.validate_registry(mixed, root)
    changes = {'owned_paths': ['other'], 'required_tests': [], 'status': 'frozen',
               'boundary_notes': 'changed', 'capabilities': ['changed'], 'forbidden_paths': [],
               'project_id': 'new-project'}
    for key, value in changes.items():
        candidate = copy.deepcopy(mixed)
        candidate['projects'][0][key] = value
        with pytest.raises(ValueError, match='runtime_target'):
            registry.validate_registry(candidate, root)
    migrated = copy.deepcopy(mixed)
    migrated['projects'][0].update(runtime_target='library_only', boundary_notes='explicit migration fixture')
    registry.validate_registry(migrated, root)
    write_registry(root, migrated)
    registry.git(root, 'add', '.')
    registry.git(root, '-c', 'commit.gpgsign=false', 'commit', '-m', 'explicit runtime migration')
    registry.git(root, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    with pytest.raises(ValueError, match='runtime_target'):
        registry.validate_registry(mixed, root)


def test_v4_legacy_reference_rejects_untrusted_or_nonlegacy_objects(repository):
    _, root = repository
    mixed = {**minimal_registry(), 'schema_version': 'project-registry/4'}
    for value in ['HEAD', 'HEAD^{commit}', 'a'*12, 'a'*40,
                  registry.git(root, 'rev-parse', 'HEAD^{tree}')]:
        mixed['legacy_registry_commit'] = value
        with pytest.raises(ValueError): registry.validate_registry(mixed, root)
    legacy_commit = registry.git(root, 'rev-parse', 'HEAD')
    # An otherwise valid v4 source cannot recursively supply legacy authority.
    data = runtime_v4_fixture(root, target='library_only')
    write_registry(root, data)
    registry.git(root, 'add', '.')
    registry.git(root, '-c', 'commit.gpgsign=false', 'commit', '-m', 'v4 source')
    newer = registry.git(root, 'rev-parse', 'HEAD')
    mixed['legacy_registry_commit'] = newer
    with pytest.raises(ValueError): registry.validate_registry(mixed, root)  # not yet on main
    registry.git(root, 'update-ref', 'refs/remotes/origin/main', newer)
    with pytest.raises(ValueError, match='Registry/1-3'): registry.validate_registry(mixed, root)
    data = minimal_registry()
    data['legacy_registry_commit'] = legacy_commit
    with pytest.raises(ValueError, match='Unexpected'): registry.validate_registry(data, root)


def test_runtime_build_input_case_aliases_and_duplicate_roles_rejected(repository):
    _, root = repository
    data = runtime_v4_fixture(root)
    path = root/'feature/runtime.json'
    original = json.loads(path.read_text())
    for duplicates in [['Dockerfile'], ['dockerfile'], ['requirements.txt', 'REQUIREMENTS.txt']]:
        contract = copy.deepcopy(original)
        contract['build']['dependency_contracts'] = duplicates
        path.write_text(json.dumps(contract), encoding='utf-8')
        with pytest.raises(ValueError, match='duplicate identities'):
            registry.validate_registry(data, root)


def test_registry_v4_migration_preserves_real_legacy_records_and_scope():
    current = registry.load_registry(ROOT)
    assert current == registration_baseline()
    assert current['schema_version'] == 'project-registry/4'
    legacy = json.loads(registry.git(ROOT, 'show', f"{current['legacy_registry_commit']}:{registry.REGISTRY_PATH}"))
    assert current['protected_paths'] == legacy['protected_paths']
    by_id = {p['project_id']: p for p in current['projects']}
    for old in legacy['projects']:
        expected = copy.deepcopy(old)
        if old['project_id'] == 'dev-governance':
            expected['runtime_target'] = 'none'
            expected['future_owned_paths'] = ADMISSION_GOVERNANCE_FILES
            expected['future_required_tests'] = ADMISSION_GOVERNANCE_TESTS
        elif old['project_id'] == 'shared-intraday':
            expected['future_owned_paths'].remove('04_scripts/capture_public_intraday.py')
            expected['shared_dependencies'].append('04_scripts/capture_public_intraday.py')
            expected['runtime_target'] = 'library_only'
            expected['boundary_notes'] += ' PROD-RUNTIME-V2 transfers only 04_scripts/capture_public_intraday.py to spread-production-runtime-wiring for explicit Git/OCI identity initialization and argument wiring. Remaining Shared Intraday schema, providers, pipelines and tests are library_only and retain their existing boundaries. The transferred CLI remains a read-only dependency here; this project cannot change it or claim container deployability. Its capture semantics, exact-contract queries, dates, secret-file contract and immutable SEALED behavior must remain unchanged.'
        assert by_id[old['project_id']] == expected
    assert by_id['shared-production-infrastructure'] == POST_TRANSFER_INFRA_REGISTRATION
    assert by_id['spread-production-runtime-wiring'] == SPREAD_RUNTIME_WIRING_REGISTRATION
    assert by_id['release-refresh'] == RELEASE_REFRESH_REGISTRATION
    assert set(by_id) == {p['project_id'] for p in legacy['projects']} | {'shared-production-infrastructure', 'shared-runtime-manifest', 'public-intraday-runtime', 'spread-production-runtime-wiring', 'soybean-production-input-authority', 'xiaoran-production-data-delivery', 'domestic-spread-status', 'windows-wrapper-platform', 'high-risk-execution-path-closure', 'release-refresh', 'rollback-evidence-lifecycle', 'release-reentry'}


def test_historical_rollback_registration_preserves_exact_consumer_and_production_boundaries():
    _, project = registry.select_project(ROOT, 'rollback-evidence-lifecycle')
    assert project['change_class'] == 'shared' and project['runtime_target'] == 'library_only'
    assert project['owned_paths'] == ['04_scripts/runtime/historical_primary_rollback.py',
                                      '08_tests/test_historical_primary_rollback.py']
    assert set(project['shared_dependencies']) == {
        '09_deploy/spread_release/high_risk_execution.py', '09_deploy/runtime_identity/host_authorization.py',
        '08_tests/shared/high_risk_execution_docker_e2e.py', '04_scripts/runtime/说明.md',
        '08_tests/test_project_registry.py'}
    assert project['forbidden_paths'] == ['03_src', '05_apps', '01_data', '06_outputs', '10_logs',
        '02_configs/production_runtime_trust.json', '02_configs/runtime_contracts',
        '09_deploy/runtime_identity/production_authorization.schema.json',
        '09_deploy/runtime_identity/candidate_validation_record.py']
    assert '08_tests/test_historical_primary_rollback.py' in project['required_tests']
    assert project['future_owned_paths'] == project['future_required_tests'] == []


def test_production_infrastructure_registration_has_only_exact_new_ownership():
    current, project = registry.select_project(ROOT, 'shared-production-infrastructure')
    assert project == POST_TRANSFER_INFRA_REGISTRATION
    assert project['runtime_target'] == 'library_only' and project['change_class'] == 'shared'
    assert not project.get('reserved_paths')
    for path in project['owned_paths'] + project['future_owned_paths']:
        assert Path(path).suffix
        assert registry.owns(project, path)
        assert all(not registry.owns(other, path) for other in current['projects'] if other is not project)
    for path in ['Dockerfile', 'docker-compose.yml', '04_scripts/quality/complete_project.py',
                 '03_src/agri_research_agent/automation/full_daily_windows.py',
                 '03_src/agri_research_agent/shared/async_update.py',
                 '03_src/agri_research_agent/import_profit/cnf_store.py',
                 '03_src/agri_research_agent/market_data/intraday.py',
                 '09_deploy/spread_release/release_contract.py',
                 '09_deploy/runtime_identity/unapproved.py']:
        assert not registry.owns(project, path), path


def test_production_grant_contract_registration_is_exact_and_readonly_dependencies():
    current, project = registry.select_project(ROOT, 'shared-production-infrastructure')
    assert project == POST_TRANSFER_INFRA_REGISTRATION
    assert project['runtime_target'] == 'library_only'
    assert project['future_owned_paths'][9:11] == [
        '03_src/agri_research_agent/shared/production_grant.py',
        '08_tests/shared/test_production_grant.py']
    assert '08_tests/shared/test_production_grant.py' in project['future_required_tests']
    assert project['shared_dependencies'][-2:] == [
        '03_src/agri_research_agent/shared/runtime_manifest.py',
        '02_configs/runtime_manifest.schema.json']
    for path in ['03_src/agri_research_agent/shared/production_grant.py',
                 '08_tests/shared/test_production_grant.py']:
        assert registry.owns(project, path)
        assert not registry.owns(project, path + '/sibling.py')
        assert all(not registry.owns(other, path) for other in current['projects'] if other is not project)
    for path in project['shared_dependencies'][-2:]:
        assert not registry.owns(project, path)


def test_deployability_engine_registration_is_exact_and_does_not_grant_scripts_directory():
    current, project = registry.select_project(ROOT, 'shared-production-infrastructure')
    assert project['future_owned_paths'][:14] == DEPLOYABILITY_ENGINE_REGISTRATION['future_owned_paths']
    assert project['future_owned_paths'][11:14] == [
        '04_scripts/runtime/validate_target_runtime.py',
        '08_tests/test_target_runtime_validator.py',
        '07_docs/projects/production-runtime-v2/目标RuntimeDeployabilityGate.md']
    assert project['future_required_tests'][3] == '08_tests/test_target_runtime_validator.py'
    for path in project['future_owned_paths'][11:14]:
        assert registry.owns(project, path)
        assert all(not registry.owns(other, path) for other in current['projects'] if other is not project)
    for path in ['04_scripts/runtime/other.py', '04_scripts/quality/complete_project.py',
                 '08_tests/test_target_runtime_validator_other.py',
                 '07_docs/projects/production-runtime-v2/unregistered.md']:
        assert not registry.owns(project, path)
    for path in ['03_src/agri_research_agent/shared/production_grant_extra.py',
                 '08_tests/shared/test_production_grant_other.py',
                 '03_src/agri_research_agent/shared/runtime_manifest.py/child.py']:
        assert not registry.owns(project, path)


def test_pre_release_registration_is_only_four_files_and_two_tests():
    current, project = registry.select_project(ROOT, 'shared-production-infrastructure')
    assert project == POST_TRANSFER_INFRA_REGISTRATION
    assert project['runtime_target'] == 'library_only'
    assert project['future_owned_paths'][:14] == DEPLOYABILITY_ENGINE_REGISTRATION['future_owned_paths']
    assert project['future_required_tests'][:-2] == DEPLOYABILITY_ENGINE_REGISTRATION['future_required_tests']
    assert project['future_owned_paths'][14:18] == [
        '09_deploy/runtime_identity/candidate_validation_record.py',
        '04_scripts/runtime/pre_release_runtime.py',
        '08_tests/test_candidate_validation_record.py',
        '08_tests/test_pre_release_runtime.py']
    assert project['future_required_tests'][-2:] == [
        '08_tests/test_candidate_validation_record.py',
        '08_tests/test_pre_release_runtime.py']
    assert project['forbidden_paths'] == DEPLOYABILITY_ENGINE_REGISTRATION['forbidden_paths']
    assert project['shared_dependencies'] == DEPLOYABILITY_ENGINE_REGISTRATION['shared_dependencies']
    for path in project['future_owned_paths'][14:18]:
        assert registry.owns(project, path)
        assert not registry.owns(project, path + '/sibling.py')
        assert not registry.owns(project, path + '/child.py')
        assert all(not registry.owns(other, path) for other in current['projects'] if other is not project)
    assert all(path not in project['future_owned_paths'] for path in (
        '07_docs/projects/production-runtime-v2/预发布证据消费合同.md',
        '04_scripts/runtime', '09_deploy/runtime_identity'))


def test_runtime_manifest_registration_is_exact_and_non_overlapping():
    current, project = registry.select_project(ROOT, 'shared-runtime-manifest')
    assert project == RUNTIME_MANIFEST_REGISTRATION
    assert project['runtime_target'] == 'library_only' and project['change_class'] == 'shared'
    assert project['owned_paths'] == [] and not project.get('reserved_paths')
    for path in project['future_owned_paths']:
        assert registry.owns(project, path)
        assert not registry.owns(project, path + '/sibling.py')
        assert all(not registry.owns(other, path) for other in current['projects'] if other is not project)
    for path in ['03_src/agri_research_agent/shared/runtime_manifest_extra.py',
                 '02_configs/runtime_manifest.schema.json.bak',
                 '07_docs/projects/其他合同.md',
                 '09_deploy/runtime_identity/host_authorization.py',
                 '04_scripts/quality/target_runtime_gate.py']:
        assert not registry.owns(project, path)


def test_pm_unfreeze_exact_registration_contract():
    data, project = registry.select_project(ROOT, 'soybean-pm')
    baseline = registration_baseline()
    assert data == baseline
    assert project['status'] == 'ready' and project['change_class'] == 'business'
    assert not project.get('reserved_paths')
    assert project['owned_paths'][-4:] == PM_EXISTING_ADDITIONS
    assert project['future_owned_paths'] == PM_FUTURE_PATHS
    assert project['future_required_tests'] == PM_FUTURE_TESTS
    original_tests = ['08_tests/test_import_profit_' + name + '.py' for name in (
        'market_snapshot', 'cnf_store', 'result_store', 'runtime_page')]
    assert project['required_tests'] == original_tests + PM_EXISTING_TEST_ADDITIONS
    assert len(set(project['required_tests'] + project['future_required_tests'])) == 34
    assert all(registry.owns(project, p) for p in PM_EXISTING_ADDITIONS + PM_FUTURE_PATHS)
    assert all(not registry.owns(project, p + '/unapproved.py') for p in PM_FUTURE_PATHS)
    assert all(not registry.owns(project, p) for p in project['shared_dependencies'] + project['forbidden_paths'])
    assert '77个PM_OWNED' in project['boundary_notes']
    assert '不新增目录或reserved namespace' in project['boundary_notes']


@pytest.mark.parametrize('path', PM_EXISTING_ADDITIONS + PM_FUTURE_PATHS + [
    '02_configs/public_intraday_schedule.yaml',
    '03_src/agri_research_agent/data_sources/tankan/client.py',
    '03_src/agri_research_agent/data_sources/tankan/models.py',
    '03_src/agri_research_agent/data_sources/tankan/queries.py',
    '03_src/agri_research_agent/market_data/calendars.py',
    '03_src/agri_research_agent/market_data/intraday.py',
    '03_src/agri_research_agent/pipelines/public_intraday.py',
    '04_scripts/capture_public_intraday.py',
    '05_apps/streamlit_app.py',
    '08_tests/data_sources/tankan/test_client.py',
    '08_tests/data_sources/tankan/test_domestic_spread.py',
    '08_tests/data_sources/tankan/test_models.py',
    '08_tests/market_data/test_intraday.py',
    '08_tests/pipelines/test_public_intraday.py',
    '08_tests/test_public_intraday_schedule.py',
    '03_src/agri_research_agent/pipelines/public_data_refresh.py',
    '03_src/agri_research_agent/pipelines/lutou_weather.py',
    '03_src/agri_research_agent/pipelines/lutou_domestic_basis.py',
    '03_src/agri_research_agent/pipelines/other_soybean.py',
    '05_apps/import_profit_unapproved.py',
    '08_tests/test_import_profit_unapproved.py',
    '01_data/manual/cnf.parquet',
])
def test_pm_unfreeze_scope_positive_and_negative(path):
    _, project = registry.select_project(ROOT, 'soybean-pm')
    def synthetic_git(root, *args):
        if args == ('diff', '--name-only', '--no-renames', 'base...HEAD'):
            return path + '\n'
        if args[:1] == ('rev-parse',):
            return 'fixture-head'
        return ''
    report = scope.run_audit(ROOT, 'base', project['owned_paths'],
                             exact_allowed=project['future_owned_paths'],
                             change_class='business', git=synthetic_git)
    expected = 'PASS' if path in PM_EXISTING_ADDITIONS + PM_FUTURE_PATHS else 'FAIL'
    assert report['PROJECT_SCOPE'] == expected


def write_registry(root, data):
    path=root/registry.REGISTRY_PATH
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data),encoding="utf-8")


@pytest.fixture
def repository(tmp_path):
    root=tmp_path/'main'
    root.mkdir()
    for path in ('feature/code.py','other/code.py','shared/code.py','tests/test_feature.py'):
        target=root/path
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text('# fixture\n',encoding='utf-8')
    write_registry(root,minimal_registry())
    registry.git(root,'init','-b','main')
    registry.git(root,'config','user.name','Scope Fixture')
    registry.git(root,'config','user.email','fixture@example.invalid')
    registry.git(root,'add','--','feature','other','shared','tests','02_configs')
    registry.git(root,'-c','commit.gpgsign=false','commit','-m','fixture baseline')
    registry.git(root,'update-ref','refs/remotes/origin/main','HEAD')
    feature=tmp_path/'feature'
    registry.git(root,'worktree','add','-b','feat/demo',str(feature),'HEAD')
    return root,feature


def test_real_registry_schema_paths_and_minimum_projects():
    data=registry.load_registry(ROOT)
    projects={p['project_id']:p for p in data['projects']}
    assert {'soybean-pm','weather-basis-push','international-spread','weather','domestic-basis',
            'usda','oil-world','palm','canola'} <= projects.keys()
    assert projects['soybean-pm']['status']=='ready'
    assert not projects['weather-basis-push']['owned_paths']
    assert projects['canola']['status']=='needs-boundary-review'
    assert len(projects['soybean-pm']['capabilities'])==7
    assert all('03_src/agri_research_agent/shared' in p['forbidden_paths'] for p in projects.values() if p['change_class']=='business')


@pytest.mark.parametrize('value',['','.', '..','a/../b','/tmp','C:/tmp','a\\b','a/**','a//b',' a','a\n'])
def test_registry_path_escape_rejected(value):
    with pytest.raises(ValueError):
        registry.relative_path(value)


def test_unknown_missing_and_bad_schema_fail(repository):
    _,root=repository
    with pytest.raises(ValueError,match='Unknown'):
        registry.select_project(root,'unknown')
    (root/registry.REGISTRY_PATH).unlink()
    with pytest.raises(ValueError,match='unavailable'):
        registry.load_registry(root)
    write_registry(root,{'schema_version':'old'})
    with pytest.raises(ValueError,match='schema'):
        registry.load_registry(root)


def test_duplicate_project_and_missing_path_fail(repository):
    _,root=repository
    data=minimal_registry()
    data['projects'].append(copy.deepcopy(data['projects'][0]))
    with pytest.raises(ValueError,match='duplicate'):
        registry.validate_registry(data,root)
    data=minimal_registry()
    data['projects'][0]['owned_paths']=['does-not-exist']
    with pytest.raises(ValueError,match='missing'):
        registry.validate_registry(data,root)


def test_project_business_pass_and_cross_project_fail(repository,monkeypatch,capsys):
    _,root=repository
    monkeypatch.setattr(scope,'PROJECT_ROOT',root)
    (root/'feature/code.py').write_text('# change\n')
    assert scope.main(['--project','demo'])==0
    report=json.loads(capsys.readouterr().out)
    assert report['PROJECT_SCOPE']=='PASS'
    assert report['comparison']=='origin/main...HEAD'
    (root/'other/code.py').write_text('# cross project\n')
    assert scope.main(['--project','demo'])==1
    report=json.loads(capsys.readouterr().out)
    assert report['forbidden_changes']==['other/code.py']


@pytest.mark.parametrize('args,expected',[
    (['--project','demo','--owned','shared'],0),
    (['--project','demo','--change-class','shared'],0),
    (['--project','demo','--baseline','HEAD'],2),
    (['--project','demo','--known-existing','other'],2),
    (['--owned','feature'],0),
    (['--project','unknown'],2),
])
def test_explicit_scope_and_test_class_are_not_registry_authorization(repository,monkeypatch,args,expected):
    _,root=repository
    monkeypatch.setattr(scope,'PROJECT_ROOT',root)
    assert scope.main(args)==expected


def test_registry_metadata_edit_does_not_override_forbidden_scope(repository,monkeypatch):
    _,root=repository
    data=minimal_registry()
    data['projects'][0]['owned_paths'].append('other')
    write_registry(root,data)
    monkeypatch.setattr(scope,'PROJECT_ROOT',root)
    assert scope.main(['--project','demo'])==0
    (root/'other/code.py').write_text('# unrelated change\n')
    assert scope.main(['--project','demo'])==1


def test_shared_file_and_rename_cannot_hide(repository,monkeypatch,capsys):
    _,root=repository
    monkeypatch.setattr(scope,'PROJECT_ROOT',root)
    (root/'shared/code.py').rename(root/'feature/moved.py')
    registry.git(root,'add','--','shared/code.py','feature/moved.py')
    # Root custom protection is applied independent of ownership and renames.
    assert scope.main(['--project','demo'])==1
    report=json.loads(capsys.readouterr().out)
    assert 'shared/code.py' in report['shared_changes']
    assert report['SHARED_CHANGE']=='YES'


def test_shared_low_level_still_passes(repository,monkeypatch,capsys):
    _,root=repository
    monkeypatch.setattr(scope,'PROJECT_ROOT',root)
    target=root/'04_scripts/quality'
    target.mkdir(parents=True)
    (target/'helper.py').write_text('# fixture')
    assert scope.main(['--change-class','shared','--owned','04_scripts/quality'])==0
    assert json.loads(capsys.readouterr().out)['SHARED_CHANGE']=='YES'


def test_main_clean_mirror_failures(repository):
    main,feature=repository
    assert registry.assert_main_mirror(feature)['main_clean_mirror']
    (main/'feature/code.py').write_text('# dirty')
    with pytest.raises(ValueError,match='NOT_CLEAN'):
        registry.assert_main_mirror(feature)
    (main/'feature/code.py').write_text('# fixture\n')
    registry.git(feature,'commit','--allow-empty','-m','fixture only')
    registry.git(feature,'update-ref','refs/remotes/origin/main','HEAD')
    with pytest.raises(ValueError,match='NOT_MIRROR'):
        registry.assert_main_mirror(feature)


def test_startup_fetches_fresh_remote_and_does_not_modify_main(repository,monkeypatch,tmp_path):
    main,feature=repository
    original=registry.git
    calls=[]
    head=original(main,'rev-parse','HEAD')
    def local_git(root,*args):
        calls.append(args)
        if args==('fetch','origin'):
            return ''
        if args[:1]==('ls-remote',):
            return head+'\trefs/heads/main'
        return original(root,*args)
    monkeypatch.setattr(registry,'git',local_git)
    destination=tmp_path/'new-project'
    result=start_project.prepare(feature,'demo','feat/new',destination)
    assert not destination.exists() and not result['created']
    result=start_project.prepare(feature,'demo','feat/new',destination,create=True)
    assert result['created'] and result['baseline_head']==head
    assert original(main,'status','--porcelain')==''
    assert original(main,'rev-parse','HEAD')==head
    assert calls[:2]==[('fetch','origin'),('ls-remote','--exit-code','origin','refs/heads/main')]


def test_startup_remote_drift_stops_before_creation(repository,monkeypatch,tmp_path):
    _,feature=repository
    original=registry.git
    def drift(root,*args):
        if args==('fetch','origin'):
            return ''
        if args[:1]==('ls-remote',):
            return '0'*40+'\trefs/heads/main'
        return original(root,*args)
    monkeypatch.setattr(registry,'git',drift)
    with pytest.raises(ValueError,match='REMOTE_MOVED'):
        start_project.prepare(feature,'demo','feat/new',tmp_path/'new',create=True)
    assert not (tmp_path/'new').exists()


def test_owned_rename_is_not_reported_as_arrow_path(repository,monkeypatch,capsys):
    _,root=repository
    monkeypatch.setattr(scope,'PROJECT_ROOT',root)
    registry.git(root,'mv','feature/code.py','feature/new.py')
    assert scope.main(['--project','demo'])==0
    assert json.loads(capsys.readouterr().out)['changed_files']==['feature/code.py','feature/new.py']


@pytest.mark.parametrize('status',['frozen','needs-boundary-review'])
def test_project_status_metadata_is_not_development_approval(repository,monkeypatch,status):
    _,root=repository
    data=minimal_registry()
    data['projects'][0]['status']=status
    write_registry(root,data)
    monkeypatch.setattr(scope,'PROJECT_ROOT',root)
    assert scope.main(['--project','demo'])==0


def test_startup_cannot_create_inside_main(repository,monkeypatch):
    main,feature=repository
    original=registry.git
    def local_git(root,*args):
        if args==('fetch','origin'):
            return ''
        if args[:1]==('ls-remote',):
            return original(main,'rev-parse','HEAD')+'\trefs/heads/main'
        return original(root,*args)
    monkeypatch.setattr(registry,'git',local_git)
    with pytest.raises(ValueError,match='OUTSIDE_CALLER'):
        start_project.prepare(feature,'demo','feat/new',main/'nested',create=True)
    assert not (main/'nested').exists()


def bootstrap_registry(root):
    data = minimal_registry()
    data['schema_version'] = 'project-registry/2'
    project = data['projects'][0]
    project['future_owned_paths'] = ['new_module/quotes.py', '08_tests/test_quotes.py']
    project['future_required_tests'] = ['08_tests/test_quotes.py']
    write_registry(root, data)
    registry.git(root, 'add', '--', registry.REGISTRY_PATH)
    registry.git(root, 'commit', '-m', 'fixture future registry')
    registry.git(root, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    return data


def test_future_missing_and_existing_state_and_exact_scope(repository, monkeypatch, capsys):
    _, root = repository
    data = bootstrap_registry(root)
    assert registry.validate_registry(data, root) == data
    # Unit fixture mirror remains independent; registry is approved on fixture remote.
    monkeypatch.setattr(registry, 'assert_main_mirror', lambda root: {})
    monkeypatch.setattr(scope, 'PROJECT_ROOT', root)
    target = root/'new_module/quotes.py'
    target.parent.mkdir()
    target.write_text('# real future creation\n')
    assert registry.validate_registry(data, root) == data
    assert scope.main(['--project', 'demo']) == 0
    assert json.loads(capsys.readouterr().out)['PROJECT_SCOPE'] == 'PASS'
    (target.parent/'random.py').write_text('# undeclared\n')
    assert scope.main(['--project', 'demo', '--owned', 'new_module/quotes.py']) == 1
    assert json.loads(capsys.readouterr().out)['out_of_scope_changes'] == ['new_module/random.py']
    assert scope.main(['--project', 'unknown']) == 2
    data['projects'][0]['status'] = 'frozen'
    write_registry(root, data)
    assert scope.main(['--project', 'demo']) == 0


@pytest.mark.parametrize('path', ['a/*.py', 'a/**', '../a.py', '/a.py', 'C:/a.py',
                                 '\\\\server\\a.py', 'a/../../x.py', 'new_module/',
                                 'new_module', 'a/file.py.', 'a/NUL.py', 'a/file.py:stream'])
def test_future_invalid_paths(repository, path):
    _, root = repository
    data = bootstrap_registry(root)
    data['projects'][0]['future_owned_paths'] = [path]
    with pytest.raises(ValueError):
        registry.validate_registry(data, root)


def test_future_windows_identity_and_no_prefix_ownership(repository):
    _, root = repository
    data = bootstrap_registry(root)
    project = data['projects'][0]
    assert registry.owns(project, 'NEW_MODULE\\QUOTES.PY')
    assert not registry.owns(project, 'new_module/quotes.py/child.py')
    assert not registry.owns(project, 'new_module/quotes_helper.py')
    project['future_owned_paths'].append('NEW_MODULE\\QUOTES.PY')
    with pytest.raises(ValueError, match='Duplicate'):
        registry.validate_registry(data, root)


def test_future_start_allows_missing_without_creating_files(repository, monkeypatch, tmp_path):
    _, root = repository
    bootstrap_registry(root)
    original = registry.git
    def local_git(root, *args):
        if args == ('fetch', 'origin'):
            return ''
        if args[:1] == ('ls-remote',):
            return original(root, 'rev-parse', 'origin/main') + '\trefs/heads/main'
        return original(root, *args)
    monkeypatch.setattr(registry, 'git', local_git)
    monkeypatch.setattr(registry, 'assert_main_mirror', lambda root: {})
    result = start_project.prepare(root, 'demo', 'feat/future', tmp_path/'future-start')
    assert not result['created']
    assert result['future_required_tests'] == ['08_tests/test_quotes.py']
    assert not (root/'08_tests/test_quotes.py').exists()


@pytest.mark.parametrize('existing', [True, False])
def test_future_ownership_collision(repository, existing):
    _, root = repository
    data = bootstrap_registry(root)
    other = copy.deepcopy(data['projects'][0])
    other['project_id'] = 'other-project'
    other['future_owned_paths'] = [] if existing else ['NEW_MODULE\\QUOTES.PY']
    other['future_required_tests'] = []
    other['owned_paths'] = ['new_module'] if existing else []
    other['status'] = 'needs-boundary-review'
    if existing:
        (root/'new_module').mkdir()
    data['projects'].append(other)
    with pytest.raises(ValueError, match='collision'):
        registry.validate_registry(data, root)


def test_future_test_collision_even_under_existing_ownership(repository):
    _, root = repository
    data = bootstrap_registry(root)
    (root/'08_tests').mkdir()
    project = data['projects'][0]
    project['future_owned_paths'] = ['new_module/quotes.py']
    project['owned_paths'].append('08_tests')
    other = copy.deepcopy(project)
    other.update(project_id='other-project', future_owned_paths=[], future_required_tests=[])
    data['projects'].append(other)
    with pytest.raises(ValueError, match='test ownership collision'):
        registry.validate_registry(data, root)


@pytest.mark.parametrize('field', ['forbidden_paths', 'shared_dependencies', 'protected_paths'])
def test_future_cannot_override_protection(repository, field):
    _, root = repository
    data = bootstrap_registry(root)
    (root/'new_module').mkdir()
    owner = data if field == 'protected_paths' else data['projects'][0]
    owner[field].append('new_module')
    with pytest.raises(ValueError, match='conflicts|protected'):
        registry.validate_registry(data, root)


def test_future_minimum_shared_protection(repository):
    _, root = repository
    data = bootstrap_registry(root)
    data['projects'][0]['future_owned_paths'].append('03_src/agri_research_agent/market_data/new.py')
    with pytest.raises(ValueError, match='protected'):
        registry.validate_registry(data, root)


def test_future_test_area_ownership_and_existing_rules(repository):
    _, root = repository
    data = bootstrap_registry(root)
    for field, value in [('future_required_tests', 'outside/test_x.py'),
                         ('future_required_tests', '08_tests/test_not_owned.py'),
                         ('owned_paths', 'missing.py'), ('required_tests', '08_tests/missing.py')]:
        candidate = copy.deepcopy(data)
        candidate['projects'][0][field] = [value]
        with pytest.raises(ValueError):
            registry.validate_registry(candidate, root)
    (root/'new_module/quotes.py').mkdir(parents=True)
    with pytest.raises(ValueError, match='not a repository file'):
        registry.validate_registry(data, root)


def test_future_symlink_or_junction_escape(repository, tmp_path):
    import os
    _, root = repository
    data = bootstrap_registry(root)
    outside = tmp_path/'outside'
    outside.mkdir()
    link = root/'new_module'
    if os.name == 'nt':
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], check=True, capture_output=True)
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(ValueError, match='link'):
            registry.validate_registry(data, root)
    finally:
        if os.name == 'nt':
            link.rmdir()
        else:
            link.unlink()


def test_future_completion_lifecycle_real_pytest(repository):
    from quality import complete_project
    _, root = repository
    bootstrap_registry(root)
    with pytest.raises(ValueError, match='missing at completion'):
        complete_project.complete(root, 'demo')
    target = root/'08_tests/test_quotes.py'
    target.parent.mkdir()
    target.write_text('def test_quote():\n    assert True\n')
    # Existing mandatory test must also execute (fixture initially only a comment).
    (root/'tests/test_feature.py').write_text('def test_existing():\n    assert True\n')
    result = complete_project.complete(root, 'demo')
    assert result['PROJECT_COMPLETION'] == 'PASS'
    assert [t['passed'] for t in result['executed_tests']] == [1, 1]
    target.write_text('def test_quote():\n    assert False\n')
    with pytest.raises(ValueError, match='execution failed'):
        complete_project.complete(root, 'demo')


@pytest.mark.parametrize('body', ['# empty\n', 'import pytest\n@pytest.mark.skip\ndef test_skip(): pass\n',
                                'import pytest\n@pytest.mark.xfail\ndef test_xfail(): assert False\n'])
def test_completion_no_empty_skip_or_xfail(repository, body):
    from quality import complete_project
    _, root = repository
    bootstrap_registry(root)
    target = root/'08_tests/test_quotes.py'
    target.parent.mkdir()
    target.write_text(body)
    (root/'tests/test_feature.py').write_text('def test_existing(): pass\n')
    with pytest.raises(ValueError, match='execution failed|without skip'):
        complete_project.complete(root, 'demo')


def test_completion_rejects_candidate_mutation(repository):
    from quality import complete_project
    _, root = repository
    bootstrap_registry(root)
    target = root/'08_tests/test_quotes.py'
    target.parent.mkdir()
    target.write_text("from pathlib import Path\ndef test_mutation():\n    Path('feature/code.py').write_text('# changed')\n")
    (root/'tests/test_feature.py').write_text('def test_existing(): pass\n')
    with pytest.raises(ValueError, match='changed during completion'):
        complete_project.complete(root, 'demo')


def test_shared_future_cannot_change_closed_scope(repository, monkeypatch, capsys):
    _, root = repository
    data = bootstrap_registry(root)
    data['projects'][0]['change_class'] = 'shared'
    write_registry(root, data)
    monkeypatch.setattr(registry, 'assert_main_mirror', lambda root: {})
    monkeypatch.setattr(scope, 'PROJECT_ROOT', root)
    (root/'other/code.py').write_text('# closed infrastructure changed\n')
    assert scope.main(['--project', 'demo', '--change-class', 'shared']) == 1
    assert json.loads(capsys.readouterr().out)['forbidden_changes'] == ['other/code.py']


def test_registered_shared_intraday_exact_boundary():
    _, project = registry.select_project(ROOT, 'shared-intraday')
    assert project['change_class'] == 'shared' and project['status'] == 'ready'
    assert project['runtime_target'] == 'library_only'
    assert project['owned_paths'] == []
    assert len(project['future_owned_paths']) == 7
    assert len(project['future_required_tests']) == 4
    assert set(project['future_required_tests']) <= set(project['future_owned_paths'])
    assert '04_scripts/capture_public_intraday.py' not in project['future_owned_paths']
    assert '04_scripts/capture_public_intraday.py' in project['shared_dependencies']
    assert 'REAL_AM_TEMPORAL_ACCEPTANCE=DEFERRED' in project['boundary_notes']
    assert 'REAL_PM_TEMPORAL_ACCEPTANCE=DEFERRED' in project['boundary_notes']
    assert 'DEFERRED_TEMPORAL_ACCEPTANCE_BLOCKING=NO' in project['boundary_notes']
    for path in project['future_owned_paths']:
        assert registry.owns(project, path)
        assert not registry.owns(project, path + '/unapproved.py')
    for path in project['forbidden_paths'] + project['shared_dependencies']:
        assert not registry.owns(project, path)
    assert registry.select_project(ROOT, 'soybean-pm')[1]['status'] == 'ready'


def test_tankan_live_registration_is_additive_and_narrow():
    _, project = registry.select_project(ROOT, 'tankan-live-query')
    assert project['status'] == 'ready' and project['change_class'] == 'shared'
    assert project['owned_paths'] == [
        '03_src/agri_research_agent/data_sources/tankan/client.py',
        '03_src/agri_research_agent/data_sources/tankan/queries.py']
    assert project['future_owned_paths'] == project['future_required_tests'] == [
        '08_tests/data_sources/tankan/test_live_queries.py']
    assert not registry.owns(project, '03_src/agri_research_agent/data_sources/tankan/models.py')
    assert not registry.owns(project, '03_src/agri_research_agent/pipelines/public_data_daily.py')


@pytest.mark.parametrize('path,expected', [
    ('03_src/agri_research_agent/market_data/intraday.py', 'PASS'),
    ('03_src/agri_research_agent/market_data/intraday_helper_random.py', 'FAIL'),
    ('03_src/agri_research_agent/pipelines/public_data_daily.py', 'FAIL'),
    ('03_src/agri_research_agent/pipelines/lutou_weather.py', 'FAIL'),
    ('03_src/agri_research_agent/pipelines/lutou_domestic_basis.py', 'FAIL'),
    ('03_src/agri_research_agent/shared/async_update.py', 'FAIL'),
    ('05_apps/import_profit_page.py', 'FAIL'),
])
def test_registered_intraday_synthetic_scope(path, expected):
    _, project = registry.select_project(ROOT, 'shared-intraday')
    def synthetic_git(root, *args):
        if args == ('diff', '--name-only', '--no-renames', 'base...HEAD'):
            return path + '\n'
        if args[:1] == ('rev-parse',):
            return 'fixture-head'
        return ''
    report = scope.run_audit(ROOT, 'base', project['owned_paths'],
                             exact_allowed=project['future_owned_paths'],
                             change_class=project['change_class'], git=synthetic_git)
    assert report['PROJECT_SCOPE'] == expected

# Namespace reservation acceptance uses only temporary repositories.
def reservation_registry(root):
    data = minimal_registry()
    data['schema_version'] = 'project-registry/3'
    for name in ('03_src/agri_research_agent/alerts', '04_scripts', '08_tests',
                 '02_configs', '07_docs/projects'):
        (root/name).mkdir(parents=True, exist_ok=True)
    project = data['projects'][0]
    project.update(project_id='notification-push-fixture', owned_paths=['03_src/agri_research_agent/alerts'],
                   reserved_paths=['04_scripts/notifications', '08_tests/alerts',
                                   '02_configs/notifications', '07_docs/projects/notification'],
                   required_tests=[], future_required_tests=['08_tests/alerts/test_core.py'])
    return data


def test_reservation_bootstrap_real_git(tmp_path, monkeypatch, capsys):
    main = tmp_path/'main'
    main.mkdir()
    for name in ('shared/code.py', 'other/code.py', 'tests/test_feature.py',
                 '03_src/agri_research_agent/pipelines/lutou_weather.py',
                 '03_src/agri_research_agent/pipelines/public_data_daily.py',
                 '04_scripts/common.py', '08_tests/test_anchor.py', '07_docs/projects/canola/contract.md'):
        path=main/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# fixture\n')
    data=reservation_registry(main)
    (main/'03_src/agri_research_agent/alerts/__init__.py').write_text('# fixture\n')
    write_registry(main,data)
    registry.validate_registry(data, main)
    registry.git(main,'init','-b','main')
    registry.git(main,'config','user.name','Scope Fixture')
    registry.git(main,'config','user.email','fixture@example.invalid')
    registry.git(main,'add','.')
    registry.git(main,'-c','commit.gpgsign=false','commit','-m','synthetic approved reservation')
    remote=tmp_path/'remote.git'
    registry.git(main,'clone','--bare',str(main),str(remote))
    registry.git(main,'remote','add','origin',str(remote))
    feature=tmp_path/'feature'
    result=start_project.prepare(main,'notification-push-fixture','feat/bootstrap',feature,create=True)
    assert result['created'] and result['reserved_paths']==data['projects'][0]['reserved_paths']
    assert all(not (feature/p).exists() for p in result['reserved_paths'])
    monkeypatch.setattr(scope,'PROJECT_ROOT',feature)
    for name in ('04_scripts/notifications/preview.py','04_scripts/notifications/README',
                 '08_tests/alerts/test_core.py','02_configs/notifications/default.json',
                 '07_docs/projects/notification/contract.md'):
        path=feature/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('# fixture\n')
    assert scope.main(['--project','notification-push-fixture'])==0
    assert json.loads(capsys.readouterr().out)['PROJECT_SCOPE']=='PASS'
    for name in ('03_src/agri_research_agent/pipelines/lutou_weather.py',
                 '03_src/agri_research_agent/pipelines/public_data_daily.py',
                 '04_scripts/common.py','07_docs/projects/canola/contract.md',
                 '04_scripts/weather_producer/foo.py'):
        path=feature/name
        existed=path.exists()
        original=path.read_bytes() if existed else None
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('# forbidden change\n')
        assert scope.main(['--project','notification-push-fixture','--owned',
                           *data['projects'][0]['owned_paths'],*data['projects'][0]['reserved_paths']])==1
        assert json.loads(capsys.readouterr().out)['PROJECT_SCOPE']=='FAIL'
        if existed: path.write_bytes(original)
        else: path.unlink()
    assert registry.git(main,'status','--porcelain')==''
    assert (feature/registry.REGISTRY_PATH).read_bytes()==(main/registry.REGISTRY_PATH).read_bytes()


@pytest.mark.parametrize('value',['/tmp/x','C:/tmp/x','../x','04_scripts/../x','.',
    '04_scripts','07_docs/projects','03_src/agri_research_agent','**','04_scripts/**',
    '04_scripts/./x','04_scripts//x','missing/leaf','04_scripts/missing/leaf',
    '04_scripts/NUL','04_scripts/x.','04_scripts/x ','04_scripts/a.py'])
def test_reserved_invalid_namespace(repository,value):
    _,root=repository
    data=reservation_registry(root)
    data['projects'][0]['reserved_paths']=[value]
    with pytest.raises(ValueError): registry.validate_registry(data,root)


@pytest.mark.parametrize('existing, reserved',[
    ('04_scripts','04_scripts/notifications'),
    ('04_scripts/notifications','04_scripts/notifications'),
    ('04_scripts/notifications/child','04_scripts/notifications'),
    ('04_scripts/Notifications','04_scripts/notifications')])
def test_reserved_conflicts_existing_owner(repository,existing,reserved):
    _,root=repository
    data=reservation_registry(root)
    (root/existing).mkdir(parents=True,exist_ok=True)
    other=copy.deepcopy(data['projects'][0])
    other.update(project_id='other-owner',owned_paths=[existing],reserved_paths=[],
                 future_required_tests=[],required_tests=['tests/test_feature.py'])
    data['projects'].append(other)
    with pytest.raises(ValueError,match='collision'): registry.validate_registry(data,root)


def test_reserved_docs_siblings_and_case_collision(repository):
    _,root=repository
    data=reservation_registry(root)
    other=copy.deepcopy(data['projects'][0])
    other.update(project_id='canola-fixture',owned_paths=[],reserved_paths=['07_docs/projects/canola'],
                 required_tests=['tests/test_feature.py'],future_required_tests=[])
    data['projects'].append(other)
    registry.validate_registry(data,root)
    for value in ['07_docs/projects/notification','07_docs/projects/NOTIFICATION','07_docs/projects']:
        other['reserved_paths']=[value]
        with pytest.raises(ValueError): registry.validate_registry(data,root)


def test_reserved_readonly_protected_and_normal_owned_missing(repository):
    _,root=repository
    data=reservation_registry(root)
    for value in ['shared/new','03_src/agri_research_agent/market_data']:
        data['projects'][0]['reserved_paths']=[value, '08_tests/alerts']
        with pytest.raises(ValueError,match='read-only|protected'): registry.validate_registry(data,root)
    data=reservation_registry(root)
    data['projects'][0]['owned_paths']=['04_scripts/notifications']
    with pytest.raises(ValueError,match='missing'): registry.validate_registry(data,root)


def test_reserved_link_rejected(repository,tmp_path):
    import os
    _,root=repository
    data=reservation_registry(root)
    outside=tmp_path/'outside'
    outside.mkdir()
    link=root/'04_scripts/notifications'
    if os.name=='nt':
        subprocess.run(['cmd','/c','mklink','/J',str(link),str(outside)],check=True,capture_output=True)
    else: link.symlink_to(outside,target_is_directory=True)
    try:
        with pytest.raises(ValueError,match='link'): registry.validate_registry(data,root)
    finally:
        if os.name=='nt': link.rmdir()
        else: link.unlink()


def test_real_records_compatibility_and_docs_partition():
    baseline=registration_baseline()
    current=registry.load_registry(ROOT)
    current_by_id = {p['project_id']: p for p in current['projects']}
    for old in baseline['projects']:
        new = current_by_id[old['project_id']]
        if old['project_id']!='dev-governance': assert old==new
        else:
            assert {k:v for k,v in old.items() if k!='owned_paths'}=={k:v for k,v in new.items() if k!='owned_paths'}
            assert '07_docs' not in new['owned_paths']
            expected=list((ROOT/'07_docs').glob('0[0-6]_*.md'))+list((ROOT/'07_docs/templates').glob('*.md'))
            expected.append(ROOT/'07_docs/projects/生产只读检出实例登记.md')
            assert all(registry.owns(new,p.relative_to(ROOT).as_posix()) for p in expected)
            assert not registry.owns(new,'07_docs/projects/notification/contract.md')
        for name in subprocess.check_output(['git','-C',str(ROOT),'ls-files','-z']).decode('utf-8').split('\0'):
            if not name: continue
            before=registry.owns(old,name)
            after=registry.owns(new,name)
            assert not after or before
            if old['project_id']!='dev-governance': assert before==after


def test_normal_ownership_collision_is_rejected(repository):
    _,root=repository
    data=minimal_registry()
    other=copy.deepcopy(data['projects'][0]);other['project_id']='second'
    data['projects'].append(other)
    with pytest.raises(ValueError,match='collision'): registry.validate_registry(data,root)


def test_reserved_only_ready_and_required_tests_still_mandatory(repository):
    _,root=repository
    data=reservation_registry(root)
    p=data['projects'][0]
    p['owned_paths']=[]
    registry.validate_registry(data,root)
    assert p['status']=='ready'
    assert not (root/'08_tests/alerts').exists()
    p['future_required_tests']=[]
    with pytest.raises(ValueError,match='tests'): registry.validate_registry(data,root)


def test_reservation_cannot_claim_governance_docs(repository):
    _,root=repository
    data=reservation_registry(root)
    (root/'07_docs/templates').mkdir()
    owner=copy.deepcopy(data['projects'][0])
    owner.update(project_id='governance-fixture',change_class='shared',owned_paths=['07_docs/templates'],
                 reserved_paths=[],future_required_tests=[],required_tests=['tests/test_feature.py'])
    data['projects'].append(owner)
    data['projects'][0]['reserved_paths'].append('07_docs/templates')
    with pytest.raises(ValueError,match='collision|protected'): registry.validate_registry(data,root)


def test_reserved_ancestor_of_exact_future_file_conflicts(repository):
    _,root=repository
    data=reservation_registry(root)
    other=copy.deepcopy(data['projects'][0])
    other.update(project_id='exact-owner',owned_paths=[],reserved_paths=[],
                 future_owned_paths=['04_scripts/notifications/preview.py'],
                 future_required_tests=[],required_tests=['tests/test_feature.py'])
    data['projects'].append(other)
    with pytest.raises(ValueError,match='collision'): registry.validate_registry(data,root)


def test_existing_project_scope_classification_unchanged():
    before=registration_baseline()
    after=registry.load_registry(ROOT)
    after_by_id = {p['project_id']: p for p in after['projects']}
    for old in before['projects']:
        new = after_by_id[old['project_id']]
        probes=old['owned_paths']+old.get('future_owned_paths',[])
        probes=probes+['04_scripts/not_owned.py','03_src/agri_research_agent/pipelines/public_data_daily.py']
        for path in probes:
            if old['project_id']=='dev-governance' and path=='07_docs': continue
            def synthetic_git(root,*args):
                if args==('diff','--name-only','--no-renames','base...HEAD'): return path+'\n'
                if args[:1]==('rev-parse',): return 'fixture-head'
                return ''
            def outcome(project):
                try:
                    return scope.run_audit(ROOT,'base',project['owned_paths'],
                        exact_allowed=project.get('future_owned_paths',[]),
                        change_class=project['change_class'],git=synthetic_git)['PROJECT_SCOPE']
                except ValueError as exc:
                    return ('REJECTED', str(exc))
            assert outcome(old)==outcome(new),(old['project_id'],path)


def test_notification_registration_contract():
    data, project = registry.select_project(ROOT, 'notification-push')
    baseline = registration_baseline()
    current_by_id = {p['project_id']: p for p in data['projects']}
    assert all(current_by_id[p['project_id']] == p for p in baseline['projects'] if p['project_id'] != 'notification-push')
    assert data['schema_version'] == baseline['schema_version'] == 'project-registry/4'
    assert data['protected_paths'] == baseline['protected_paths']
    assert project['change_class'] == 'business' and project['status'] == 'ready'
    assert project['owned_paths'] == ['03_src/agri_research_agent/alerts']
    assert project['reserved_paths'] == ['04_scripts/notifications', '08_tests/alerts', '02_configs/notifications', '07_docs/projects/notification']
    assert project['future_required_tests'] == ['08_tests/alerts/test_notification.py']
    assert len(project['shared_dependencies']) == 9
    assert all((ROOT/p).is_file() and not registry.owns(project,p) for p in project['shared_dependencies'])
    assert 'Soybean Notification BLOCKED' in project['boundary_notes']
    assert '06_outputs/push_logs' in project['boundary_notes']


def test_notification_registered_scope_in_temporary_git(tmp_path, monkeypatch, capsys):
    _, project = registry.select_project(ROOT, 'notification-push')
    main = tmp_path/'main'
    main.mkdir()
    for name in project['owned_paths'] + project['shared_dependencies'] + project['forbidden_paths']:
        path=main/name
        if (ROOT/name).is_file():
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('# fixture\n')
        else:
            path.mkdir(parents=True,exist_ok=True)
            (path/'fixture_anchor.txt').write_text('# fixture\n')
    for name in ['03_src/agri_research_agent/alerts/__init__.py','04_scripts/anchor.py',
                 '08_tests/test_anchor.py','02_configs/anchor.json','07_docs/projects/anchor.md']:
        path=main/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('# fixture\n')
    data={'schema_version':'project-registry/3','protected_paths':[], 'projects':[copy.deepcopy(project)]}
    write_registry(main,data)
    registry.validate_registry(data,main)
    registry.git(main,'init','-b','main')
    registry.git(main,'config','user.name','Scope Fixture')
    registry.git(main,'config','user.email','fixture@example.invalid')
    registry.git(main,'add','.')
    registry.git(main,'-c','commit.gpgsign=false','commit','-m','fixture approved registration')
    registry.git(main,'update-ref','refs/remotes/origin/main','HEAD')
    feature=tmp_path/'feature'
    registry.git(main,'worktree','add','-b','feat/notification',str(feature),'HEAD')
    monkeypatch.setattr(scope,'PROJECT_ROOT',feature)
    positives=['03_src/agri_research_agent/alerts/new_module.py','04_scripts/notifications/preview.py',
               '08_tests/alerts/test_notification.py','02_configs/notifications/channel.yaml',
               '07_docs/projects/notification/contract.md']
    negatives=project['shared_dependencies']+[
        '03_src/agri_research_agent/pipelines/lutou_weather.py',
        '03_src/agri_research_agent/pipelines/lutou_domestic_basis.py',
        '03_src/agri_research_agent/pipelines/public_data_daily.py',
        '04_scripts/automation/run_full_daily_windows.ps1',
        '03_src/agri_research_agent/import_profit/market_snapshot.py',
        '03_src/agri_research_agent/pipelines/public_data_prewarm.py',
        '03_src/agri_research_agent/pipelines/public_data_providers.py',
        '07_docs/03_标准开发与生产发布规范.md',
        '07_docs/projects/other.md','07_docs/projects/canola/contract.md']
    for name in positives+negatives:
        path=feature/name;original=path.read_bytes() if path.is_file() else None
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text('# simulated change\n')
        expected='PASS' if name in positives else 'FAIL'
        assert scope.main(['--project','notification-push','--owned',
                           *project['owned_paths'],*project.get('reserved_paths',[])]) == (0 if expected=='PASS' else 1), name
        assert json.loads(capsys.readouterr().out)['PROJECT_SCOPE']==expected, name
        if original is None:path.unlink()
        else:path.write_bytes(original)
    assert registry.git(main,'status','--porcelain')==''
    assert registry.git(feature,'status','--porcelain')==''


def test_tankan_fixture_isolation_registration():
    data, project = registry.select_project(ROOT, 'tankan-fixture-isolation')
    baseline = registration_baseline()
    assert data['schema_version'] == baseline['schema_version']
    assert data['protected_paths'] == baseline['protected_paths']
    current = {p['project_id']: p for p in data['projects']}
    assert all(current[p['project_id']] == p for p in baseline['projects'])
    assert project['status'] == 'ready' and project['change_class'] == 'shared'
    assert project['owned_paths'] == ['08_tests/data_sources/tankan/test_domestic_spread.py']
    assert not project.get('reserved_paths') and not project.get('future_owned_paths')
    assert project['owned_paths'][0] in project['required_tests']
    for path in ['04_scripts/refresh_public_data.py', '03_src/agri_research_agent/data_sources/tankan/client.py',
                 '08_tests/data_sources/tankan/test_client.py', '01_data/historical_spread_database.parquet']:
        assert not registry.owns(project, path)


def test_public_intraday_runtime_production_registration_is_exact_and_inert():
    data, project = registry.select_project(ROOT, "public-intraday-runtime")
    assert data["schema_version"] == "project-registry/4"
    assert project == PUBLIC_INTRADAY_RUNTIME_PRODUCTION_REGISTRATION
    assert project["status"] == "ready"
    assert project["change_class"] == "shared"
    assert project["runtime_target"] == "production_container"
    assert project["runtime_contract"] == "02_configs/runtime_contracts/public-intraday-runtime.json"
    assert project["owned_paths"] == [
        "02_configs/runtime_contracts/public-intraday-runtime.json",
        "04_scripts/runtime/public_intraday_runtime.py",
        "09_deploy/public_intraday_runtime/Dockerfile.public-intraday",
        "09_deploy/public_intraday_runtime/compose.yml",
        "08_tests/test_public_intraday_runtime_v2.py",
        "07_docs/projects/public-intraday-runtime/运行合同.md",
    ]
    assert project["future_owned_paths"] == []
    assert project["future_required_tests"] == []
    assert "08_tests/test_public_intraday_runtime_v2.py" in project["required_tests"]
    assert "02_configs/project_registry.json" in project["forbidden_paths"]
    assert "04_scripts/capture_public_intraday.py" in project["shared_dependencies"]
    assert "AM_PM_AUTO_EXECUTION=NO" in project["boundary_notes"]
    assert "INTRADAY_FULL_DAILY_DEPENDENCY=NONE" in project["boundary_notes"]


def test_spread_contract_bootstrap_is_historical_and_transfer_is_exact():
    path = '02_configs/runtime_contracts/spread-production-runtime.json'
    assert CONTRACT_SOURCE_ALLOCATION['future_owned_paths'] == [
        *PRE_RELEASE_REGISTRATION['future_owned_paths'], path]
    assert POST_TRANSFER_INFRA_REGISTRATION['future_owned_paths'] == PRE_RELEASE_REGISTRATION['future_owned_paths']
    for key in PRE_RELEASE_REGISTRATION.keys() - {'future_owned_paths', 'boundary_notes'}:
        assert POST_TRANSFER_INFRA_REGISTRATION[key] == PRE_RELEASE_REGISTRATION[key]
    current, project = registry.select_project(ROOT, 'shared-production-infrastructure')
    assert project == POST_TRANSFER_INFRA_REGISTRATION
    assert not registry.owns(project, path)


def test_spread_runtime_wiring_registration_owns_only_exact_paths_and_contract():
    from quality import target_runtime_gate

    current, project = registry.select_project(ROOT, 'spread-production-runtime-wiring')
    path = '02_configs/runtime_contracts/spread-production-runtime.json'
    assert project == SPREAD_RUNTIME_WIRING_REGISTRATION
    assert project['runtime_target'] == 'production_container'
    assert project['runtime_contract'] == path
    assert target_runtime_gate.read_contract(ROOT, project)['project_id'] == project['project_id']
    assert project['future_required_tests'] == ['08_tests/test_spread_runtime_contract.py']
    assert project['future_required_tests'][0] in project['future_owned_paths']
    assert registry.owns(project, path)
    for owned in project['owned_paths'] + project['future_owned_paths']:
        assert registry.owns(project, owned)
        assert not registry.owns(project, owned + '/child')
        assert not registry.owns(project, owned + '.sibling')
        assert all(not registry.owns(other, owned) for other in current['projects'] if other is not project)
    for other in ('02_configs/runtime_contracts/other.json', '04_scripts/capture_public_intraday_extra.py',
                  '04_scripts/runtime/other.py', '09_deploy/spread_runtime/extra.yml',
                  '08_tests/fixtures/spread_runtime/unregistered.json', 'Dockerfile',
                  'docker-compose.yml', '05_apps/streamlit_app.py'):
        assert not registry.owns(project, other)


def test_production_input_authority_registration_has_exact_host_only_boundary():
    data, project = registry.select_project(ROOT, 'soybean-production-input-authority')
    assert project == PRODUCTION_INPUT_AUTHORITY_REGISTRATION
    assert project['runtime_target'] == 'library_only'
    assert project['change_class'] == 'shared' and project['status'] == 'ready'
    assert not project['owned_paths'] and not project.get('reserved_paths')
    assert len(project['future_owned_paths']) == 6
    assert project['future_required_tests'] == ['08_tests/test_production_input_assets.py']
    for path in project['future_owned_paths']:
        assert registry.owns(project, path)
        assert not registry.owns(project, path + '.sibling')
        assert not registry.owns(project, path + '/child')
        assert all(not registry.owns(other, path) for other in data['projects'] if other is not project)
    for path in ('03_src/agri_research_agent/import_profit/cnf_store.py',
                 '05_apps/import_profit_intraday_page.py', '04_scripts/capture_public_intraday.py',
                 '09_deploy/runtime_identity/host_authorization.py', '09_deploy/spread_runtime/compose.yml'):
        assert not registry.owns(project, path)
    assert 'separate' in project['boundary_notes']


def test_xiaoran_update_migration_has_exact_infrastructure_ownership():
    data, project = registry.select_project(ROOT, 'xiaoran-production-data-delivery')
    expected = set(XIAORAN_PRODUCTION_DATA_DELIVERY_REGISTRATION['future_owned_paths'])
    assert project == XIAORAN_PRODUCTION_DATA_DELIVERY_REGISTRATION
    assert project['change_class'] == 'shared'
    assert project['runtime_target'] == 'windows_git_worktree'
    assert not project['owned_paths'] and not project.get('reserved_paths')
    assert set(project['future_owned_paths']) == expected
    assert set(project['future_required_tests']) == {p for p in expected if p.startswith('08_tests/')}
    for path in expected:
        assert registry.owns(project, path)
        assert not registry.owns(project, path + '.unapproved')
        assert not any(registry.owns(other, path) for other in data['projects'] if other is not project)
    for path in project['shared_dependencies'] + [
            '04_scripts/refresh_public_data.py', '05_apps/streamlit_app.py',
            '09_deploy/runtime_identity/host_authorization.py',
            '03_src/agri_research_agent/import_profit/cnf_store.py',
            '09_deploy/production_data_delivery/unregistered.py']:
        assert not registry.owns(project, path)


@pytest.fixture
def bootstrap_start_base(repository, monkeypatch):
    main, _ = repository
    data = minimal_registry()
    data['schema_version'] = 'project-registry/3'
    write_registry(main, data)
    for name in ['03_src/agri_research_agent/__init__.py', '08_tests/existing.py']:
        path = main/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# trusted parent\n')
    registry.git(main, 'add', '.')
    registry.git(main, '-c', 'commit.gpgsign=false', 'commit', '-m', 'bootstrap fixture parents')
    registry.git(main, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    original = registry.git
    def local_git(root, *args):
        if args == ('fetch', 'origin'): return ''
        if args[:1] == ('ls-remote',): return original(main, 'rev-parse', 'HEAD') + '\trefs/heads/main'
        return original(root, *args)
    monkeypatch.setattr(registry, 'git', local_git)
    return main


def test_auto_bootstrap_start_resume_scope_and_completion_cycle(bootstrap_start_base, tmp_path, monkeypatch):
    from quality import complete_project
    main = bootstrap_start_base
    feature = tmp_path/'new-business'
    result = start_project.prepare(main, 'new-business', 'feat/new-business', feature, create=True)
    assert result['action'] == 'STARTED' and result['auto_bootstrap']
    assert result['changed_files'] == [registry.REGISTRY_PATH]
    assert registry.git(main, 'status', '--porcelain') == ''
    source = feature/'03_src/agri_research_agent/new_business/code.py'
    source.parent.mkdir(parents=True)
    source.write_text('VALUE = 1\n')
    test = feature/'08_tests/new_business/test_project.py'
    test.parent.mkdir(parents=True)
    test.write_text('def test_business():\n    assert True\n')
    resumed = start_project.prepare(main, 'new-business', 'feat/new-business', feature)
    assert resumed['action'] == 'RESUMED' and not resumed['created']
    assert set(resumed['changed_files']) == {registry.REGISTRY_PATH, source.relative_to(feature).as_posix(), test.relative_to(feature).as_posix()}
    monkeypatch.setattr(scope, 'PROJECT_ROOT', feature)
    assert scope.main(['--project', 'new-business']) == 0
    result = complete_project.complete(feature, 'new-business')
    assert result['PROJECT_COMPLETION'] == 'PASS'
    assert result['executed_tests'][0]['passed'] == 1
    registry.git(feature, 'add', '.')
    registry.git(feature, '-c', 'commit.gpgsign=false', 'commit', '-m', 'first business with registry')
    assert start_project.prepare(main, 'new-business', 'feat/new-business', feature)['action'] == 'RESUMED'


def test_existing_resume_and_identity_dirty_conflicts(bootstrap_start_base, tmp_path):
    main = bootstrap_start_base
    feature = tmp_path/'resume-demo'
    start_project.prepare(main, 'demo', 'feat/resume-demo', feature, create=True)
    (feature/'feature/code.py').write_text('# legitimate owned work\n')
    assert start_project.prepare(main, 'demo', 'feat/resume-demo', feature)['action'] == 'RESUMED'
    with pytest.raises(ValueError, match='IDENTITY_CONFLICT'):
        start_project.prepare(main, 'other', 'feat/resume-demo', feature)
    with pytest.raises(ValueError, match='IDENTITY_CONFLICT'):
        start_project.prepare(main, 'demo', 'feat/wrong', feature)
    (feature/'other/code.py').write_text('# unknown dirty state\n')
    result=start_project.prepare(main, 'demo', 'feat/resume-demo', feature)
    assert 'other/code.py' in result['changed_files']  # Report for Scope, never overwrite edits.


@pytest.mark.parametrize('name', ['shared', 'market-data', 'automation'])
def test_shared_start_without_existing_owner_requires_no_approval(bootstrap_start_base, tmp_path, name):
    main=bootstrap_start_base
    result=start_project.prepare(main,name,'feat/'+name,tmp_path/name,change_class='shared',create=True)
    assert result['action']=='STARTED'
    assert result['change_class']=='shared'
    assert registry.git(main,'status','--porcelain')==''
    assert not (tmp_path/name/'01_data').exists()



def test_bootstrap_scope_and_completion_reject_self_expansion(bootstrap_start_base, tmp_path, monkeypatch):
    from quality import complete_project
    main = bootstrap_start_base
    feature = tmp_path/'self-expansion'
    start_project.prepare(main, 'new-business', 'feat/new-business', feature, create=True)
    data = json.loads((feature/registry.REGISTRY_PATH).read_text())
    data['projects'][0]['required_tests'] = []
    write_registry(feature, data)
    monkeypatch.setattr(scope, 'PROJECT_ROOT', feature)
    assert scope.main(['--project', 'new-business']) == 2
    with pytest.raises(ValueError):
        complete_project.complete(feature, 'new-business')


@pytest.mark.parametrize('main_state', ['dirty', 'stale', 'no-checkout', 'no-main-ref'])
def test_business_resume_independent_of_local_main(bootstrap_start_base, tmp_path, monkeypatch, main_state):
    main = bootstrap_start_base
    feature = tmp_path/'independent-business'
    start_project.prepare(main, 'demo', 'feat/independent', feature, create=True)
    if main_state == 'dirty':
        (main/'unrelated.txt').write_text('unrelated local work')
    elif main_state == 'stale':
        registry.git(main, 'commit', '--allow-empty', '-m', 'remote advanced')
        registry.git(main, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
        latest = registry.git(main, 'rev-parse', 'HEAD')
        registry.git(main, 'checkout', '--detach', latest)
        registry.git(main, 'update-ref', 'refs/heads/main', latest+'^')
    else:
        registry.git(main, 'checkout', '--detach')
        if main_state == 'no-main-ref':
            registry.git(main, 'branch', '-D', 'main')
    before = registry.git(main, 'status', '--porcelain')
    (feature/'feature/code.py').write_text('# owned work\n')
    result = start_project.prepare(feature, 'demo', 'feat/independent', feature)
    assert result['action'] == 'RESUMED' and not result['created']
    assert 'main_clean_mirror' not in result  # Never claim an unperformed check passed.
    monkeypatch.setattr(scope, 'PROJECT_ROOT', feature)
    assert scope.main(['--project', 'demo']) == 0
    assert registry.git(main, 'status', '--porcelain') == before
    # START is also independent of a main checkout, using the fresh remote ref.
    assert start_project.prepare(feature, 'demo', 'feat/another', tmp_path/'another', create=True)['action'] == 'STARTED'


def append_trusted_unrelated_project(main):
    data = json.loads((main/registry.REGISTRY_PATH).read_text())
    added = registry.bootstrap_record('unrelated-project', data['protected_paths'])
    added.pop('runtime_target')
    data['projects'].append(added)
    write_registry(main, data)
    registry.git(main, 'add', registry.REGISTRY_PATH)
    registry.git(main, 'commit', '-m', 'trusted unrelated registration')
    registry.git(main, 'update-ref', 'refs/remotes/origin/main', 'HEAD')


def test_business_resume_after_trusted_registry_addition(bootstrap_start_base, tmp_path, monkeypatch):
    main = bootstrap_start_base
    feature = tmp_path/'registry-drift'
    start_project.prepare(main, 'demo', 'feat/drift', feature, create=True)
    before = (feature/registry.REGISTRY_PATH).read_bytes()
    append_trusted_unrelated_project(main)
    (feature/'feature/code.py').write_text('# owned change\n')
    result = start_project.prepare(feature, 'demo', 'feat/drift', feature)
    assert result['action'] == 'RESUMED'
    assert result['baseline_head'] != result['head']  # Development may lag; Admission cannot.
    assert (feature/registry.REGISTRY_PATH).read_bytes() == before
    monkeypatch.setattr(scope, 'PROJECT_ROOT', feature)
    assert scope.main(['--project', 'demo']) == 0


@pytest.mark.parametrize('committed', [False, True])
def test_registry_metadata_change_can_resume_in_same_candidate(bootstrap_start_base, tmp_path, monkeypatch, committed):
    main = bootstrap_start_base
    feature = tmp_path/'registry-attack'
    start_project.prepare(main, 'demo', 'feat/attack', feature, create=True)
    append_trusted_unrelated_project(main)
    data = json.loads((feature/registry.REGISTRY_PATH).read_text())
    data['projects'][0]['owned_paths'].append('tests')
    write_registry(feature, data)
    if committed:
        registry.git(feature, 'add', registry.REGISTRY_PATH)
        registry.git(feature, 'commit', '-m', 'candidate expansion')
    assert start_project.prepare(feature, 'demo', 'feat/attack', feature)['action']=='RESUMED'
    monkeypatch.setattr(scope, 'PROJECT_ROOT', feature)
    assert scope.main(['--project', 'demo']) == 0


@pytest.mark.parametrize('change', ['project', 'policy'])
def test_registry_remote_drift_is_reported_without_start_approval(bootstrap_start_base, tmp_path, change):
    main = bootstrap_start_base
    feature = tmp_path/'incompatible'
    start_project.prepare(main, 'demo', 'feat/incompatible', feature, create=True)
    append_trusted_unrelated_project(main)
    data = json.loads((main/registry.REGISTRY_PATH).read_text())
    if change == 'project':
        data['projects'][0]['status'] = 'needs-boundary-review'
    else:
        data['protected_paths'].append('other')
    write_registry(main, data)
    registry.git(main, 'add', registry.REGISTRY_PATH)
    registry.git(main, 'commit', '-m', 'trusted incompatible policy')
    registry.git(main, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    result=start_project.prepare(feature, 'demo', 'feat/incompatible', feature)
    assert result['action']=='RESUMED' and result['baseline_head']!=result['head']


def test_shared_start_is_independent_of_unrelated_main_checkout(bootstrap_start_base, tmp_path, monkeypatch):
    main = bootstrap_start_base
    data = json.loads((main/registry.REGISTRY_PATH).read_text())
    data['projects'][0]['change_class'] = 'shared'
    write_registry(main, data)
    registry.git(main, 'add', registry.REGISTRY_PATH)
    registry.git(main, 'commit', '-m', 'shared fixture')
    registry.git(main, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    feature = tmp_path/'strict-shared'
    start_project.prepare(main, 'demo', 'feat/strict', feature, change_class='shared', create=True)
    (main/'unrelated.txt').write_text('dirty main')
    assert start_project.prepare(feature, 'demo', 'feat/strict', feature, change_class='shared')['action']=='RESUMED'
    monkeypatch.setattr(scope, 'PROJECT_ROOT', feature)
    assert scope.main(['--project', 'demo', '--change-class', 'shared']) == 0


def test_none_business_needs_no_runtime_marker(bootstrap_start_base, tmp_path):
    from quality import target_runtime_gate
    main = bootstrap_start_base
    data = json.loads((main/registry.REGISTRY_PATH).read_text())
    data['schema_version'] = 'project-registry/4'
    data['projects'][0]['runtime_target'] = 'none'
    write_registry(main, data)
    registry.git(main, 'add', registry.REGISTRY_PATH)
    registry.git(main, 'commit', '-m', 'none runtime fixture')
    registry.git(main, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    feature = tmp_path/'no-runtime'
    assert start_project.prepare(main, 'demo', 'feat/no-runtime', feature, create=True)['action'] == 'STARTED'
    assert start_project.prepare(feature, 'demo', 'feat/no-runtime', feature)['action'] == 'RESUMED'
    assert not list(feature.rglob('.market-data-runtime.json'))
    assert target_runtime_gate.validate_target(feature, data['projects'][0]) == {'TARGET_RUNTIME_VALIDATION': 'NOT_REQUIRED'}
    for target in ('windows_git_worktree', 'production_container'):
        assert scope.requires_main_mirror(dict(data['projects'][0], runtime_target=target))


def test_domestic_spread_status_one_time_ownership_normalization():
    import json
    from pathlib import Path
    from quality import main_admission
    root = Path(__file__).resolve().parents[1]
    data = json.loads((root/'02_configs/project_registry.json').read_text(encoding='utf-8'))
    project = next(p for p in data['projects'] if p['project_id']=='domestic-spread-status')
    assert project['change_class']=='business' and project['status']=='ready'
    assert project['runtime_target']=='none'
    assert set(project['owned_paths']) == {
        '03_src/agri_research_agent/application/domestic_spreads.py',
        '08_tests/application/test_domestic_spread_status.py'}
    assert project['required_tests'] == ['08_tests/application/test_domestic_spread_status.py']
    for path in ('05_apps/streamlit_app.py', '04_scripts/automation/full_daily_windows.py',
                 '03_src/agri_research_agent/pipelines/public_data_refresh.py',
                 '09_deploy/production_data_delivery/README.md', '04_scripts/runtime/validate_target_runtime.py'):
        assert not main_admission.owns(project,path)
    assert not project.get('reserved_paths') and not project.get('future_owned_paths')


def test_windows_wrapper_platform_registration_is_exact_and_inert():
    data, project = registry.select_project(ROOT, 'windows-wrapper-platform')
    assert project == WINDOWS_WRAPPER_PLATFORM_REGISTRATION
    assert project['change_class'] == 'shared'
    assert project['owned_paths'] == [
        '03_src/agri_research_agent/automation/full_daily_windows.py',
        '08_tests/pipelines/test_full_daily_windows_wrapper.py']
    assert project['owned_paths'][1] in project['required_tests']
    for path in project['owned_paths']:
        assert all(not registry.owns(other, path) for other in data['projects'] if other is not project)
        assert not registry.owns(project, path + '.extra')
    for path in project['shared_dependencies'] + project['forbidden_paths'] + [
        '01_data/db.sqlite', '06_outputs/snapshot.json', '10_logs/production.log',
        '03_src/agri_research_agent/automation/other.py',
        '05_apps/import_profit_intraday_page.py',
        '09_deploy/spread_release/release_contract.py']:
        assert not registry.owns(project, path)


def test_high_risk_execution_registration_is_host_tool_only():
    data, project = registry.select_project(ROOT, 'high-risk-execution-path-closure')
    assert project['runtime_target'] == 'library_only'
    assert project['change_class'] == 'shared'
    assert not project.get('future_owned_paths') and not project.get('reserved_paths')
    assert set(project['owned_paths']) == {
        '09_deploy/spread_release/high_risk_execution.py',
        '09_deploy/spread_release/deployment_plan.schema.json',
        '09_deploy/spread_release/artifact_manifest.schema.json',
        '09_deploy/spread_release/release_contract.py',
        '09_deploy/spread_release/create_deployment_plan.py',
        '09_deploy/spread_release/verify_release_contract.py',
        '04_scripts/runtime/routine_release.py',
        '08_tests/test_high_risk_execution.py',
        '08_tests/shared/high_risk_execution_docker_e2e.py',
        '04_scripts/runtime/说明.md',
    }
    for path in project['owned_paths']:
        assert all(not registry.owns(other, path) for other in data['projects'] if other is not project)
    for path in project['forbidden_paths'] + [
        '.github/workflows/trusted-main-admission.yml', '08_tests/test_project_registry.py',
        '09_deploy/runtime_identity/host_authorization.py',
        '04_scripts/runtime/validate_target_runtime.py',
        '09_deploy/spread_release/other.py', '04_scripts/runtime/other.py',
    ]:
        assert not registry.owns(project, path)
