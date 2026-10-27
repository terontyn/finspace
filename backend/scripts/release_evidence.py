"""Validate release-acceptance evidence and render the release-candidate evidence document.

The release-candidate gate (`scripts/release-candidate-gate.sh`) can decide every *engineering*
question by running a command. It cannot decide the two operational ones — whether a verified
backup really reached a second failure domain (F003) and whether a clean host really restored it
(F004) — because those happen on hardware, not in a repository. This module is the contract for
how an operator states that they happened, and the rules that make such a statement acceptable.

Three properties matter more than convenience here.

*Binding.* An acceptance document names the exact candidate it authorises. That is what stops a
year-old drill from silently approving today's commit; no arbitrary expiry window is invented,
because an operator re-stating the candidate is both stronger and more honest than a countdown.

*Secret safety.* Acceptance evidence is written by hand and may be pasted into a report, so the
schema is a strict allowlist and every value is additionally scanned for secret shapes. An unknown
key is refused rather than ignored.

*Truthfulness.* The renderer records what actually happened, including a BLOCKED release, and
publishes the document atomically so a failed run can never leave a truncated file that reads as
success.

Standard library only: this runs on the operator's host, outside the backend virtualenv.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------------------------

EVIDENCE_VERSION = 1

COMMIT = re.compile(r"\A[0-9a-f]{40}\Z")
SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
SET_ID = re.compile(r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z\Z")
REVISION = re.compile(r"\A[0-9a-z_]{1,64}\Z")
LABEL = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")
DRILL_ID = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")

# Known non-blocking debt carried into 1.0. This tuple is the machine-readable half of
# docs/supported-scope.md; validate_docs.py fails when the document stops mentioning an entry, so
# the two cannot drift apart.
KNOWN_LIMITATIONS: tuple[dict[str, str], ...] = (
    {
        "id": "transaction-page-n-plus-1",
        "summary": (
            "the transaction page costs about 3.3 SQL statements per returned row: one splits "
            "query plus an account and a category load per row"
        ),
        "detected_by": "F014",
        "blocking": "no",
        "reference": "docs/supported-scope.md",
    },
    {
        "id": "aggregate-materialisation",
        "summary": (
            "balance and financial-summary paths load every matching transaction into Python "
            "instead of aggregating in SQL; statement counts stay flat"
        ),
        "detected_by": "F014",
        "blocking": "no",
        "reference": "docs/supported-scope.md",
    },
)


class EvidenceError(Exception):
    """An acceptance document is missing, malformed, unsafe or not bound to this candidate."""


# ---------------------------------------------------------------------------------------------
# Secret safety
# ---------------------------------------------------------------------------------------------

# Substrings that must never name a key in an acceptance document. Acceptance evidence records
# *that* something happened, never *how to do it again*.
FORBIDDEN_KEY_PARTS = (
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "database_url",
    "dsn",
    "encryption_key",
    "jwt",
    "known_hosts",
    "passwd",
    "password",
    "private_key",
    "secret",
    "ssh_key",
    "token",
)

# Shapes a value must never have, even under an allowed key.
FORBIDDEN_VALUE_PATTERNS = (
    re.compile(r"postgres(?:ql)?(?:\+[a-z]+)?://", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"ssh-(?:rsa|ed25519) ", re.IGNORECASE),
    re.compile(r"Bearer\s+\S", re.IGNORECASE),
    re.compile(r"\A[A-Za-z0-9._-]+://[^/\s]*:[^@/\s]+@", re.IGNORECASE),
)


def assert_secret_safe(document: object, path: str = "") -> None:
    """Refuse a document whose keys or values look like credentials.

    Runs before schema validation so a careless file is rejected on its shape alone, whatever else
    it happens to contain.
    """
    if isinstance(document, Mapping):
        for key, value in document.items():
            if not isinstance(key, str):
                raise EvidenceError(f"{path or 'document'}: non-string key")
            folded = key.casefold()
            for part in FORBIDDEN_KEY_PARTS:
                if part in folded:
                    raise EvidenceError(
                        f"forbidden key {key!r}: acceptance evidence carries no secrets"
                    )
            assert_secret_safe(value, f"{path}.{key}" if path else key)
        return
    if isinstance(document, list):
        for index, value in enumerate(document):
            assert_secret_safe(value, f"{path}[{index}]")
        return
    if isinstance(document, str):
        for pattern in FORBIDDEN_VALUE_PATTERNS:
            if pattern.search(document):
                raise EvidenceError(f"{path or 'document'}: value looks like a credential")


# ---------------------------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------------------------


def load_document(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise EvidenceError(f"{path}: cannot be read ({error.strerror or error})") from error
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise EvidenceError(
            f"{path}: is not valid JSON ({error.msg}, line {error.lineno})"
        ) from error
    if not isinstance(parsed, dict):
        raise EvidenceError(f"{path}: top level must be a JSON object")
    assert_secret_safe(parsed)
    return parsed


def require_exact_keys(
    document: Mapping[str, Any], allowed: Iterable[str], required: Iterable[str]
) -> None:
    unknown = sorted(set(document) - set(allowed))
    if unknown:
        raise EvidenceError(
            f"unknown field(s) {', '.join(unknown)}: the schema is a strict allowlist"
        )
    missing = sorted(set(required) - set(document))
    if missing:
        raise EvidenceError(f"missing required field(s) {', '.join(missing)}")


def text(document: Mapping[str, Any], field: str, pattern: re.Pattern[str] | None = None) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{field}: must be a non-empty string")
    if pattern is not None and not pattern.match(value):
        raise EvidenceError(f"{field}: {value!r} does not match the expected form")
    return value


def flag(document: Mapping[str, Any], field: str) -> bool:
    value = document.get(field)
    if not isinstance(value, bool):
        raise EvidenceError(f"{field}: must be true or false")
    return value


def require_true(document: Mapping[str, Any], field: str) -> None:
    if not flag(document, field):
        raise EvidenceError(f"{field}: acceptance requires this to be true")


def timestamp(document: Mapping[str, Any], field: str) -> datetime:
    value = text(document, field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise EvidenceError(f"{field}: {value!r} is not an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise EvidenceError(f"{field}: {value!r} has no timezone")
    if parsed > datetime.now(UTC):
        raise EvidenceError(f"{field}: {value!r} is in the future")
    return parsed


def require_version(document: Mapping[str, Any]) -> None:
    if document.get("version") != EVIDENCE_VERSION:
        raise EvidenceError(f"version: expected {EVIDENCE_VERSION}")


def require_candidate(document: Mapping[str, Any], candidate: str) -> None:
    stated = text(document, "candidate", COMMIT)
    if stated != candidate:
        raise EvidenceError(
            f"candidate: evidence authorises {stated[:12]}, the gate is running {candidate[:12]}"
        )


def require_kind(document: Mapping[str, Any], expected: str) -> None:
    if document.get("acceptance") != expected:
        raise EvidenceError(
            f"acceptance: expected {expected!r}, found {document.get('acceptance')!r}"
        )


# ---------------------------------------------------------------------------------------------
# F003 — physical off-host backup acceptance
# ---------------------------------------------------------------------------------------------

OFFHOST_FIELDS = (
    "version",
    "acceptance",
    "candidate",
    "accepted_at",
    "set_id",
    "dump_sha256",
    "alembic_revision",
    "finspace_commit",
    "local_verified",
    "offhost_verified",
    "offhost_verified_at",
    "offhost_destination_label",
    "separate_failure_domain",
    "remote_sha256_verified",
    "note",
)

OFFHOST_REQUIRED = tuple(field for field in OFFHOST_FIELDS if field != "note")


def validate_offhost(
    document: Mapping[str, Any], candidate: str, expected_head: str
) -> dict[str, str]:
    """F003: a verified backup set reached a genuinely separate host, accepted for this candidate.

    `local_verified` and `offhost_verified` are the two facts `backup-set-report.json` already
    records. `separate_failure_domain` is the one thing no script can prove and the operator must
    assert, because a second directory on the same disk satisfies every automated check.
    """
    require_exact_keys(document, OFFHOST_FIELDS, OFFHOST_REQUIRED)
    require_version(document)
    require_kind(document, "F003")
    require_candidate(document, candidate)
    timestamp(document, "accepted_at")
    timestamp(document, "offhost_verified_at")

    set_id = text(document, "set_id", SET_ID)
    text(document, "dump_sha256", SHA256)
    revision = text(document, "alembic_revision", REVISION)
    text(document, "finspace_commit", COMMIT)
    label = text(document, "offhost_destination_label", LABEL)
    if "note" in document:
        text(document, "note")

    require_true(document, "local_verified")
    require_true(document, "offhost_verified")
    require_true(document, "remote_sha256_verified")
    require_true(document, "separate_failure_domain")

    # Schema compatibility rather than commit equality: a backup taken from the release currently
    # in production is exactly what a real recovery would use, and demanding a same-commit backup
    # would force the whole exercise to be repeated after a documentation change.
    if revision != expected_head:
        raise EvidenceError(
            f"alembic_revision: the backup set carries {revision!r}, "
            f"the candidate expects {expected_head!r}"
        )
    return {"set_id": set_id, "alembic_revision": revision, "destination": label}


# ---------------------------------------------------------------------------------------------
# F004 — clean-environment restore acceptance
# ---------------------------------------------------------------------------------------------

RESTORE_FIELDS = (
    "version",
    "acceptance",
    "candidate",
    "accepted_at",
    "drill_id",
    "drill_evidence",
    "verdict",
    "clean_host_proven",
    "isolated_test_mode",
    "restore_result",
    "data_probe_comparison",
    "operator_login",
    "operator_ui_data_review",
    "target_commit",
    "target_alembic_head",
    "backup_alembic_revision",
    "compatibility_case",
    "candidate_relation",
    "predecessor_commit",
    "predecessor_review",
    "note",
)

RESTORE_REQUIRED = (
    "version",
    "acceptance",
    "candidate",
    "accepted_at",
    "drill_id",
    "verdict",
    "clean_host_proven",
    "isolated_test_mode",
    "restore_result",
    "data_probe_comparison",
    "operator_login",
    "operator_ui_data_review",
    "target_commit",
    "target_alembic_head",
    "backup_alembic_revision",
    "compatibility_case",
    "candidate_relation",
)

RELATIONS = ("same-commit", "reviewed-predecessor")

OUTCOME_FIELDS = (
    "restore_result",
    "data_probe_comparison",
    "operator_login",
    "operator_ui_data_review",
)

ACCEPTED_OUTCOMES = ("ok", "match", "restored")


def validate_restore(
    document: Mapping[str, Any],
    candidate: str,
    expected_head: str,
    project_root: Path | None = None,
) -> dict[str, str]:
    """F004: a disposable clean host restored a verified backup and ran the candidate.

    The drill itself is `scripts/dr-restore-drill.sh` and its evidence keeps its own shape; nothing
    is re-implemented here. When that file is present it is cross-checked, so an acceptance
    document cannot claim a drill result the drill did not record.
    """
    require_exact_keys(document, RESTORE_FIELDS, RESTORE_REQUIRED)
    require_version(document)
    require_kind(document, "F004")
    require_candidate(document, candidate)
    timestamp(document, "accepted_at")

    drill_id = text(document, "drill_id", DRILL_ID)
    if text(document, "verdict") != "PASSED":
        raise EvidenceError("verdict: only a PASSED drill can accept a release candidate")
    require_true(document, "clean_host_proven")
    if flag(document, "isolated_test_mode"):
        raise EvidenceError(
            "isolated_test_mode: a rehearsal on the development machine is not F004 acceptance"
        )
    for field in OUTCOME_FIELDS:
        value = text(document, field)
        if value not in ACCEPTED_OUTCOMES:
            raise EvidenceError(f"{field}: {value!r} is not an accepted outcome")

    target_commit = text(document, "target_commit", COMMIT)
    head = text(document, "target_alembic_head", REVISION)
    backup_revision = text(document, "backup_alembic_revision", REVISION)
    text(document, "compatibility_case")
    if "note" in document:
        text(document, "note")
    if head != expected_head:
        raise EvidenceError(
            f"target_alembic_head: the drill reached {head!r}, "
            f"the candidate expects {expected_head!r}"
        )

    relation = text(document, "candidate_relation")
    if relation not in RELATIONS:
        raise EvidenceError(f"candidate_relation: must be one of {', '.join(RELATIONS)}")
    if relation == "same-commit":
        if target_commit != candidate:
            raise EvidenceError(
                "candidate_relation: 'same-commit' requires target_commit to be the candidate"
            )
        for field in ("predecessor_commit", "predecessor_review"):
            if field in document:
                raise EvidenceError(f"{field}: only 'reviewed-predecessor' carries this field")
    else:
        # The escape hatch exists so a documentation-only change does not force a destructive
        # re-run, and it is deliberately narrow: the operator must name the drilled commit and
        # state, in writing, why the difference cannot affect restore.
        predecessor = text(document, "predecessor_commit", COMMIT)
        review = text(document, "predecessor_review")
        if len(review.strip()) < 16:
            raise EvidenceError(
                "predecessor_review: state why the difference cannot affect restore"
            )
        if predecessor != target_commit:
            raise EvidenceError("predecessor_commit: must be the commit the drill actually ran")
        if predecessor == candidate:
            raise EvidenceError(
                "candidate_relation: the drill ran the candidate itself, declare 'same-commit'"
            )

    cross_checked = "not supplied"
    reference = document.get("drill_evidence")
    if isinstance(reference, str) and reference.strip():
        cross_checked = _cross_check_drill(reference, drill_id, project_root)

    return {
        "drill_id": drill_id,
        "relation": relation,
        "backup_alembic_revision": backup_revision,
        "drill_evidence": cross_checked,
    }


def _cross_check_drill(reference: str, drill_id: str, project_root: Path | None) -> str:
    """Compare the acceptance claims against the drill's own evidence file when it is available."""
    path = Path(reference)
    if not path.is_absolute() and project_root is not None:
        path = project_root / path
    if not path.is_file():
        # The drill runs on a disposable host, so its file legitimately may not travel with the
        # acceptance document. Absence is not a failure, but a file that is present must agree.
        return "referenced file is not present on this host"
    drill = load_document(path)
    if drill.get("drill_id") != drill_id:
        raise EvidenceError(f"drill_evidence: {reference} records a different drill_id")
    if drill.get("verdict") != "PASSED":
        raise EvidenceError(f"drill_evidence: {reference} records verdict {drill.get('verdict')!r}")
    environment = drill.get("environment")
    if not isinstance(environment, Mapping):
        raise EvidenceError(f"drill_evidence: {reference} has no environment block")
    if environment.get("clean_host_proven") is not True:
        raise EvidenceError(f"drill_evidence: {reference} does not prove a clean host")
    if environment.get("isolated_test_mode") is not False:
        raise EvidenceError(f"drill_evidence: {reference} was an isolated rehearsal")
    return f"cross-checked against {reference}"


# ---------------------------------------------------------------------------------------------
# Release checklist
# ---------------------------------------------------------------------------------------------

CHECKLIST_FIELDS = (
    "version",
    "acceptance",
    "candidate",
    "acknowledged_at",
    "open_p0_p1",
    "clean_server_install_verified",
    "production_acceptance_verified",
    "note",
)

CHECKLIST_REQUIRED = tuple(field for field in CHECKLIST_FIELDS if field != "note")

CHECKLIST_ITEMS = ("clean_server_install_verified", "production_acceptance_verified")


def validate_checklist(
    document: Mapping[str, Any], candidate: str
) -> tuple[dict[str, str], list[str]]:
    """Facts no repository check can establish: open defects and the two operator procedures.

    An outstanding item blocks the release. An open P0/P1 fails it, because that is an asserted
    defect rather than missing paperwork.
    """
    require_exact_keys(document, CHECKLIST_FIELDS, CHECKLIST_REQUIRED)
    require_version(document)
    require_kind(document, "release-checklist")
    require_candidate(document, candidate)
    timestamp(document, "acknowledged_at")
    if "note" in document:
        text(document, "note")

    open_defects = document.get("open_p0_p1")
    if isinstance(open_defects, bool) or not isinstance(open_defects, int) or open_defects < 0:
        raise EvidenceError("open_p0_p1: must be a non-negative integer")

    pending = [field for field in CHECKLIST_ITEMS if not flag(document, field)]
    return {"open_p0_p1": str(open_defects)}, pending


# ---------------------------------------------------------------------------------------------
# Rendering the release-candidate evidence document
# ---------------------------------------------------------------------------------------------

PHASE_STATUSES = ("pass", "fail", "skipped", "pending")


def read_phases(path: Path) -> list[dict[str, Any]]:
    """Read the tab-separated phase log the orchestrator appends to.

    A tab-separated log keeps the shell honest: it cannot accidentally emit something that parses
    as a valid evidence document while describing a run that did not happen.
    """
    phases: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 4:
            raise EvidenceError(f"{path}:{number}: expected four tab-separated fields")
        name, status, duration, summary = parts
        if status not in PHASE_STATUSES:
            raise EvidenceError(f"{path}:{number}: unknown phase status {status!r}")
        try:
            duration_ms = int(duration)
        except ValueError as error:
            raise EvidenceError(f"{path}:{number}: duration must be whole milliseconds") from error
        phases.append(
            {"name": name, "status": status, "duration_ms": duration_ms, "summary": summary}
        )
    return phases


def build_document(
    candidate: str,
    expected_head: str,
    expected_count: int,
    phases: Sequence[Mapping[str, Any]],
    engineering_status: str,
    operational_status: str,
    release_status: str,
    blockers: Sequence[str],
) -> dict[str, Any]:
    return {
        "version": EVIDENCE_VERSION,
        "candidate": candidate,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expected_alembic_head": expected_head,
        "expected_alembic_count": expected_count,
        "engineering_status": engineering_status,
        "operational_status": operational_status,
        "release_status": release_status,
        "phases": [dict(phase) for phase in phases],
        "blockers": list(blockers),
        "known_limitations": [dict(item) for item in KNOWN_LIMITATIONS],
    }


def write_atomically(path: Path, document: Mapping[str, Any]) -> None:
    """Publish the evidence document in one step.

    A gate that fails halfway must not leave a half-written file behind: the next reader would see
    a truncated document, and truncated JSON that happens to parse is worse than none at all.
    """
    partial = path.with_name(path.name + ".partial")
    payload = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    os.replace(partial, path)


# ---------------------------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------------------------


def template(kind: str, candidate: str) -> dict[str, Any]:
    """A skeleton carrying every required field, so an operator never has to guess the schema.

    Every placeholder is deliberately invalid: a template that is merely saved, not filled in,
    must fail validation rather than accidentally accept a release.
    """
    if kind == "F003":
        return {
            "version": EVIDENCE_VERSION,
            "acceptance": "F003",
            "candidate": candidate,
            "accepted_at": "REPLACE",
            "set_id": "REPLACE",
            "dump_sha256": "REPLACE",
            "alembic_revision": "REPLACE",
            "finspace_commit": "REPLACE",
            "local_verified": False,
            "offhost_verified": False,
            "offhost_verified_at": "REPLACE",
            "offhost_destination_label": "REPLACE",
            "separate_failure_domain": False,
            "remote_sha256_verified": False,
        }
    if kind == "F004":
        return {
            "version": EVIDENCE_VERSION,
            "acceptance": "F004",
            "candidate": candidate,
            "accepted_at": "REPLACE",
            "drill_id": "REPLACE",
            "drill_evidence": "data/acceptance/dr-restore-REPLACE.json",
            "verdict": "REPLACE",
            "clean_host_proven": False,
            "isolated_test_mode": True,
            "restore_result": "REPLACE",
            "data_probe_comparison": "REPLACE",
            "operator_login": "REPLACE",
            "operator_ui_data_review": "REPLACE",
            "target_commit": "REPLACE",
            "target_alembic_head": "REPLACE",
            "backup_alembic_revision": "REPLACE",
            "compatibility_case": "REPLACE",
            "candidate_relation": "same-commit",
        }
    return {
        "version": EVIDENCE_VERSION,
        "acceptance": "release-checklist",
        "candidate": candidate,
        "acknowledged_at": "REPLACE",
        "open_p0_p1": -1,
        "clean_server_install_verified": False,
        "production_acceptance_verified": False,
    }


# ---------------------------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------------------------

# 0 valid and complete, 1 invalid or an asserted release blocker, 3 valid but items still pending.
EXIT_VALID = 0
EXIT_INVALID = 1
EXIT_PENDING = 3


def _summary(pairs: Mapping[str, str]) -> str:
    return " ".join(f"{key}={value}" for key, value in pairs.items())


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate release-acceptance evidence and render release-candidate evidence.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    for name in ("validate-offhost", "validate-restore", "validate-checklist"):
        sub = commands.add_parser(name)
        sub.add_argument("--file", type=Path, required=True)
        sub.add_argument("--candidate", required=True)
        if name != "validate-checklist":
            sub.add_argument("--expect-alembic-head", required=True)
        if name == "validate-restore":
            sub.add_argument("--project-root", type=Path, default=Path.cwd())

    render = commands.add_parser("render")
    render.add_argument("--phases", type=Path, required=True)
    render.add_argument("--candidate", required=True)
    render.add_argument("--expect-alembic-head", required=True)
    render.add_argument("--expect-alembic-count", type=int, required=True)
    render.add_argument("--engineering-status", required=True)
    render.add_argument("--operational-status", required=True)
    render.add_argument("--release-status", required=True)
    render.add_argument("--blocker", action="append", default=[])
    render.add_argument("--output", type=Path, default=None)

    skeleton = commands.add_parser("template")
    skeleton.add_argument("kind", choices=("F003", "F004", "checklist"))
    skeleton.add_argument("--candidate", default="0" * 40)

    return parser.parse_args(argv)


def _render(arguments: argparse.Namespace) -> int:
    try:
        phases = read_phases(arguments.phases)
    except (EvidenceError, OSError) as error:
        print(f"release evidence: FAIL: {error}", file=sys.stderr)
        return EXIT_INVALID
    document = build_document(
        arguments.candidate,
        arguments.expect_alembic_head,
        arguments.expect_alembic_count,
        phases,
        arguments.engineering_status,
        arguments.operational_status,
        arguments.release_status,
        arguments.blocker,
    )
    if arguments.output is None:
        print(json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False))
        return EXIT_VALID
    try:
        write_atomically(arguments.output, document)
    except OSError as error:
        print(f"release evidence: FAIL: {arguments.output}: {error}", file=sys.stderr)
        return EXIT_INVALID
    print(f"release evidence written to {arguments.output}")
    return EXIT_VALID


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parse_arguments(argv)

    if arguments.command == "template":
        print(json.dumps(template(arguments.kind, arguments.candidate), indent=2, sort_keys=True))
        return EXIT_VALID
    if arguments.command == "render":
        return _render(arguments)

    if not COMMIT.match(arguments.candidate):
        print(
            "release evidence: FAIL: candidate must be a full 40-character commit", file=sys.stderr
        )
        return EXIT_INVALID

    try:
        document = load_document(arguments.file)
        if arguments.command == "validate-offhost":
            facts = validate_offhost(document, arguments.candidate, arguments.expect_alembic_head)
            print(f"F003 acceptance: VALID {_summary(facts)}")
        elif arguments.command == "validate-restore":
            facts = validate_restore(
                document,
                arguments.candidate,
                arguments.expect_alembic_head,
                arguments.project_root,
            )
            print(f"F004 acceptance: VALID {_summary(facts)}")
        else:
            facts, pending = validate_checklist(document, arguments.candidate)
            if facts["open_p0_p1"] != "0":
                raise EvidenceError(
                    f"open_p0_p1: {facts['open_p0_p1']} open P0/P1 defect(s) block a 1.0 release"
                )
            print(f"release checklist: VALID {_summary(facts)}")
            for item in pending:
                print(f"release checklist: PENDING {item}")
            if pending:
                return EXIT_PENDING
    except EvidenceError as error:
        print(f"{arguments.file}: INVALID: {error}", file=sys.stderr)
        return EXIT_INVALID
    return EXIT_VALID


if __name__ == "__main__":
    raise SystemExit(main())
