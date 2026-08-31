FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY runtime-identity.env /usr/local/share/finspace/runtime-identity.env

RUN set -eu; \
    . /usr/local/share/finspace/runtime-identity.env; \
    addgroup --system --gid "$FINSPACE_RUNTIME_GID" app; \
    adduser --system --uid "$FINSPACE_RUNTIME_UID" --ingroup app app; \
    test "$(id -u app)" = "$FINSPACE_RUNTIME_UID"; \
    test "$(id -g app)" = "$FINSPACE_RUNTIME_GID"

WORKDIR /app

COPY pyproject.toml alembic.ini ./
COPY app ./app
COPY alembic ./alembic
COPY tests ./tests
COPY scripts ./scripts

RUN pip install --no-cache-dir --editable ".[dev]" && chown -R app:app /app

USER app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
