"""Contract checks for the current, repository-wide documentation entrypoint."""

from __future__ import annotations

import re
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


def test_current_authority_internal_markdown_links_resolve() -> None:
    files = [INDEX, SPEC, MANUAL, FEATURE_TEMPLATE, SERVICE_TEMPLATE]
    files.extend(sorted((DOCS / "projects").glob("*.md")))
    for source in files:
        targets = re.findall(r"\[[^]]+\]\(([^)#]+)(?:#[^)]+)?\)", read(source))
        for target in targets:
            if "://" in target or target.startswith("mailto:"):
                continue
            assert (source.parent / unquote(target)).resolve().exists(), (source, target)


def test_templates_have_scope_and_release_decision_fields() -> None:
    feature = read(FEATURE_TEMPLATE)
    for field in ("目标用户", "展示入口", "当前数据位置", "页面、交互", "是否需要候选验证", "是否需要正式发布", "停止条件"):
        assert field in feature

    service = read(SERVICE_TEMPLATE)
    for field in ("容器名称", "正式端口", "Compose 路径", "project name", "正式环境文件路径", "健康检查", "候选本机端口", "回滚镜像", "Build Cache"):
        assert field in service
