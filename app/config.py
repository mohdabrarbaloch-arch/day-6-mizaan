"""Application settings, loaded from the environment only.

If SECRET_KEY is absent we generate a random one and warn — we never fall back
to a hardcoded constant, because a hardcoded signing key is a public key.
"""

from __future__ import annotations

import secrets
import sys

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_name: str = "Mizaan"
    version: str = "1.0.0"

    secret_key: str = ""
    access_token_minutes: int = 1440
    database_url: str = "sqlite:///./mizaan.db"

    allowed_origins: str = "*"
    production: int = 0

    auth_rate_limit: int = 10
    write_rate_limit: int = 60

    seed_regions: str = "us-east:64:12:2.48,eu-west:32:8:2.61,ap-south:24:4:2.19"

    llm_base_url: str | None = None
    llm_api_key: str | None = None

    @property
    def is_production(self) -> bool:
        return self.production == 1

    @property
    def origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def signing_key(self) -> str:
        if not self.secret_key:
            # Generated once per process. Documented in .env.example as the
            # reason to set SECRET_KEY in anything long-lived.
            self.secret_key = secrets.token_urlsafe(48)
            print(
                "WARNING: SECRET_KEY is not set; using a per-process random key. "
                "Sessions will not survive a restart. Set SECRET_KEY for real use.",
                file=sys.stderr,
            )
        return self.secret_key

    def parsed_regions(self) -> list[dict[str, object]]:
        """Parse SEED_REGIONS into region dicts. Bad entries are skipped, not fatal."""
        out: list[dict[str, object]] = []
        for chunk in self.seed_regions.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            parts = chunk.split(":")
            if len(parts) != 4:
                print(f"WARNING: skipping malformed region seed {chunk!r}", file=sys.stderr)
                continue
            name, quota, used, rate = parts
            try:
                out.append(
                    {
                        "name": name,
                        "gpu_quota": int(quota),
                        "gpu_used": int(used),
                        "usd_per_gpu_hour": rate,
                    }
                )
            except ValueError:
                print(f"WARNING: skipping unparsable region seed {chunk!r}", file=sys.stderr)
        return out


settings = Settings()
