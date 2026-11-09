"""Writes docs/openapi.json, the machine-readable contract the frontend generates its types from.

Run after any API change:  uv run python scripts/export_openapi.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.main import create_app  # noqa: E402
from tests.conftest import make_settings  # noqa: E402

TARGET = ROOT / "docs" / "openapi.json"


def render() -> str:
    schema = create_app(make_settings()).openapi()
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    TARGET.write_text(render(), encoding="utf-8")
    print(f"Wrote {TARGET.relative_to(ROOT)}")
