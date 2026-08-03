import importlib
import json
import sys
import uuid
from pathlib import Path

import pytest

SCRIPTS_DIRECTORY = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))
acceptance = importlib.import_module("scripts.google_live_acceptance")


def _registry(run_id: uuid.UUID) -> dict[str, object]:
    return {
        "acceptance_run_id": str(run_id),
        "state": "prepared",
        "manual_gates": {name: False for name in acceptance.MANUAL_GATES},
        "evidence": {},
    }


def test_acceptance_evidence_is_allowlisted_and_rejects_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = uuid.uuid4()
    monkeypatch.setattr(acceptance, "REGISTRY_DIRECTORY", tmp_path)
    monkeypatch.setattr(acceptance.settings, "environment", "development")
    monkeypatch.setattr(acceptance.settings, "testing", False)
    acceptance._write_json(tmp_path / f"{run_id}.json", _registry(run_id))

    acceptance.mark(
        run_id,
        "initial_export",
        "passed",
        "accounts=3 categories=3 transactions=4",
    )
    stored = json.loads((tmp_path / f"{run_id}.json").read_text(encoding="utf-8"))
    assert stored["evidence"]["initial_export"]["status"] == "passed"

    with pytest.raises(acceptance.AcceptanceError, match="Unknown"):
        acceptance.mark(run_id, "typo_item", "passed", None)
    with pytest.raises(acceptance.AcceptanceError, match="credential-like"):
        acceptance.mark(run_id, "oauth", "passed", "token=value")
    with pytest.raises(acceptance.AcceptanceError, match="credential-like"):
        acceptance.mark(
            run_id,
            "sheet_template",
            "passed",
            "https://example.invalid/sheet?code=value",
        )


def test_live_acceptance_refuses_non_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(acceptance.settings, "environment", "production")
    monkeypatch.setattr(acceptance.settings, "testing", False)
    with pytest.raises(acceptance.AcceptanceError, match="only in development"):
        acceptance._require_development()

    monkeypatch.setattr(acceptance.settings, "environment", "development")
    monkeypatch.setattr(acceptance.settings, "testing", True)
    with pytest.raises(acceptance.AcceptanceError, match="only in development"):
        acceptance._require_development()
