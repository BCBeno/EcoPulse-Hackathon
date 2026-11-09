# EcoPulse — documentul de design (v1)

> **Sursa de adevăr** pentru backend și frontend. Contractul API detaliat e în
> [api-contract.md](api-contract.md). Chatul de frontend citește ambele fișiere de aici
> (`D:\PythonProjects\EcoPulse-Hackathon\docs\`). Contractul se schimbă **doar prin chatul de backend**.
>
> Stare: **aprobat 2026-11-09** · Termen: vineri 2026-11-13
> Echipa: Beno + Claude (backend și frontend în chaturi separate).

---

## 1. Ce face v1

Pentru pietonii și bicicliștii din Iași:

1. **Harta aerului** — senzori (Sensor.Community, OpenAQ), prognoza CAMS (Open-Meteo) și o
   **grilă a poluării** peste tot orașul, actualizată la câteva minute. Se poate vedea și cum va fi
   aerul în următoarele 72 de ore.
2. **Trei trasee** pe jos sau cu bicicleta — *Cel mai curat*, *Echilibrat*, *Cel mai rapid* — fiecare
   cu scor de expunere, colorat pe porțiuni după nivelul aerului și cu link „Deschide în Google Maps”.
   Plecare acum sau mai târziu (până la 72 h).
3. **Agentul AI**, într-un singur chat: răspunde la întrebări despre aer, planifică trasee, arată
   rapoartele din zonă și **pregătește rapoarte** pe care utilizatorul le publică printr-un buton.
4. **Rapoarte comunitare** — fum, praf, șantier etc. — cu „Încă e acolo” / „Nu mai e”, rezolvare de
   către autor, semnalare abuz.
5. **Șantierele din OpenStreetMap** ca strat pe hartă și ca sursă de penalizare la rutare.

**Nu intră în v1:** voce (amânată), trafic estimat, trasee salvate, lucrări oficiale din RSS,
motocicletă, conturi cu email/parolă.

## 2. Principii

1. **Backendul calculează, frontendul afișează.** Grila, traseele, scorurile, nivelurile, etichetele
   și culorile vin din backend. Frontendul nu are constante de business duplicate (le citește din
   `GET /config`).
2. **Aceeași grilă pentru hartă și rutare.** Ce vede utilizatorul pe hartă e exact ce folosește
   algoritmul de trasee și agentul.
3. **Onestitate.** Fiecare valoare are sursă și vârstă. Lipsa datelor nu înseamnă aer curat. Senzorii
   ieftini și modelul CAMS sunt etichetați ca atare. Agentul nu inventează date.
4. **LLM-ul nu publică nimic singur.** Agentul doar citește date publice și creează **ciorne**;
   publicarea e un apel REST declanșat de buton.
5. **Totul merge și fără LLM** — mai puțin conversația liberă. Fără LLM, chatul trece pe reguli:
   rapoartele se pregătesc tot, întrebările despre aer primesc un răspuns-șablon din date.
6. **Simplu de rulat:** un singur `docker compose up` pe server, o singură bază de date, un singur `.env`.

## 3. Arhitectură și deploy

```
Internet ─► Cloudflare (bcbeno.me) ─► nginx-proxy-manager (LXC `infra`, 80/443)
                                         │  ecopulse.bcbeno.me
                                         ▼
                 LXC `ecopulse` (Docker Compose)
   ┌─────────────────────────────────────────────────────────────────┐
   │ web (nginx)   /        → fișierele statice ale frontendului     │
   │               /api/    → api:8000 (fără buffering pentru SSE)   │
   │ api           FastAPI (uvicorn, 1 proces)                       │
   │ worker        sincronizare surse + calcul grilă (APScheduler)   │
   │ db            PostgreSQL 17 + PostGIS                           │
   │ graphhopper   rutare pe jos/bicicletă, harta zonei Iași         │
   └─────────────────────────────────────────────────────────────────┘
```

- **Un singur origin** (`ecopulse.bcbeno.me`): frontendul la `/`, API-ul la `/api/v1`. Fără CORS în
  producție (CORS rămâne doar pentru `localhost:5173` în dezvoltare).
- **IP-ul real al clientului** (pentru limite): se ia din `CF-Connecting-IP` / `X-Forwarded-For`
  **doar** dacă cererea vine de la proxy-urile de încredere (configurate în `.env`).
- **SSE prin proxy:** nginx din LXC are `proxy_buffering off` pe ruta agentului; backendul trimite
  `X-Accel-Buffering: no` și un comentariu keep-alive la 15 s (Cloudflare închide conexiunile
  inactive la ~100 s).
- **Resurse LXC:** 4 GB RAM, 4 nuclee, 25 GB disc. Docker în LXC cere `nesting=1` (și `keyctl=1`
  dacă e neprivilegiat).
- **Backup:** `pg_dump` nocturn într-un volum montat de pe gazdă; păstrăm 7 zile.
- **Loguri:** JSON pe stdout, rotite de Docker (`max-size`), cu stack trace complet la erori.
- **Harta pentru GraphHopper:** extras Geofabrik România (~330 MB), tăiat cu pyosmium la zona
  Iașului plus o margine de 5 km (rezultă ~2 MB). Se reconstruiește săptămânal (sau manual) cu
  `deploy/graphhopper/build_extract.py`, apoi se șterge `graph-cache` și se repornește containerul.

## 4. Stack backend

| Ce | Alegere | De ce |
|---|---|---|
| Limbaj | Python 3.13 | e instalat local; toate bibliotecile au wheel-uri |
| Dependențe | `uv` + `uv.lock` | instalare rapidă și reproductibilă |
| Web | FastAPI + Pydantic v2 | modele de răspuns → OpenAPI → tipuri TS |
| DB | PostgreSQL 17 + PostGIS, SQLAlchemy 2.1 async, `asyncpg`, GeoAlchemy2 | `asyncpg` merge și pe Windows (psycopg async nu merge cu bucla implicită de pe Windows); Alembic rulează tot async |
| Migrări | Alembic, de la prima zi | fără `create_all` la pornire |
| Geometrie | Shapely 2, pyproj (UTM 35N, EPSG:32635), NumPy | calcule metrice corecte, grilă rapidă |
| HTTP extern | httpx async, client comun, timeout și reîncercări | |
| Programare | APScheduler 3.11 în procesul `worker` | |
| LLM | SDK-ul `openai` cu `base_url` pentru Groq / OpenRouter | același cod pentru ambii furnizori, tool calling și streaming |
| SSE | `sse-starlette` | |
| Rutare | GraphHopper (self-hosted, Java) | `custom_model` cu zone penalizate per cerere |
| Teste | pytest, pytest-asyncio, respx, PostGIS real în Docker | |
| Calitate | ruff (format + lint), pyright în modul basic | |

## 5. Structura codului

Organizat pe funcționalități. Fiecare modul are aceleași fișiere, ca să fie ușor de navigat.

```
app/
  main.py              # creează aplicația, middleware, include routerele
  config.py            # Settings (un singur .env, în rădăcina proiectului)
  db.py                # engine, sesiuni, Base
  errors.py            # AppError(code, message, status) + handlere → {"error": {...}}
  http.py              # client httpx comun
  security.py          # token-uri dispozitiv, cheie admin, limite de cereri
  devices/             # conturi anonime
  reports/             # rapoarte, ciorne, voturi, semnalări
  air/
    providers/         # sensor_community.py, openaq.py, open_meteo.py (CAMS + vreme), osm.py
    ingest.py          # fetch → normalizare → upsert în lot → curățenie
    quality.py         # filtre QA pentru senzori
    eaqi.py            # benzile indicelui european
    grid.py            # calculul grilei (acum + prognoză)
    service.py         # aerul într-un punct, prognoză, date pentru hartă
  routing/
    graphhopper.py     # client GraphHopper
    candidates.py      # zone penalizate → custom_model → trasee candidate
    exposure.py        # scorul de expunere de-a lungul traseului
    service.py         # alege cele 3 trasee, etichete, link Google Maps
  places/              # dicționar local de locuri din Iași + Photon/Nominatim, cu cache
  agent/
    llm.py             # lanțul de furnizori (Groq → OpenRouter), streaming
    tools.py           # uneltele (apelează serviciile de mai sus)
    rules.py           # modul fără LLM: intenție + extragere raport pe reguli
    prompts.py         # promptul de sistem (RO)
    service.py         # bucla agentului, carduri, istoric
    quota.py           # cote pe dispozitiv și buget global
  admin/               # endpointuri interne (X-Admin-Key)
  worker.py            # punctul de intrare al workerului
migrations/
tests/                 # unit/ (logică pură), integration/ (PostGIS real), fixtures/
docs/                  # design.md, api-contract.md, openapi.json (generat)
deploy/                # compose.yaml, nginx.conf, graphhopper/config.yml, scripturi
```

Fiecare modul de business are `models.py` (tabele), `schemas.py` (Pydantic, intrare și ieșire),
`service.py` (logica) și `router.py` (subțire: validare → serviciu → răspuns).
Logica pură (EAQI, grilă, expunere, reguli) nu atinge baza de date, ca să fie testabilă direct.

## 6. Modelul de date

ID-uri UUID (v7, ordonate în timp). Toate orele sunt `timestamptz` în UTC. Toate geometriile sunt
`geometry(…, 4326)` cu index GiST; distanțele în metri se calculează cu cast la `geography`.
Valorile închise (categorie, stare etc.) sunt `varchar` cu `CHECK`, nu enum-uri PostgreSQL (migrări
mai simple).

### Identitate
- **devices** — `id`, `token_hash` (SHA-256 al token-ului opac, unic), `role` (`user`|`admin`),
  `created_at`, `last_seen_at`.

### Rapoarte
- **reports** — `id`, `device_id`, `category`, `severity` (1–3), `description` (text public),
  `original_text` (privat, doar pentru moderare), `location`, `location_label`, `radius_m`,
  `observed_at`, `expires_at`, `status` (`active`|`resolved`|`hidden`), `resolved_at`,
  `resolved_by` (`author`|`community`|`admin`), `source` (`agent`|`form`), `extraction`
  (`llm`|`rules`|`manual`), `created_at`.
  „Expirat” nu se stochează: e `status = active` și `expires_at <= now`.
- **report_votes** — PK (`report_id`, `device_id`), `kind` (`still_there`|`gone`), `created_at`.
- **report_flags** — PK (`report_id`, `device_id`), `reason`, `created_at`.
- **report_drafts** — `id`, `device_id`, `conversation_id`, `category`, `severity`, `description`,
  `original_text`, `location` (poate lipsi), `location_label`, `location_source`
  (`gps`|`place`|`map`), `status` (`pending`|`published`|`cancelled`), `report_id`, `created_at`.
  Ciornele `pending` expiră după 24 h.

**Reguli pentru rapoarte**
- Durata și raza implicite sunt pe categorie (tabelul de mai jos, expuse în `/config`).
- „Încă e acolo” prelungește: `expires_at = max(expires_at, now + durata/2)`.
- Rezolvat de comunitate: `gone >= 3` **și** `gone > still_there`.
- Nu poți vota propriul raport și votezi o singură dată (poți schimba votul).
- Semnalările doar apar în lista adminului; nu ascund automat nimic (altfel oricine cu mai multe
  dispozitive ar putea ascunde rapoarte).

| Categorie | Etichetă | Afectează | Durată | Rază |
|---|---|---|---|---|
| `smoke` | Fum | aer | 6 h | 400 m |
| `dust` | Praf | aer | 8 h | 250 m |
| `industrial` | Poluare industrială / miros | aer | 12 h | 500 m |
| `construction` | Șantier | aer + obstacol | 24 h | 200 m |
| `machinery` | Utilaje | aer | 8 h | 150 m |
| `traffic` | Trafic intens | aer | 2 h | 300 m |
| `obstruction` | Obstacol | obstacol | 24 h | 50 m |
| `flooding` | Apă / inundație | obstacol | 24 h | 80 m |
| `infrastructure` | Infrastructură stricată | obstacol | 72 h | 30 m |
| `lighting` | Iluminat stricat | informativ | 72 h | 50 m |
| `other` | Altceva | informativ | 6 h | 100 m |

Valorile sunt o propunere; se pot ajusta fără migrare (stau în cod/config, nu în baza de date).

### Aer
- **sensors** — `id`, `provider` (`sensor_community`|`openaq`), `external_id`, `kind`
  (`low_cost`|`official`), `location`, `meta` (JSON: tip senzor, proprietar, licență), `first_seen_at`,
  `last_seen_at`. Unic (`provider`, `external_id`).
- **measurements** — `sensor_id`, `parameter` (`pm25`, `pm10`, `no2`, `o3`, `so2`, `temperature`,
  `humidity`), `value` (µg/m³, °C, %), `observed_at`, `retrieved_at`. Semnalele QA nu se stochează:
  se calculează la citire (depind de vecini și de momentul cererii).
  PK (`sensor_id`, `parameter`, `observed_at`). **Păstrate 7 zile.**
- **model_forecasts** — CAMS Europe prin Open-Meteo, pe puncte fixe ale grilei CAMS din zona Iașului:
  `point_id`, `location`, `parameter` (`pm25`, `pm10`, `no2`, `o3`, `so2`), `valid_at`, `value`,
  `retrieved_at`. PK (`point_id`, `parameter`, `valid_at`). Păstrate de la −24 h la +96 h.
- **weather_hourly** — centrul orașului: `valid_at`, `temperature_c`, `humidity_pct`,
  `wind_speed_ms`, `wind_dir_deg`, `precipitation_mm`, `retrieved_at`.
- **grid_snapshots** — `id`, `valid_at`, `kind` (`now`|`forecast`), `computed_at`, `south`, `west`,
  `d_lat`, `d_lon`, `rows`, `cols`, `layers` (`pm25`, `pm10`, `no2`, `o3` ca float32 comprimat,
  `eaqi` și `confidence`), `inputs` (JSON: câți senzori, ce surse, vârste). Grila „acum” se
  recalculează la fiecare 5 min; se păstrează 6 h (istoricul nu e folosit). Grilele de prognoză se calculează la cerere și
  se țin în cache pe oră.
- **osm_features** — `osm_id` (`way/123`), `kind` (`construction`), `geometry`, `tags`,
  `retrieved_at`. Șterse dacă nu mai apar 48 h.
- **source_runs** — `id`, `source`, `started_at`, `finished_at`, `status`, `records`, `error`.

### Agent
- **conversations** — `id`, `device_id`, `title` (primele cuvinte), `created_at`, `updated_at`.
- **messages** — `id`, `conversation_id`, `client_message_id` (unic per conversație),
  `role` (`user`|`assistant`), `text`, `cards` (JSON), `tool_trace` (JSON, compact),
  `provider`, `model`, `tokens_in`, `tokens_out`, `created_at`.
- **llm_usage** — PK (`day`, `device_id`), `requests`, `tokens`; plus un rând global pe zi.

Conversațiile se șterg după 30 de zile.

### Locuri
- **places_cache** — `query_norm`, `provider`, `results` (JSON), `created_at`; păstrate 30 de zile.
- Dicționar local (fișier în cod) cu cartiere și repere din Iași: Copou, Tătărași, Podu Roș,
  Podu de Piatră, Gara, Piața Unirii, Palas, Nicolina, Dacia, Alexandru cel Bun, Păcurari etc.

## 7. Datele despre aer

### Surse și program (worker)

| Sursă | Ce | La cât timp | Note |
|---|---|---|---|
| Sensor.Community | PM2.5, PM10, temp., umiditate | 5 min | `GET data.sensor.community/airrohr/v1/filter/box=S,W,N,E` (filtrul e în cale) |
| OpenAQ v3 | stații oficiale (PM, NO₂, O₃, SO₂) | 60 min | cheie gratuită în `X-API-Key`; descoperire pe rază de 15 km |
| Open-Meteo CAMS Europe | PM2.5, PM10, NO₂, O₃, SO₂ orar, 4 zile | 3 h | ~11 km rezoluție; e model, nu măsurătoare |
| Open-Meteo vreme | temp., umiditate, vânt, precipitații | 60 min | context pentru agent și pentru filtrul de umiditate |
| Overpass (OSM) | șantiere | 24 h | `highway=construction`, `landuse=construction`, `construction=*` |
| Grila „acum” | calcul | 5 min, după Sensor.Community | |
| Curățenie | retenție | la 6 h | măsurători 7 zile, prognoze și vreme 24 h în urmă, grile 6 h, rulări 7 zile |

Fiecare sursă: un modul cu `fetch()` → `normalize()` → listă de înregistrări tipizate, salvate cu
`INSERT … ON CONFLICT DO UPDATE` în lot (nu o interogare pe rând). Starea fiecărei surse
(ultima reușită, ultima eroare, vârsta datelor) e în `source_runs` și în `/health/ready`.

### Filtrul de calitate pentru senzori (QA)
- Valori negative, nule sau > 1000 µg/m³ → eliminate.
- `pm25 > pm10` la același senzor și moment → marcat `inconsistent`, ponderea redusă.
- Umiditate ≥ 90 % (de la senzor sau, în lipsă, de la Open-Meteo) → marcat `high_humidity`, pondere
  redusă. Senzorii optici ieftini supraestimează în ceață; utilizatorul vede avertismentul.
- Senzor care se abate de la mediana vecinilor (raza 2 km) cu mai mult de 4 × MAD → marcat `outlier`,
  exclus din grilă, dar tot vizibil pe hartă cu avertisment.
- Senzor fără date de 2 h → `stale`, exclus din grilă.

### Indicele european (EAQI)
- Șase niveluri: 1 Bun, 2 Acceptabil, 3 Moderat, 4 Slab, 5 Foarte slab, 6 Extrem de slab.
  Nivelul general = cel mai rău sub-indice dintre poluanții disponibili.
- Benzile sunt cele publicate de EEA pe <https://airindex.eea.europa.eu/AQI/index.html> (revizia din
  raportul ETC HE 2024/17), **verificate 2026-11-09**: PM2.5 5/15/50/90/140, PM10 15/45/120/195/270,
  NO₂ 10/25/60/100/150, O₃ 60/100/120/160/180, SO₂ 20/40/125/190/275 (µg/m³, limita superioară
  inclusivă a nivelurilor 1–5; valoarea se rotunjește la întreg).
- EEA definește indicele pe **concentrații orare**, deci și noi folosim valoarea orară. Nu folosim
  `european_aqi` gata calculat de Open-Meteo, ca să nu amestecăm versiuni de benzi.
- Culorile nu apar pe pagina EEA; folosim paleta EEA uzuală.

### Grila poluării
- Zona: bbox-ul orașului `47.05,27.40,47.26,27.75`, celule de ~250 m, definite în grade
  (`d_lat = 0.00225`, `d_lon = 0.0033`) ca frontendul să le poată desena direct.
  Rezultă ~93 × 106 ≈ 9.900 de celule.
- **Metoda (fond + corecție locală):**
  1. **Fond** = CAMS la ora respectivă, interpolat biliniar între punctele CAMS.
  2. **Reziduuri** la fiecare senzor valid: `măsurat − fond` (media ultimelor ~15 min, ca să reducă
     zgomotul).
  3. **Corecția locală** = reziduurile interpolate IDW (putere 2, rază 2 km; stațiile oficiale au
     pondere ×3, cele marcate QA au pondere redusă).
  4. **Valoare** = fond + `încredere × corecție`, unde `încredere` scade cu distanța până la cel mai
     apropiat senzor valid (1 lângă senzor, ~0 la 2 km).
  5. **Prognoză** la ora `t`: fondul CAMS de la `t` + corecția de acum, care scade exponențial cu
     constanta de 6 h.
- Gazele (NO₂, O₃, SO₂) vin doar din CAMS și din stațiile oficiale; senzorii ieftini măsoară doar PM.
- Stratul `confidence` (0–1) ajunge la frontend, ca zonele fără senzori să fie desenate mai estompat.

## 8. Rutare și scorul de expunere

### Candidați (GraphHopper, `POST /route`, `ch.disable=true`, profil `foot` sau `bike`)
1. **Rapid:** fără `custom_model`, cu `algorithm=alternative_route` (până la 3 variante).
2. **Echilibrat** și **Curat:** `custom_model` cu `areas` = poligoane din celulele grilei (unite și
   simplificate, maximum 15 pe nivel), plus cercurile rapoartelor active și șantierele OSM (30 m).
   Zonele de poluare sunt **relative la oraș** în acel moment, ca traseul să ocolească punctele
   fierbinți chiar și când tot orașul e la același nivel EAQI:
   `hot1` = PM2.5 ≥ max(percentila 75, mediana + 4) sau EAQI ≥ 4;
   `hot2` = PM2.5 ≥ max(percentila 90, mediana + 8) sau EAQI ≥ 5.
   Reguli de `priority` (`multiply_by`):

| Zonă | Echilibrat | Curat |
|---|---|---|
| `hot1` | 0.8 | 0.5 |
| `hot2` | 0.55 | 0.25 |
| raport care afectează aerul | 0.6 | 0.2 |
| șantier OSM | 0.85 | 0.6 |
| raport-obstacol | 0.2 | 0.05 |

### Scorul (același pentru toți candidații)
- Traseul se eșantionează la 20 m. Timpul până la fiecare punct vine din duratele GraphHopper pe
  porțiuni. Concentrația PM2.5 se ia din grila de la `plecare + timp scurs` (pentru plecare acum,
  grila „acum”).
- `exposure_index = Σ PM2.5 × Δt` (µg·h/m³). Se compară doar între trasee cu același mod de deplasare.
- Rapoartele active care afectează aerul nu sunt în grilă; în raza lor adaugă un echivalent PM2.5 după
  gravitate (10 / 25 / 50 µg/m³). Fără asta, un traseu prin fum ar ieși „curat”: am văzut exact asta
  în testul pe date reale, unde GraphHopper ocolea raportul, dar alegerea îl păstra pe cel rapid.
- Pentru plecare mai târziu se folosește grila de la ora plecării (nu se schimbă pe parcursul drumului).
- Mai calculăm: PM2.5 mediu ponderat cu timpul, nivelul maxim de pe traseu, minutele petrecute la
  nivel ≥ 4 și diferența față de cel mai rapid (`vs_fastest_pct`).
- Porțiuni colorate: puncte consecutive cu același nivel EAQI sunt comasate în `segments`.

### Alegerea celor trei
- **Cel mai rapid:** durata minimă.
- **Cel mai curat:** expunerea minimă, dacă durata ≤ 1,5 × cel mai rapid. Dacă expunerea e cu mai
  puțin de 5 % mai mică decât la cel mai rapid, se comasează cu acesta.
- **Echilibrat:** minimul pe `0.5 × expunere normalizată + 0.5 × durată normalizată`, dintre ceilalți
  candidați.
- Traseele aproape identice (> 90 % suprapunere) se comasează; un traseu poate avea mai multe
  etichete (`kinds: ["fastest", "cleanest"]`). Atunci frontendul afișează „Aerul e similar pe toate
  traseele”.
- Linkul Google Maps conține până la 8 puncte intermediare luate de pe traseu, ca Google să urmeze
  aproximativ aceeași cale.

**Prototip (2026-11-09) — confirmat.** GraphHopper 11.1, harta zonei Iași (32.100 noduri, 41.700
muchii, ~360 MB RAM în container), traseu pe jos Gara → Tătărași:
- fără penalizare: 3 trasee în ~400 ms (prima cerere), cel mai rapid trece 644 m printr-o zonă de
  350 m rază de pe mijlocul lui;
- cu `custom_model.areas` (prioritate × 0.1 în zonă) **și** `alternative_route`: 3 trasee în ~150 ms,
  **0 m** prin zonă, cu 4,5–10 minute în plus;
- pe bicicletă, aceeași cerere a dat un singur traseu (alternativele nu sunt garantate), deci
  „Echilibrat” și „Curat” se cer separat, fiecare cu propriul `custom_model`.

Harta se construiește din extrasul Geofabrik pentru România, tăiat cu pyosmium
(`deploy/graphhopper/build_extract.py`). Overpass a picat la toate cele trei oglinzi în timpul
testului, deci nu e potrivit pentru reconstruirea hărții.

## 9. Agentul AI

### Flux
1. Frontendul trimite `POST /agent/conversations/{id}/messages` cu textul și contextul (locația GPS,
   centrul hărții, punctul selectat). Răspunsul vine ca flux SSE.
2. Backendul verifică cota, salvează mesajul, construiește contextul: promptul de sistem în română,
   data și ora locală, locația (rotunjită la ~50 m) și ultimele 10 mesaje.
3. Bucla LLM: maximum 3 runde de unelte. Fiecare unealtă întoarce **două lucruri**: un rezumat JSON
   compact pentru LLM (fără geometrii, ca să economisim tokeni) și un **card** complet pentru
   frontend.
4. Textul final vine în bucăți (`text.delta`). Cardurile vin pe măsură ce sunt gata.

### Unelte
| Unealtă | Ce face | Card |
|---|---|---|
| `get_air` | aerul acum sau la o oră/interval, într-un loc (nume sau „aici”) + vremea; poate găsi „cea mai bună oră” dintr-un interval | `air` |
| `plan_routes` | cele 3 trasee între două locuri, pe jos sau cu bicicleta, acum sau mai târziu | `routes` |
| `find_reports` | rapoarte active lângă un loc, opțional pe categorie | `reports` |
| `draft_report` | pregătește un raport: categorie, gravitate, descriere rescrisă, loc | `report_draft` |

Uneltele primesc locuri **după nume** și le rezolvă intern (dicționarul local, apoi Photon/Nominatim),
ca să nu consumăm o rundă LLM pe căutare. Dacă numele e ambiguu, unealta întoarce variantele, iar
agentul întreabă utilizatorul (card `places`).

### Raportarea prin chat
- Agentul recunoaște un raport („e fum la Podu Roș”), apelează `draft_report` și răspunde scurt
  („Am înțeles: Fum, gravitate mare, la Podu Roș. Verifică și apasă Publică.”).
- Locul vine, în ordine: din text (loc numit) → din GPS (dacă textul spune „aici” sau nu numește un
  loc) → din alegerea pe hartă (`needs_location: true`).
- Publicarea: `POST /reports/drafts/{id}/publish`, din butonul cardului. Utilizatorul poate muta pinul
  sau schimba categoria înainte. Nu se consumă LLM la publicare.

### Furnizori și limite
- Lanț: **Groq** (model principal pentru agent) → **OpenRouter** (model gratuit) la 429, 5xx sau
  timeout → **modul pe reguli**.
- Modelele concrete se aleg joi, după un test scurt în română cu tool calling. Sunt configurabile din
  `.env`.
- Cote: 30 de mesaje pe zi per dispozitiv; buget global zilnic de tokeni (configurabil, sub limita
  Groq); un singur flux activ per dispozitiv. Peste cotă, chatul trece pe modul pe reguli și spune asta.
- Economie de tokeni: prompt de sistem scurt, 4 unelte cu scheme mici, istoric limitat,
  `max_tokens` ~ 400.

### Modul pe reguli (fără LLM)
- Intenția: cuvinte-cheie pentru raport (fum, praf, groapă…) versus întrebare (cum e, unde, când,
  semn de întrebare).
- Raport → clasificator pe cuvinte-cheie (preluat din vechiul `quick_report`, curățat), card
  `report_draft` cu `extraction: "rules"`.
- Întrebare despre aer → răspuns-șablon din `get_air` pentru locul din context.
- Altceva → „Asistentul nu e disponibil acum; harta și planificatorul merg normal.”

### Siguranță
- Textul utilizatorului e tratat ca date, nu ca instrucțiuni. Uneltele nu pot modifica nimic în afară
  de ciorne.
- Descrierile rescrise nu includ date personale. Promptul cere asta, iar un filtru simplu elimină
  telefoane, emailuri și numere de înmatriculare.
- Răspunsurile citează vârsta datelor și nu dau sfaturi medicale individuale, doar recomandările
  standard asociate nivelului EAQI.

## 10. Securitate și abuz

- **Conturi anonime:** `POST /devices` întoarce un token opac de 32 de octeți. În baza de date stă
  doar hash-ul. Token-ul nu expiră; frontendul îl ține în `localStorage`.
- **Limite** (în memorie, un singur proces `api`):
  - creare dispozitiv: 20/oră per IP — suficient pentru un demo cu multe telefoane pe același Wi-Fi;
  - citiri: 120/min per IP;
  - scrieri: 20/min per dispozitiv;
  - trasee: 20/min per dispozitiv;
  - agent: cota de mai sus.
- **Admin:** header `X-Admin-Key` comparat cu `hmac.compare_digest`. Dacă cheia lipsește din `.env`,
  endpointurile admin răspund 503 (închis implicit).
- Baza de date, GraphHopper și workerul nu sunt expuse în afara rețelei Docker.
- Nu stocăm IP-uri. Locația GPS din chat se folosește doar pentru cererea curentă și pentru raportul
  publicat.
- Validare strictă pe toate intrările (lungimi, coordonate în zona orașului, enumerări).

## 11. Convenții comune backend ↔ frontend

- Prefix API: `/api/v1`. JSON în UTF-8. Câmpuri `snake_case`.
- Ore: ISO 8601 cu fus orar, emise în UTC (`2026-11-10T07:45:00Z`). Frontendul afișează în
  `Europe/Bucharest`.
- Coordonate în obiecte `{ "lat": 47.16, "lon": 27.59 }`. Geometriile sunt GeoJSON (ordinea
  `[lon, lat]`).
- Erori: `{"error": {"code": "...", "message": "mesaj în română", "fields": [...]}}`.
- Texte vizibile (etichete, culori, recomandări): vin din backend, în română.
- Tipuri TS: generate din `docs/openapi.json` cu `openapi-typescript`. Fișierul se regenerează la
  fiecare schimbare de contract și se versionează.

## 12. Testare

- **Unit** (fără rețea și fără DB): EAQI, QA senzori, grilă, scor de expunere, alegerea traseelor,
  reguli de raport, clasificatorul fără LLM, normalizarea surselor din fișiere-fixture reale.
- **Integrare** (PostGIS real în Docker): rapoarte, voturi, ciorne, cote, interogări spațiale,
  endpointuri prin `httpx.AsyncClient`.
- **Agent:** client LLM fals, care întoarce apeluri de unelte scriptate; verificăm cardurile,
  evenimentele SSE, căderea pe rezervă și cotele.
- **Contract:** testul compară `openapi.json` generat cu cel versionat; orice diferență trebuie
  asumată explicit.
- **Smoke** pe server: un script care creează un dispozitiv, citește harta, cere un traseu, trimite un
  mesaj agentului, pregătește și publică un raport, apoi îl rezolvă.
- Rulare locală: `docker compose -f deploy/compose.dev.yaml up db graphhopper`, `uv run pytest`,
  `uv run ruff check`.

## 13. Planul până vineri

| Zi | Backend | Frontend (chat separat) |
|---|---|---|
| Luni 9 | acest document + contractul | — |
| Marți 10 | schelet, DB, migrări, dispozitive, rapoarte + ciorne + voturi, erori, `/config`, prototip GraphHopper, **primul deploy** | pornește pe contract: structură nouă, hartă, client API generat |
| Miercuri 11 | surse de aer, QA, grilă, `/map`, `/air/*`, locuri, trasee + scor | harta aerului, planificator, carduri trasee |
| Joi 12 | agent (unelte, SSE, Groq/OpenRouter, cote, reguli), admin, smoke | chatul cu agentul și cardurile, rapoarte |
| Vineri 13 | doar reparații și verificare pe telefon | doar reparații |

Fiecare zi se încheie cu un deploy pe server.

**Ce faci tu:** LXC-ul cu Docker; cheile Groq, OpenRouter și OpenAQ (toate gratuite); proxy host-ul
`ecopulse.bcbeno.me` în nginx-proxy-manager.

## 14. Decizii deschise

1. ~~Benzile EAQI exacte~~ — verificate pe pagina EEA (secțiunea 7).
2. **Modelele Groq și OpenRouter** — joi, după un test în română cu tool calling.
3. ~~GraphHopper vs Valhalla~~ — **GraphHopper**, confirmat de prototip (secțiunea 8).
4. **Cum ajunge codul pe server** — tu faci push pe GitHub și LXC-ul face pull, sau copiem direct.
   Eu nu fac push.
5. **Gravitatea pe 3 niveluri** (mică / medie / mare) în loc de 1–5 — propunerea mea, mai ușor de
   ales pentru oameni și mai consecventă pentru LLM.

## Anexă: ce am păstrat din proiectele vechi

- Endpointurile și formatele surselor (vechiul `app/integrations/`, `EPH2/docs/source-integrations.md`).
- Clasificatorul pe cuvinte-cheie și durata pe categorii (vechiul `quick_report.py`, `reports.py`).
- De la Mihail (`air-quality-service`): retenția datelor, cheia admin închisă implicit.
- Atribuiri obligatorii: © OpenStreetMap contributors (ODbL), Sensor.Community (ODbL/DbCL),
  OpenAQ + furnizorii originali, Copernicus CAMS + Open-Meteo (CC BY 4.0).
