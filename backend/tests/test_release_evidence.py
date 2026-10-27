"""Acceptance evidence is the only part of the release decision a human writes by hand.

Everything else in the release gate is a command whose output is either zero or not. F003 and F004
arrive as JSON somebody typed, possibly weeks after the exercise, possibly by copying an older
file. So the tests here are mostly about refusal: a document bound to a different commit, a
rehearsal presented as a drill, a template saved without being filled in, a field that carries a
credential. A validator that only accepts good input would be worthless.

The documentation checker is exercised here too, against fixtures: the supported-scope contract
and the machine-readable limitation list are two halves of one statement, so the check that keeps
them together has to be known to work before the release gate relies on it.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts import release_evidence, validate_docs
from scripts.release_evidence import EvidenceError

CANDIDATE = "6604960da05fea328bfafbfc4b67b6ecb03dcc91"
OTHER_COMMIT = "742d505ece5eb198b3ed4cf7e62cac2296055da8"
HEAD = "0017_categorization_history"
SHA = "0123456789abcdef" * 4


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def offhost(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "version": 1,
        "acceptance": "F003",
        "candidate": CANDIDATE,
        "accepted_at": _now(),
        "set_id": "2026-09-01T010000Z",
        "dump_sha256": SHA,
        "alembic_revision": HEAD,
        "finspace_commit": OTHER_COMMIT,
        "local_verified": True,
        "offhost_verified": True,
        "offhost_verified_at": _now(),
        "offhost_destination_label": "homelab-backup",
        "separate_failure_domain": True,
        "remote_sha256_verified": True,
    }
    document.update(overrides)
    return document


def restore(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "version": 1,
        "acceptance": "F004",
        "candidate": CANDIDATE,
        "accepted_at": _now(),
        "drill_id": "drill-2026-09-01",
        "verdict": "PASSED",
        "clean_host_proven": True,
        "isolated_test_mode": False,
        "restore_result": "restored",
        "data_probe_comparison": "match",
        "operator_login": "ok",
        "operator_ui_data_review": "ok",
        "target_commit": CANDIDATE,
        "target_alembic_head": HEAD,
        "backup_alembic_revision": HEAD,
        "compatibility_case": "same",
        "candidate_relation": "same-commit",
    }
    document.update(overrides)
    return document


def checklist(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "version": 1,
        "acceptance": "release-checklist",
        "candidate": CANDIDATE,
        "acknowledged_at": _now(),
        "open_p0_p1": 0,
        "clean_server_install_verified": True,
        "production_acceptance_verified": True,
    }
    document.update(overrides)
    return document


# ---------------------------------------------------------------------------------------------
# Secret safety
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "POSTGRES_PASSWORD",
        "jwt_secret",
        "n8n_encryption_key",
        "ssh_key",
        "known_hosts",
        "refresh_token",
        "database_url",
        "authorization",
        "api_key",
    ],
)
def test_a_secret_shaped_key_is_refused(key: str) -> None:
    with pytest.raises(EvidenceError, match="forbidden key"):
        release_evidence.assert_secret_safe({key: "anything"})


@pytest.mark.parametrize(
    "value",
    [
        "postgresql+asyncpg://finspace:hunter2@postgres:5432/finspace",
        "postgres://user:pass@host/db",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5",
        "Bearer eyJhbGciOiJIUzI1NiJ9",
    ],
)
def test_a_credential_shaped_value_is_refused(value: str) -> None:
    with pytest.raises(EvidenceError, match="credential"):
        release_evidence.assert_secret_safe({"note": value})


def test_the_secret_scan_reaches_nested_structures() -> None:
    with pytest.raises(EvidenceError, match="forbidden key"):
        release_evidence.assert_secret_safe({"outer": [{"inner": {"password": "x"}}]})


def test_an_allowed_document_survives_the_secret_scan() -> None:
    release_evidence.assert_secret_safe(offhost())
    release_evidence.assert_secret_safe(restore())
    release_evidence.assert_secret_safe(checklist())


# ---------------------------------------------------------------------------------------------
# F003
# ---------------------------------------------------------------------------------------------


def test_a_complete_offhost_acceptance_is_valid() -> None:
    facts = release_evidence.validate_offhost(offhost(), CANDIDATE, HEAD)
    assert facts["set_id"] == "2026-09-01T010000Z"
    assert facts["destination"] == "homelab-backup"


def test_offhost_evidence_for_another_candidate_is_refused() -> None:
    with pytest.raises(EvidenceError, match="candidate"):
        release_evidence.validate_offhost(offhost(candidate=OTHER_COMMIT), CANDIDATE, HEAD)


def test_a_second_directory_on_the_same_disk_is_not_acceptance() -> None:
    with pytest.raises(EvidenceError, match="separate_failure_domain"):
        release_evidence.validate_offhost(offhost(separate_failure_domain=False), CANDIDATE, HEAD)


@pytest.mark.parametrize("field", ["local_verified", "offhost_verified", "remote_sha256_verified"])
def test_an_unverified_copy_is_refused(field: str) -> None:
    with pytest.raises(EvidenceError, match=field):
        release_evidence.validate_offhost(offhost(**{field: False}), CANDIDATE, HEAD)


def test_a_backup_from_an_incompatible_schema_is_refused() -> None:
    with pytest.raises(EvidenceError, match="alembic_revision"):
        release_evidence.validate_offhost(offhost(alembic_revision="0009_older"), CANDIDATE, HEAD)


def test_an_unknown_field_is_refused_rather_than_ignored() -> None:
    with pytest.raises(EvidenceError, match="unknown field"):
        release_evidence.validate_offhost(offhost(extra="value"), CANDIDATE, HEAD)


def test_a_missing_field_is_named() -> None:
    document = offhost()
    del document["dump_sha256"]
    with pytest.raises(EvidenceError, match="dump_sha256"):
        release_evidence.validate_offhost(document, CANDIDATE, HEAD)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("set_id", "yesterday"),
        ("dump_sha256", "short"),
        ("finspace_commit", "6604960"),
        ("offhost_destination_label", "label with spaces"),
    ],
)
def test_a_malformed_value_is_refused(field: str, value: str) -> None:
    with pytest.raises(EvidenceError, match=field):
        release_evidence.validate_offhost(offhost(**{field: value}), CANDIDATE, HEAD)


def test_evidence_cannot_be_dated_in_the_future() -> None:
    ahead = (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with pytest.raises(EvidenceError, match="future"):
        release_evidence.validate_offhost(offhost(accepted_at=ahead), CANDIDATE, HEAD)


def test_a_timestamp_without_a_timezone_is_refused() -> None:
    with pytest.raises(EvidenceError, match="timezone"):
        release_evidence.validate_offhost(
            offhost(accepted_at="2026-09-01T01:00:00"), CANDIDATE, HEAD
        )


# ---------------------------------------------------------------------------------------------
# F004
# ---------------------------------------------------------------------------------------------


def test_a_complete_restore_acceptance_is_valid() -> None:
    facts = release_evidence.validate_restore(restore(), CANDIDATE, HEAD)
    assert facts["relation"] == "same-commit"
    assert facts["drill_evidence"] == "not supplied"


def test_a_rehearsal_is_not_a_disaster_recovery_drill() -> None:
    with pytest.raises(EvidenceError, match="isolated_test_mode"):
        release_evidence.validate_restore(restore(isolated_test_mode=True), CANDIDATE, HEAD)


def test_an_unproven_host_is_refused() -> None:
    with pytest.raises(EvidenceError, match="clean_host_proven"):
        release_evidence.validate_restore(restore(clean_host_proven=False), CANDIDATE, HEAD)


@pytest.mark.parametrize("verdict", ["FAILED", "PARTIAL", "passed"])
def test_only_a_passed_drill_accepts_a_candidate(verdict: str) -> None:
    with pytest.raises(EvidenceError, match="verdict"):
        release_evidence.validate_restore(restore(verdict=verdict), CANDIDATE, HEAD)


@pytest.mark.parametrize(
    "field",
    ["restore_result", "data_probe_comparison", "operator_login", "operator_ui_data_review"],
)
def test_an_unsuccessful_outcome_is_refused(field: str) -> None:
    with pytest.raises(EvidenceError, match=field):
        release_evidence.validate_restore(restore(**{field: "mismatch"}), CANDIDATE, HEAD)


def test_same_commit_requires_the_drill_to_have_run_the_candidate() -> None:
    with pytest.raises(EvidenceError, match="same-commit"):
        release_evidence.validate_restore(restore(target_commit=OTHER_COMMIT), CANDIDATE, HEAD)


def test_a_reviewed_predecessor_needs_a_written_review() -> None:
    document = restore(
        target_commit=OTHER_COMMIT,
        candidate_relation="reviewed-predecessor",
        predecessor_commit=OTHER_COMMIT,
        predecessor_review="ok",
    )
    with pytest.raises(EvidenceError, match="predecessor_review"):
        release_evidence.validate_restore(document, CANDIDATE, HEAD)


def test_a_reviewed_predecessor_is_accepted_when_it_is_explained() -> None:
    document = restore(
        target_commit=OTHER_COMMIT,
        candidate_relation="reviewed-predecessor",
        predecessor_commit=OTHER_COMMIT,
        predecessor_review=(
            "the candidate differs only in documentation; no schema or runtime change"
        ),
    )
    facts = release_evidence.validate_restore(document, CANDIDATE, HEAD)
    assert facts["relation"] == "reviewed-predecessor"


def test_a_predecessor_that_is_the_candidate_must_be_declared_as_such() -> None:
    document = restore(
        candidate_relation="reviewed-predecessor",
        predecessor_commit=CANDIDATE,
        predecessor_review="this is in fact the candidate itself, declared incorrectly",
    )
    with pytest.raises(EvidenceError, match="same-commit"):
        release_evidence.validate_restore(document, CANDIDATE, HEAD)


def test_predecessor_fields_may_not_ride_along_with_same_commit() -> None:
    document = restore(predecessor_commit=OTHER_COMMIT)
    with pytest.raises(EvidenceError, match="predecessor_commit"):
        release_evidence.validate_restore(document, CANDIDATE, HEAD)


def test_a_drill_that_reached_another_head_is_refused() -> None:
    with pytest.raises(EvidenceError, match="target_alembic_head"):
        release_evidence.validate_restore(
            restore(target_alembic_head="0009_older"), CANDIDATE, HEAD
        )


def _write_drill(path: Path, **overrides: Any) -> None:
    document: dict[str, Any] = {
        "version": 1,
        "drill_id": "drill-2026-09-01",
        "verdict": "PASSED",
        "environment": {"clean_host_proven": True, "isolated_test_mode": False},
    }
    document.update(overrides)
    path.write_text(json.dumps(document), encoding="utf-8")


def test_the_drills_own_evidence_is_cross_checked(tmp_path: Path) -> None:
    drill = tmp_path / "dr-restore-drill-2026-09-01.json"
    _write_drill(drill)
    facts = release_evidence.validate_restore(
        restore(drill_evidence=drill.name), CANDIDATE, HEAD, tmp_path
    )
    assert "cross-checked" in facts["drill_evidence"]


def test_an_acceptance_cannot_claim_a_result_the_drill_did_not_record(tmp_path: Path) -> None:
    drill = tmp_path / "dr.json"
    _write_drill(drill, verdict="FAILED")
    with pytest.raises(EvidenceError, match="verdict"):
        release_evidence.validate_restore(
            restore(drill_evidence=drill.name), CANDIDATE, HEAD, tmp_path
        )


def test_a_cross_checked_drill_must_also_prove_a_clean_host(tmp_path: Path) -> None:
    drill = tmp_path / "dr.json"
    _write_drill(drill, environment={"clean_host_proven": False, "isolated_test_mode": False})
    with pytest.raises(EvidenceError, match="clean host"):
        release_evidence.validate_restore(
            restore(drill_evidence=drill.name), CANDIDATE, HEAD, tmp_path
        )


def test_a_drill_file_left_behind_on_the_disposable_host_is_tolerated(tmp_path: Path) -> None:
    facts = release_evidence.validate_restore(
        restore(drill_evidence="data/acceptance/dr-restore-absent.json"), CANDIDATE, HEAD, tmp_path
    )
    assert facts["drill_evidence"] == "referenced file is not present on this host"


# ---------------------------------------------------------------------------------------------
# Checklist
# ---------------------------------------------------------------------------------------------


def test_a_complete_checklist_reports_no_pending_items() -> None:
    facts, pending = release_evidence.validate_checklist(checklist(), CANDIDATE)
    assert facts["open_p0_p1"] == "0"
    assert pending == []


def test_an_outstanding_operator_procedure_is_reported_as_pending() -> None:
    _, pending = release_evidence.validate_checklist(
        checklist(production_acceptance_verified=False), CANDIDATE
    )
    assert pending == ["production_acceptance_verified"]


@pytest.mark.parametrize("value", ["none", -1, True, 1.5, None])
def test_the_open_defect_count_must_be_a_real_count(value: Any) -> None:
    with pytest.raises(EvidenceError, match="open_p0_p1"):
        release_evidence.validate_checklist(checklist(open_p0_p1=value), CANDIDATE)


def test_a_checklist_for_another_candidate_is_refused() -> None:
    with pytest.raises(EvidenceError, match="candidate"):
        release_evidence.validate_checklist(checklist(candidate=OTHER_COMMIT), CANDIDATE)


# ---------------------------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "validator"),
    [
        ("F003", lambda document: release_evidence.validate_offhost(document, CANDIDATE, HEAD)),
        ("F004", lambda document: release_evidence.validate_restore(document, CANDIDATE, HEAD)),
        ("checklist", lambda document: release_evidence.validate_checklist(document, CANDIDATE)),
    ],
)
def test_an_unfilled_template_never_accepts_a_release(kind: str, validator: Any) -> None:
    # The template exists so nobody has to guess the schema. A template that validated as-is would
    # be a way to approve a release by saving a file.
    with pytest.raises(EvidenceError):
        validator(release_evidence.template(kind, CANDIDATE))


def test_every_template_carries_the_full_required_field_set() -> None:
    assert set(release_evidence.template("F003", CANDIDATE)) >= set(
        release_evidence.OFFHOST_REQUIRED
    )
    assert set(release_evidence.template("F004", CANDIDATE)) >= set(
        release_evidence.RESTORE_REQUIRED
    )
    assert set(release_evidence.template("checklist", CANDIDATE)) >= set(
        release_evidence.CHECKLIST_REQUIRED
    )


# ---------------------------------------------------------------------------------------------
# Loading, the phase log and the rendered document
# ---------------------------------------------------------------------------------------------


def test_broken_json_is_reported_with_its_line(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{\n  not json\n}", encoding="utf-8")
    with pytest.raises(EvidenceError, match="line"):
        release_evidence.load_document(path)


def test_a_missing_file_is_reported_rather_than_crashing(tmp_path: Path) -> None:
    with pytest.raises(EvidenceError, match="cannot be read"):
        release_evidence.load_document(tmp_path / "absent.json")


def test_a_json_array_is_not_an_acceptance_document(tmp_path: Path) -> None:
    path = tmp_path / "array.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(EvidenceError, match="JSON object"):
        release_evidence.load_document(path)


def test_the_phase_log_is_read_strictly(tmp_path: Path) -> None:
    path = tmp_path / "phases.tsv"
    path.write_text("candidate-identity\tpass\t120\tcommit=6604960d\n", encoding="utf-8")
    phases = release_evidence.read_phases(path)
    assert phases == [
        {
            "name": "candidate-identity",
            "status": "pass",
            "duration_ms": 120,
            "summary": "commit=6604960d",
        }
    ]


@pytest.mark.parametrize(
    ("line", "match"),
    [
        ("only\ttwo\n", "four tab-separated"),
        ("phase\tgreen\t1\tsummary\n", "unknown phase status"),
        ("phase\tpass\tquickly\tsummary\n", "milliseconds"),
    ],
)
def test_a_corrupt_phase_log_is_refused(tmp_path: Path, line: str, match: str) -> None:
    path = tmp_path / "phases.tsv"
    path.write_text(line, encoding="utf-8")
    with pytest.raises(EvidenceError, match=match):
        release_evidence.read_phases(path)


def test_the_rendered_document_publishes_the_known_limitations() -> None:
    document = release_evidence.build_document(
        CANDIDATE, HEAD, 17, [], "pass", "pending", "blocked", ["F003 pending"]
    )
    assert document["release_status"] == "blocked"
    assert [item["id"] for item in document["known_limitations"]] == [
        item["id"] for item in release_evidence.KNOWN_LIMITATIONS
    ]
    assert document["blockers"] == ["F003 pending"]


def test_the_document_is_published_atomically_and_privately(tmp_path: Path) -> None:
    target = tmp_path / "rc.json"
    document = release_evidence.build_document(CANDIDATE, HEAD, 17, [], "pass", "pass", "pass", [])
    release_evidence.write_atomically(target, document)
    assert json.loads(target.read_text(encoding="utf-8"))["candidate"] == CANDIDATE
    assert not (tmp_path / "rc.json.partial").exists()
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_a_failed_write_leaves_no_partial_file(tmp_path: Path) -> None:
    target = tmp_path / "rc.json"

    class Unserialisable:
        pass

    with pytest.raises(TypeError):
        release_evidence.write_atomically(target, {"bad": Unserialisable()})
    assert not (tmp_path / "rc.json.partial").exists()
    assert not target.exists()


# ---------------------------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------------------------


def _write(path: Path, document: dict[str, Any]) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_the_cli_reports_a_valid_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path / "f003.json", offhost())
    code = release_evidence.main(
        [
            "validate-offhost",
            "--file",
            str(path),
            "--candidate",
            CANDIDATE,
            "--expect-alembic-head",
            HEAD,
        ]
    )
    assert code == 0
    assert "VALID" in capsys.readouterr().out


def test_the_cli_reports_an_invalid_document(tmp_path: Path) -> None:
    path = _write(tmp_path / "f003.json", offhost(separate_failure_domain=False))
    code = release_evidence.main(
        [
            "validate-offhost",
            "--file",
            str(path),
            "--candidate",
            CANDIDATE,
            "--expect-alembic-head",
            HEAD,
        ]
    )
    assert code == release_evidence.EXIT_INVALID


def test_an_open_defect_fails_rather_than_blocks(tmp_path: Path) -> None:
    path = _write(tmp_path / "checklist.json", checklist(open_p0_p1=2))
    code = release_evidence.main(
        ["validate-checklist", "--file", str(path), "--candidate", CANDIDATE]
    )
    assert code == release_evidence.EXIT_INVALID


def test_an_outstanding_procedure_blocks_rather_than_fails(tmp_path: Path) -> None:
    path = _write(tmp_path / "checklist.json", checklist(clean_server_install_verified=False))
    code = release_evidence.main(
        ["validate-checklist", "--file", str(path), "--candidate", CANDIDATE]
    )
    assert code == release_evidence.EXIT_PENDING


def test_the_cli_refuses_an_abbreviated_candidate(tmp_path: Path) -> None:
    path = _write(tmp_path / "checklist.json", checklist())
    code = release_evidence.main(
        ["validate-checklist", "--file", str(path), "--candidate", "6604960"]
    )
    assert code == release_evidence.EXIT_INVALID


# ---------------------------------------------------------------------------------------------
# Documentation gate
# ---------------------------------------------------------------------------------------------


# The checks below run against fixtures, not against the repository: the backend container mounts
# only backend/, so docs/ and README.md are not reachable from here. Whether the real documentation
# resolves is the release gate's `docs-gate` phase, which runs validate_docs.py on the host.


def test_a_broken_relative_link_is_caught(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text("see [gone](docs/gone.md)\n", encoding="utf-8")
    _, failures = validate_docs.check_links(tmp_path)
    assert any("missing file" in failure for failure in failures)


def test_a_broken_anchor_is_caught(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("# Title\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("see [x](docs/a.md#absent)\n", encoding="utf-8")
    _, failures = validate_docs.check_links(tmp_path)
    assert any("missing anchor" in failure for failure in failures)


def test_a_link_that_leaves_the_repository_is_caught(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text("see [x](../outside.md)\n", encoding="utf-8")
    _, failures = validate_docs.check_links(tmp_path)
    assert any("leaves the repository" in failure for failure in failures)


def test_links_inside_fenced_code_are_not_checked(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text(
        "```sh\nsed 's/x/y/' [not](a-link.md)\n```\n", encoding="utf-8"
    )
    checked, failures = validate_docs.check_links(tmp_path)
    assert (checked, failures) == (0, [])


def test_a_scope_document_that_forgets_a_limitation_is_caught(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text(
        "[scope](docs/supported-scope.md) [release](docs/release.md)\n", encoding="utf-8"
    )
    (tmp_path / "docs" / "supported-scope.md").write_text("# Scope\n", encoding="utf-8")
    (tmp_path / "docs" / "release.md").write_text("# Release\n", encoding="utf-8")
    failures = validate_docs.check_supported_scope(tmp_path)
    assert len(failures) == len(release_evidence.KNOWN_LIMITATIONS)
    assert all("is not documented" in failure for failure in failures)


@pytest.mark.parametrize(
    ("heading", "anchor"),
    [
        ("## 1. Хост и развёртывание", "1-хост-и-развёртывание"),
        ("### `transaction-page-n-plus-1`", "transaction-page-n-plus-1"),
        ("## Backup и restore PostgreSQL", "backup-и-restore-postgresql"),
    ],
)
def test_anchors_follow_the_rendered_form(heading: str, anchor: str) -> None:
    assert validate_docs.slug(heading.lstrip("# ")) == anchor
