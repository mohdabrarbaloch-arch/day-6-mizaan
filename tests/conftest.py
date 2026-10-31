"""Shared fixtures: a fresh in-memory database and an authenticated client."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

# Settings are read at import time, so the environment must be prepared before
# any app module is imported.
_tmpdir = tempfile.mkdtemp(prefix="mizaan-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_tmpdir) / 'test.db'}"
os.environ["SECRET_KEY"] = "test-signing-key-not-for-production-use-only"
os.environ["AUTH_RATE_LIMIT"] = "1000"
os.environ["WRITE_RATE_LIMIT"] = "1000"
os.environ["SEED_REGIONS"] = "us-east:64:12:2.48,eu-west:32:8:2.61,ap-south:24:4:2.19"

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db() -> Iterator[None]:
    """Drop and recreate the schema per test so nothing leaks between cases."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    from app.database import seed_regions

    seed_regions()
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def register(client: TestClient, email: str = "eng@example.com", role: str = "admin") -> dict:
    res = client.post(
        "/api/auth/register",
        json={
            "email": email,
            "password": "correct horse battery",
            "display_name": "Test",
            "role": role,
        },
    )
    assert res.status_code == 201, res.text
    return res.json()


@pytest.fixture
def auth(client: TestClient) -> dict[str, str]:
    data = register(client)
    return {"Authorization": f"Bearer {data['access_token']}"}


MANIFEST = {
    "components": {
        "weights": {"sha256": "a" * 64, "format": "safetensors", "quantization": "awq-int4"},
        "tokenizer": {"sha256": "b" * 64, "vocab_size": 32000},
        "template": {"sha256": "c" * 64, "name": "chatml"},
        "runtime": {"framework": "vllm", "max_seq_len": 8192},
    },
    "params": {"temperature": 0.7, "top_p": 0.95, "max_tokens": 2048},
}


def make_model(
    client: TestClient, headers: dict[str, str], name: str = "tarteel", version: str = "1.0.0"
) -> dict:
    res = client.post(
        "/api/models",
        headers=headers,
        json={"name": name, "version": version, "description": "", "manifest": MANIFEST},
    )
    assert res.status_code == 201, res.text
    return res.json()
