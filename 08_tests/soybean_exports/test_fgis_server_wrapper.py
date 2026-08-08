from __future__ import annotations

from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
WRAPPER = REPOSITORY / "09_deploy/soybean_exports/run_fgis_yearly_update.sh"


def test_fgis_wrapper_has_finite_range_resume_and_fail_closed_integrity_contract() -> None:
    source = WRAPPER.read_text(encoding="utf-8")

    assert "set -euo pipefail" in source
    assert "MAX_DOWNLOAD_ATTEMPTS" in source
    assert "<= 10" in source
    assert "MAX_NO_PROGRESS_ATTEMPTS=2" in source
    assert "--range" in source
    assert "-C -" in source
    assert '"${status}" == "206"' in source
    assert "Content-Length" in source
    assert "Last-Modified" in source
    assert "Accept-Ranges" in source
    assert "metadata_before" in source
    assert "metadata_after" in source
    assert "sha256sum" in source
    assert '"source_size": int(os.environ["CONTENT_LENGTH_BEFORE"])' in source
    assert "decode(\"utf-8\", errors=\"strict\")" in source
    assert '{"Thursday", "Cert Date", "Grain", "Destination", "Metric Ton"}' in source
    assert 'row.get("Grain") != "SOYBEANS"' in source


def test_fgis_wrapper_promotes_only_before_offline_read_only_container_pipeline() -> None:
    source = WRAPPER.read_text(encoding="utf-8")

    promote_position = source.index('mv -f "${PARTIAL_FILE}" "${FINAL_FILE}"')
    pipeline_position = source.index("docker run --rm --network none")
    assert promote_position < pipeline_position
    assert 'PARTIAL_FILE="${STAGING_DIR}/${SOURCE_FILE}.downloading"' in source
    assert '"${PARTIAL_FILE}.rejected.${RUN_ID}"' in source
    assert 'reject_partial "oversize"' in source
    assert 'reject_partial "integrity"' in source
    assert 'reject_partial "metadata-change"' in source
    assert 'type=bind,src=${STAGING_DIR},dst=/source,readonly' in source
    assert 'type=bind,src=${RUNTIME_ROOT}/01_data,dst=/runtime/01_data' in source
    assert "--source-file" in source
    assert "--source-metadata-file" in source
    assert 'exit "${pipeline_exit}"' in source
    assert "pipeline exit status could not be recorded" in source


def test_fgis_wrapper_uses_runtime_release_identity_without_git_or_secret_governance() -> None:
    source = WRAPPER.read_text(encoding="utf-8")
    lowered = source.lower()

    assert "printenv MARKET_DATA_GIT_HEAD" in source
    assert "--env \"MARKET_DATA_GIT_HEAD=${MARKET_DATA_GIT_HEAD}\"" in source
    assert "RELEASE.json" not in source
    assert "git -C" not in source
    assert "git rev-parse" not in source
    assert "api_key" not in lowered
    assert "secret" not in lowered
    assert "apt-get" not in lowered
    assert "apt install" not in lowered
    assert "yum " not in lowered
    assert "dnf " not in lowered
    assert "proxy" not in lowered
    assert "crontab" not in lowered
    assert "docker build" not in lowered
