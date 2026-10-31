"""End-to-end API behaviour, including the security invariants."""

from __future__ import annotations

from tests.conftest import MANIFEST, make_model, register


class TestAuth:
    def test_register_returns_token_and_user(self, client):
        data = register(client)
        assert data["access_token"]
        assert data["user"]["email"] == "eng@example.com"
        assert data["user"]["role"] == "admin"

    def test_duplicate_email_is_409(self, client):
        register(client)
        res = client.post(
            "/api/auth/register",
            json={"email": "eng@example.com", "password": "another good password"},
        )
        assert res.status_code == 409

    def test_short_password_is_422(self, client):
        res = client.post(
            "/api/auth/register",
            json={"email": "x@example.com", "password": "short"},
        )
        assert res.status_code == 422

    def test_login_succeeds(self, client):
        register(client)
        res = client.post(
            "/api/auth/login",
            json={"email": "eng@example.com", "password": "correct horse battery"},
        )
        assert res.status_code == 200
        assert res.json()["access_token"]

    def test_wrong_password_is_401(self, client):
        register(client)
        res = client.post(
            "/api/auth/login",
            json={"email": "eng@example.com", "password": "wrong password entirely"},
        )
        assert res.status_code == 401

    def test_unknown_user_gives_same_error_as_wrong_password(self, client):
        """The response must not reveal whether the account exists."""
        register(client)
        wrong_pw = client.post(
            "/api/auth/login",
            json={"email": "eng@example.com", "password": "nope nope nope"},
        )
        no_user = client.post(
            "/api/auth/login",
            json={"email": "ghost@example.com", "password": "nope nope nope"},
        )
        assert wrong_pw.status_code == no_user.status_code == 401
        assert wrong_pw.json()["detail"] == no_user.json()["detail"]

    def test_me_requires_token(self, client):
        assert client.get("/api/auth/me").status_code == 401

    def test_me_returns_current_user(self, client, auth):
        res = client.get("/api/auth/me", headers=auth)
        assert res.status_code == 200
        assert res.json()["email"] == "eng@example.com"

    def test_garbage_token_is_401(self, client):
        res = client.get("/api/auth/me", headers={"Authorization": "Bearer not.a.jwt"})
        assert res.status_code == 401


class TestRegistry:
    def test_create_model_returns_digest(self, client, auth):
        model = make_model(client, auth)
        assert len(model["digest"]) == 64
        assert model["digest_short"] == model["digest"][:12]

    def test_same_manifest_same_digest_across_models(self, client, auth):
        """Two names, one manifest: the artefact identity is the manifest."""
        a = make_model(client, auth, name="alpha")
        b = make_model(client, auth, name="beta")
        assert a["digest"] == b["digest"]

    def test_different_manifest_different_digest(self, client, auth):
        a = make_model(client, auth, name="alpha")
        changed = {**MANIFEST, "params": {**MANIFEST["params"], "temperature": 0.2}}
        res = client.post(
            "/api/models",
            headers=auth,
            json={"name": "beta", "version": "1.0.0", "manifest": changed},
        )
        assert res.status_code == 201
        assert res.json()["digest"] != a["digest"]

    def test_duplicate_name_version_is_409(self, client, auth):
        make_model(client, auth)
        res = client.post(
            "/api/models",
            headers=auth,
            json={"name": "tarteel", "version": "1.0.0", "manifest": MANIFEST},
        )
        assert res.status_code == 409

    def test_out_of_range_temperature_is_422(self, client, auth):
        bad = {**MANIFEST, "params": {**MANIFEST["params"], "temperature": 9.0}}
        res = client.post(
            "/api/models",
            headers=auth,
            json={"name": "bad", "version": "1.0.0", "manifest": bad},
        )
        assert res.status_code == 422

    def test_engineer_cannot_write_registry(self, client):
        """Registry writes are admin-only; deployment is not."""
        data = register(client, email="eng2@example.com", role="engineer")
        headers = {"Authorization": f"Bearer {data['access_token']}"}
        res = client.post(
            "/api/models",
            headers=headers,
            json={"name": "x", "version": "1.0.0", "manifest": MANIFEST},
        )
        assert res.status_code == 403

    def test_other_users_model_is_404_not_403(self, client, auth):
        """403 would confirm the id exists; 404 does not."""
        model = make_model(client, auth)
        other = register(client, email="other@example.com", role="admin")
        other_headers = {"Authorization": f"Bearer {other['access_token']}"}
        res = client.get(f"/api/models/{model['id']}", headers=other_headers)
        assert res.status_code == 404

    def test_delete_model_with_deployments_is_409(self, client, auth):
        model = make_model(client, auth)
        dep = client.post(
            "/api/deployments",
            headers=auth,
            json={"model_version_id": model["id"], "region": "us-east"},
        )
        assert dep.status_code == 201
        res = client.delete(f"/api/models/{model['id']}", headers=auth)
        assert res.status_code == 409


class TestDeployments:
    def test_deploy_returns_digest(self, client, auth):
        model = make_model(client, auth)
        res = client.post(
            "/api/deployments",
            headers=auth,
            json={"model_version_id": model["id"], "region": "us-east"},
        )
        assert res.status_code == 201
        body = res.json()
        assert body["region"] == "us-east"
        # No overrides: the deployment digest equals the model digest.
        assert body["digest"] == model["digest"]

    def test_two_regions_same_digest_is_parity(self, client, auth):
        """The product's central claim, checked end to end through the API."""
        model = make_model(client, auth)
        a = client.post(
            "/api/deployments",
            headers=auth,
            json={"model_version_id": model["id"], "region": "us-east"},
        ).json()
        b = client.post(
            "/api/deployments",
            headers=auth,
            json={"model_version_id": model["id"], "region": "ap-south"},
        ).json()
        assert a["digest"] == b["digest"]

        report = client.get(f"/api/drift/{model['id']}", headers=auth).json()
        assert report["summary"]["all_identical"] is True

    def test_override_creates_drift(self, client, auth):
        model = make_model(client, auth)
        client.post(
            "/api/deployments",
            headers=auth,
            json={"model_version_id": model["id"], "region": "us-east"},
        )
        drifted = client.post(
            "/api/deployments",
            headers=auth,
            json={
                "model_version_id": model["id"],
                "region": "ap-south",
                "overrides": {"components.runtime.max_seq_len": 4096},
            },
        )
        assert drifted.status_code == 201
        assert drifted.json()["digest"] != model["digest"]

        report = client.get(f"/api/drift/{model['id']}", headers=auth).json()
        assert report["summary"]["all_identical"] is False
        assert report["summary"]["worst_severity"] == "runtime"

    def test_quota_exceeded_is_409(self, client, auth):
        """ap-south has 24 quota and 4 held, so 21 GPUs cannot fit."""
        model = make_model(client, auth)
        res = client.post(
            "/api/deployments",
            headers=auth,
            json={
                "model_version_id": model["id"],
                "region": "ap-south",
                "replicas": 21,
                "gpus_per_replica": 1,
            },
        )
        assert res.status_code == 409
        assert "available" in res.json()["detail"]

    def test_unknown_region_is_404(self, client, auth):
        model = make_model(client, auth)
        res = client.post(
            "/api/deployments",
            headers=auth,
            json={"model_version_id": model["id"], "region": "moon-base"},
        )
        assert res.status_code == 404

    def test_invalid_override_path_is_422(self, client, auth):
        model = make_model(client, auth)
        res = client.post(
            "/api/deployments",
            headers=auth,
            json={
                "model_version_id": model["id"],
                "region": "us-east",
                "overrides": {"name": "hijack"},
            },
        )
        assert res.status_code == 422

    def test_scale_up_updates_quota(self, client, auth):
        model = make_model(client, auth)
        dep = client.post(
            "/api/deployments",
            headers=auth,
            json={"model_version_id": model["id"], "region": "us-east"},
        ).json()
        before = client.get("/api/capacity/regions", headers=auth).json()
        before_used = next(r["gpu_used"] for r in before if r["name"] == "us-east")

        res = client.patch(
            f"/api/deployments/{dep['id']}/scale",
            headers=auth,
            json={"replicas": 4},
        )
        assert res.status_code == 200
        assert res.json()["gpus_total"] == 4

        after = client.get("/api/capacity/regions", headers=auth).json()
        after_used = next(r["gpu_used"] for r in after if r["name"] == "us-east")
        assert after_used == before_used + 3

    def test_teardown_releases_quota(self, client, auth):
        model = make_model(client, auth)
        dep = client.post(
            "/api/deployments",
            headers=auth,
            json={"model_version_id": model["id"], "region": "us-east", "replicas": 2},
        ).json()

        res = client.delete(f"/api/deployments/{dep['id']}", headers=auth)
        assert res.status_code == 204

        regions = client.get("/api/capacity/regions", headers=auth).json()
        used = next(r["gpu_used"] for r in regions if r["name"] == "us-east")
        assert used == 12  # back to the seeded value


class TestDriftEndpoint:
    def test_fleet_drift_empty_is_clean(self, client, auth):
        res = client.get("/api/drift", headers=auth)
        assert res.status_code == 200
        assert res.json()["summary"]["all_identical"] is True

    def test_fleet_drift_detects_drifting_model(self, client, auth):
        model = make_model(client, auth)
        client.post(
            "/api/deployments",
            headers=auth,
            json={"model_version_id": model["id"], "region": "us-east"},
        )
        client.post(
            "/api/deployments",
            headers=auth,
            json={
                "model_version_id": model["id"],
                "region": "eu-west",
                "overrides": {"params.temperature": 0.2},
            },
        )
        body = client.get("/api/drift", headers=auth).json()
        assert body["summary"]["drifting_models"] == 1
        assert body["summary"]["worst_severity"] == "parameters"

    def test_single_deployment_reports_no_pairs(self, client, auth):
        model = make_model(client, auth)
        client.post(
            "/api/deployments",
            headers=auth,
            json={"model_version_id": model["id"], "region": "us-east"},
        )
        report = client.get(f"/api/drift/{model['id']}", headers=auth).json()
        assert report["cells"] == []


class TestPipelines:
    NODES = [
        {"id": "ingest", "kind": "ingest", "depends_on": []},
        {"id": "train", "kind": "train", "depends_on": ["ingest"]},
        {"id": "eval", "kind": "eval", "depends_on": ["train"]},
    ]

    def test_validate_returns_order(self, client, auth):
        res = client.post("/api/pipelines/validate", headers=auth, json={"nodes": self.NODES})
        assert res.status_code == 200
        assert res.json()["valid"] is True
        assert res.json()["order"] == ["ingest", "train", "eval"]

    def test_cycle_is_rejected_at_creation(self, client, auth):
        nodes = [
            {"id": "a", "kind": "train", "depends_on": ["b"]},
            {"id": "b", "kind": "eval", "depends_on": ["a"]},
        ]
        res = client.post(
            "/api/pipelines",
            headers=auth,
            json={"name": "bad", "nodes": nodes},
        )
        assert res.status_code == 422

    def test_create_pipeline_with_gates(self, client, auth):
        res = client.post(
            "/api/pipelines",
            headers=auth,
            json={
                "name": "nightly",
                "nodes": self.NODES,
                "gates": {"accuracy": {"min": 0.9}},
            },
        )
        assert res.status_code == 201
        assert res.json()["validated"] is True

    def test_run_with_passing_gates_is_promotable(self, client, auth):
        p = client.post(
            "/api/pipelines",
            headers=auth,
            json={"name": "nightly", "nodes": self.NODES, "gates": {"accuracy": {"min": 0.9}}},
        ).json()
        run = client.post(
            f"/api/pipelines/{p['id']}/runs",
            headers=auth,
            json={"observed": {"accuracy": 0.95}},
        ).json()
        assert run["gate_result"]["passed"] is True
        assert run["promotable"] is True

    def test_run_with_failing_gate_is_not_promotable(self, client, auth):
        p = client.post(
            "/api/pipelines",
            headers=auth,
            json={"name": "nightly", "nodes": self.NODES, "gates": {"accuracy": {"min": 0.9}}},
        ).json()
        run = client.post(
            f"/api/pipelines/{p['id']}/runs",
            headers=auth,
            json={"observed": {"accuracy": 0.5}},
        ).json()
        assert run["promotable"] is False

    def test_promote_blocked_when_gates_fail(self, client, auth):
        p = client.post(
            "/api/pipelines",
            headers=auth,
            json={"name": "nightly", "nodes": self.NODES, "gates": {"accuracy": {"min": 0.9}}},
        ).json()
        run = client.post(
            f"/api/pipelines/{p['id']}/runs",
            headers=auth,
            json={"observed": {"accuracy": 0.5}},
        ).json()
        res = client.post(
            f"/api/pipelines/{p['id']}/runs/{run['id']}/promote",
            headers=auth,
            json={},
        )
        assert res.status_code == 409

    def test_promote_succeeds_when_gates_pass(self, client, auth):
        p = client.post(
            "/api/pipelines",
            headers=auth,
            json={"name": "nightly", "nodes": self.NODES, "gates": {"accuracy": {"min": 0.9}}},
        ).json()
        run = client.post(
            f"/api/pipelines/{p['id']}/runs",
            headers=auth,
            json={"observed": {"accuracy": 0.99}},
        ).json()
        res = client.post(
            f"/api/pipelines/{p['id']}/runs/{run['id']}/promote",
            headers=auth,
            json={},
        )
        assert res.status_code == 200
        assert res.json()["promoted"] is True

    def test_run_history_is_readable_after_promotion(self, client, auth):
        """A promoted run must not poison the list endpoint.

        Regression: `promoted` was missing from the RunStatus literal, so every
        later GET /runs returned 422 and the history was unreadable.
        """
        p = client.post(
            "/api/pipelines",
            headers=auth,
            json={"name": "nightly", "nodes": self.NODES, "gates": {"accuracy": {"min": 0.9}}},
        ).json()
        run = client.post(
            f"/api/pipelines/{p['id']}/runs",
            headers=auth,
            json={"observed": {"accuracy": 0.99}},
        ).json()
        client.post(f"/api/pipelines/{p['id']}/runs/{run['id']}/promote", headers=auth, json={})

        listed = client.get(f"/api/pipelines/{p['id']}/runs", headers=auth)
        assert listed.status_code == 200
        assert listed.json()[0]["status"] == "promoted"

    def test_promoting_twice_reports_already_promoted(self, client, auth):
        p = client.post(
            "/api/pipelines",
            headers=auth,
            json={"name": "nightly", "nodes": self.NODES, "gates": {"accuracy": {"min": 0.9}}},
        ).json()
        run = client.post(
            f"/api/pipelines/{p['id']}/runs",
            headers=auth,
            json={"observed": {"accuracy": 0.99}},
        ).json()
        url = f"/api/pipelines/{p['id']}/runs/{run['id']}/promote"
        assert client.post(url, headers=auth, json={}).status_code == 200

        again = client.post(url, headers=auth, json={})
        assert again.status_code == 409
        assert "already promoted" in again.json()["detail"]

    def test_cannot_create_run_already_promoted(self, client, auth):
        """The promoted transition belongs to the endpoint, not the payload."""
        p = client.post(
            "/api/pipelines",
            headers=auth,
            json={"name": "nightly", "nodes": self.NODES},
        ).json()
        res = client.post(
            f"/api/pipelines/{p['id']}/runs",
            headers=auth,
            json={"observed": {}, "status": "promoted"},
        )
        assert res.status_code == 422


class TestCapacity:
    def test_regions_are_seeded(self, client, auth):
        regions = client.get("/api/capacity/regions", headers=auth).json()
        assert {r["name"] for r in regions} == {"us-east", "eu-west", "ap-south"}

    def test_capacity_totals(self, client, auth):
        body = client.get("/api/capacity", headers=auth).json()
        assert body["summary"]["total_quota"] == 120
        assert body["fleet"]["hours_per_month"] == "730"

    def test_fleet_cost_includes_deployment(self, client, auth):
        model = make_model(client, auth)
        client.post(
            "/api/deployments",
            headers=auth,
            json={
                "model_version_id": model["id"],
                "region": "us-east",
                "replicas": 2,
                "gpus_per_replica": 1,
            },
        )
        body = client.get("/api/capacity", headers=auth).json()
        # 2 × 1 × 2.48 × 730 = 3620.80
        assert body["fleet"]["total_monthly_usd"] == "3620.80"


class TestHealthAndHeaders:
    def test_health_reports_no_backend(self, client):
        body = client.get("/api/health").json()
        assert body["status"] == "ok"
        assert body["backend"] == "none"

    def test_security_headers_present(self, client):
        res = client.get("/api/health")
        assert res.headers["X-Content-Type-Options"] == "nosniff"
        assert res.headers["X-Frame-Options"] == "DENY"
        assert "X-Request-ID" in res.headers

    def test_spa_is_served(self, client):
        res = client.get("/")
        assert res.status_code == 200
        assert "Mizaan" in res.text

    def test_static_css_served(self, client):
        res = client.get("/static/style.css")
        assert res.status_code == 200
        assert len(res.content) > 1000
