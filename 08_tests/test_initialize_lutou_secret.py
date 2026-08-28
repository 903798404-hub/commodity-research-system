from __future__ import annotations

from pathlib import Path

import pytest

import initialize_lutou_secret


def test_initializer_default_is_user_config_directory_and_git_ignored() -> None:
    project_root = Path(__file__).resolve().parents[1]
    assert initialize_lutou_secret.DEFAULT_SECRET_FILE == (
        Path.home() / ".market-data-secrets" / "lutou.env"
    )
    assert not initialize_lutou_secret.DEFAULT_SECRET_FILE.is_relative_to(project_root)
    assert ".market-data-secrets/" in (
        project_root / ".gitignore"
    ).read_text(encoding="utf-8").splitlines()


def test_initializer_hides_password_and_writes_env_style_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = iter(("fixture.invalid", "3306", "fixture-reader"))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(
        initialize_lutou_secret.getpass,
        "getpass",
        lambda _prompt: "fixture-password",
    )
    secret = tmp_path / "private" / "lutou.env"
    initialize_lutou_secret.initialize(secret)
    output = capsys.readouterr().out
    assert output.strip() == "LUTOU SECRET INITIALIZED"
    assert "fixture-password" not in output
    assert secret.read_text(encoding="utf-8").splitlines() == [
        "LUTOU_HOST=fixture.invalid",
        "LUTOU_PORT=3306",
        "LUTOU_USER=fixture-reader",
        "LUTOU_PASSWORD=fixture-password",
    ]
    assert list(secret.parent.glob(f".{secret.name}.*.tmp")) == []


def test_initializer_publishes_only_after_complete_fsync_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = iter(("fixture.invalid", "3306", "fixture-reader"))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(
        initialize_lutou_secret.getpass,
        "getpass",
        lambda _prompt: "fixture-password",
    )
    secret = tmp_path / "lutou.env"
    real_link = initialize_lutou_secret.os.link
    observed: list[str] = []

    def link_after_complete_write(source: Path, target: Path) -> None:
        assert not target.exists()
        observed.extend(Path(source).read_text(encoding="utf-8").splitlines())
        real_link(source, target)

    monkeypatch.setattr(initialize_lutou_secret.os, "link", link_after_complete_write)
    initialize_lutou_secret.initialize(secret)
    assert len(observed) == 4
    assert observed[-1] == "LUTOU_PASSWORD=fixture-password"


def test_initializer_refuses_to_overwrite_existing_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "lutou.env"
    secret.write_text("existing", encoding="utf-8")
    monkeypatch.setattr(
        "builtins.input", lambda _prompt: pytest.fail("must not prompt")
    )
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        initialize_lutou_secret.initialize(secret)
