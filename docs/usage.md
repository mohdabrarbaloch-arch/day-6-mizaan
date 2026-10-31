# Usage

A walkthrough of the thing Mizaan is actually for, then the corners.

Every digest in this file was produced by the code in this repository. You can reproduce
each one; they are not illustrations.

---

## The five-minute version

You have one model, three regions, and a question you can't currently answer.

### 1. Register the artefact

**Registry → Register model.** The drawer opens with a realistic 7B chat manifest
already in it. Give it a name and hit **Register & hash**.

You get back:

```
3f09f24a1bdb963c6697dbd87505b4e7dbfea612ffba89405cdbcfcc001bdb06
```

That digest is the model's identity: SHA-256 over a canonical encoding of the manifest.
Register the *same* manifest under a different name and it produces the *same* digest —
because the artefact is the weights, not the label.

### 2. Deploy it twice, unchanged

**Deploy** to `us-east`. Then deploy again to `ap-south`, no overrides.

Both deployment rows show **`3f09f24a1bdb`**, identical to the model digest. Overview →
*Parity by model* now reads **identical**.

That is the entire product in one line: **two regions, one digest, no assumptions.**

### 3. Create drift on purpose

Deploy a third time to `eu-west`, this time with:

```json
{ "components.runtime.max_seq_len": 4096 }
```

That deployment's digest becomes **`a46f091179c7`** — different, as it must be.

Note what did *not* happen. Recompute the per-component digests and exactly one moves:

| Component | Before | After |
|---|---|---|
| `weights` | `d2316a35468c…` | `d2316a35468c…` |
| `tokenizer` | `0e7b862ad985…` | `0e7b862ad985…` |
| `template` | `a5484577e082…` | `a5484577e082…` |
| `runtime` | `d3f78d9536ab…` | *changed* |

Three of four components are byte-identical. Only one leaf of the runtime block moved —
exactly the kind of change that ships silently.

### 4. Watch the report catch it

**Overview → Matrix** on that model:

- The matrix cell for `eu-west` vs `us-east` turns red
- Severity: **`runtime`**
- The diff reads `components.runtime.max_seq_len: 8192 → 4096`
- Summary: *"Different runtime configuration (framework, opset or context length)."*

That's a sentence someone can act on at 2am, which "sounds different to me" is not.

---

## Override paths

Overrides address a leaf with a dotted path. Two shapes:

| Path | Effect |
|---|---|
| `params.temperature` | Replaces that key in `params` |
| `components.runtime.max_seq_len` | Replaces one leaf, siblings untouched |
| `components.runtime` | Replaces the whole runtime object |

Anything not starting with `components.` or `params.` is rejected with a 422 — including
`name`, so you can't quietly rename a deployment out of its identity.

**The distinction that matters:** `components.runtime.max_seq_len` says *this region
serves a shorter context*. `components.runtime` says *this region serves a different
runtime*, which is a much bigger claim and reports as such.

---

## Reading the severity classes

Ordered worst-first. A pair can carry several; the headline is the worst.

| Severity | What it means | What you actually do |
|---|---|---|
| `weights` | Different model binary | Roll back the region. This is the emergency. |
| `tokenizer` | Different vocabulary | Pin the tokenizer revision; output changes at token boundaries |
| `template` | Different prompt template | Diff the template — the model is being told something else |
| `runtime` | Framework, opset or context length | Check the serving image tag first |
| `parameters` | Same model, different sampling | Usually a leftover override nobody cleaned up |
| `version` | Different declared version | A rollout is halfway. Finish it or stop it. |

`weights` and `parameters` both mean "these differ", but one is a rollback and the other
is a config edit. Lumping them together is how alerting gets muted.

---

## Pipelines and gates

A pipeline is a DAG over `ingest → train → eval → register → deploy`.

**Validation reports every problem at once** — duplicates, unknown deps, self-deps,
cycles (with the actual node list), and kind-order violations. Fixing a graph should be
one pass, not twenty rounds of whack-a-mole.

```json
POST /api/pipelines/validate
{ "nodes": [
  { "id": "a", "kind": "train", "depends_on": ["b"] },
  { "id": "b", "kind": "eval",  "depends_on": ["a"] }
]}

→ 200
{ "valid": false,
  "errors": [{ "code": "cycle",
               "message": "Dependency cycle among: a, b — these nodes can never become ready.",
               "nodes": ["a", "b"] }],
  "warnings": [],
  "order": [] }
```

Note it returns **200**, not 422. Validation is a question; an invalid graph is a valid
answer to it. Only `POST /api/pipelines` refuses to store one.

The path above is `/api/pipelines/validate` — the validator is a sub-path of the
collection, not a separate `/api/validate` root.

### Gates

```json
"gates": { "accuracy": { "min": 0.92 }, "latency_p95_ms": { "max": 800 } }
```

Record a run:

```json
POST /api/pipelines/1/runs
{ "observed": { "accuracy": 0.947, "latency_p95_ms": 612 } }

→ { "gate_result": { "passed": true, "results": [
      { "metric": "accuracy", "observed": 0.947, "min": 0.92, "passed": true,
        "reason": "within bounds" }, …
    ] },
    "promotable": true,
    "promotion_reason": "gates passed and run succeeded" }
```

**Leave a gated metric out and its gate fails.** `{ "observed": {} }` against that same
pipeline gives `"no observed value for a gated metric"` and `promotable: false`. The
alternative — treating absence as success — is how an unmeasured model reaches
production, and it's the reason this behaviour is pinned by a test.

**Promotion** (`POST /api/pipelines/{id}/runs/{run_id}/promote`) is 409 unless the run
succeeded *and* every gate passed. On success it sets the run's `status` to `"promoted"` —
a real state change, which is why `RunStatus` includes `promoted`; without it, reading the
run history back after a promotion would fail validation.

It returns the parameters a deploy call would need, and deploys nothing. One click
silently touching every region is not a feature.

---

## Capacity and cost

`replicas × gpus_per_replica` is held against the region's quota **in the same
transaction** as the deployment row. Over-quota is a 409 with the arithmetic:

```
Region ap-south has 20 GPU(s) available (quota 24, used 4);
this deployment needs 22 (short by 2)
```

Scaling adjusts the delta and re-checks. Teardown releases what was held. Nothing
oversubscribes.

Cost is `Decimal`, 730 hours/month, quantised to 2dp and returned as a **string** — so
two calls never disagree in the last digit:

```
2 replicas × 1 GPU × $2.48 × 730 = "3620.80"
```

**A deployment whose region has no rate is reported as unpriced and excluded from the
total**, not costed at zero. A zero would quietly understate the bill and look like a
working number. In the UI it renders as `—`, not `$0.00`.

---

## Roles

| Role | Can |
|---|---|
| `viewer` | Read everything, validate graphs |
| `engineer` | + deploy, scale, create pipelines, promote runs |
| `admin` | + write the registry, tear down deployments |

Registry writes and teardowns are admin-only on purpose: those are the two operations that
change what exists rather than what is running, and they are the hardest to shrug off.

Cross-tenant access returns **404, never 403**. A 403 confirms the object exists, which
is exactly what an enumeration attempt wants to know.

---

## Things that will surprise you, on purpose

**The digest ignores `name` and `version`.** They're registry labels. If they were in the
hash, two names for one artefact would look like two different models and the digest
would stop being an identity.

**Unknown params still change the hash.** Add `min_p` to a manifest and the digest moves,
because it changes behaviour. The alternative lets a team add a sampling parameter and get
a digest claiming two different deployments are identical.

**`{"components": {"runtime": {}}}` and `{"components": {}}` hash differently.** "Present
but empty" and "absent" are different facts, and the digest keeps them apart.

**Float noise is normalised but real changes aren't.** `0.7` and `0.7000001` hash the
same; `0.7` and `0.71` don't. NaN and infinity raise rather than hashing something
arbitrary.

**Dotted override paths are limited to `components.` and `params.`** — but a path may go
deeper than two segments, and an intermediate object is created if it does not exist.
Setting `components.weights.sha256` on a manifest with no `weights` block creates one
rather than silently doing nothing.
