# API reference

Base path `/api`. All bodies are JSON. Auth is `Authorization: Bearer <jwt>` unless the
endpoint is marked public.

**Every response body below was captured from a running instance**, not written from
memory. The digests, costs and error strings are what the server actually returned.

**Error shape.** Every error is `{"detail": "..."}` — a string for domain errors, or a
list of `{loc, msg, type}` for FastAPI's own 422 validation failures.

| Status | When |
|---|---|
| `401` | Missing, malformed or expired token |
| `403` | Authenticated but the role is insufficient |
| `404` | Not found — **or another tenant's object** (deliberately indistinguishable) |
| `409` | Quota exceeded, duplicate deployment, model still has live deployments, gates failed |
| `422` | Schema validation, invalid DAG at creation, unusable override path, NaN in a manifest |
| `429` | Rate limited (see headers) |

A malformed override path is `422`, not `400`: it is a schema-level rejection, not a
conflict with server state. `ManifestError`, `DriftError` and `PipelineError` all subclass
`ValueError`, and the app registers a `ValueError` handler that turns them into `422` with
the real message — so a domain error never becomes an opaque 500.

---

## Health

### `GET /api/health` — public

```json
{ "status": "ok", "version": "1.0.0", "backend": "none", "database": "ok" }
```

`backend` is `"configured"` only when `LLM_BASE_URL` is set. This build ships no inference
server, so it reports `"none"` rather than implying one exists. `database` is `"ok"` if
`SELECT 1` succeeds, `"unavailable"` otherwise — and the overall `status` degrades with it.

---

## Auth

### `POST /api/auth/register` — public, rate limited (auth bucket)

```json
{ "email": "you@example.com", "password": "correct horse battery", "display_name": "ABraz", "role": "admin" }
```

| Field | Rule |
|---|---|
| `email` | Valid address (`EmailStr`), lowercased, unique — `409` if taken |
| `password` | 10–72 characters, at least 4 distinct characters |
| `display_name` | Up to 120 chars, optional (defaults to the email local-part) |
| `role` | `viewer` \| `engineer` \| `admin`, defaults to `engineer` |

The 72-character ceiling is not arbitrary: bcrypt silently truncates input at 72 bytes, so
a 100-character password and its first 72 characters would hash identically. `security.py`
rejects over-length input instead of truncating it.

The 4-distinct-character rule exists because `aaaaaaaaaa` passes a length check and is not
a password.

`201` with:

```json
{
  "access_token": "eyJhbGciOi…",
  "token_type": "bearer",
  "expires_in_minutes": 1440,
  "user": { "id": 1, "email": "you@example.com", "display_name": "ABraz", "role": "admin" }
}
```

> **On self-registration:** the API accepts `role` on register, and the bundled UI sends
> `admin` so the first person in can actually use the product. In a real deployment you
> close that (force `viewer`) and grant admin out of band. The role *guards* are correct
> and tested either way.

### `POST /api/auth/login` — public, rate limited (auth bucket)

```json
{ "email": "you@example.com", "password": "correct horse battery" }
```

`200` with a token. `401` on a bad email *or* a bad password, with the identical message
for both, and the same work done in both cases — a distinct "no such user" reply is an
account-enumeration oracle.

### `GET /api/auth/me` — viewer

```json
{ "id": 1, "email": "you@example.com", "display_name": "ABraz", "role": "admin" }
```

`UserOut` carries exactly those four fields; there is no `created_at` on it.

---

## Registry

### `POST /api/models` — **admin**, rate limited (write bucket)

```json
{
  "name": "tarteel-7b",
  "version": "1.0.0",
  "description": "Urdu-English chat, AWQ 4-bit",
  "manifest": {
    "components": {
      "weights":   { "sha256": "9f2c", "format": "safetensors", "quantization": "awq-int4" },
      "tokenizer": { "sha256": "3a1b", "vocab_size": 32000 },
      "template":  { "sha256": "c4d6", "name": "chatml" },
      "runtime":   { "framework": "vllm", "max_seq_len": 8192 }
    },
    "params": { "temperature": 0.7, "top_p": 0.95, "max_tokens": 2048 }
  }
}
```

`201` with (captured):

```json
{
  "id": 1,
  "name": "tarteel-7b",
  "version": "1.0.0",
  "description": "Urdu-English chat, AWQ 4-bit",
  "digest": "0dee95c0fd33c356e0ac1e20da2141f93830a6efa2a2aa6d8e052f6d8c06c9c9",
  "digest_short": "0dee95c0fd33",
  "component_digests": {
    "weights": "…", "tokenizer": "…", "template": "…", "runtime": "…"
  },
  "summary": {
    "weight_format": "safetensors",
    "quantization": "awq-int4",
    "vocab_size": 32000,
    "max_seq_len": 8192,
    "framework": "vllm",
    "temperature": 0.7,
    "component_digests": { "…": "…" }
  },
  "deployment_count": 0,
  "created_at": "2026-10-31T19:24:18.699012"
}
```

**How the digest is computed.** The four known components and the known params are
filtered and serialised with sorted keys, no insignificant whitespace, and floats rounded
to 6 places (`0.7` and `0.7000001` hash the same; `0.7` and `0.71` do not). Anything
outside the known keys lands in `extra_params` / `extra` and still moves the hash — an
unrecognised parameter changes behaviour, so it must change the identity.

`name` and `version` are excluded: they are registry labels, not behaviour. Register the
same manifest under a different name and you get the same digest, which is the whole point
of calling it an identity rather than a row id.

`component_digests` is what lets a drift report say *which piece* moved instead of only
"they differ". A component that is absent hashes differently from one that is present but
empty — `{"runtime": {}}` and no `runtime` at all are different facts.

`409` if the name/version pair is already registered for this user.

### `GET /api/models` — viewer

List, newest first, each with `digest`, `digest_short`, `summary`, `component_digests`
and `deployment_count` (a real `COUNT`, not a guess).

### `GET /api/models/{id}` — viewer

One model, same shape. `404` for another user's model rather than `403`, because a `403`
confirms the id exists.

### `DELETE /api/models/{id}` — **admin**

`204`. `409` if any *active* deployment still references it — deleting the artefact out
from under a running deployment would leave an unattributable thing in production:

```json
{ "detail": "2 active deployment(s) reference this model; tear them down first" }
```

---

## Deployments

### `POST /api/deployments` — engineer, rate limited (write bucket)

```json
{
  "model_version_id": 1,
  "region": "eu-west",
  "environment": "production",
  "replicas": 2,
  "gpus_per_replica": 1,
  "overrides": { "components.runtime.max_seq_len": 4096 }
}
```

| Field | Default | Notes |
|---|---|---|
| `environment` | `production` | `production` \| `staging` \| `canary` \| `shadow` |
| `replicas` | 1 | 1–64 |
| `gpus_per_replica` | 1 | 0–16 |
| `overrides` | `{}` | up to 32 dotted paths, each starting `components.` or `params.` |

`201` with (captured, no overrides):

```json
{
  "id": 1, "model_version_id": 1,
  "model_name": "tarteel-7b", "model_version": "1.0.0",
  "region": "us-east", "environment": "production",
  "replicas": 2, "gpus_per_replica": 1, "gpus_total": 2,
  "monthly_usd": "3620.80",
  "overrides": {},
  "digest": "0dee95c0fd33c356e0ac1e20da2141f93830a6efa2a2aa6d8e052f6d8c06c9c9",
  "digest_short": "0dee95c0fd33",
  "status": "active",
  "created_at": "2026-10-31T19:24:18.733225"
}
```

Note the digest is **identical to the model digest** when there are no overrides. That is
the parity proof, not a bug: the deployment is serving exactly the registered artefact.

`monthly_usd` is `null` when the deployment's region has no rate row, rather than `"0.00"`
— a zero would understate the bill and look like a working number.

Failures:

- `404` — model id unknown, or model belongs to another user
- `404` — region not seeded
- `409` — quota exceeded, always with the arithmetic (captured):

```json
{ "detail": "Region ap-south has 20 GPU(s) available (quota 24, used 4); this deployment needs 22 (short by 2)" }
```

- `409` — the same model already has a deployment with that environment in that region
  (there is a unique constraint on `model_version_id + region + environment`)
- `422` — override path outside `components.` / `params.`, or an override that cannot be
  applied to the manifest

Quota is held on the `regions` row **in the same transaction** as the deployment insert,
so two concurrent deploys cannot both pass the check and oversubscribe the region.

### `GET /api/deployments` — viewer

All of the caller's deployments, newest first, each with `monthly_usd`. There is no
`?model_version_id=` filter — filter client-side, or use `GET /api/drift/{id}`.

### `GET /api/deployments/{id}` — viewer

One deployment. `404` if it is not yours.

### `PATCH /api/deployments/{id}/scale` — engineer

```json
{ "replicas": 4 }
```

Adjusts the region's held quota by the delta and re-checks capacity for increases only.
`409` if the increase does not fit, and the deployment is **not** left half-scaled:

```json
{ "detail": "Scaling to 4 replica(s) needs 3 more GPU(s); 20 available in ap-south" }
```

### `DELETE /api/deployments/{id}` — **admin**

`204`. Releases the held GPU quota in the same transaction as the delete.

---

## Pipelines

### `POST /api/pipelines/validate` — viewer

```json
{ "nodes": [
  { "id": "ingest",   "kind": "ingest",   "depends_on": [] },
  { "id": "train",    "kind": "train",    "depends_on": ["ingest"] },
  { "id": "eval",     "kind": "eval",     "depends_on": ["train"] },
  { "id": "register", "kind": "register", "depends_on": ["eval"] },
  { "id": "deploy",   "kind": "deploy",   "depends_on": ["register"] }
]}
```

`200` **always** — validation is a question, and an invalid graph is a valid answer to it:

```json
{
  "valid": false,
  "errors": [
    { "code": "cycle",
      "message": "Dependency cycle among: a, b — these nodes can never become ready.",
      "nodes": ["a", "b"] }
  ],
  "warnings": [],
  "order": []
}
```

Error codes: `empty_pipeline`, `malformed_node`, `missing_id`, `duplicate_id`,
`unknown_kind`, `malformed_depends_on`, `malformed_dependency`, `self_dependency`,
`unknown_dependency`, `cycle` (carries `nodes`), `kind_order_violation`.

**All errors are collected, not just the first** — fixing a graph should be one pass, not
twenty rounds of whack-a-mole.

Two `kind_order_violation` rules are worth knowing because they are stricter than they
look:

1. A stage may not depend on a *later* stage. `train` depending on `eval` is nonsense even
   without a cycle.
2. A `deploy` node must have a `register` somewhere upstream. You cannot ship something
   the registry has never seen.

`order` is a deterministic topological sort, tie-broken on `(kind_rank, id)` — the same
graph always yields the same order, so "which node ran first" is reproducible.

A `deploy` with no `eval` anywhere in the pipeline produces a **warning**, not an error:
the graph is buildable, but nothing gates promotion.

### `POST /api/pipelines` — engineer, rate limited (write bucket)

Same body plus `name`, optional `description`, and `gates`. `201` with the stored pipeline
(`validated: true`, `order`, `warnings`) — or `422` carrying the same error list, so you
never have to validate first just to find out why.

### `GET /api/pipelines` — viewer
### `GET /api/pipelines/{id}` — viewer

### `POST /api/pipelines/{id}/runs` — engineer, rate limited (write bucket)

```json
{ "observed": { "accuracy": 0.947, "latency_p95_ms": 612 }, "status": "succeeded" }
```

```json
{
  "id": 1, "pipeline_id": 1,
  "status": "succeeded",
  "observed": { "accuracy": 0.947, "latency_p95_ms": 612 },
  "gate_result": {
    "passed": true,
    "results": [
      { "metric": "accuracy", "observed": 0.947, "min": 0.92, "max": null,
        "passed": true, "reason": "within bounds" },
      { "metric": "latency_p95_ms", "observed": 612, "min": null, "max": 800,
        "passed": true, "reason": "within bounds" }
    ],
    "gated_metric_count": 2,
    "failed_count": 0
  },
  "order": ["ingest", "train", "eval", "register", "deploy"],
  "promotable": true,
  "promotion_reason": "gates passed and run succeeded",
  "created_at": "…"
}
```

A gated metric with **no** observed value fails, with
`"no observed value for a gated metric"`. Absence is not a pass — treating silence as
success is precisely how an unmeasured model reaches production, and it is pinned by a
test.

A run cannot be *created* already `promoted`; `status` accepts only `pending`, `running`,
`succeeded`, `failed`. Promotion is a transition, not a birth state.

### `GET /api/pipelines/{id}/runs` — viewer

Run history, oldest first. `RunOut.status` is the wider `RunStatus` literal, which
includes `promoted` — so a promoted run still validates when read back.

### `POST /api/pipelines/{id}/runs/{run_id}/promote` — engineer

`200` with the parameters a deploy call would need, but only if the run **succeeded and
every gate passed**. Otherwise `409`, with `promotion_reason` explaining which condition
failed. A `running` run is never promotable regardless of its numbers: a partial
evaluation that happens to look good is not a pass.

It does not deploy anything. One click silently touching every region is not a feature.

---

## Drift

### `GET /api/drift` — viewer

Fleet verdict, one entry per model (captured):

```json
{
  "models": [
    {
      "model_version_id": 1,
      "name": "tarteel-7b",
      "version": "1.0.0",
      "deployment_count": 3,
      "regions": ["eu-west", "us-east"],
      "unknown_regions": [],
      "identical": false,
      "drifting_pairs": 2,
      "worst_severity": "runtime",
      "summary": "2 drifting pair(s); worst: runtime"
    }
  ],
  "summary": {
    "model_count": 1,
    "drifting_models": 1,
    "identical_models": 0,
    "worst_severity": "runtime",
    "all_identical": false
  }
}
```

Note the summary keys are `model_count` / `identical_models`, not `models` / `pair_count`.
There is no fleet-level `pair_count` — pair counts are per model, because pairs across
different models are meaningless.

`unknown_regions` lists regions a deployment names that have no `regions` row, which is
usually a seed that was removed or a typo'd region.

### `GET /api/drift/{model_id}` — viewer

The full pairwise matrix over **effective** manifests (captured cell, trimmed to the
differing one):

```json
{
  "model_version_id": 1,
  "model_name": "tarteel-7b",
  "model_version": "1.0.0",
  "deployment_count": 3,
  "cells": [
    {
      "a": 2, "b": 1,
      "a_region": "eu-west", "b_region": "us-east",
      "a_environment": "production", "b_environment": "production",
      "identical": false,
      "classes": ["runtime"],
      "severity": "runtime",
      "diffs": [
        { "field": "components.runtime.max_seq_len", "a": 4096, "b": 8192 }
      ],
      "summary": "Different runtime configuration (framework, opset or context length)."
    }
  ],
  "summary": { "deployment_count": 3, "pair_count": 3, "drifting_pairs": 2,
               "all_identical": false, "worst_severity": "runtime" }
}
```

Severity is ordered worst-first: `weights` › `tokenizer` › `template` › `runtime` ›
`parameters` › `version`. `severity` is the worst class present in that pair, or `null`
when identical. `classes` carries every class present, so a pair that changed weights
*and* temperature reports both but headlines `weights`.

`diffs` are leaf-level, with both values, so the report says
`components.runtime.max_seq_len: 4096 → 8192` and not "these differ".

---

## Capacity

### `GET /api/capacity` — viewer

Captured, with two deployments live:

```json
{
  "summary": {
    "region_count": 3,
    "total_quota": 120,
    "total_used": 27,
    "total_available": 93,
    "utilisation": 0.225,
    "regions": [
      { "name": "ap-south", "gpu_quota": 24, "gpu_used": 4, "available": 20,
        "utilisation": 0.16666666666666666, "usd_per_gpu_hour": "2.19" },
      { "name": "eu-west", "gpu_quota": 32, "gpu_used": 9, "available": 23,
        "utilisation": 0.28125, "usd_per_gpu_hour": "2.61" },
      { "name": "us-east", "gpu_quota": 64, "gpu_used": 14, "available": 50,
        "utilisation": 0.21875, "usd_per_gpu_hour": "2.48" }
    ]
  },
  "fleet": {
    "total_monthly_usd": "5526.10",
    "by_region": { "eu-west": "1905.30", "us-east": "3620.80" },
    "unpriced_deployments": [],
    "deployment_count": 2,
    "hours_per_month": "730"
  }
}
```

Two things to notice, because the old version of this page got both wrong:

- regions are nested **inside** `summary`; there is no top-level `regions` key.
- the field is `utilisation` (a 0–1 fraction) and `total_available`, not
  `utilization_pct` / `total_free`.

`usd_per_gpu_hour` is a **string**, and cost is `Decimal` arithmetic quantised to 2dp and
returned as a string, so two identical calls can never disagree in the last digit.
`hours_per_month` is fixed at 730 (365 × 24 / 12) so projections are comparable.

A deployment in an unpriced region is listed in `unpriced_deployments` and **excluded**
from `total_monthly_usd`, not costed at zero.

Verify the arithmetic: `2 × 1 × 2.48 × 730 = 3620.80`.

### `GET /api/capacity/regions` — viewer

Bare region rows (captured):

```json
{ "id": 3, "name": "ap-south", "display_name": "Ap South",
  "gpu_quota": 24, "gpu_used": 4, "usd_per_gpu_hour": "2.19" }
```

There is no computed `gpu_free` or `utilization_pct` here — this is the raw table. Use
`/api/capacity` for the derived numbers.

---

## Rate limits

| Bucket | Default | Applied to |
|---|---|---|
| Auth | 10/min | `POST /api/auth/register`, `POST /api/auth/login` |
| Write | 60/min | `POST /api/models`, `POST /api/deployments`, `POST /api/pipelines`, `POST /api/pipelines/{id}/runs` |

Exceeded → `429` with `Retry-After: 60`. Sliding window, keyed on `X-Forwarded-For`'s
first hop when present, else the socket address.

**It is per-process, in memory.** With N uvicorn workers the effective limit is N × the
configured value, and a restart clears it. That is a real limitation, stated here rather
than hidden; `ARCHITECTURE.md` records the shared-store fix (Redis `INCR` + `EXPIRE`, or a
gateway limiter) and why an in-process dict was chosen for a single-worker build.

---

## Security headers

Every response carries:

- `X-Request-ID` — echoed from the request if supplied, else generated
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: no-referrer`
- `Strict-Transport-Security: max-age=31536000; includeSubDomains` — **only** when
  `PRODUCTION=1`

With `PRODUCTION=1`, `ALLOWED_ORIGINS=*` is also replaced with an empty list rather than
honoured. `allow_credentials=True` plus a wildcard origin is a browser-side backdoor, so
production refuses the combination instead of quietly allowing it.

There is **no** `Content-Security-Policy` header in this build — the SPA uses inline event
attributes in a few places and a CSP strong enough to matter would need those refactored
first. Recorded in `ARCHITECTURE.md` as known work rather than claimed as done.
