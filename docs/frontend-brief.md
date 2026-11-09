# Brief pentru chatul de frontend

> Scris de chatul de backend. Se actualizează când se schimbă ceva care te privește.
> Ultima actualizare: 2026-11-09 (seara: agentul e gata).

## Context
EcoPulse: aerul din Iași pe hartă, trasee curate pe jos și cu bicicleta, rapoarte comunitare și un
agent AI într-un singur chat. Interfața și textele sunt în română. Termen: **vineri 2026-11-13**,
publicat la `https://ecopulse.bcbeno.me` (frontendul la `/`, API-ul la `/api/v1`, același origin).

Backendul a fost rescris de la zero (repo `D:\PythonProjects\EcoPulse-Hackathon`, branch
`rewrite/backend`). Frontendul (`D:\PythonProjects\EcoPulse_Frontend`) se îmbunătățește în chatul tău.

## Ce citești întâi
1. `D:\PythonProjects\EcoPulse-Hackathon\docs\design.md` — secțiunile 1, 2, 9, 11.
2. `D:\PythonProjects\EcoPulse-Hackathon\docs\api-contract.md` — contractul complet.
3. `D:\PythonProjects\EcoPulse-Hackathon\docs\openapi.json` — generat din backend; din el generezi tipurile.

## Reguli comune
- **Contractul API se schimbă doar prin chatul de backend.** Dacă ai nevoie de altceva (un câmp, alt
  format), scrie ce și de ce; utilizatorul duce cererea în chatul de backend. Nu ocoli contractul în cod.
- **Backendul calculează, frontendul afișează.** Fără scoruri, rute, culori EAQI, etichete de categorii
  sau praguri calculate/dublate în frontend: totul vine din `GET /config` și din răspunsuri.
- Tipuri TS **generate** din `openapi.json` cu `openapi-typescript`, nu scrise de mână.
- Erorile au forma `{"error": {"code", "message", "fields"?}}`; `message` e deja în română și se poate
  afișa direct.
- Orele vin în UTC (ISO 8601); le afișezi în `Europe/Bucharest`.
- Coordonatele sunt `{lat, lon}`; geometriile sunt GeoJSON (`[lon, lat]`).
- Fără voce în v1.
- Git: lucrezi pe un branch, nu pe `main`; nu faci push (îl face utilizatorul).

## Ce e gata în backend (2026-11-09)
- `POST /devices` → token anonim (se ține în `localStorage`, se trimite ca `Authorization: Bearer`);
  `GET /devices/me`.
- `GET /config` (cu ETag): oraș, niveluri EAQI (etichete, culori, sfaturi), categorii de rapoarte,
  gravități, moduri, tipuri de trasee, atribuiri.
- Rapoarte: listă, detaliu, creare, voturi „Încă e acolo” / „Nu mai e”, rezolvare, semnalare, publicare
  și anulare de ciorne.
- `GET /map` (cu ETag; polling la `config.map.poll_interval_s`, trimite `If-None-Match`): grila poluării
  (`eaqi`, `pm25`, `confidence`), senzori, rapoarte active, șantiere OSM, starea surselor.
  `GET /map?at=<ISO>` dă grila de prognoză (până la 72 h).
- `GET /air/point`, `GET /air/forecast` (cu `best_window`).
- `GET /places/search` (debounce 300 ms), `GET /places/reverse`.
- `POST /routes`: 1–3 trasee (`kinds`), `segments` pentru colorare, `summary`, `notes`, `warnings`,
  `google_maps_url`.

- Agentul: `POST /agent/conversations`, `GET /agent/conversations[/{id}]`,
  `POST /agent/conversations/{id}/messages` (flux SSE; vezi tabelul de evenimente și cardurile în contract).

**Urmează:** admin (nu e pentru interfața publică).

## Ce trebuie să facă interfața
- **Harta:** grila ca strat de culori EAQI (opacitatea după `confidence`; `null` = fără date, nu
  „aer curat”), senzorii (cu avertismente pentru `high_humidity`, `outlier`, `stale`), rapoartele cu
  raza lor, șantierele, legenda EAQI, atribuirile din `/config`. Un selector de oră (acum … +72 h)
  folosește `GET /map?at=`.
- **Planificatorul:** origine/destinație (căutare cu debounce de 300 ms, poziția GPS, punct pe hartă),
  pe jos/bicicletă, plecare acum sau mai târziu; afișează cele 3 trasee din `POST /routes`, colorate pe
  porțiuni după `segments`, cu expunerea și linkul Google Maps.
- **Chatul cu agentul (un singur chat):** trimite textul și contextul (GPS, centrul hărții, punctul
  selectat); citește fluxul SSE (`fetch` + `ReadableStream`, nu `EventSource`); afișează cardurile:
  `report_draft` (butoane Publică / Anulează, pin mutabil, alegere pe hartă când `needs_location`),
  `routes` (desenate pe hartă), `air`, `reports`, `places` (alegere între variante). Arată cota rămasă.
- **Rapoartele:** popup cu detaliile, voturile și butoanele permise de `viewer`.
- Merge bine pe telefon (cei mai mulți utilizatori vor fi pe mobil).

## Decizii pe care le iei tu (în chatul de frontend)
- Biblioteca de hartă. Recomandarea backendului: MapLibre GL cu hărți vectoriale gratuite (de ex.
  OpenFreeMap); Leaflet merge și el. Grila vine ca listă de celule, rând cu rând de la sud-vest.
- Structura aplicației, starea pe server (de ex. TanStack Query), stilurile.

## Cum rulezi backendul local
În `D:\PythonProjects\EcoPulse-Hackathon` (vezi README.md):
```bash
docker compose -f deploy/compose.dev.yaml up -d
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload            # API pe http://localhost:8000/api/v1
uv run python -m app.worker                      # alt terminal: aduce datele despre aer
```
CORS permite `http://localhost:5173` și `http://127.0.0.1:5173`. În dezvoltare, setează URL-ul API-ului
prin `VITE_API_URL` (implicit `/api/v1` în producție).
