"""Contract checks for the current, repository-wide documentation entrypoint."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "07_docs"
INDEX = DOCS / "00_文档索引与适用范围.md"
SPEC = DOCS / "03_标准开发与生产发布规范.md"
MANUAL = DOCS / "04_开发与发布检查清单.md"
FEATURE_TEMPLATE = DOCS / "templates" / "新功能开发任务模板.md"
SERVICE_TEMPLATE = DOCS / "templates" / "新独立服务或重大架构变更模板.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_current_documentation_layers_exist_and_are_distinct() -> None:
    for path in (INDEX, SPEC, MANUAL, FEATURE_TEMPLATE, SERVICE_TEMPLATE):
        assert path.is_file(), path

    assert "唯一当前入口" in read(INDEX)
    assert "必须、不得、应当、停止" in read(SPEC)
    assert "当前权威操作手册" in read(MANUAL)
    assert "不证明任何候选或发布工具已经通过真实生产门槛" in read(MANUAL)


def test_readme_does_not_reintroduce_obsolete_integration_or_draft_authority():
    body = read(ROOT / 'README.md')
    assert '保留原严格 Completion/integration lane' not in body
    assert '当前仍是候选实现' not in body
    targets = re.findall(r'\[[^]]+\]\(([^)#]+)(?:#[^)]+)?\)', body)
    assert '07_docs/03_标准开发与生产发布规范.md' in targets
    assert '07_docs/04_开发与发布检查清单.md' in targets
    assert '完整 `.txt` 依赖闭包' in body
    assert '传递依赖未受同等 hash 锁定' not in body


def test_index_covers_runtime_and_current_soybean_contracts():
    body = read(INDEX)
    targets = {unquote(target) for target in re.findall(r'\[[^]]+\]\(([^)#]+)(?:#[^)]+)?\)', body)}
    for path in (DOCS/'projects/application-service-write-identity/服务写入身份合同.md',
                 DOCS/'projects/production-runtime-v2/生产执行身份合同.md',
                 DOCS/'projects/production-runtime-v2/目标RuntimeDeployabilityGate.md',
                 DOCS/'projects/public-intraday-runtime/运行合同.md',
                 DOCS/'projects/大豆榨利新版说明.md'):
        assert path.relative_to(DOCS).as_posix() in targets
    guide = read(DOCS/'projects/大豆榨利新版说明.md')
    assert '](进口商品利润研究框架契约.md)' in guide
    assert '0.795' not in guide  # Formula has one current authority.


def test_manual_covers_all_required_operational_stages() -> None:
    manual = read(MANUAL)
    for stage in ('选择任务流程', '环境预检与任务范围', '开发、测试与 GitHub 接纳',
                  '发布准备与只读基线', '候选与页面验收', '正式部署与最终验收',
                  '回滚、清理与生产基线', '完成记录与失败处理'):
        assert re.search(rf'^## \d+\. {re.escape(stage)}$', manual, flags=re.MULTILINE), stage
    for marker in ('ROUTINE_STATELESS', 'ADDITIVE_REVERSIBLE', 'UNKNOWN 不自动 PASS',
                   '数据更新', '停止条件', '只切换目标服务', 'fresh grant',
                   '同一 Image ID', '最终 UI/business acceptance', '上一可用回滚版本'):
        assert marker in manual, marker


def test_documented_toolchain_and_service_contract_match_repository_sources() -> None:
    spec = read(SPEC)
    architecture = read(DOCS / "01_系统架构与项目边界.md")
    root_compose = read(ROOT / "docker-compose.yml")
    usda_compose = read(ROOT / "09_deploy" / "usda_release" / "compose.production.yml")
    oil_compose = read(ROOT / "09_deploy" / "oil_world_release" / "compose.production.yml")

    assert ".venv-py312" in spec
    assert "Python `>=3.12,<3.13`" in spec
    assert "Node `>=24,<25`" in spec
    assert "pnpm `10.12.1`" in spec
    assert "spread-dashboard" in architecture and '"8501:8501"' in root_compose
    assert "market-data-usda" in architecture and "name: market-data-usda" in usda_compose
    assert "oil-world-dashboard" in architecture and "name: market-data-oil-world" in oil_compose


def test_current_docs_preserve_release_safety_and_baseline_semantics() -> None:
    spec = read(SPEC)
    manual = read(MANUAL)

    for required in ("candidate_result", "deployment_plan", "deployment_result", "同一 Image ID"):
        assert required in spec
    for forbidden in ("docker system prune", "docker image prune -a", "docker volume prune", "docker compose down"):
        assert forbidden in spec
        assert forbidden in manual
    assert '核验 fresh main' in manual
    assert '某日 SHA 不能成为永久规范常量' in manual
    assert "oil_world/RELEASE.json" not in "\n".join((read(INDEX), spec, manual))


def test_release_treatments_and_runtime_details_have_one_policy_source() -> None:
    root = read(ROOT / "AGENTS.md")
    spec = read(SPEC)
    manual = read(MANUAL)
    runtime = read(ROOT / "09_deploy/runtime_identity/说明.md")
    tooling = read(ROOT / "04_scripts/runtime/说明.md")

    assert len(root.splitlines()) < 80
    assert "| STATEFUL_OR_INFRA + ADDITIVE_REVERSIBLE | NO | NO |" in spec
    assert "| STATEFUL_OR_INFRA + IRREVERSIBLE_OR_DESTRUCTIVE | YES | YES |" in spec
    assert "NEEDS_MAINTAINER_RISK_REVIEW" in spec and "不自动 PASS" in spec
    assert "真实 targeted recovery 是可选诊断" in spec
    assert "无 targeted recovery 或 full rollback" in manual
    assert "Recovery Runtime Lifecycle Hardening 技术债" in runtime
    assert "## Routine Stateless 工具入口" in tooling
    assert "production-release-request/1" in runtime
    assert "production-release-request/1" not in spec


def test_business_fast_lane_and_strict_lane_are_consistent_at_all_entrypoints():
    for path in (ROOT/'AGENTS.md', SPEC):
        body=read(path)
        for marker in ('main → feature/fix branch → implementation → automated tests → required CI → main',
                       'scoped required tests','impact/consumer tests','windows-2022',
                       'MAINTAINER_REVIEW_REQUIRED = YES','main != production'):
            assert marker in body,(path,marker)
        assert 'GOVERNANCE_ROOT_APPROVAL_REQUIRED' not in body
    for path in (MANUAL, FEATURE_TEMPLATE):
        body = read(path)
        assert '(03_标准开发与生产发布规范.md)' in body or '(../03_标准开发与生产发布规范.md)' in body
        assert 'main != production' in body and '生产单独授权' in body
        assert 'required CI' in body or 'hosted CI' in body
        assert 'GOVERNANCE_ROOT_APPROVAL_REQUIRED' not in body



def test_strict_completion_entrypoint_keeps_all_runtime_and_test_validation():
    """The CLI remains callable and advertises the ordinary-business exception."""
    from quality import complete_project

    assert 'optional for ordinary business' in complete_project.__doc__
    spec = read(SPEC)
    assert '`complete_project.py --candidate-record <记录>`' in spec
    assert '全部 required tests 与签名容器门禁' in spec
    assert '全部 required' in read(ROOT / 'AGENTS.md')


def test_ordinary_development_does_not_require_unrelated_main_or_runtime_marker():
    for path in (ROOT/'AGENTS.md', SPEC, MANUAL):
        body=read(path)
        assert '不依赖无关 local main checkout 是否 clean、mirror' in body
        assert 'runtime_target=none 不要求 runtime root、marker 或 production evidence' in body
        assert '直接创建 feature/fix branch 同样合法' in body



def test_current_authority_internal_markdown_links_resolve() -> None:
    names = subprocess.check_output(['git','-C',str(ROOT),'ls-files','--cached','--others','--exclude-standard','-z']).decode('utf-8').split('\0')
    files = [ROOT/name for name in sorted(set(names)) if name.endswith('.md') and '/archive/' not in name and (ROOT/name).is_file()]
    broken=[]
    for source in files:
        if re.search(r'^> ARCHIVED / NOT AUTHORITATIVE / DO NOT EXECUTE', read(source), re.MULTILINE):
            continue
        targets = re.findall(r"\[[^]]+\]\(([^)#]+)(?:#[^)]+)?\)", read(source))
        for target in targets:
            if "://" in target or target.startswith("mailto:"):
                continue
            if not (source.parent / unquote(target)).resolve().exists():
                broken.append((str(source.relative_to(ROOT)),target))
    assert not broken, broken


def test_templates_have_scope_and_release_decision_fields() -> None:
    feature = read(FEATURE_TEMPLATE)
    for field in ("目标用户", "展示入口", "当前数据位置", "页面、交互", "是否需要候选验证", "是否需要正式发布", "停止条件"):
        assert field in feature

    service = read(SERVICE_TEMPLATE)
    for field in ("容器名称", "正式端口", "Compose 路径", "project name", "正式环境文件路径", "健康检查", "候选本机端口", "回滚镜像", "Build Cache"):
        assert field in service


def test_development_authority_chain_and_no_old_execution_rules():
    root=read(ROOT/'AGENTS.md')
    index=read(INDEX)
    spec=read(SPEC)
    feature=read(FEATURE_TEMPLATE)
    assert 'archive 不在执行权威链中' in root
    assert 'ARCHIVED / NOT AUTHORITATIVE / DO NOT EXECUTE' in index
    for body in (root,spec,feature):
        assert '--project' in body and 'Registry' in body
        assert '独立' in body and 'worktree' in body.lower()
        assert 'ls-remote' in body
    assert '聊天历史 SHA 不是执行权威' in root
    for phrase in ('稳定集成线','提交、main 集成','本地 `main` 只领先预期提交'):
        assert phrase not in '\n'.join((root,index,spec,read(MANUAL),feature))
    assert 'Prewarm Separation 仍为未闭包 candidate' in spec
    assert '不要求开发 caller 是 clean main' in index


def test_archive_is_not_an_executable_dependency():
    names=subprocess.check_output(['git','-C',str(ROOT),'ls-files','-z']).decode('utf-8').split('\0')
    for name in names:
        path=ROOT/name
        if not path.is_file() or name==str(Path(__file__).relative_to(ROOT)).replace('\\','/'):
            continue
        if name.endswith(('.py','.ps1','.sh','.yaml','.yml','.json')):
            text=read(path)
            assert '07_docs/archive/legacy-sources/' not in text, name
    archive=DOCS/'archive'
    for name in ('2026-09-04-AsyncContractRollout候选记录.md','2026-09-04-Missing非阻断候选记录.md'):
        assert 'ARCHIVED / NOT AUTHORITATIVE / DO NOT EXECUTE' in read(archive/name)


def test_governance_v2_active_docs_and_update_entry():
    assert 'PM 当前冻结' not in read(DOCS/'01_系统架构与项目边界.md')
    assert 'PM 和旧 FULL DAILY 继续冻结' not in read(INDEX)
    usda = read(ROOT/'11_独立应用/USDA平衡表/AGENTS.md')
    assert '普通 business 无强制 integration' in usda
    assert '独立 feature/integration worktree' not in usda
    soybean = read(DOCS/'projects/进口商品利润研究框架契约.md')
    assert '不构成每次 bug fix 的审批' in soybean
    for p in (ROOT/'AGENTS.md', SPEC, MANUAL):
        assert 'local Git metadata' in read(p) and '不是纯 read-only' in read(p)
    daily = read(DOCS/'06_日常运行与数据更新手册.md')
    assert 'run_production_data_delta_windows.py' in daily and '--domain akshare' in daily
    assert 'Legacy/Recovery Only' in daily and '--publish' in daily
    assert 'SHADOW ONLY' not in read(ROOT/'.github/workflows/trusted-main-admission.yml')
    for marker in ('Approved', 'Commit/Tree', 'Image ID', 'rollback', 'Manifest', '生产单独授权'):
        assert marker in read(SPEC)


def test_governance_transition_is_separate_from_business_and_release():
    for path in (ROOT/'AGENTS.md',SPEC):
        body=read(path)
        for marker in ('Repository Maintainer/Admin','MAINTAINER_REVIEW_REQUIRED = YES',
                       'REGISTRY_IS_HARD_AUTHORIZATION = NO','STAGED_GOVERNANCE_MIGRATION_REQUIRED = NO',
                       'PROJECT_EXISTENCE_APPROVAL_REQUIRED = NO','ORDINARY_BUSINESS_NEEDS_HUMAN_APPROVAL = NO',
                       'Approved identity','Commit/Tree/Image','Manifest','rollback','candidate 不写 production','生产单独授权'):
            assert marker in body,(path,marker)
        assert 'GOVERNANCE_ROOT_APPROVAL_REQUIRED' not in body
        assert 'Always allow' not in body
    for path in (MANUAL, FEATURE_TEMPLATE, DOCS/'templates/GovernanceTransition.md'):
        body = read(path)
        assert '03_标准开发与生产发布规范.md)' in body
        assert '生产单独授权' in body and 'main != production' in body
        assert 'GOVERNANCE_ROOT_APPROVAL_REQUIRED' not in body and 'Always allow' not in body
    review = read(DOCS/'templates/GovernanceTransition.md')
    for marker in ('MAINTAINER_REVIEW_REQUIRED = YES', 'Repository Maintainer/Admin',
                   'authoritative base Commit/Tree', 'candidate Commit/Tree',
                   'required coverage', '生产未触碰', '接纳决定', '审查不覆盖失败'):
        assert marker in review, marker


def test_catalog_routes_updates_and_releases_to_current_contracts():
    import yaml
    catalog = yaml.safe_load(read(ROOT/'02_configs/app_catalog.yaml'))
    apps = {a['app_id']:a for a in catalog['applications']}
    main = apps['main_dashboard']
    assert 'run_production_data_delta_windows.py' in main['update_command']['spreads']
    assert '--domain akshare' in main['update_command']['spreads'] and '--publish' in main['update_command']['spreads']
    assert 'server_update_spreads.py' not in main['update_command']['spreads']
    assert 'explicit_recovery_authorization_only' in main['legacy_recovery_update_command']['spreads']
    assert main['code_release_entrypoints']['routine']=='04_scripts/runtime/routine_release.py'
    assert 'build_once' in main['deploy_method'] and 'deploy_same_image' in main['deploy_method']
    assert 'root_compose_build' not in read(ROOT/'02_configs/app_catalog.yaml')
    assert 'Legacy/Recovery' in read(DOCS/'06_日常运行与数据更新手册.md')
    for app in apps.values():
        assert app['observed_compose_files'].startswith('/')
        assert app['server_project_path']!=app['legacy_server_project_path']
    assert catalog['instance_observation']['verified_at_utc']
    assert '待' not in apps['oil_world_dashboard']['runtime_data'][0]['host_path']


def test_full_policy_is_current_in_review_templates_and_runtime_guide():
    template = read(DOCS/'templates/GovernanceTransition.md')
    tooling = read(ROOT/'04_scripts/runtime/说明.md')
    for body in (template, tooling, read(MANUAL)):
        assert 'candidate' in body and 'ALL_GREEN' in body
    assert '同次 full 对照：新增 failure' not in template
    assert '只能由新精确 SHA 的完整成对结果证明消除' not in tooling


def test_log_scope_and_recovery_preservation_do_not_create_cleanup_side_effects():
    import yaml
    catalog = yaml.safe_load(read(ROOT/'02_configs/app_catalog.yaml'))
    policy = catalog['shared_retention_policy']
    assert policy['system_journal_and_rotated_log_days']==14
    assert policy['system_log_persistent_configuration']=='not_applied_by_policy_change'
    assert policy['application_and_docker_logs']=='retention_pending_separate_confirmation'
    assert 'archive_outside_git' in policy['required_release_evidence']
    assert 'no_automatic_prune_or_timer' in policy['cleanup']
    for state in ['active_production','retained_rollback','awaiting_ui_acceptance','retained_failure','archived','cleanup_eligible']:
        assert state in catalog['resource_lifecycle']['states']
    assert '完整依赖闭包' in read(SPEC)
    assert '唯一备份' in read(DOCS/'02_数据与输出规范.md')
    assert '不授权删近期排查证据' in read(DOCS/'02_数据与输出规范.md')
