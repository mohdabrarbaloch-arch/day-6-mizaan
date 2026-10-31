# Setup

## Requirements

- Python 3.11+
- Docker + Docker Compose v2 (optional — SQLite is the default and needs nothing)
- ~120 MB of disk for the venv and dependencies

## Two ways in

### 1. Docker, with PostgreSQL

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"   # paste into SECRET_KEY
docker compose up --build
```

Then **http://localhost:8000**.

The compose stack runs PostgreSQL 16 and points the app at it with
`DATABASE_URL=postgresql+psycopg://mizaan:mizaan@db:5432/mizaan`. Data lives in the
`pgdata` volume, so `docker compose down` keeps it and `down -v` discards it.

**Postgres is not published to the host.** It is on the compose network only (`expose`,
not `ports`), so `psql -h localhost -p 5432` will not reach it. An open 5432 with a
default password is how dev databases end up on the internet. To poke at it:

```bash
docker compose exec db psql -U mizaan -d mizaan
```

`SECRET_KEY` is required by compose (`${SECRET_KEY:?…}`), so an empty value fails fast at
`up` with a clear message rather than booting a stack with a throwaway signing key.

The Postgres driver is `psycopg[binary]==3.2.9`, pinned in `requirements.txt`. Earlier
versions of this file shipped no driver at all, which made the compose path fail at
`create_engine` with `ModuleNotFoundError: No module named 'psycopg'`. That is fixed and
the pin is present.

### 2. Local, SQLite

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

The app creates `mizaan.db`, builds the schema and seeds regions on first start. No
database to install, no migration step to run.

## Environment

Every variable is documented in `.env.example`. The ones that matter:

| Variable | Default | What it does |
|---|---|---|
| `SECRET_KEY` | *(generated)* | JWT signing. If unset, a random per-process key is generated and a warning logged — **sessions then die on restart**. Set it for anything long-lived. |
| `ACCESS_TOKEN_MINUTES` | `1440` | Token lifetime (24h) |
| `DATABASE_URL` | `sqlite:///./mizaan.db` | SQLAlchemy URL |
| `ALLOWED_ORIGINS` | `*` | Comma-separated CORS origins. `*` is refused when `PRODUCTION=1`. |
| `PRODUCTION` | `0` | `1` enables HSTS and disables the permissive CORS fallback |
| `AUTH_RATE_LIMIT` | `10` | Auth requests/min per process |
| `WRITE_RATE_LIMIT` | `60` | Write requests/min per process |
| `SEED_REGIONS` | `us-east:64:12:2.48,…` | `name:quota:used:usd_per_gpu_hour`, comma-separated |
| `LLM_BASE_URL` | *(unset)* | Optional inference backend. Leave unset — health reports `backend: none` |

## First run

1. Open **http://localhost:8000**
2. Click **Create one**, pick any email and a password of 10+ characters with 4+ distinct characters
3. The UI registers with `role: admin` so you can write to the registry

> **On the role:** self-registration as admin is a deliberate choice for a demo so the
> first person in can actually use the thing. In a real deployment you'd close
> registration (`RegisterRequest` already takes `role`, so gate it to `viewer`) and grant
> admin out of band. The role guards themselves are correct and tested.

## Verifying the install

```bash
curl -s localhost:8000/api/health
# {"status":"ok","version":"1.0.0","backend":"none","database":"ok"}

pytest -q          # 148 passed
ruff check .       # All checks passed
black --check .    # 28 files would be left unchanged
```

Those three numbers are the ones measured on the committed tree. If you get a different
test count, something in your environment differs and it is worth knowing before you
trust a result.

## Common snags

**`ImportError: email-validator is not installed`** — `EmailStr` validates through
`email-validator`, and without it `import app.main` fails outright: the app cannot start
at all. It is pinned in `requirements.txt` (`email-validator==2.3.0`). If you installed
dependencies by hand instead, `pip install email-validator`. This bit us in development —
the package was missing from `requirements.txt` while `EmailStr` was used in three
schemas, so a clean clone could not import.

**Port 8000 busy** — `uvicorn app.main:app --port 8010`.

**`sqlite3.OperationalError: database is locked`** — two uvicorn workers on one SQLite
file. Use one worker (the Dockerfile does), or switch to PostgreSQL. WAL is enabled, which
helps but doesn't make SQLite a multi-writer database.

**Costs show `—` instead of a number** — that deployment's region is missing from the
`regions` table, so it is reported as unpriced and excluded from the total rather than
costed at zero (intentional — see `capacity.fleet_cost`). It shows `—`, not `$0.00`,
precisely so it doesn't look like a working figure. Check `GET /api/capacity/regions`.

**Every deployment shows the same digest, is that broken?** No — that's the point. Same
effective manifest, same digest. Add an override to one region and watch them diverge.

**Rate limits feel higher than configured** — they are per-process and in memory. With N
workers the effective limit is N × the configured value, and a restart clears the window.
Deliberate for a single-worker build; the shared-store fix is in `ARCHITECTURE.md`.
