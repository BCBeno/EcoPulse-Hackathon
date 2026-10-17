import re

from app.integrations.http import ProviderError
from app.services.assistant_reporting import mentions_reporting, normalized
from app.services.journeys import calculate


def local_reply(content):
    text = normalized(content).strip(" \n\t.!?,")
    message = None
    if re.fullmatch(r"(salut|buna|buna ziua|buna seara|hei|hello|hi|neata)( ecopulse)?", text):
        message = "Salut! Te pot ajuta să raportezi un incident, să compari rute sau să verifici sursele de mediu. Ce vrei să facem?"
    elif re.fullmatch(r"(multumesc|mersi|merci|ok|bine|super|perfect)( mult| frumos)?", text):
        message = "Cu plăcere! Spune-mi dacă mai ai nevoie de ajutor."
    elif re.fullmatch(r"(nu|anuleaza|renunta|lasa|nu mai vreau)( raportarea)?", text):
        message = "Am oprit cererea în curs. Nu am publicat o raportare nouă."
    elif mentions_reporting(content):
        message = "Pentru o raportare, spune ce ai observat și unde: de exemplu «Vreau să raportez fum aici». Voi solicita locația browserului înainte de publicare. Nu am publicat nimic din acest mesaj."
    elif not re.search(
        r"\b(rut\w*|trase\w*|drum\w*|ocol\w*|compar\w*|surs\w*|sursel\w*|senzor\w*|date\w*|aer\w*|poluar\w*|vrem\w*|meteo|temperatur\w*|vant\w*|lucrar\w*|santier\w*|calator\w*|salvat\w*)\b",
        text,
    ):
        message = "Te pot ajuta cu raportări, rute și date de mediu. Spune-mi concret ce vrei să verific sau să raportez."
    if message:
        return {"content": message, "evidence": [], "engine": "deterministic", "tools_used": []}
    return None


class DeterministicAssistant:
    """Grounded Romanian fallback; no generated external claims or unrestricted tools."""

    async def answer(self, db, payload, settings, http):
        conversational = local_reply(payload.content)
        if conversational is not None:
            return conversational
        if not re.search(r"\b(rut\w*|trase\w*|drum\w*|ocol\w*|compar\w*)\b", normalized(payload.content)):
            return {
                "content": "Consultă fila Surse pentru măsurătorile și prognozele disponibile. Explicația automată nu este disponibilă acum; nu am recalculat traseul.",
                "evidence": [],
                "engine": "deterministic",
                "tools_used": [],
            }
        if payload.route_request is None:
            return {
                "content": "Alege originea, destinația, modul de deplasare și ora plecării pentru a compara rutele pe baza datelor disponibile. Raportările comunitare, senzorii, prognozele regionale și lucrările verificate sunt surse distincte.",
                "evidence": [],
                "engine": "deterministic",
                "llm_status": "not_configured",
            }
        try:
            result = await calculate(db, payload.route_request, settings, http)
        except ProviderError as exc:
            return {
                "content": "Rutarea nu este disponibilă în acest moment. Nu pot recomanda un traseu fără rute valide de la furnizor.",
                "evidence": [],
                "engine": "deterministic",
                "provider_status": exc.code,
                "llm_status": "not_configured",
            }
        routes = result["routes"]
        recommended = next(r for r in routes if r["id"] == result["recommended_route_id"])
        content = f"Ruta {recommended['id']}: {recommended['distance_m'] / 1000:.1f} km, aproximativ {recommended['duration_s'] / 60:.0f} minute; {recommended['additional_time_s'] / 60:.0f} minute suplimentare față de cea mai rapidă. Încredere: {recommended['confidence']}. "
        if "avoids_incidents" in recommended["perspectives"]:
            content += recommended["explanation"] + " "
        content += (
            "Există indicii locale pentru o expunere potențial mai redusă. "
            if "lower_exposure" in recommended["perspectives"]
            else "Nu există suficiente date pentru o estimare precisă a expunerii pe fiecare stradă. "
        )
        content += (
            "Prognozele regionale nu măsoară aerul la nivel de stradă. Nu pot garanta o rută fără poluare."
        )
        return {
            "content": content,
            "evidence": recommended["evidence"],
            "routes": routes,
            "engine": "deterministic",
            "llm_status": "not_configured",
        }


async def answer_with_optional_llm(db, user, payload, settings, http):
    conversational = local_reply(payload.content)
    if conversational is not None:
        return conversational
    if settings.llm_provider not in {"openai", "openrouter"}:
        return await DeterministicAssistant().answer(db, payload, settings, http)
    from app.integrations.llm import OpenAIResponsesProvider
    from app.integrations.openrouter import OpenRouterProvider
    from app.services.assistant_tools import backend_tools

    try:
        provider = OpenRouterProvider if settings.llm_provider == "openrouter" else OpenAIResponsesProvider
        selection, results = await provider(settings, http).select(
            payload.content, backend_tools(db, user, payload, settings, http)
        )
        evidence = [
            e
            for result in results.values()
            for e in result.get("evidence", [])
            if e["id"] in selection.evidence_ids
        ]
        evidence = list({e["id"]: e for e in evidence}.values())
        if selection.focus == "routes":
            answer = await DeterministicAssistant().answer(db, payload, settings, http)
            answer["llm_status"], answer["engine"] = (
                "tool_selection_completed",
                "llm_tools_with_deterministic_explanation",
            )
            return answer
        labels = {
            "sources": "Surse de mediu",
            "weather": "Context meteo",
            "works": "Lucrări oficiale verificate",
            "journeys": "Călătorii salvate",
        }
        content = f"{labels[selection.focus]}: "
        if selection.focus == "journeys":
            content += f"{results.get('get_saved_journeys', {}).get('count', 0)} călătorii recuperate pentru contul tău."
        elif evidence:
            content += f"{len(evidence)} înregistrări disponibile. Sursele, valorile și orele sunt enumerate mai jos. Acestea nu garantează condițiile pe fiecare stradă."
        else:
            content += "nu există suficiente date verificate în rezultatul disponibil."
        return {
            "content": content,
            "evidence": evidence,
            "engine": "llm_tools_with_deterministic_explanation",
            "llm_status": "tool_selection_completed",
            "tools_used": list(results),
        }
    except ProviderError as exc:
        answer = await DeterministicAssistant().answer(db, payload, settings, http)
        answer["llm_status"] = exc.code
        return answer
