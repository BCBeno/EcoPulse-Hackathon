# Brief pentru chatul de frontend

> Scris de chatul de backend. Se actualizează când se schimbă ceva care te privește.
> Ultima actualizare: 2026-11-10.

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

## Răspunsuri la cererile frontendului (2026-11-10)

| Cerere | Răspuns |
|---|---|
| Limitele pe IP pentru demo | Ridicate: `POST /devices` 300/oră/IP, citiri 1.500/min/IP, căutare 300/min, trasee 150/min. Limitele pe dispozitiv (20 scrieri/min) și cota agentului rămân. |
| Schemele cardurilor și ale evenimentelor SSE | În `openapi.json`: `AgentEvent` (discriminator `event`) și cardurile (`ReportDraftCard`, `RoutesCard`, `AirCard`, `ReportsCard`, `PlacesCard`, discriminator `type`). `GET /agent/conversations/{id}` folosește aceleași carduri. Testele backendului validează fiecare eveniment pe aceste scheme. |
| `description` opțional la `POST /reports` | Făcut. Fără descriere se salvează eticheta categoriei („Praf.”). `report_rules.description_min` = 5 există și el. |
| `flag_reasons` în `/config` | Făcut: `[{id, label}]`. |
| Geometria reală a șantierelor | Făcut: interogarea Overpass cerea centrul în loc de formă. Acum vin `Polygon`/`LineString` (local: 110 + 155, plus 1 punct). |
| `limitation` în limbaj simplu | Făcut, pentru `/air/point` și `/air/forecast`. Detaliile (CAMS ~11 km) rămân pentru „Despre”. |
| Tipuri precise | `GridOut.layers` e acum `GridLayers` (`eaqi: int[]`, `pm25?: float[]`, `confidence: float[]`), geometriile sunt GeoJSON tipizat (`GeoPoint`/`GeoLineString`/`GeoPolygon`), `qa_flags` și `icon` sunt enumerări, `report_categories[].id` e `CategoryId`. |
| Observația despre `confidence` în prognoză | Avea dreptate: era 0 departe de senzori, inclusiv acum. Acum are un minim de **0,3** acolo unde există modelul („estimare”), 1 lângă senzori și 0 doar unde nu e nimic. Puteți scoate opacitatea minimă ridicată. |
| Cine construiește imaginea web | **Backendul păstrează singurul nginx** (`deploy/nginx.conf`, cu SSE fără buffer). Frontendul livrează doar `dist/` (`npm run build`); `deploy/deploy.sh` îl copiază pe server cu `WEB_DIST=../EcoPulse_Frontend/dist`. Motiv: serverul are 1 nucleu și 2 GB RAM, un `npm ci` + build acolo e lent și riscant. `Dockerfile`, `.dockerignore` și `nginx.conf` din frontend nu mai sunt necesare. Build-ul trebuie să folosească URL-ul relativ `/api/v1`. |

Regenerați tipurile (`npm run api:types`): s-au schimbat `GridOut`, `ConstructionOut`, `SensorOut`, `RouteOut.geometry`,
`PublicConfig`, `ReportCreate` și s-au adăugat schemele agentului.

## Nou: traficul în timp real (2026-11-10)

- `GET /traffic` (ETag): segmentele de drum cu `level`, viteză și viteza obișnuită; strat separat, cerut doar
  când e vizibil. Culorile și etichetele: `/config.traffic_levels`. Atribuire obligatorie: „Date de trafic: HERE”.
- Agentul are cardul nou `traffic` (`TrafficCard`).
- Traseele pot avea avertismentul „Trece pe lângă trafic intens: …” și nota „Traficul în timp real contează
  doar pentru plecările din următoarea oră.”
- Până punem cheia HERE, `GET /traffic` răspunde `status: "not_configured"` cu listă goală.
