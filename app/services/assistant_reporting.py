"""Explicit reporting intents invoke a bounded tool, without an LLM dependency."""

import hashlib
import re

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.entities import AssistantAction
from app.schemas.domain import ReportCreate
from app.services.report_locations import normalize as normalized
from app.services.report_locations import resolve_location
from app.services.reports import create_report


def one_edit_apart(word, target):
    if word == target:
        return True
    if len(word) == len(target):
        different = [i for i, (a, b) in enumerate(zip(word, target, strict=True)) if a != b]
        return len(different) == 1 or (
            len(different) == 2
            and different[1] == different[0] + 1
            and word[different[0]] == target[different[1]]
            and word[different[1]] == target[different[0]]
        )
    if abs(len(word) - len(target)) == 1:
        longer, shorter = (word, target) if len(word) > len(target) else (target, word)
        return any(longer[:i] + longer[i + 1 :] == shorter for i in range(len(longer)))
    return False


def intent_text(content):
    def correct(match):
        word = match.group()
        if not word.startswith(("rap", "semn")):
            return word
        return next(
            (
                verb
                for verb in ("raportez", "raporteaza", "raportati", "semnalez", "semnaleaza")
                if one_edit_apart(word, verb)
            ),
            word,
        )

    return re.sub(r"\b\w+\b", correct, normalized(content))


def mentions_reporting(content):
    return bool(re.search(r"\b(rapor\w*|rapot\w*|semnal\w*)\b", intent_text(content)))


def blocked_reporting(content):
    text = normalized(content)
    return bool(
        re.search(r"\b(nu|nici|daca|cum|exemplu|exemple|ipotetic|test|testeaza|anuleaza|maine|ieri)\b", text)
    ) or any(c in text for c in ('"', "„", "”", "«", "»", "`"))


def reporting_intent(content):
    text = intent_text(content)
    if blocked_reporting(content):
        return False
    if (
        "?" not in text
        and (
            re.match(r"^\s*(este|e|observ|vad|simt|se vede|se simte)\b", text)
            or re.search(r"\b(aici|la|pe|in|langa)\b", text)
        )
        and re.search(
            r"^\s*(?:(?:este|e|observ|vad|simt|se vede|se simte)\s+)?(?:mult\s+)?(?:fum\w*|praf|pulberi|santier\w*|demolar\w*|utilaj\w*)\b",
            text,
        )
    ):
        return True
    return bool(re.search(r"\b(raport\w*|semnal\w*)\b", text)) and bool(
        re.search(
            r"^\s*(?:(?:salut|buna)[,! .]*)?(?:te rog[,: ]*)?"
            r"(?:(?:vreau|doresc|as vrea)\s+(?:sa\s+|o\s+)?|(?:poti|puteti)\s+(?:sa\s+)?)?"
            r"(?:raporteaza|raportez|raportati|raportare|semnaleaza|semnalez|adauga|adaug|publica|public|trimite|trimit|creeaza|creez)\b",
            text,
        )
    )


def response(status, content, **details):
    return {
        "content": content,
        "evidence": [],
        "engine": "deterministic_tools",
        "tools_used": ["create_pollution_report"] if status == "created" else [],
        "action": {"type": "create_report", "status": status, **details},
    }


async def completed_action(db, owner_id, message_id, fingerprint):
    action = await db.scalar(
        select(AssistantAction).where(
            AssistantAction.owner_id == owner_id,
            AssistantAction.client_message_id == str(message_id),
        )
    )
    if action and action.request_hash != fingerprint:
        raise HTTPException(409, "Message identifier has already been used for a different request")
    return action


def reporting_description(payload, history):
    if reporting_intent(payload.content):
        return payload.content
    if blocked_reporting(payload.content):
        return None
    previous = None
    message_id = str(payload.client_message_id) if payload.client_message_id else None
    for message in reversed(history):
        if (
            message.get("role") == "assistant"
            and message_id
            and message.get("client_message_id") == message_id
        ):
            previous = message
            break
    if previous is None and history:
        last = history[-1]
        if last.get("role") == "assistant" and last.get("action", {}).get("status") in {
            "needs_details",
            "needs_location_choice",
        }:
            previous = last
    action = (previous or {}).get("action", {})
    description = action.get("context", {}).get("description")
    if not description or action.get("status") not in {
        "needs_details",
        "needs_location",
        "needs_location_choice",
    }:
        return None
    if (
        previous
        and message_id
        and previous.get("client_message_id") == message_id
        and action.get("status") in {"needs_location", "needs_location_choice"}
    ):
        return description
    # Only short incident/location answers continue a pending report. Greetings and unrelated queries do not.
    text = normalized(payload.content)
    if not re.fullmatch(r"[\w\s,!.-]+", text) or len(text.split()) > 16:
        return None
    if (
        action.get("context", {}).get("awaiting") != "location"
        and not re.search(r"^(la|pe|in|langa)\s+", text)
        and not re.search(
            r"\b(fum\w*|praf|pulberi|santier\w*|demolar\w*|trafic|industr\w*|utilaj\w*|aici|locatia curenta)\b",
            text,
        )
    ):
        return None
    if re.search(
        r"\b(salut|buna|multumesc|mersi|merci|ok|perfect|super|bine|rute|ruta|vreme|surse|cum|ce|de ce)\b",
        text,
    ):
        return None
    combined = f"{description}. {payload.content}"
    return combined if len(combined) <= 2000 else None


async def create_pollution_report(db, user, payload, settings, conversation_id, history=(), *, geocoder=None):
    """Return None for read intents. Commit is owned by the conversation endpoint."""
    description = reporting_description(payload, history)
    if description is None:
        return None
    text = normalized(description)
    context = {"description": description}
    categories = {
        "smoke": r"\bfum\w*\b",
        "dust": r"\b(praf|pulberi)\b",
        "construction": r"\b(santier\w*|demolar\w*|construct\w*)\b",
        "traffic": r"\b(trafic|ambuteiaj\w*)\b",
        "industrial": r"\b(industr\w*|fabrica)\b",
        "machinery": r"\b(utilaj\w*|masinar\w*)\b",
    }
    matches = [key for key, pattern in categories.items() if re.search(pattern, text)]
    # Prefer the observed pollutant to its possible cause (e.g. smoke from a factory).
    category = next((c for c in ("smoke", "dust") if c in matches), None)
    if category is None and len(matches) == 1:
        category = matches[0]
    if not category:
        return response(
            "needs_details",
            "Ce ai observat: fum, praf, șantier sau alt incident? Poți preciza și adresa, de exemplu «fum la Parcul Copou». Nu am publicat nimic.",
            context=context,
        )
    if payload.client_message_id is None:
        raise HTTPException(422, "Reporting requires a client_message_id to prevent duplicates")
    fingerprint = hashlib.sha256(f"{conversation_id}:{payload.content}".encode()).hexdigest()
    owner_id = user.id
    previous = await completed_action(db, owner_id, payload.client_message_id, fingerprint)
    if previous:
        return previous.result
    location, observed_at, location_label, pending = await resolve_location(
        payload, description, history, settings, geocoder
    )
    if pending is not None:
        return pending
    s, w, n, e = settings.bounds
    if not (s <= location.latitude <= n and w <= location.longitude <= e):
        return response(
            "unsupported_location",
            "Locația curentă este în afara zonei acoperite de EcoPulse. Nu am publicat raportarea.",
        )
    action = AssistantAction(
        owner_id=owner_id,
        client_message_id=str(payload.client_message_id),
        request_hash=fingerprint,
    )
    db.add(action)
    try:
        # Reserve the idempotency key before any report is inserted, also across workers.
        await db.flush()
    except IntegrityError:
        await db.rollback()
        previous = await completed_action(db, owner_id, payload.client_message_id, fingerprint)
        if previous:
            return previous.result
        raise
    report = await create_report(
        db,
        user,
        ReportCreate(
            latitude=location.latitude,
            longitude=location.longitude,
            category=category,
            description=description,
            observed_at=observed_at,
            severity=2,
        ),
        commit=False,
    )
    result = response(
        "created",
        f"Am publicat raportarea la {location_label}. Este o observație comunitară, în așteptarea confirmărilor. Harta și rutele se actualizează. Severitate inițială: 2/5.",
        report=report,
    )
    action.result = jsonable_encoder(result)
    return action.result
