from __future__ import annotations

from pathlib import Path

import pytest

from quality import audit_changed_scope, run_scoped_tests


def test_special_mode_never_becomes_full_regression(tmp_path: Path) -> None:
    test_file = tmp_path / "08_tests" / "test_feature.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_example(): pass\n", encoding="utf-8")

    plan = run_scoped_tests.build_test_plan("special", ["08_tests/test_feature.py"], tmp_path)

    assert plan["full_regression"] is False
    assert plan["selected_tests"] == ["08_tests/test_feature.py"]
    assert plan["command"][-1] == "08_tests/test_feature.py"


@pytest.mark.parametrize("mode", ["special", "impact"])
def test_partial_modes_require_explicit_tests(mode: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="不会猜测执行范围"):
        run_scoped_tests.build_test_plan(mode, [], tmp_path)


def test_dry_run_does_not_execute_tests(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    test_file = tmp_path / "08_tests" / "test_feature.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_example(): pass\n", encoding="utf-8")
    plan = run_scoped_tests.build_test_plan("impact", ["08_tests/test_feature.py"], tmp_path)
    monkeypatch.setattr(run_scoped_tests.subprocess, "run", lambda *args, **kwargs: pytest.fail("不应执行 pytest"))

    assert run_scoped_tests.execute_plan(plan, tmp_path, dry_run=True) == 0
    assert test_file.read_text(encoding="utf-8") == "def test_example(): pass\n"


def _stable_git(_: Path, *args: str) -> str:
    command = tuple(args)
    responses = {
        ("rev-parse", "--verify", "base^{commit}"): "base\n",
        ("rev-parse", "HEAD"): "head\n",
        ("status", "--porcelain=v1", "--untracked-files=all"): "",
        ("diff", "--name-only", "base..HEAD"): "03_src/feature.py\n05_apps/unrelated.py\n",
        ("diff", "--name-only", "--cached"): "",
        ("diff", "--name-only"): "",
    }
    return responses[command]


def test_incremental_audit_reports_out_of_scope_changes(tmp_path: Path) -> None:
    report = audit_changed_scope.run_audit(tmp_path, "base", ["03_src/feature.py"], git=_stable_git)

    assert report["out_of_scope_changes"] == ["05_apps/unrelated.py"]
    assert report["findings"][-1]["classification"] == "OUT_OF_SCOPE"
    assert report["allow_next_stage"] is False


def test_git_quoted_utf8_path_is_decoded_without_windows_locale() -> None:
    assert audit_changed_scope._decode_git_path('"07_docs/templates/\\346\\226\\260.md"') == "07_docs/templates/新.md"


def test_audit_stops_when_worktree_changes(tmp_path: Path) -> None:
    statuses = iter(["", "", " M 03_src/feature.py\n"])

    def moving_git(_: Path, *args: str) -> str:
        if args == ("status", "--porcelain=v1", "--untracked-files=all"):
            return next(statuses)
        return _stable_git(tmp_path, *args)

    with pytest.raises(audit_changed_scope.WorktreeChangedError, match="工作区发生变化"):
        audit_changed_scope.run_audit(tmp_path, "base", ["03_src/feature.py"], git=moving_git)


def test_blocker_and_follow_up_keep_distinct_meanings(tmp_path: Path) -> None:
    blocker = audit_changed_scope.parse_finding("BLOCKER|缺少验收|测试失败|08_tests/test_feature.py|修复验收")
    follow_up = audit_changed_scope.parse_finding("FOLLOW_UP|清理命名|静态检查|03_src/feature.py|另建任务")
    insufficient = audit_changed_scope.parse_finding("INSUFFICIENT_EVIDENCE|来源未核验|无可复现证据|03_src/feature.py|补充证据")
    report = audit_changed_scope.run_audit(
        tmp_path,
        "base",
        ["03_src/feature.py"],
        findings=[blocker, follow_up],
        git=_stable_git,
    )

    assert blocker["blocking_current_feature"] is True
    assert follow_up["blocking_current_feature"] is False
    assert report["allow_next_stage"] is False
    assert [item["classification"] for item in report["findings"][:2]] == ["BLOCKER", "FOLLOW_UP"]
    evidence_report = audit_changed_scope.run_audit(
        tmp_path,
        "base",
        ["03_src/feature.py"],
        findings=[insufficient],
        git=_stable_git,
    )
    assert evidence_report["allow_next_stage"] is False


def test_quality_scripts_expose_no_commit_push_or_deploy_commands() -> None:
    script_text = (Path(run_scoped_tests.__file__).read_text(encoding="utf-8") + Path(audit_changed_scope.__file__).read_text(encoding="utf-8")).lower()

    assert '["git", "commit"' not in script_text
    assert '["git", "push"' not in script_text
    assert '["docker"' not in script_text
    assert "subprocess.run(plan[\"command\"]" in script_text
