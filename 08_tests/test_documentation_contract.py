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


def test_manual_covers_all_required_operational_stages() -> None:
    manual = read(MANUAL)
    for number in range(1, 27):
        assert re.search(rf"^## {number}\. ", manual, flags=re.MULTILINE), number

    for marker in ("**目标**", "**输入**", "**入口与关键检查**", "**输出**", "**停止**", "**禁止**"):
        assert manual.count(marker) >= 25, marker


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
    assert "2026-07-26" in manual
    assert "仅是当日参考实例，非永久常量" in manual
    assert "oil_world/RELEASE.json" not in "\n".join((read(INDEX), spec, manual))


def test_business_fast_lane_and_strict_lane_are_consistent_at_all_entrypoints():
    for path in (ROOT / 'AGENTS.md', SPEC, MANUAL, FEATURE_TEMPLATE):
        body = read(path)
        assert 'START / RESUME → development → push feature → trusted-main-admission-v1 PASS → human approval → exact fast-forward main' in body
        assert '不再是普通 Business 进入 main 的强制 Gate' in body
        assert '不得因未执行它们判定未完成' in body
        assert '任何新 commit 都必须重新获得 hosted PASS' in body.replace('\n', '')
        assert '高风险 lane 仍必须通过 `complete_project.py` 和独立 integration 验收' in body
        for boundary in ('shared infrastructure', 'governance', 'protected paths', 'production-control-plane',
                         'FULL DAILY', 'Production Wrapper', 'Runtime', 'deployment', 'production data/write path'):
            assert boundary in body
    spec = read(SPEC)
    for marker in ('app.id=15368', 'conclusion=success', 'main-admission/1', 'candidate Commit/Tree',
                   'git push origin <approved_candidate_sha>:refs/heads/main', '不自行 Disabled Ruleset'):
        assert marker in spec
    assert 'git push origin HEAD:main' not in spec + read(MANUAL)
    assert '仍要求登记已进入可信 origin/main' not in spec


def test_strict_completion_entrypoint_keeps_all_runtime_and_test_validation():
    """The CLI remains callable and advertises the ordinary-business exception."""
    from quality import complete_project

    assert 'optional for ordinary business' in complete_project.__doc__
    spec = read(SPEC)
    assert '`complete_project.py --candidate-record <记录>` 的全部 required' in spec
    assert 'tests 与签名容器门禁，并在独立 integration 中验收' in spec
    assert '全部 required' in read(ROOT / 'AGENTS.md')


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
