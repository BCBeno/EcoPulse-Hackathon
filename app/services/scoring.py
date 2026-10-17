import math
from datetime import UTC, datetime, timedelta

from shapely.affinity import scale
from shapely.geometry import LineString, Point, shape


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


WORK_VERIFICATION_MAX_AGE = timedelta(days=7)


def active_report(report, departure):
    """An open community report that is valid at the departure time."""
    return report.status in {"pending", "confirmed"} and (
        aware(report.observed_at) <= aware(departure) < aware(report.expires_at)
    )


def active_work(work, departure, now=None):
    """A verified, located, recently reviewed planned work overlapping the departure time."""
    now = now or datetime.now(UTC)
    return (
        work.verification == "verified"
        and work.status in {"active", "scheduled"}
        and work.latitude is not None
        and work.longitude is not None
        and bool(work.starts_at and work.ends_at and work.last_verified_at)
        and aware(work.starts_at) <= aware(departure) <= aware(work.ends_at)
        and now - aware(work.last_verified_at) <= WORK_VERIFICATION_MAX_AGE
    )


def projected(geometry, latitude):
    longitude_scale = math.cos(math.radians(latitude))
    return scale(geometry, xfact=111320 * longitude_scale, yfact=111320, origin=(0, 0))


class EnvironmentalRouteScoringService:
    """Relative heuristic burden; not AQI, dosage or a health-risk calculation."""

    POLLUTANTS = {"pm25": 15, "pm10": 45, "no2": 25, "o3": 100, "so2": 40, "co": 4000}

    def score(self, route, departure, observations=(), reports=(), features=(), works=(), weather=()):
        departure = aware(departure)
        now = datetime.now(UTC)
        line = LineString(route["geometry"]["coordinates"])
        latitude = line.centroid.y
        metric = projected(line, latitude)
        evidence, seen, independent_local = [], set(), set()
        # Select the closest valid time per physical sensor and pollutant.
        latest = {}
        for observation in observations:
            if (
                observation.value is None
                or observation.parameter not in self.POLLUTANTS
                or observation.unit != "µg/m³"
            ):
                continue
            forecast = observation.category == "MODEL_FORECAST"
            valid_time, retrieved = aware(observation.observed_at), aware(observation.retrieved_at)
            age = (departure - valid_time).total_seconds()
            if forecast:
                if abs(age) > 3600 or (now - retrieved).total_seconds() > 21600:
                    continue
            elif age < -300 or age > 7200 or abs((departure - now).total_seconds()) > 7200:
                continue
            distance = metric.distance(
                projected(Point(observation.longitude, observation.latitude), latitude)
            )
            if distance > (20000 if forecast else 1000):
                continue
            sensor = observation.canonical_sensor_id or f"{observation.provider}:{observation.sensor_id}"
            key = (sensor, observation.parameter, observation.category)
            if key not in latest or abs(age) < latest[key][0]:
                latest[key] = (abs(age), observation, distance)
        for _, observation, distance in latest.values():
            sensor = observation.canonical_sensor_id or f"{observation.provider}:{observation.sensor_id}"
            canonical = (sensor, observation.parameter, aware(observation.observed_at))
            if canonical in seen:
                continue
            seen.add(canonical)
            if observation.category in {"OFFICIAL_MEASUREMENT", "LOW_COST_SENSOR_MEASUREMENT"}:
                station = observation.provenance.get("location_id")
                independent_local.add(
                    observation.canonical_sensor_id
                    or (f"{observation.provider}:location:{station}" if station else sensor)
                )
            weight = {
                "OFFICIAL_MEASUREMENT": 1.0,
                "LOW_COST_SENSOR_MEASUREMENT": 0.45,
                "MODEL_FORECAST": 0.25,
            }.get(observation.category, 0)
            if observation.category != "MODEL_FORECAST":
                weight *= max(0.1, 1 - distance / 1200)
            burden = min(100, observation.value / self.POLLUTANTS[observation.parameter] * 25)
            evidence.append(
                {
                    "id": observation.id,
                    "category": observation.category,
                    "provider": observation.provider,
                    "sensor_id": observation.sensor_id,
                    "parameter": observation.parameter,
                    "value": observation.value,
                    "unit": observation.unit,
                    "timestamp": aware(observation.observed_at).isoformat(),
                    "retrieved_at": aware(observation.retrieved_at).isoformat(),
                    "distance_m": round(distance),
                    "weight": round(weight, 3),
                    "burden": burden,
                    "provenance": observation.provenance,
                }
            )
        for report in reports:
            if not active_report(report, departure):
                continue
            distance = metric.distance(projected(Point(report.longitude, report.latitude), latitude))
            if distance > 150:
                continue
            independent_local.add(f"community:{getattr(report, 'creator_id', report.id)}")
            evidence.append(
                {
                    "id": report.id,
                    "category": "COMMUNITY_REPORT",
                    "provider": "EcoPulse community",
                    "timestamp": aware(report.observed_at).isoformat(),
                    "distance_m": round(distance),
                    "weight": 0.55 if report.status == "confirmed" else 0.2,
                    "burden": report.severity * 12,
                    "description": report.description,
                }
            )
        for feature in features:
            if (now - aware(feature.retrieved_at)).total_seconds() > 172800:
                continue
            geometry = (
                shape(feature.geometry) if feature.geometry else Point(feature.longitude, feature.latitude)
            )
            distance = metric.distance(projected(geometry, latitude))
            if distance <= 100:
                evidence.append(
                    {
                        "id": feature.id,
                        "category": "OSM_MAPPED_FEATURE",
                        "provider": "OpenStreetMap",
                        "timestamp": aware(feature.retrieved_at).isoformat(),
                        "weight": 0.15,
                        "burden": 20,
                        "description": "Mapped construction; activity and closure unverified",
                        "provenance": feature.provenance,
                    }
                )
        for work in works:
            if not active_work(work, departure, now):
                continue
            distance = metric.distance(projected(Point(work.longitude, work.latitude), latitude))
            if distance <= 150:
                independent_local.add(f"work:{work.source_id}")
                evidence.append(
                    {
                        "id": work.id,
                        "category": "OFFICIAL_PLANNED_WORK",
                        "provider": work.original_url,
                        "timestamp": aware(work.last_verified_at).isoformat(),
                        "weight": 0.85,
                        "burden": 55,
                        "description": work.title,
                    }
                )
        pollution = [
            e
            for e in evidence
            if e["category"] in {"OFFICIAL_MEASUREMENT", "LOW_COST_SENSOR_MEASUREMENT", "MODEL_FORECAST"}
        ]
        weights = sum(x["weight"] for x in pollution)
        baseline = sum(x["weight"] * x["burden"] for x in pollution) / weights if weights else 0
        # Incidents add a bounded penalty; a low-severity incident must never dilute pollution burden.
        penalties = sum(e["weight"] * e["burden"] for e in evidence if e not in pollution)
        burden = min(100, baseline + min(60, penalties)) if evidence else None
        local = len(independent_local)
        confidence = "low" if local < 2 else "medium"
        warnings = ["Relative heuristic score; not a measured personal exposure or health guarantee."]
        if not evidence:
            warnings.append("No sufficiently recent and spatially relevant environmental evidence.")
        if any(x["category"] == "MODEL_FORECAST" for x in evidence):
            warnings.append("CAMS is a regional baseline (~11 km); it cannot resolve individual streets.")
        if local < 2:
            warnings.append("Local coverage is limited; environmental ranking is uncertain.")
        context = [
            x
            for x in weather
            if abs((aware(x.observed_at) - departure).total_seconds()) <= 3600
            and (now - aware(x.retrieved_at)).total_seconds() <= 21600
            and x.value is not None
        ]
        return {
            **route,
            "environmental_score": round(100 - burden, 1) if burden is not None else None,
            "confidence": confidence,
            "evidence": evidence,
            "weather_context": [
                {
                    "id": x.id,
                    "parameter": x.parameter,
                    "value": x.value,
                    "unit": x.unit,
                    "timestamp": aware(x.observed_at).isoformat(),
                    "provenance": x.provenance,
                }
                for x in context
            ],
            "warnings": warnings,
            "explanation": "Scor relativ bazat pe sursele enumerate; acoperirea locală este limitată."
            if evidence
            else "Nu există suficiente date pentru o estimare de mediu.",
        }

    def compare(self, routes):
        fastest = min(routes, key=lambda r: r["duration_s"])
        for route in routes:
            route["additional_time_s"] = route["duration_s"] - fastest["duration_s"]
            route["perspectives"] = ["fastest"] if route is fastest else []
        # A regional baseline alone must not determine the preferred street route.
        eligible = [
            r
            for r in routes
            if r["environmental_score"] is not None
            and any(e["category"] not in {"MODEL_FORECAST", "OSM_MAPPED_FEATURE"} for e in r["evidence"])
        ]
        if (
            len(eligible) >= 2
            and max(r["environmental_score"] for r in eligible)
            - min(r["environmental_score"] for r in eligible)
            > 0.5
        ):
            min(eligible, key=lambda r: (-r["environmental_score"], r["duration_s"]))["perspectives"].append(
                "lower_exposure"
            )
            min(
                eligible,
                key=lambda r: (
                    (100 - r["environmental_score"]) + 30 * (r["duration_s"] / fastest["duration_s"] - 1)
                ),
            )["perspectives"].append("balanced")
        return routes
