"""Creates deploy/.env.production (git-ignored) from the local .env plus production settings.

Keeps an existing POSTGRES_PASSWORD so re-running never locks the app out of its database.
Usage:  uv run python deploy/make_env.py
"""

import secrets
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "deploy" / ".env.production"
COPIED = (
    "ADMIN_API_KEY",
    "OPENAQ_API_KEY",
    "GROQ_API_KEY",
    "GROQ_MODEL",
    "OPENROUTER_API_KEY",
    "OPENROUTER_MODEL",
)


def main() -> None:
    local = dotenv_values(ROOT / ".env")
    previous = dotenv_values(TARGET) if TARGET.exists() else {}
    values = {
        "APP_ENV": "production",
        "LOG_JSON": "true",
        "LOG_LEVEL": "INFO",
        # Same origin in production: no CORS.
        "CORS_ORIGINS": "",
        # Docker networks + nginx-proxy-manager on the infra LXC (192.168.0.20).
        "TRUSTED_PROXIES": "172.16.0.0/12,192.168.0.20/32",
        "CITY_NAME": "Iași",
        "POSTGRES_PASSWORD": previous.get("POSTGRES_PASSWORD") or secrets.token_urlsafe(24),
    }
    for key in COPIED:
        if local.get(key):
            values[key] = local[key]
    if not values.get("ADMIN_API_KEY"):
        values["ADMIN_API_KEY"] = secrets.token_urlsafe(32)
    TARGET.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    print(f"Wrote {TARGET.relative_to(ROOT)} with: {', '.join(values)}")


if __name__ == "__main__":
    main()
