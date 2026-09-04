"""Report what Finspace is storing and who owns its retention. Read-only.

    python scripts/data_lifecycle_report.py            # human summary
    python scripts/data_lifecycle_report.py --json     # stable machine-readable document

There is no apply mode and no delete mode, by design: this answers "what is growing and who may
reclaim it", and every answer it gives points at a mechanism that already exists. Two JSON
snapshots taken weeks apart can be diffed externally; nothing is persisted here and no table is
added to store history.

Exits non-zero if the database cannot be inspected, so a partial answer can never be mistaken for
a healthy one.
"""

import argparse
import asyncio
import json
import sys

from app.core.logging import configure_logging
from app.db.session import AsyncSessionFactory, engine
from app.services import data_lifecycle

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB")


def _parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only inventory of Finspace data growth and retention ownership.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit the stable JSON document instead of the human summary",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=15,
        help="how many tables to show in the human summary (default 15; JSON always has all)",
    )
    return parser.parse_args(argv)


def human_bytes(value: int) -> str:
    """IEC sizes for people. The JSON keeps integers; only this rendering rounds."""
    size = float(value)
    for unit in _UNITS:
        if size < 1024 or unit == _UNITS[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} {_UNITS[-1]}"


def render(report: data_lifecycle.LifecycleReport, *, top: int) -> str:
    lines = [
        f"database              {report.database_name}",
        f"total size            {human_bytes(report.database_bytes)} "
        f"({report.database_bytes} bytes)",
        f"tables                {len(report.tables)}",
        f"generated at          {report.generated_at}",
        f"status                {report.status}",
        "",
        "Row counts are PostgreSQL estimates and the whole report is a point-in-time view:",
        "writes continue while it runs. No row is read and nothing is modified.",
        "",
        f"{'TABLE':<38} {'TOTAL':>10} {'INDEX':>10} {'ROWS~':>10}  LIFECYCLE / RETENTION OWNER",
    ]
    for table in report.tables[: max(top, 0)]:
        lines.append(
            f"{table.table:<38} "
            f"{human_bytes(table.total_bytes):>10} "
            f"{human_bytes(table.index_bytes):>10} "
            f"{table.row_estimate:>10}  "
            f"{table.lifecycle_class} / {table.retention_owner}"
        )
    remaining = len(report.tables) - max(top, 0)
    if remaining > 0:
        lines.append(f"... {remaining} smaller tables omitted; --json lists all")

    lines += ["", "MANAGED DIRECTORIES VISIBLE TO THIS PROCESS"]
    for directory in report.directories:
        state = "readable" if directory.readable else "UNREADABLE"
        lines.append(
            f"{directory.path:<38} {human_bytes(directory.total_bytes):>10} "
            f"{directory.entries:>10} entries  {state}"
        )
        lines.append(f"{'':<38} owner: {directory.lifecycle_owner}")
        if directory.detail:
            lines.append(f"{'':<38} note:  {directory.detail}")
    lines += [
        "",
        "Backups are not measured here: the host keeps them 0700 root-owned and this process is",
        "unprivileged. Use scripts/data-lifecycle-report.sh on the host for backups and for the",
        "staged-import classification, which F010 owns.",
    ]

    if report.warnings:
        lines += ["", "WARNINGS"]
        lines += [f"  {warning['code']}: {warning['detail']}" for warning in report.warnings]
    return "\n".join(lines)


async def _collect() -> data_lifecycle.LifecycleReport:
    try:
        async with AsyncSessionFactory() as session, session.begin():
            return await data_lifecycle.build_report(session)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    arguments = _parse_arguments(argv)
    configure_logging()
    try:
        report = asyncio.run(_collect())
    except Exception as error:
        # Fail closed: no fallback guess, no empty document that could read as "nothing is growing".
        print(f"data lifecycle report failed: {type(error).__name__}", file=sys.stderr)
        return 1

    if arguments.as_json:
        # The JSON document must be the only thing on stdout, or it cannot be piped into anything.
        # The application's log handler writes there too, so the summary line is skipped here: the
        # document already carries every number that line would have reported.
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        data_lifecycle.log_report(report)
        print(render(report, top=arguments.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
