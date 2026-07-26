from __future__ import annotations

import json
import sys
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
CONTRACT_DIR = REPOSITORY / "09_deploy" / "spread_release"
sys.path.insert(0, str(CONTRACT_DIR))

from audit_spread_images import build_report, main  # noqa: E402
from image_governance import (  # noqa: E402
    ACTIVE_PRODUCTION,
    ELIGIBLE_FOR_IMAGE_DELETE,
    ORPHAN_CANDIDATE,
    ROLLBACK_PROTECTED,
    SEALED_CANDIDATE,
    UNKNOWN_PROTECTED,
    VALIDATION_ONLY,
    classify_image,
    evidence_from_artifacts,
)


IMAGE = "sha256:" + "a" * 64
ROLLBACK = "sha256:" + "b" * 64
COMMIT = "c" * 40


def _image(*, image_id: str = IMAGE, tags: list[str] | None = None, labels: dict[str, str] | None = None) -> dict:
    return {
        "id": image_id,
        "repo_tags": tags or [],
        "labels": labels or {},
    }


def _production_result() -> dict:
    return {
        "status": "production_verified",
        "actual_image_id": IMAGE,
        "candidate_image_id": IMAGE,
        "image_ref": "market-data-spread-dashboard:spread-20260726-cccccccccccc-b02",
    }


def test_legacy_candidate_origin_cannot_override_active_promoted_image() -> None:
    evidence = evidence_from_artifacts(
        containers=[{"name": "spread-dashboard", "image_id": IMAGE, "running": True}],
        deployment_results=[_production_result()],
        candidate_results=[
            {
                "status": "candidate-validated",
                "candidate_image_id": IMAGE,
                "image_ref": "market-data-spread-dashboard:spread-20260726-cccccccccccc-b01",
            }
        ],
    )
    classified = classify_image(
        _image(
            tags=[
                "market-data-spread-dashboard:spread-20260726-cccccccccccc-b02",
                "market-data-spread-dashboard:spread-20260726-cccccccccccc-b01",
            ],
            labels={"market-data.release.type": "candidate"},
        ),
        evidence,
    )

    assert classified["artifact_origin"] == "candidate"
    assert classified["artifact_origin_source"] == "legacy market-data.release.type"
    assert classified["deployment_status"] == "production_verified"
    assert classified["promotion_mode"] == "same_image_id"
    assert classified["classification"] == ACTIVE_PRODUCTION
    assert classified["cleanup_action"] == "remove_candidate_tags_only"
    assert classified["formal_tag_present"] is True


def test_sealed_production_result_protects_image_without_running_container() -> None:
    evidence = evidence_from_artifacts(deployment_results=[_production_result()])
    classified = classify_image(_image(), evidence)
    assert classified["classification"] == ACTIVE_PRODUCTION
    assert classified["cleanup_action"] == "retain"


def test_current_b56_production_image_is_never_a_cleanup_target() -> None:
    b56_image = "sha256:e3c8c6c6a087a5de2a272365cf791bcb4d3669c81199589fe5c427f97de0948c"
    b56_tag = "market-data-spread-dashboard:spread-20260725-b56a6660887e-b02"
    evidence = evidence_from_artifacts(
        containers=[{"name": "spread-dashboard", "image_id": b56_image, "running": True}],
        deployment_results=[
            {
                "status": "production_verified",
                "actual_image_id": b56_image,
                "candidate_image_id": b56_image,
                "image_ref": b56_tag,
            }
        ],
    )
    classified = classify_image(
        _image(
            image_id=b56_image,
            tags=[b56_tag],
            labels={"market-data.release.type": "candidate"},
        ),
        evidence,
    )
    assert classified["classification"] == ACTIVE_PRODUCTION
    assert classified["cleanup_action"] == "retain"
    assert classified["active_container_reference"] is True


def test_rollback_reference_is_protected() -> None:
    evidence = evidence_from_artifacts(
        deployment_plans=[{"rollback_image_id": ROLLBACK, "rollback_image_ref": "spread:rollback"}]
    )
    classified = classify_image(_image(image_id=ROLLBACK, tags=["spread:rollback"]), evidence)
    assert classified["classification"] == ROLLBACK_PROTECTED
    assert classified["rollback_references"] is True


def test_sealed_validation_and_orphan_categories_are_distinct() -> None:
    sealed = classify_image(
        _image(),
        evidence_from_artifacts(candidate_results=[{"status": "candidate-validated", "candidate_image_id": IMAGE}]),
    )
    validation_only = classify_image(
        _image(),
        evidence_from_artifacts(candidate_results=[{"status": "failed", "candidate_image_id": IMAGE}]),
    )
    orphan = classify_image(
        {
            **_image(
                tags=["market-data-spread-dashboard:candidate-old"],
                labels={"market-data.artifact.origin": "candidate"},
            ),
            "created_at": "2026-07-01T00:00:00Z",
        },
        evidence_from_artifacts(),
    )
    assert sealed["classification"] == SEALED_CANDIDATE
    assert validation_only["classification"] == VALIDATION_ONLY
    assert orphan["classification"] == ORPHAN_CANDIDATE
    assert orphan["cleanup_action"] == ELIGIBLE_FOR_IMAGE_DELETE


def test_release_bundle_reference_protects_a_candidate_image() -> None:
    classified = classify_image(
        _image(labels={"market-data.artifact.origin": "candidate"}),
        evidence_from_artifacts(release_manifests=[{"image_id": IMAGE, "image_ref": "spread:bundle"}]),
    )
    assert classified["classification"] == SEALED_CANDIDATE
    assert classified["release_bundle_references"] is True


def test_candidate_origin_label_alone_never_authorizes_deletion() -> None:
    classified = classify_image(
        _image(labels={"market-data.artifact.origin": "candidate"}), evidence_from_artifacts()
    )
    assert classified["classification"] == "unsealed_candidate"
    assert classified["cleanup_action"] == "retain"


def test_unknown_image_is_protected_by_default() -> None:
    classified = classify_image(_image(), evidence_from_artifacts())
    assert classified["classification"] == UNKNOWN_PROTECTED
    assert classified["cleanup_action"] == "retain"


def test_dry_run_report_is_offline_and_reports_cleanup_reason(tmp_path: Path, capsys) -> None:
    release_root = tmp_path / "release"
    release_root.mkdir()
    (release_root / "deployment_result.json").write_text(
        json.dumps(_production_result()), encoding="utf-8"
    )
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "images": [
                    _image(
                        tags=["market-data-spread-dashboard:spread-20260726-cccccccccccc-b02"],
                        labels={"market-data.artifact.origin": "candidate"},
                    )
                ],
                "containers": [],
            }
        ),
        encoding="utf-8",
    )

    assert main(["--inventory", str(inventory), "--release-root", str(release_root)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mutation_performed"] is False
    assert report["images"][0]["classification"] == ACTIVE_PRODUCTION
    assert report["images"][0]["cleanup_action"] == "retain"
    assert str(release_root / "deployment_result.json") in report["artifact_paths"]


def test_build_report_never_emits_a_mutating_action_for_unknown_image() -> None:
    report = build_report({"images": [_image()], "containers": []}, {})
    assert report["mutation_performed"] is False
    assert report["images"][0]["cleanup_action"] == "retain"
