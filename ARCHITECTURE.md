# Mizaan — Architecture

> میزان — *the balance*.

Mizaan is an enterprise AI control plane. It holds a model registry, deploys models into regions, builds ML pipelines, and — the part everything else gets wrong — **proves whether two deployments actually behave the same.**

---

## The problem this is built around

Every enterprise AI platform can push a model to three regions. Almost none of them can answer a simple question a week later:

> *"Are East US and Southeast Asia right now serving the same model?"*

The honest answer in most stacks is "probably". Something drifted — a quantisation flag, a prompt template, a tokenizer revision, a `max_seq_len` — and there is no artefact that says so. You find out when a customer in Singapore gets a different answer than a customer in Ohio, and by then you're debugging a behaviour difference with no baseline.

Mizaan's answer is one primitive: **the deployment digest.**

---

## The core invariant

A model is a **manifest** — a structured description of everything that changes behaviour:

```
manifest
├── components
│   ├── weights    → sha256, format (safetensors/gguf/onnx), quantization
│   ├── tokenizer  → sha256, vocab_size
│   ├── template   → sha256 of the prompt template text
│   └── runtime    → framework, opset, max_seq_len
└── params         → temperature, top_p, max_tokens
```

The **digest** is `SHA-256` over a *canonical* serialisation of that manifest — keys sorted, no insignificant whitespace, numbers rendered at fixed precision (6 decimal places). So:

> **Same manifest → same digest. Always. In any process, on any machine, in any key order.**

A deployment is `(model_version, region, environment, overrides)`. Its effective manifest is the model manifest with `overrides` merged on top, and:

> **The deployment digest covers effective behaviour — not location.**

This is the design decision the whole product rests on. **Two deployments in different regions with no overrides have the *same* digest.** That is not a bug; that is the proof of parity. The moment a region override changes a parameter, the digests diverge and the drift report names the field.

`region` is deliberately excluded from the digest hash. If it were included, every deployment would differ from every other by construction, the digest would carry no information, and you'd be back to guessing.

Two consequences worth stating, because they are easy to get wrong:

- **`name` and `version` are excluded too.** They are registry labels, not behaviour. Two names for the same weights share a digest — which is what makes it an identity rather than a row id with extra steps.
- **Unknown keys still move the hash.** They land in `extra_params` / `extra` rather than being dropped. A team that adds `min_p` must not get a digest claiming two different deployments are identical.

---

## System diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│  Browser — control plane SPA (vanilla JS, mobile-first, no build)   │
│  Registry · Deployments · Pipelines · Drift matrix · Capacity       │
└───────────────────────────────┬─────────────────────────────────────┘
                                │  JSON over HTTPS, Bearer JWT
┌───────────────────────────────▼─────────────────────────────────────┐
│  FastAPI application                                                │
│                                                                     │
│  middleware   request-id · security headers · CORS                  │
│  dependencies rate limiting · current user · role guard             │
│                                                                     │
│  routers                                                            │
│    /api/auth         register · login · me                          │
│    /api/models       registry CRUD, digest computation              │
│    /api/deployments  deploy · list · get · scale · teardown         │
│    /api/pipelines    validate · CRUD · runs · gate eval · promote   │
│    /api/drift        fleet verdict · per-model parity matrix        │
│    /api/capacity     quota per region, cost projection              │
│    /api/health       liveness, backend, version                     │
│                                                                     │
│  engine/  ── pure, deterministic, no DB, no I/O ──                  │
│    digest.py     canonical encode → per-component + whole digest    │
│    drift.py      compare two manifests → classes + field diffs      │
│    pipeline.py   DAG validation (cycles, ordering) + gate evaluation│
│    capacity.py   quota arithmetic + cost projection                 │
│                                                                     │
│  models.py    SQLAlchemy 2.1 ORM — User, ModelVersion, Deployment,  │
│               Region, Pipeline, PipelineRun                         │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                  ┌─────────────▼─────────────┐
                  │  SQLite (dev)             │
                  │  PostgreSQL 16 (compose)  │
                  └───────────────────────────┘
```

---

## Why `engine/` is a separate package

Everything under `app/engine/` is **pure functions over plain data**. No ORM import, no HTTP, no environment lookup, no clock. That buys three things:

1. **Testability without a database.** The interesting logic — canonicalisation, drift classification, cycle detection, gate arithmetic — is tested directly, not through an API client.
2. **Determinism that can be asserted.** A digest that depends on dict ordering or float formatting is useless. The tests pin exact hex values so a refactor that quietly changes the encoding fails immediately.
3. **Reuse.** The same `drift.compare()` serves the API, the pipeline gates, and any future CLI.

---

## Data flow — deploying a model and proving parity

```
1. POST /api/models
     manifest submitted → digest.py.canonicalise() → per-component SHA-256
     → whole-manifest SHA-256 stored on the row
     → response includes digest + component breakdown

2. POST /api/deployments   {model_version_id, region, environment, overrides}
     ├─ authz: engineer or admin
     ├─ capacity.fits()  →  409 if quota exceeded
     ├─ effective = manifest ⊕ overrides
     ├─ digest    = SHA-256(canonical(effective))
     └─ row written with effective_manifest + digest
        (region.gpu_used incremented in the SAME transaction)

3. GET /api/drift/{model_id}
     ├─ load that model's active deployments
     ├─ for each unordered pair: drift.compare(effectiveA, effectiveB)
     │     → identical? classes? field-level diffs? severity?
     └─ return matrix + worst-severity summary
```

The drift step never re-reads the model manifest to "recompute" anything — it compares the **stored effective manifests**, because those are what the deployments are actually running. Recomputing from the source manifest would hide exactly the overrides you're trying to catch.

---

## Drift classification

`drift.compare(a, b)` returns an ordered set of classes, most severe first:

| Class | Meaning | Typical cause |
|---|---|---|
| `weights` | Different model binary | Wrong version promoted |
| `tokenizer` | Different vocabulary | Tokenizer revision not pinned |
| `template` | Different prompt template | Template edited in one region only |
| `runtime` | Different framework/opset/context length | Inconsistent serving image |
| `parameters` | Same model, different sampling params | Region override left behind |
| `version` | Different model version entirely | Rollout halfway |

A pair can carry several classes at once; the report keeps them all in `classes` and surfaces the **highest severity** as `severity`, because "weights differ" and "temperature differs" are not the same emergency.

Each class also carries field-level diffs — `components.tokenizer.vocab_size: 30000 → 32000` — so the report tells you *what* to fix, not just *that* something is wrong.

---

## Pipeline DAG and promotion gates

Pipelines are node graphs. Node kinds and their required ordering:

```
ingest ──▶ train ──▶ eval ──▶ register ──▶ deploy
```

Validation is strict and returns **all** errors at once rather than the first:

- duplicate node ids
- unknown dependency
- self-dependency
- cycles — reported with the actual node list, e.g.
  `Dependency cycle among: a, b — these nodes can never become ready.`
- kind-order violations: a node may not depend on a **later** stage, and a `deploy` node must have a `register` node somewhere upstream (you cannot ship what the registry has never seen)

A `deploy` node with no `eval` anywhere in the pipeline is a **warning**, not an error — the graph is buildable, but nothing gates promotion.

Topological order is deterministic: ready nodes are sorted by `(kind_rank, id)` so the same graph always produces the same execution order. A pipeline that shuffles its own run order is not reproducible.

**Gates are a property of the pipeline, not of a node.** They live in the top-level `gates` object on `PipelineCreate`:

```json
{"accuracy": {"min": 0.92}, "latency_p95_ms": {"max": 800}}
```

They are evaluated when a run is *recorded*, by `pipeline.evaluate_gates()`, which returns a per-gate verdict with the observed value and the bound it crossed. A gated metric with no observed value **fails** — absence is not a pass. A run cannot be promoted unless it succeeded *and* every gate passed; the API enforces it and a test pins it.

---

## Capacity and cost

Each region carries `gpu_quota` and `gpu_used`. A deployment requests `replicas × gpus_per_replica`.

- `capacity.fits()` returns a **dict** — `{ok, quota, used, available, requested, shortfall}` — not a tuple, so the 409 body can carry the arithmetic rather than a bare "quota exceeded".
- `capacity.monthly_cost()` → deterministic `replicas × gpus × hourly_rate × 730`, returned as a string-quantised decimal so two calls never disagree in the last digit.
- A deployment whose region has no rate row is **unpriced**: excluded from the total and listed in `unpriced_deployments`, rather than costed at zero. A zero would understate the bill and look like a working number.

Region rates live in the `Region` table, seeded at startup from `SEED_REGIONS`, so pricing is data rather than code.

---

## Security

| Concern | Approach |
|---|---|
| Password storage | bcrypt, cost 12, explicit 72-byte guard **before** hashing — bcrypt truncates silently, so over-length input is rejected rather than truncated |
| Sessions | JWT HS256, 24h expiry, `sub` = user id, `role` claim |
| Authz | 3 roles — `viewer` (read), `engineer` (deploy, run, promote), `admin` (registry write, teardown) |
| Ownership | Cross-tenant access returns **404**, never 403 — a 403 confirms the object exists |
| Input | Pydantic v2 validation on every body; length and range bounds on every field |
| Rate limiting | Sliding window: auth 10/min, writes 60/min — **per process**, see limits below |
| Headers | `X-Request-ID`, `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, HSTS when `PRODUCTION=1` |
| CORS | `ALLOWED_ORIGINS=*` is **refused** when `PRODUCTION=1` (wildcard + `allow_credentials` is a browser-side backdoor) |
| Secrets | env only; `SECRET_KEY` generated with a warning if absent, never a hardcoded constant |
| Logging | request id per request; no tokens or passwords logged |

---

## Scaling notes

**Digest computation** is O(manifest size) and pure — it scales horizontally with the API tier and needs no coordination. In a real deployment it belongs behind a cache keyed on the raw manifest hash, because the same manifest is hashed on every deploy.

**Drift is O(n²) per model** across deployments. With tens of regions that is trivial. Past a few hundred deployments per model, switch to comparing each deployment against a designated *canonical* reference rather than all pairs — that is O(n) and answers the question enterprises actually ask ("does everything match prod?").

**The heavy work belongs in a queue.** `PipelineRun` execution is modelled synchronously here because the interesting part is validation and gating, not scheduling. In production, recording a run becomes a job record and the API returns `202` with the run's `id` — which is why the row already carries `status` and is addressable by id.

**Registry storage** in this build keeps manifests in the database. Real weights belong in object storage with the digest as the key: `s3://models/{component}/{sha256}`. Content-addressed storage means an identical binary is stored once no matter how many models reference it, and the digest you already compute becomes the address.

**Multi-tenancy** is on `user_id` today. The natural progression is an `org_id` on every row plus an org-scoped composite index, at which point the 404-not-403 rule already does the right thing.

---

## Known limits, stated rather than hidden

**Rate limiting is per-process, in memory.** With N uvicorn workers the effective limit is N × the configured value, and a restart clears the window. That is why the Dockerfile pins `--workers 1`. The fix is a shared store — Redis `INCR` + `EXPIRE` on a key of `bucket:ip`, or a limiter at the gateway — which is a dependency this build does not need yet.

**No Content-Security-Policy header.** The SPA uses a handful of inline event attributes; a CSP strong enough to matter needs those refactored into listeners first. Shipping `default-src 'unsafe-inline'` to claim a CSP would be worse than shipping none, because it would look like protection.

**Self-registration accepts a `role`, and the UI sends `admin`,** so the first account can populate the registry. Deliberate for a demo. For anything real, force `viewer` at registration and grant admin out of band.

**The Postgres driver is pinned explicitly** (`psycopg[binary]`) because the compose path is unusable without it — an earlier revision of `requirements.txt` omitted it, and `create_engine` fails with `ModuleNotFoundError: No module named 'psycopg'` before the app ever serves a request.

---

## What this deliberately does not do

- **No real model serving.** Deployments are metadata describing intent. There is no inference server, and the API says so (`/api/health` reports `backend: "none"`).
- **No real metrics.** Gate evaluation is arithmetic on numbers you submit. It does not train or measure anything.
- **No fabricated parity.** If a deployment has no overrides and the same manifest, the digests match — and that is a computed fact, not an assumption. If they differ, the report says which field moved.
- **Promotion does not deploy.** It marks the run promoted and returns the parameters a deploy call would need. One click silently touching every region is not a feature.
