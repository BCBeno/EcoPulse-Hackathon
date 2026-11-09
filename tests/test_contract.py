from scripts.export_openapi import TARGET, render


def test_openapi_snapshot_is_current():
    """The frontend builds its types from docs/openapi.json; regenerate it on every API change."""
    assert TARGET.read_text(encoding="utf-8") == render(), (
        "API changed: run `uv run python scripts/export_openapi.py` and update docs/api-contract.md"
    )
