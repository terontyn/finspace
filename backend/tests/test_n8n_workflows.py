import json
import os
import urllib.request
from pathlib import Path

import pytest

WORKFLOW_ROOT = Path("/app/n8n/workflows")
if not WORKFLOW_ROOT.exists():
    WORKFLOW_ROOT = Path(__file__).resolve().parents[2] / "n8n" / "workflows"

EXPECTED = {
    "01-recurring-rules.json",
    "02-telegram-bot.json",
    "03-weekly-report.json",
    "04-uncategorized-reminder.json",
    "05-month-close-reminder.json",
    "06-backup-health.json",
}


def test_workflow_contracts_are_safe_and_backend_driven() -> None:
    paths = {item.name: item for item in WORKFLOW_ROOT.glob("*.json")}
    assert EXPECTED <= paths.keys()
    for name in EXPECTED:
        payload = json.loads(paths[name].read_text(encoding="utf-8"))
        serialized = json.dumps(payload, ensure_ascii=False).casefold()
        assert payload["active"] is False
        assert payload["nodes"]
        assert "n8n-nodes-base.postgres" not in serialized
        assert "n8n-nodes-base.redis" not in serialized
        assert "n8n-nodes-base.executecommand" not in serialized
        assert "password" not in serialized
        assert "bot_token" not in serialized
        assert "servicekey " not in serialized
        assert "finspace_backend_url" in serialized
        assert any(node["name"] == "Handle error safely" for node in payload["nodes"])
        for node in payload["nodes"]:
            if node["type"] in {"n8n-nodes-base.httpRequest", "n8n-nodes-base.telegram"}:
                assert node.get("onError") == "continueErrorOutput"
                assert node.get("credentials")


@pytest.mark.skipif(
    not os.environ.get("N8N_SMOKE_URL"),
    reason="Set N8N_SMOKE_URL to run the opt-in live n8n smoke test",
)
def test_n8n_live_health_smoke() -> None:
    base_url = os.environ["N8N_SMOKE_URL"].rstrip("/")
    with urllib.request.urlopen(f"{base_url}/healthz", timeout=5) as response:
        assert response.status == 200
