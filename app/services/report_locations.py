"""Resolve report locations without substituting device GPS for a named place."""

import re
import unicodedata
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException

from app.integrations.http import ProviderError
from app.schemas.domain import Coordinates


def normalize(text):
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


def named_location(description, current):
    query_text = current if re.match(r"^(la|pe|in|în|langa|lângă)\s+", current, re.I) else description
    match = re.search(r"\b(?:la|pe|în|in|lângă|langa)\s+(.+)", query_text, re.I)
    if not match:
        return None
    query = re.sub(r"\.\s+(fum\w*|praf|pulberi)\s*$", "", match[1], flags=re.I).strip(" .!?,;")
    if normalize(query) in {"locatia mea", "locatia curenta", "mine"}:
        return None
    return query[:200] if len(query) >= 3 else None


def waiting(status, content, description, **details):
    return (
        None,
        None,
        None,
        {
            "content": content,
            "evidence": [],
            "engine": "deterministic_tools",
            "tools_used": [],
            "action": {
                "type": "create_report",
                "status": status,
                "context": {"description": description, "awaiting": "location"},
                **details,
            },
        },
    )


async def resolve_location(payload, description, history, settings, geocoder):
    query = named_location(description, payload.content)
    current = normalize(payload.content).strip(" .!,")
    if current in {"aici", "locatia curenta", "la locatia curenta", "locatia mea"}:
        query = None
    elif (
        history
        and history[-1].get("action", {}).get("context", {}).get("awaiting") == "location"
        and not re.search(r"\b(fum\w*|praf|pulberi|raport\w*|santier\w*)\b", current)
        and not re.match(r"^(la|pe|in|langa)\s+", current)
    ):
        query = payload.content.strip(" .!,")[:200]
    s, w, n, e = settings.bounds
    if payload.location_candidate_id:
        previous = next(
            (
                m.get("action", {})
                for m in reversed(history)
                if m.get("role") == "assistant"
                and m.get("client_message_id") == str(payload.client_message_id)
            ),
            {},
        )
        candidate = next(
            (p for p in previous.get("candidates", []) if p["id"] == str(payload.location_candidate_id)), None
        )
        if previous.get("status") != "needs_location_choice" or candidate is None:
            raise HTTPException(409, "Select a location offered for this message")
        point = Coordinates.model_validate(candidate)
        return point, datetime.now(UTC), candidate["label"], None
    if query:
        if geocoder is None:
            return waiting(
                "needs_details",
                "Căutarea adreselor nu este disponibilă. Încearcă o adresă mai exactă sau spune «aici» pentru locația browserului.",
                description,
            )
        try:
            items = await geocoder.search(query)
        except ProviderError:
            return waiting(
                "needs_details",
                "Nu pot căuta adresa în acest moment. Nu am publicat nimic. Încearcă din nou cu adresa exactă.",
                description,
            )
        candidates = []
        for item in items:
            try:
                point = Coordinates.model_validate(item)
            except ValueError:
                continue
            if s <= point.latitude <= n and w <= point.longitude <= e:
                candidate = {"id": str(uuid4()), "label": item.get("label") or query, **point.model_dump()}
                if not any(
                    abs(p["latitude"] - point.latitude) < 0.00001
                    and abs(p["longitude"] - point.longitude) < 0.00001
                    for p in candidates
                ):
                    candidates.append(candidate)
        if not candidates:
            return waiting(
                "needs_details",
                f"Nu am găsit «{query}» în zona Iași. Scrie strada, numărul sau un reper mai exact. Nu am publicat nimic.",
                description,
            )
        if len(candidates) == 1:
            return Coordinates.model_validate(candidates[0]), datetime.now(UTC), candidates[0]["label"], None
        return waiting(
            "needs_location_choice",
            f"Am găsit mai multe locuri pentru «{query}». Alege punctul incidentului de mai jos sau scrie o adresă mai exactă. Coordonatele raportării vor fi publice.",
            description,
            candidates=candidates[:5],
        )
    if not re.search(r"\b(aici|locatia (mea|curenta)|unde (sunt|ma aflu))\b", normalize(description)):
        return waiting(
            "needs_details",
            "Unde ai observat incidentul? Scrie adresa sau reperul, de exemplu «la Parcul Copou», ori «aici» pentru locația curentă.",
            description,
        )
    location = payload.current_location
    if location is None:
        return waiting(
            "needs_location",
            "Am înțeles raportarea. Permite accesul la locația curentă în browser pentru a o publica aici. Coordonatele incidentului vor fi publice.",
            description,
            code="location_required",
        )
    age = (datetime.now(UTC) - location.captured_at).total_seconds()
    if age < -30 or age > 120 or location.accuracy_m > 200:
        return waiting(
            "needs_location",
            "Locația este prea veche sau imprecisă. Încearcă din nou cu localizarea activă. Nu am publicat nimic.",
            description,
            code="location_unreliable",
        )
    return (
        location,
        location.captured_at,
        f"locația ta curentă ({location.latitude:.5f}, {location.longitude:.5f}, precizie aproximativ {location.accuracy_m:.0f} m)",
        None,
    )
