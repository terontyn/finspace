.PHONY: up down logs ps migrate downgrade test auth-test import-test google-test sync-test \
	reconciliation-test test-db-safety google-config-check google-live-acceptance \
	google-live-cleanup google-live-report sync-worker apps-script-package lint format \
	frontend-check backup backup-verify restore-test backup-cleanup n8n-up n8n-status \
	n8n-export n8n-import automation-test telegram-test recurring-test month-close-test \
	backup-secondary-test apps-script-test reset

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs --follow

ps:
	docker compose ps

migrate:
	docker compose exec backend alembic upgrade head
	docker compose exec backend python -m app.db.seed

downgrade:
	docker compose exec backend alembic downgrade -1

test:
	docker compose exec -e TESTING=true backend python scripts/test_runner.py

auth-test:
	docker compose exec -e TESTING=true backend python scripts/test_runner.py tests/test_auth.py -q

import-test:
	docker compose exec -e TESTING=true backend python scripts/test_runner.py tests/test_imports.py -q

google-test:
	docker compose exec -e TESTING=true backend python scripts/test_runner.py tests/test_google_sync.py -q

sync-test:
	docker compose exec -e TESTING=true backend python scripts/test_runner.py -q tests/test_google_sync.py::test_sheet_template_outbox_disconnect_and_revoke tests/test_google_sync.py::test_webhook_hmac_idempotency_transaction_and_conflict tests/test_google_sync.py::test_outbox_covers_entities_lifecycle_and_paused_binding tests/test_google_sync.py::test_import_commit_and_rollback_enqueue_outbox tests/test_google_sync.py::test_worker_lock_retry_completion_and_permanent_failure

reconciliation-test:
	docker compose exec -e TESTING=true backend python scripts/test_runner.py -q tests/test_google_sync.py::test_reconciliation_creates_run_and_restores_missing_rows tests/test_google_sync.py::test_reconciliation_classifies_hash_version_duplicates_and_tamper

test-db-safety:
	docker compose exec -e TESTING=true backend python scripts/test_db_safety_check.py

google-config-check:
	docker compose exec backend python scripts/google_config_check.py

google-live-acceptance: google-config-check
	docker compose exec backend python scripts/google_live_acceptance.py start

google-live-cleanup:
	@test -n "$(ACCEPTANCE_RUN_ID)" || (echo "ACCEPTANCE_RUN_ID is required" >&2; exit 2)
	docker compose exec backend python scripts/google_live_acceptance.py cleanup --run-id "$(ACCEPTANCE_RUN_ID)"

google-live-report:
	@test -n "$(ACCEPTANCE_RUN_ID)" || (echo "ACCEPTANCE_RUN_ID is required" >&2; exit 2)
	docker compose exec backend python scripts/google_live_acceptance.py report --run-id "$(ACCEPTANCE_RUN_ID)"

sync-worker:
	docker compose up -d sync-worker

apps-script-package:
	mkdir -p dist
	tar -czf dist/finspace-google-apps-script-v1.tar.gz google-apps-script

apps-script-test:
	node --test google-apps-script/tests/queue-reliability.test.cjs

n8n-up:
	docker compose up -d n8n

n8n-status:
	powershell -ExecutionPolicy Bypass -File scripts/n8n-status.ps1

n8n-export:
	powershell -ExecutionPolicy Bypass -File scripts/n8n-export.ps1

n8n-import:
	powershell -ExecutionPolicy Bypass -File scripts/n8n-import.ps1

automation-test:
	docker compose exec -e TESTING=true backend python scripts/test_runner.py tests/test_automations.py tests/test_n8n_workflows.py -q

telegram-test:
	docker compose exec -e TESTING=true backend python scripts/test_runner.py tests/test_automations.py -q -k telegram

recurring-test:
	docker compose exec -e TESTING=true backend python scripts/test_runner.py tests/test_automations.py -q -k recurring

month-close-test:
	docker compose exec -e TESTING=true backend python scripts/test_runner.py tests/test_automations.py -q -k month_close

backup-secondary-test:
	docker compose exec -e TESTING=true backend python scripts/test_runner.py tests/test_backup_secondary.py -q

lint:
	docker compose exec backend ruff check .
	docker compose exec backend ruff format --check .
	docker compose exec backend mypy app

format:
	docker compose exec backend ruff format .
	docker compose exec backend ruff check --fix .

frontend-check:
	docker compose exec frontend npm run lint
	docker compose exec frontend npm run typecheck
	docker compose exec frontend npm run build

backup:
	docker compose --profile tools run --rm backup sh /scripts/backup.sh

backup-verify:
	docker compose --profile tools run --rm backup sh /scripts/verify-backup.sh --create

restore-test:
	docker compose --profile tools run --rm backup sh /scripts/verify-backup.sh

backup-cleanup:
	docker compose --profile tools run --rm backup sh /scripts/backup-cleanup.sh

reset:
	@echo "DANGER: make reset permanently removes all local project volumes."
	@printf "Type RESET to continue: "; read confirmation; \
	if [ "$$confirmation" = "RESET" ]; then \
		docker compose down --volumes --remove-orphans; \
	else \
		echo "Reset cancelled."; \
	fi
