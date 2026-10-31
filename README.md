# Mizaan · میزان

**An enterprise AI control plane where multi-region parity is a provable fact, not a guess.**

Model registry · multi-region deployments · pipeline gates · drift detection

---

## The problem

Every enterprise AI platform can push a model to three regions. Almost none of them can answer a simple question a week later:

> *"Are East US and Southeast Asia right now serving the same model?"*

The honest answer in most stacks is "probably". Something drifted — a quantisation flag, a prompt template, a tokenizer revision, a `max_seq_len` — and there is no artefact that says so. You find out when a customer in Singapore gets a different answer than a customer in Ohio, and by then you're debugging a behaviour difference with no baseline.

## The answer

A model is a **manifest** — a structured description of everything that changes behaviour: weights hash, tokenizer hash, prompt-template hash, runtime config, sampling parameters. Mizaan hashes that manifest canonically:

```
same manifest  →  same digest  →  same behaviour
```

A deployment's digest covers its **effective** manifest, the model with region overrides applied. Which means:

- Two regions, no overrides → **identical digest**. That's the parity proof.
- One region with `{"components.runtime.max_seq_len": 4096}` → **digests diverge**, and the drift report names the exact field.

`region` is deliberately **not** in the hash. If it were, every deployment would differ from every other by construction and the digest would carry no information.

---

## What's in it

**Registry** — register a manifest, get a digest plus per-component digests so a report can say *which* piece moved. `name` and `version` are labels, not behaviour, so they're excluded from the hash: two entries for the same weights share a digest.

**Deployments** — place a model into a region with optional dotted-path overrides. Quota is held in the same transaction as the deployment row, so a region cannot be oversubscribed. Cost is `Decimal` arithmetic quantised to 2dp, so two calls never disagree.

**Drift** — a pairwise parity matrix per model with six severity classes ordered worst-first: `weights → tokenizer → template → runtime → parameters → version`. "Different weights" and "different temperature" are not the same emergency, and the report doesn't pretend they are.

**Pipelines** — a DAG validator that reports *all* errors at once (duplicates, unknown deps, self-deps, cycles with the actual path, kind-order violations) and a deterministic topological order, so the same graph always runs the same way.

**Promotion gates** — `{"accuracy": {"min": 0.92}, "latency_p95_ms": {"max": 800}}`. A gated metric with no observed value **fails** its gate. Silence is not a pass — treating it as one is how unmeasured models reach production.

---

## See it work

No screenshots in this README on purpose — I didn't want to ship mockups of a UI you can
run yourself in 30 seconds. Clone it, `docker compose up`, and open
**http://localhost:8000**. You'll see the overview (fleet parity, registry count, GPU
capacity, monthly spend), the parity matrix, the pipeline builder and the capacity page.

What I *can* show you is a real transcript. This is unedited output from a live server on
the committed tree — a fresh database, a registered model, three deployments and a
pipeline. Every digest below can be reproduced by registering the manifest the UI
pre-fills.

```
--- create model
digest: 3f09f24a1bdb

--- deploy us-east (no overrides)
  region: us-east    digest: 3f09f24a1bdb   gpus: 2   cost: 3620.80

--- deploy ap-south (no overrides) -> should MATCH
  region: ap-south   digest: 3f09f24a1bdb

--- PREDICT: parity holds
  all_identical: True | drifting: 0

--- deploy eu-west WITH override -> should DRIFT
  region: eu-west    digest: a46f091179c7

--- PREDICT: drift detected, severity runtime
  all_identical: False | worst: runtime

--- drift detail diffs
  pair ap-south vs eu-west | classes: ['runtime']
     components.runtime.max_seq_len : 8192 -> 4096
  pair eu-west vs us-east | classes: ['runtime']
     components.runtime.max_seq_len : 4096 -> 8192

--- quota guard: 22 GPUs in ap-south (24 quota, 5 used, 19 free)
  status: 409
  detail: Region ap-south has 19 GPU(s) available (quota 24, used 5);
          this deployment needs 22 (short by 3)

--- run PASSES gates
  passed: True  | promotable: True

--- run FAILS gates
  passed: False | promotable: False
```

Two regions, byte-identical digests, and the drift caught with the exact field named.
That's the whole pitch, and you can reproduce every line of it.

Note the third deployment made the quota arithmetic move (`5 used` not `4`) — the numbers
in that 409 are live state, not a fixed string.

---

## Run it

### Docker (PostgreSQL included)

```bash
git clone https://github.com/mohdabrarbaloch-arch/day-6-mizaan.git
cd day-6-mizaan
cp .env.example .env
# generate a real key:
python -c "import secrets; print(secrets.token_urlsafe(48))"   # paste into SECRET_KEY

docker compose up --build
```

Open **http://localhost:8000**. Register an account (the UI asks for `admin` so you can write to the registry).

PostgreSQL is not published to the host — reach it with
`docker compose exec db psql -U mizaan -d mizaan`.

### Local, no Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

SQLite is the default (`DATABASE_URL=sqlite:///./mizaan.db`), so it boots with no database setup at all. Regions are seeded on first start from `SEED_REGIONS`.

### Try the whole thing in 30 seconds

1. **Registry → Register model.** The drawer is pre-filled with a realistic 7B chat manifest — just hit *Register & hash*. You should get `3f09f24a1bdb`.
2. **Deploy** it to `us-east`, then again to `ap-south`, both with no overrides. **Both show the same digest.**
3. **Overview → Parity by model** now says `identical`. That is the claim, verified.
4. Deploy a third time to `eu-west` **with** `{"components.runtime.max_seq_len": 4096}`.
5. **Matrix** now shows drift, severity `runtime`, and the diff reads `components.runtime.max_seq_len: 8192 → 4096`.

---

## API

All endpoints are JSON. Auth is `Authorization: Bearer <jwt>`.

| Method | Path | Role | What it does |
|---|---|---|---|
| `GET` | `/api/health` | — | Liveness, version, backend truth |
| `POST` | `/api/auth/register` | — | Create account, returns JWT |
| `POST` | `/api/auth/login` | — | Sign in |
| `GET` | `/api/auth/me` | viewer | Current user |
| `POST` | `/api/models` | **admin** | Register manifest → digest |
| `GET` | `/api/models` | viewer | List with digests + deploy counts |
| `GET` | `/api/models/{id}` | viewer | One model + component digests |
| `DELETE` | `/api/models/{id}` | **admin** | Delete (409 if deployments exist) |
| `POST` | `/api/deployments` | engineer | Deploy with overrides (409 on quota) |
| `GET` | `/api/deployments` | viewer | List with cost |
| `GET` | `/api/deployments/{id}` | viewer | One deployment |
| `PATCH` | `/api/deployments/{id}/scale` | engineer | Change replicas, adjust quota |
| `DELETE` | `/api/deployments/{id}` | **admin** | Tear down, release quota |
| `POST` | `/api/pipelines/validate` | viewer | Validate a DAG without storing |
| `POST` | `/api/pipelines` | engineer | Create (422 if the graph is invalid) |
| `GET` | `/api/pipelines` | viewer | List pipelines with their order |
| `POST` | `/api/pipelines/{id}/runs` | engineer | Record run, evaluate gates |
| `GET` | `/api/pipelines/{id}/runs` | viewer | Run history |
| `POST` | `/api/pipelines/{id}/runs/{rid}/promote` | engineer | Promote (409 if gates fail) |
| `GET` | `/api/drift` | viewer | Fleet-wide parity verdict per model |
| `GET` | `/api/drift/{model_id}` | viewer | Full pairwise matrix + diffs |
| `GET` | `/api/capacity` | viewer | Quota totals + fleet cost |
| `GET` | `/api/capacity/regions` | viewer | Per-region quota and rate |

Full request/response shapes: [`docs/api.md`](docs/api.md).
Interactive docs at `/docs` (OpenAPI) once running.

### A real response

Captured from `GET /api/drift/1` on the same run as the transcript above:

```json
{
  "model_name": "tarteel-7b",
  "deployment_count": 3,
  "summary": {
    "deployment_count": 3,
    "pair_count": 3,
    "drifting_pairs": 2,
    "all_identical": false,
    "worst_severity": "runtime"
  },
  "cells": [
    {
      "a": 2,
      "b": 3,
      "a_region": "ap-south",
      "b_region": "eu-west",
      "a_environment": "production",
      "b_environment": "production",
      "identical": false,
      "classes": ["runtime"],
      "severity": "runtime",
      "diffs": [
        { "field": "components.runtime.max_seq_len", "a": 8192, "b": 4096 }
      ],
      "summary": "Different runtime configuration (framework, opset or context length)."
    }
  ]
}
```

Two of the three pairs drift here, because one of three deployments carries the override.
With only two deployments and no override it is `pair_count: 1, drifting_pairs: 0,
all_identical: true`.

---

## Tests

```bash
pytest -q          # 148 passed
ruff check .       # clean
black --check .    # 28 files unchanged
```

The suite is split so that **the interesting logic is tested without a database**:

| File | Covers |
|---|---|
| `test_digest.py` | Canonicalisation, key-order independence, float normalisation, NaN rejection, override merging |
| `test_drift.py` | All six severity classes, ordering, field-level diffs, matrix arithmetic |
| `test_pipeline.py` | DAG validation, cycle detection, kind ordering, gate evaluation, promotion rules |
| `test_capacity.py` | Quota arithmetic, Decimal cost, unpriced-region handling |
| `test_api.py` | Auth, role guards, 404-not-403, quota 409, drift end to end, security headers |

Some tests exist to lock in **decisions**, not behaviour:

- `test_two_regions_same_digest_is_parity` fails if anyone makes `region` part of the hash.
- `test_same_manifest_same_digest_across_models` fails if `name`/`version` creep back into the manifest.
- `test_missing_observation_fails_the_gate` fails if someone "improves" a missing metric into a pass.
- `test_other_users_model_is_404_not_403` fails if the ownership guard becomes a 403.

---

## Stack

Python 3.11 · FastAPI 0.115 · SQLAlchemy 2.1 (SQLite dev / PostgreSQL 16 in compose) · Pydantic v2 · PyJWT + bcrypt · vanilla JS SPA, no build step · ruff + black + pytest · Docker + Docker Compose

Pinned exactly in `requirements.txt`, including `email-validator` (required by `EmailStr` —
without it the app will not import) and `psycopg[binary]` (required by the compose
`DATABASE_URL` — without it `create_engine` raises `ModuleNotFoundError`).

---

## Notes and limits

**This ships no inference server.** Deployments are metadata describing intent; `/api/health` reports `backend: "none"` rather than pretending otherwise. Wire a serving layer behind the same deployment records and the digests still mean exactly what they mean now.

**Gate evaluation is arithmetic on numbers you submit.** It does not train or measure anything. What it does enforce is that a run can't be promoted while a gate fails — which is the part people skip.

**Rate limiting is per-process.** With N workers the effective limit is N × the configured value. The fix (a shared store) is written up in `ARCHITECTURE.md` rather than hidden.

**There is no Content-Security-Policy header yet.** The SPA uses a few inline event
attributes; a CSP worth having needs those refactored first. Listed as known work in
`ARCHITECTURE.md` rather than claimed as done.

**Self-registration accepts a role, and the UI sends `admin`.** Deliberate for a demo so
the first account can populate the registry. Close it for real deployments.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the system diagram, data flow, scaling notes and security table.

---

## License

MIT — see [`LICENSE`](LICENSE).
