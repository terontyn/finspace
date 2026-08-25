from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASE_COMPOSE = PROJECT_ROOT / "docker-compose.yml"
PRODUCTION_COMPOSE = PROJECT_ROOT / "compose.production.yml"

PRODUCTION_BACKEND_MOUNTS = {
    "/app/data/imports": (PROJECT_ROOT / "data" / "imports", False),
    "/app/data/acceptance": (PROJECT_ROOT / "data" / "acceptance", False),
    "/app/data/acceptance-reports": (
        PROJECT_ROOT / "backups" / "acceptance-reports",
        False,
    ),
    "/app/backups": (PROJECT_ROOT / "backups", True),
    "/app/google-apps-script": (PROJECT_ROOT / "google-apps-script", True),
    "/app/n8n": (PROJECT_ROOT / "n8n", True),
}


class TopologyError(RuntimeError):
    pass


def _compose_config(mode: str) -> dict[str, Any]:
    command = ["docker", "compose", "-f", str(BASE_COMPOSE)]
    if mode == "production":
        command.extend(["-f", str(PRODUCTION_COMPOSE)])
    command.extend(["config", "--format", "json"])

    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if result.stderr:
            print(result.stderr.rstrip(), file=sys.stderr)
        raise TopologyError(f"docker compose config failed for {mode}")
    return _parse_config(result.stdout)


def _parse_config(raw_config: str) -> dict[str, Any]:
    try:
        config = json.loads(raw_config)
    except json.JSONDecodeError as error:
        raise TopologyError("Compose config is not valid JSON") from error
    if not isinstance(config, dict):
        raise TopologyError("Compose config root must be an object")
    return config


def _service(config: dict[str, Any], name: str) -> dict[str, Any]:
    services = config.get("services")
    if not isinstance(services, dict):
        raise TopologyError("Compose config has no services object")
    service = services.get(name)
    if not isinstance(service, dict):
        raise TopologyError(f"Compose config has no {name!r} service")
    return service


def _command(service: dict[str, Any]) -> list[str]:
    command = service.get("command") or []
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        raise TopologyError("Service command must be a string list")
    return command


def _mounts(service: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_mounts = service.get("volumes") or []
    if not isinstance(raw_mounts, list):
        raise TopologyError("Service volumes must be a list")

    mounts: dict[str, dict[str, Any]] = {}
    for mount in raw_mounts:
        if not isinstance(mount, dict) or not isinstance(mount.get("target"), str):
            raise TopologyError("Compose returned an invalid volume entry")
        target = mount["target"]
        if target in mounts:
            raise TopologyError(f"Duplicate mount target: {target}")
        mounts[target] = mount
    return mounts


def _assert_bind_source(mount: dict[str, Any], expected_source: Path, target: str) -> None:
    if mount.get("type") != "bind":
        raise TopologyError(f"{target} must be a bind mount")
    source = mount.get("source")
    if not isinstance(source, str):
        raise TopologyError(f"{target} has no source path")
    if Path(source).resolve() != expected_source.resolve():
        raise TopologyError(f"{target} points outside the expected project path")


def validate_development(config: dict[str, Any]) -> None:
    backend = _service(config, "backend")
    worker = _service(config, "sync-worker")
    frontend = _service(config, "frontend")

    if "--reload" not in _command(backend):
        raise TopologyError("Development backend must retain --reload")

    backend_mounts = _mounts(backend)
    worker_mounts = _mounts(worker)
    frontend_mounts = _mounts(frontend)
    if "/app" not in backend_mounts:
        raise TopologyError("Development backend source mount is missing")
    if "/app" not in worker_mounts:
        raise TopologyError("Development worker source mount is missing")
    if "/app" not in frontend_mounts:
        raise TopologyError("Development frontend source mount is missing")

    _assert_bind_source(backend_mounts["/app"], PROJECT_ROOT / "backend", "/app")
    _assert_bind_source(worker_mounts["/app"], PROJECT_ROOT / "backend", "/app")
    _assert_bind_source(frontend_mounts["/app"], PROJECT_ROOT / "frontend", "/app")

    build = frontend.get("build")
    if not isinstance(build, dict) or build.get("target") != "development":
        raise TopologyError("Development frontend must use the development image target")
    for target in ("/app/node_modules", "/app/.next"):
        if target not in frontend_mounts:
            raise TopologyError(f"Development frontend mount is missing: {target}")


def validate_production(config: dict[str, Any]) -> None:
    backend = _service(config, "backend")
    worker = _service(config, "sync-worker")
    frontend = _service(config, "frontend")

    expected_backend_command = [
        "uvicorn",
        "app.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ]
    backend_command = _command(backend)
    if backend_command != expected_backend_command:
        raise TopologyError("Production backend command is not the immutable runtime command")
    if "--reload" in backend_command:
        raise TopologyError("Production backend command contains --reload")

    backend_mounts = _mounts(backend)
    if set(backend_mounts) != set(PRODUCTION_BACKEND_MOUNTS):
        raise TopologyError("Production backend mounts differ from the approved runtime set")
    for target, (source, read_only) in PRODUCTION_BACKEND_MOUNTS.items():
        mount = backend_mounts[target]
        _assert_bind_source(mount, source, target)
        if bool(mount.get("read_only", False)) is not read_only:
            raise TopologyError(f"Production mount has incorrect read-only mode: {target}")

    if _mounts(worker):
        raise TopologyError("Production sync-worker must not have source or runtime mounts")

    if _mounts(frontend):
        raise TopologyError("Production frontend must not have source or cache mounts")
    if _command(frontend) != ["npm", "run", "start"]:
        raise TopologyError("Production frontend must run npm run start")
    build = frontend.get("build")
    if not isinstance(build, dict) or build.get("target") != "production":
        raise TopologyError("Production frontend must use the production image target")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate Finspace development and production Compose topology."
    )
    parser.add_argument("mode", choices=("development", "production", "all"))
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read one already merged Compose JSON document from stdin.",
    )
    args = parser.parse_args()

    try:
        if args.stdin:
            if args.mode == "all":
                raise TopologyError("--stdin requires development or production mode")
            config = _parse_config(sys.stdin.read())
            if args.mode == "development":
                validate_development(config)
            else:
                validate_production(config)
            print(f"{args.mode} topology: PASS")
            return 0

        modes = ("development", "production") if args.mode == "all" else (args.mode,)
        for mode in modes:
            config = _compose_config(mode)
            if mode == "development":
                validate_development(config)
            else:
                validate_production(config)
            print(f"{mode} topology: PASS")
    except TopologyError as error:
        print(f"topology validation: FAIL: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
