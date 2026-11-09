# EcoPulse — contractul API (v1)

> Contractul dintre backend și frontend. Deciziile și motivele sunt în [design.md](design.md).
> Stare: **aprobat 2026-11-09** · Se schimbă doar prin chatul de backend. Pentru ce e deja implementat,
> sursa exactă e [openapi.json](openapi.json) (generat), din care frontendul își generează tipurile.
>
> **Implementat:** sistem (`/health`, `/health/ready`, `/config`), dispozitive, rapoarte (inclusiv ciorne,
> voturi, rezolvare, semnalări), `/map`, `/air/point`, `/air/forecast`.
> **Urmează:** locuri, trasee, agent, admin.

## Convenții

- Bază: `/api/v1` (în producție `https://ecopulse.bcbeno.me/api/v1`; în dezvoltare
  `http://localhost:8000/api/v1`).
- Autentificare: `Authorization: Bearer <token>` (vezi `POST /devices`). Endpointurile marcate 🔓
  merg și fără token; dacă token-ul e trimis, răspunsul poate conține câmpuri personalizate (`viewer`).
- Ore: ISO 8601 UTC (`"2026-11-10T07:45:00Z"`). Coordonate: `{"lat": 47.1585, "lon": 27.6014}`.
  Geometrii: GeoJSON, ordinea `[lon, lat]`.
- `GET /config` și `GET /map` au `ETag`. Trimite `If-None-Match` și vei primi `304` dacă nu s-a
  schimbat nimic.
- Paginare: `?limit=` (implicit 50, maximum 200) și `?cursor=` (opac, din `next_cursor`).

### Erori
```json
{
  "error": {
    "code": "outside_city",
    "message": "Locația e în afara zonei acoperite de EcoPulse (Iași).",
    "fields": [{"field": "location.lat", "message": "În afara zonei"}]
  }
}
```
`fields` apare doar la `422`. Codurile HTTP folosite: 400, 401, 403, 404, 409, 422, 429 (cu
`Retry-After`), 503 (sursă sau furnizor indisponibil).

| `code` | HTTP | Când |
|---|---|---|
| `validation_error` | 422 | date de intrare invalide |
| `outside_city` | 422 | coordonate în afara bbox-ului Iași |
| `unauthorized` | 401 | token lipsă sau invalid |
| `forbidden` | 403 | acțiune nepermisă (ex. votezi propriul raport) |
| `not_found` | 404 | resursa nu există sau e ascunsă |
| `conflict` | 409 | ciornă deja publicată, raport închis etc. |
| `rate_limited` | 429 | prea multe cereri |
| `quota_exceeded` | 429 | cota zilnică a agentului e epuizată |
| `routing_unavailable` | 503 | GraphHopper nu răspunde |
| `no_route` | 422 | nu există traseu între puncte |

---

## Sistem

### `GET /health` 🔓
`{"status": "ok"}` — doar verifică faptul că procesul rulează.

### `GET /health/ready` 🔓
```json
{
  "status": "ok",
  "checks": {
    "db": "ok",
    "graphhopper": "ok",
    "llm": "ok",
    "sources": {
      "sensor_community": {"last_success": "2026-11-10T07:40:00Z", "age_s": 300, "status": "ok"},
      "openaq": {"last_success": null, "age_s": null, "status": "not_configured"}
    }
  }
}
```
`status`: `ok` | `degraded` (o sursă are probleme) | `down` (DB sau API).

### `GET /config` 🔓 (ETag)
Tot ce are nevoie frontendul ca să nu dubleze constante.
```json
{
  "city": {
    "name": "Iași",
    "timezone": "Europe/Bucharest",
    "center": {"lat": 47.1585, "lon": 27.6014},
    "bbox": {"south": 47.05, "west": 27.40, "north": 47.26, "east": 27.75}
  },
  "aqi_levels": [
    {"level": 1, "key": "good", "label": "Bun", "color": "#50F0E6",
     "advice": "Calitatea aerului e bună. Ieși liniștit."},
    {"level": 2, "key": "fair", "label": "Acceptabil", "color": "#50CCAA", "advice": "…"},
    {"level": 3, "key": "moderate", "label": "Moderat", "color": "#F0E641", "advice": "…"},
    {"level": 4, "key": "poor", "label": "Slab", "color": "#FF5050", "advice": "…"},
    {"level": 5, "key": "very_poor", "label": "Foarte slab", "color": "#960032", "advice": "…"},
    {"level": 6, "key": "extremely_poor", "label": "Extrem de slab", "color": "#7D2181", "advice": "…"}
  ],
  "aqi_note": "Nivel orientativ, calculat pe valoarea orară.",
  "report_categories": [
    {"id": "smoke", "label": "Fum", "icon": "smoke", "affects": "air",
     "duration_h": 6, "radius_m": 400}
  ],
  "severities": [
    {"value": 1, "label": "Mică"}, {"value": 2, "label": "Medie"}, {"value": 3, "label": "Mare"}
  ],
  "report_rules": {"gone_votes_to_resolve": 3, "description_max": 600, "text_max": 1000},
  "modes": [{"id": "walk", "label": "Pe jos"}, {"id": "bike", "label": "Bicicletă"}],
  "route_kinds": [
    {"id": "cleanest", "label": "Cel mai curat"},
    {"id": "balanced", "label": "Echilibrat"},
    {"id": "fastest", "label": "Cel mai rapid"}
  ],
  "forecast_horizon_h": 72,
  "map": {"poll_interval_s": 60},
  "agent": {"enabled": true, "daily_messages": 30},
  "attributions": [
    {"label": "© OpenStreetMap contributors", "url": "https://www.openstreetmap.org/copyright"},
    {"label": "Sensor.Community", "url": "https://sensor.community/"},
    {"label": "OpenAQ și furnizorii originali", "url": "https://openaq.org/"},
    {"label": "Copernicus CAMS / Open-Meteo (CC BY 4.0)", "url": "https://open-meteo.com/"}
  ]
}
```
`affects`: `air` | `obstacle` | `air_obstacle` | `info`. Culorile EAQI sunt cele oficiale EEA (de
confirmat odată cu benzile). Iconițele sunt chei; frontendul le mapează pe lucide-react.

---

## Dispozitive (identitate anonimă)

### `POST /devices` 🔓
Fără corp. Limitat la 20/oră per IP. Se apelează o singură dată, la prima deschidere a aplicației.
```json
// 201
{"device_id": "0193…", "token": "q8sV…(43 caractere)", "created_at": "2026-11-10T07:45:00Z"}
```

### `GET /devices/me`
```json
{
  "device_id": "0193…",
  "role": "user",
  "created_at": "2026-11-10T07:45:00Z",
  "agent_quota": {"limit": 30, "used": 4, "resets_at": "2026-11-10T22:00:00Z"}
}
```
`resets_at` = miezul nopții, ora României, exprimat în UTC.

---

## Harta

### `GET /map?at=` 🔓 (ETag)
Tot ce se desenează pe hartă, într-un singur apel, pentru polling la `poll_interval_s`.
`at` lipsă = acum; `at` în viitor (≤ 72 h) = grila de prognoză. Senzorii și rapoartele sunt mereu
cele de acum.
```json
{
  "generated_at": "2026-11-10T07:45:12Z",
  "at": "2026-11-10T07:45:00Z",
  "kind": "now",
  "grid": {
    "valid_at": "2026-11-10T07:45:00Z",
    "south": 47.05, "west": 27.40, "d_lat": 0.00225, "d_lon": 0.0033,
    "rows": 93, "cols": 106,
    "layers": {
      "eaqi": [2, 2, 3, null, "…rows*cols valori, rând cu rând, de la sud-vest spre est apoi spre nord"],
      "pm25": [12.4, 13.1, "…"],
      "confidence": [0.9, 0.85, "…"]
    }
  },
  "sensors": [ /* Sensor */ ],
  "reports": [ /* Report (fără viewer) */ ],
  "construction": [
    {"id": "way/123456", "geometry": {"type": "Polygon", "coordinates": [[[27.58, 47.16], "…"]]},
     "name": "Șantier pasaj", "source": "osm"}
  ],
  "sources": {
    "sensor_count": 34, "official_count": 2,
    "newest_reading_at": "2026-11-10T07:42:00Z",
    "model": "CAMS Europe (~11 km)"
  }
}
```
- Indexul celulei: `i = row * cols + col`; centrul celulei:
  `lat = south + (row + 0.5) * d_lat`, `lon = west + (col + 0.5) * d_lon`.
- `null` = fără date în celula respectivă (nu „aer curat”).
- `rows`/`cols` se iau din răspuns (acum 94 × 107); nu le fixa în frontend.
- `eaqi` are valori întregi 1–6; `pm25` are o zecimală; `confidence` are două zecimale.
- `at` din răspuns e momentul grilei (stabil între două recalculări), ca ETag-ul să rămână același.
- `sources.last_success` dă ultima sincronizare reușită a fiecărei surse.
- Stațiile care măsoară doar vremea (fără particule) nu apar în `sensors`.
- `kind`: `now` | `forecast`. La `forecast`, lista `sensors` e aceeași, dar frontendul ar trebui să o
  atenueze vizual.

**Sensor**
```json
{
  "id": "0193…",
  "provider": "sensor_community",
  "kind": "low_cost",
  "location": {"lat": 47.1621, "lon": 27.5889},
  "readings": {
    "pm25": {"value": 18.2, "observed_at": "2026-11-10T07:40:00Z"},
    "pm10": {"value": 27.0, "observed_at": "2026-11-10T07:40:00Z"},
    "humidity": {"value": 93.0, "observed_at": "2026-11-10T07:40:00Z"}
  },
  "eaqi_level": 3,
  "status": "ok",
  "qa_flags": ["high_humidity"]
}
```
`kind`: `low_cost` | `official`. `status`: `ok` | `stale` | `outlier`. `qa_flags` ⊂
`high_humidity`, `inconsistent`, `outlier`. Mesajele afișate pentru fiecare flag stau în frontend
(sunt doar 3).

---

## Aer

### `GET /air/point?lat=&lon=&at=` 🔓
```json
{
  "location": {"lat": 47.1585, "lon": 27.6014},
  "place_label": "Piața Unirii",
  "at": "2026-11-10T07:45:00Z",
  "kind": "now",
  "eaqi": {"level": 3, "dominant": "pm25"},
  "pollutants": {
    "pm25": {"value": 21.3, "unit": "µg/m³", "level": 3},
    "pm10": {"value": 30.1, "unit": "µg/m³", "level": 2},
    "no2": {"value": 18.0, "unit": "µg/m³", "level": 2, "source": "model"},
    "o3": {"value": 35.0, "unit": "µg/m³", "level": 1, "source": "model"}
  },
  "confidence": 0.8,
  "nearest_sensors": [
    {"id": "0193…", "kind": "low_cost", "distance_m": 340, "pm25": 22.0,
     "observed_at": "2026-11-10T07:40:00Z", "qa_flags": []}
  ],
  "weather": {"temperature_c": 4.2, "humidity_pct": 94, "wind_speed_ms": 1.1,
              "wind_dir_deg": 310, "precipitation_mm": 0.0},
  "notes": ["Umiditate ridicată: senzorii ieftini pot supraestima particulele."],
  "limitation": "Estimare din senzori din apropiere și modelul CAMS (~11 km); nu e o măsurătoare exactă la stradă."
}
```
Eticheta, culoarea și sfatul pentru `level` se iau din `/config`. `source` lipsă = combinat
(senzori + model).

### `GET /air/forecast?lat=&lon=&hours=24` 🔓
`hours` între 1 și 72.
```json
{
  "location": {"lat": 47.1585, "lon": 27.6014},
  "hourly": [
    {"at": "2026-11-10T08:00:00Z", "eaqi_level": 3, "pm25": 21.0, "pm10": 30.0, "no2": 18.0, "o3": 35.0}
  ],
  "best_window": {"start": "2026-11-10T13:00:00Z", "end": "2026-11-10T16:00:00Z", "eaqi_level": 2},
  "source": "CAMS Europe, corectat cu senzorii locali",
  "limitation": "…"
}
```

---

## Locuri

### `GET /places/search?q=&lat=&lon=` 🔓
`q` are cel puțin 2 caractere. `lat`/`lon` (opționale) dau prioritate rezultatelor apropiate.
Frontendul face debounce de 300 ms. Doar în zona Iași.
```json
{
  "items": [
    {"id": "local:podu-ros", "name": "Podu Roș", "label": "Podu Roș, Iași",
     "kind": "neighbourhood", "location": {"lat": 47.1536, "lon": 27.5876}}
  ]
}
```
`kind`: `neighbourhood` | `landmark` | `street` | `address` | `poi`.

### `GET /places/reverse?lat=&lon=` 🔓
`{"label": "Bulevardul Ștefan cel Mare și Sfânt, lângă Palas"}` sau `{"label": null}`.

---

## Trasee

### `POST /routes` 🔓
```json
{
  "origin": {"lat": 47.1647, "lon": 27.5675, "label": "Gara"},
  "destination": {"lat": 47.1810, "lon": 27.5670, "label": "Copou"},
  "mode": "bike",
  "depart_at": null
}
```
`label` e opțional (doar pentru afișare). `depart_at`: `null` = acum, altfel ≤ 72 h în viitor.
Distanța în linie dreaptă între puncte trebuie să fie între 100 m și 30 km.
```json
{
  "depart_at": "2026-11-10T07:45:00Z",
  "mode": "bike",
  "routes": [ /* Route, ordonate: cleanest, balanced, fastest */ ],
  "summary": "Traseul cel mai curat are cu 23 % mai puține particule, pentru 4 minute în plus.",
  "notes": []
}
```

**Route**
```json
{
  "id": "r1",
  "kinds": ["cleanest"],
  "distance_m": 4120,
  "duration_s": 1140,
  "geometry": {"type": "LineString", "coordinates": [[27.5675, 47.1647], "…"]},
  "exposure": {
    "index": 4.1,
    "avg_pm25": 13.0,
    "max_eaqi_level": 3,
    "minutes_poor_or_worse": 0,
    "vs_fastest_pct": -23
  },
  "segments": [{"from": 0, "to": 41, "eaqi_level": 2}, {"from": 41, "to": 88, "eaqi_level": 3}],
  "reports_nearby": ["0193…"],
  "warnings": [],
  "google_maps_url": "https://www.google.com/maps/dir/?api=1&origin=…&destination=…&waypoints=…&travelmode=bicycling"
}
```
- `kinds` poate conține mai multe valori când traseele coincid (ex. `["fastest", "cleanest"]`).
- `segments.from`/`to` sunt indici în `geometry.coordinates`.
- `exposure.index` (µg·h/m³) se compară doar între traseele aceleiași cereri.
- `warnings`, ex.: `"Trece pe lângă un raport activ de fum."`, `"Date puține pe o porțiune a traseului."`.

---

## Rapoarte

**Report**
```json
{
  "id": "0193…",
  "category": "smoke",
  "severity": 3,
  "description": "Fum gros de la arderea frunzelor, pe trotuarul dinspre parc.",
  "location": {"lat": 47.1536, "lon": 27.5876},
  "location_label": "Podu Roș",
  "radius_m": 400,
  "observed_at": "2026-11-10T07:30:00Z",
  "expires_at": "2026-11-10T13:30:00Z",
  "status": "active",
  "votes": {"still_there": 2, "gone": 0},
  "created_at": "2026-11-10T07:31:00Z",
  "viewer": {"is_author": false, "my_vote": null, "can_vote": true, "can_resolve": false}
}
```
`status`: `active` | `expired` | `resolved` (cele `hidden` nu apar public). `viewer` e completat doar
dacă cererea are token; altfel e `null`. Eticheta categoriei se ia din `/config`.

### `GET /reports?status=active&category=&south=&west=&north=&east=&limit=&cursor=` 🔓
`{"items": [Report], "next_cursor": null}`. Implicit: `status=active`, tot orașul.

### `GET /reports/{id}` 🔓
`Report`. Răspunde `404` dacă raportul e ascuns.

### `POST /reports`
Raport direct, fără agent (din formular sau din butoanele rapide).
```json
{
  "category": "dust",
  "severity": 2,
  "description": "Praf mult de la șantier.",
  "location": {"lat": 47.16, "lon": 27.59},
  "location_label": null,
  "observed_at": null
}
```
`observed_at`: `null` = acum, altfel cel mult 48 h în urmă. `201` → `Report`.

### `POST /reports/drafts/{draft_id}/publish`
Butonul „Publică” de pe cardul agentului. Toate câmpurile sunt opționale (corecturi ale
utilizatorului); `location` e obligatoriu dacă ciorna are `needs_location: true`.
```json
{"location": {"lat": 47.1540, "lon": 27.5880}, "category": null, "severity": null, "description": null}
```
`201` → `Report`. `409` dacă ciorna e deja publicată sau anulată (corpul conține `report_id` în
`error.fields` doar pentru publicare dublă).

### `DELETE /reports/drafts/{draft_id}`
Butonul „Anulează” → `204`.

### `POST /reports/{id}/votes`
`{"kind": "still_there"}` sau `{"kind": "gone"}` → `Report` actualizat. Un vot nou înlocuiește
votul anterior al aceluiași dispozitiv. Răspunde `403` pentru propriul raport și `409` dacă raportul
nu mai e activ.

### `POST /reports/{id}/resolve`
Doar autorul (sau adminul) → `Report` cu `status: "resolved"`.

### `POST /reports/{id}/flags`
`{"reason": "spam" | "offensive" | "personal_data" | "wrong"}` → `204`. Repetat: tot `204`.

---

## Agent

### `POST /agent/conversations`
`201` → `{"id": "0193…", "created_at": "…"}`

### `GET /agent/conversations`
`{"items": [{"id": "…", "title": "Fum la Podu Roș", "updated_at": "…"}]}` (ultimele 20).

### `GET /agent/conversations/{id}`
Pentru reîncărcarea chatului.
```json
{
  "id": "0193…",
  "messages": [
    {"id": "…", "role": "user", "text": "e fum mult la podu ros", "cards": [], "created_at": "…"},
    {"id": "…", "role": "assistant", "text": "Am înțeles: …", "cards": [ /* Card */ ], "created_at": "…"}
  ]
}
```
Cardurile vechi își păstrează datele, dar o ciornă publicată între timp apare cu
`status: "published"`.

### `POST /agent/conversations/{id}/messages` → `text/event-stream`
Cerere:
```json
{
  "client_message_id": "7c1e…(uuid generat de frontend)",
  "text": "e fum mult la podu ros",
  "context": {
    "location": {"lat": 47.1539, "lon": 27.5880, "accuracy_m": 25},
    "map_center": {"lat": 47.16, "lon": 27.59},
    "selected_point": null
  }
}
```
- `context` și toate câmpurile din el sunt opționale. Locația GPS se trimite doar dacă utilizatorul a
  dat permisiunea; dacă `accuracy_m > 1000`, backendul o ignoră.
- Același `client_message_id` retrimis (de exemplu după o reconectare) nu consumă cota: backendul
  reia răspunsul salvat.
- Fetch cu `ReadableStream` (EventSource nu suportă POST).

Evenimente (fiecare are `event:` și `data:` JSON):

| `event` | `data` | Când |
|---|---|---|
| `message.start` | `{"message_id": "…"}` | imediat |
| `status` | `{"text": "Caut traseele…"}` | când agentul folosește o unealtă |
| `text.delta` | `{"text": "Am înțeles"}` | bucăți din răspuns |
| `card` | `Card` | când o unealtă a terminat |
| `message.end` | `{"message_id": "…", "engine": "llm" \| "rules", "quota": {"limit": 30, "used": 5}}` | la final |
| `error` | `{"code": "…", "message": "…"}` | eroare; fluxul se închide după |

Pe lângă acestea, la fiecare 15 s vine un comentariu `: ping` (keep-alive). Cota se verifică înainte
de stream: dacă e epuizată, chatul primește răspuns pe reguli, cu `engine: "rules"`, nu o eroare.

**Card** — câmpul `type` decide forma:
```json
{"type": "report_draft", "draft_id": "0193…", "status": "pending",
 "category": "smoke", "severity": 3,
 "description": "Fum gros la Podu Roș.",
 "location": {"lat": 47.1536, "lon": 27.5876}, "location_label": "Podu Roș",
 "location_source": "place", "needs_location": false, "extraction": "llm"}

{"type": "routes", "request": {"origin": {…}, "destination": {…}, "mode": "walk", "depart_at": null},
 "result": { /* exact răspunsul de la POST /routes */ }}

{"type": "air", "point": { /* GET /air/point */ }, "forecast": { /* GET /air/forecast, opțional */ }}

{"type": "reports", "center": {"lat": …, "lon": …}, "items": [ /* Report */ ]}

{"type": "places", "query": "gara", "items": [ /* Place */ ]}
```
- `location_source`: `place` | `gps` | `map` | `none`.
- La cardul `places`, utilizatorul alege o variantă, iar frontendul trimite un mesaj nou cu
  `selected_point` setat și textul variantei alese.

---

## Admin (`X-Admin-Key`)

Nu e folosit de frontendul public; util pentru demo și depanare.

- `GET /admin/sources` — starea surselor + ultimele 20 de rulări.
- `POST /admin/sources/{name}/sync` — rulează o sursă acum (`202`).
- `GET /admin/reports/flagged` — rapoartele semnalate, cu motive.
- `POST /admin/reports/{id}/hide` / `POST /admin/reports/{id}/unhide`.
- `GET /admin/llm/usage?days=7` — consumul pe zile, furnizori și modele.
