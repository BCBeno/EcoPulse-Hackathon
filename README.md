# EcoPulse — backend

Calitatea aerului, trasee curate și rapoarte comunitare pentru Iași.

- Design și decizii: [docs/design.md](docs/design.md)
- Contractul API (pentru frontend): [docs/api-contract.md](docs/api-contract.md) și
  [docs/openapi.json](docs/openapi.json) (generat)
- Versiunea de la hackathon: tag-ul `v0-hackathon`

## Rulare locală

Cerințe: Python 3.13, [uv](https://docs.astral.sh/uv/), Docker.

```bash
docker compose -f deploy/compose.dev.yaml up -d
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
uv run python -m app.worker   # într-un alt terminal: sincronizează sursele și grila
```

Harta pentru GraphHopper (o dată, apoi săptămânal):

```bash
uv run python deploy/graphhopper/build_extract.py
docker compose -f deploy/compose.dev.yaml up -d graphhopper
```

API: <http://localhost:8000/api/v1>, documentație interactivă: <http://localhost:8000/api/v1/docs>.

## Verificări

```bash
uv run ruff format --check . && uv run ruff check .
uv run pytest
```

Testele folosesc baza `ecopulse_test` din containerul de dezvoltare și o reconstruiesc din migrări la
fiecare rulare.

După orice schimbare de API:

```bash
uv run python scripts/export_openapi.py
```

## Migrări

```bash
uv run alembic revision --autogenerate -m "ce s-a schimbat"
uv run alembic upgrade head
```

Migrările generate se citesc și se curăță înainte de commit.
