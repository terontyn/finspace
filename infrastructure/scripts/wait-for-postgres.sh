#!/bin/sh
set -eu

python - <<'PY'
import asyncio
import os
import sys

import asyncpg


async def main() -> None:
    database_url = os.environ.get("DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql://", 1)
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    for attempt in range(30):
        try:
            connection = await asyncpg.connect(database_url, timeout=2)
            await connection.close()
            return
        except Exception:
            if attempt == 29:
                raise SystemExit("PostgreSQL did not become ready in time")
            await asyncio.sleep(2)


asyncio.run(main())
PY
