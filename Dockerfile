FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies (e.g., libpq for psycopg2 if needed, curl for health checks)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# PostgreSQL 16 client tools (pg_dump / pg_restore) for backups and restores. They must be at least as new as the
# database server (postgres:16), which Debian's own package is not, so they come from the PostgreSQL project's repository.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates gnupg     && install -d /usr/share/postgresql-common/pgdg     && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc     && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" > /etc/apt/sources.list.d/pgdg.list     && apt-get update && apt-get install -y --no-install-recommends postgresql-client-16     && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Copy application code
COPY backend/ backend/
COPY alembic/ alembic/
COPY alembic.ini .
COPY templates/ templates/
COPY static/ static/
# The operational scripts the runbooks tell people to run inside the container:
#   docker compose exec app python scripts/seed_admins.py
#   docker compose exec app python scripts/fallback_sheets.py ...
#   bash scripts/failover.sh
COPY scripts/ scripts/

# Expose port
EXPOSE 8000

# Default command
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
