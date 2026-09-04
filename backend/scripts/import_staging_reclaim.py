"""Inspect, and optionally reclaim, staged import artifacts under ``data/imports``.

Runs in the backend container, the only service that has both the database and the
``data/imports`` mount. It is one-shot: there is no daemon to supervise and no new always-running
process in the deployment.

    python scripts/import_staging_reclaim.py            # inspect only, deletes nothing
    python scripts/import_staging_reclaim.py --json     # the same, machine-readable
    python scripts/import_staging_reclaim.py --apply    # delete only the proven-safe artifacts

Inspection is the default precisely because deletion must be something someone asked for. An
invocation with no arguments can never remove a file.
"""

import argparse
import asyncio
import json
import sys

from app.core.config import settings
from app.core.logging import configure_logging
from app.db.session import AsyncSessionFactory, engine
from app.services import import_staging


def _parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect or reclaim staged import artifacts.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="delete the artifacts classified as safe; without it nothing is removed",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="print the full report as JSON instead of a human summary",
    )
    return parser.parse_args(argv)


def _human(report: import_staging.StagingReport) -> str:
    lines = [
        f"root                        {report.root}",
        f"mode                        {'apply' if report.applied else 'inspect (dry run)'}",
        f"grace hours                 {report.grace_hours}",
        f"batch size                  {report.batch_size}",
        "",
        f"files                       {report.scanned}",
        f"bytes                       {report.total_bytes}",
        f"reclaimable files           {report.reclaimable}",
        f"reclaimable bytes           {report.reclaimable_bytes}",
        f"  of which orphans          {report.orphans_eligible}",
        "",
        f"active (never reclaimed)    {report.skipped_active}",
        f"orphan within grace         {report.skipped_orphan_within_grace}",
        f"ambiguous                   {report.skipped_ambiguous}",
        f"unknown                     {report.skipped_unknown}",
    ]
    if report.applied:
        lines += [
            "",
            f"reclaimed                   {report.reclaimed}",
            f"reclaimed bytes             {report.reclaimed_bytes}",
            f"already absent              {report.already_absent}",
            f"failures                    {report.failures}",
            f"bounded by batch size       {report.bounded}",
        ]
    return "\n".join(lines)


async def _execute(apply: bool) -> import_staging.StagingReport:
    try:
        async with AsyncSessionFactory() as session:
            if apply:
                return await import_staging.reclaim_staging(session)
            return await import_staging.inspect_staging(session)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    arguments = _parse_arguments(argv)
    configure_logging()

    if arguments.apply and not settings.import_staging_reclaim_enabled:
        # A central switch beats editing a timer on every host: with reclamation disabled the
        # inspection still works, so an operator can always see what would be reclaimed.
        print(
            "IMPORT_STAGING_RECLAIM_ENABLED is false; refusing to delete. "
            "Run without --apply to inspect.",
            file=sys.stderr,
        )
        return 2

    report = asyncio.run(_execute(arguments.apply))

    if arguments.as_json:
        # stdout has to stay parseable, and the application's log handler writes there too. The
        # document carries every number the summary line would have, so it is skipped in this mode.
        # The scheduled --apply run does not use --json and still writes its line to the journal.
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        import_staging.log_report(report)
        print(_human(report))

    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
