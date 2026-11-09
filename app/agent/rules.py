"""The chat without an LLM: recognise reports and air questions with keywords, answer from data.

Used when no provider is configured or reachable, or when the daily quota/budget is used up.
The keyword lists come from the v0 classifier (quick_report.py), cleaned up.
"""

import re
import unicodedata

from app.reports.categories import CATEGORIES


def plain(text: str) -> str:
    text = text.lower().replace("ş", "ș").replace("ţ", "ț")
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


# Ordered: the first match wins (the observed effect before its possible cause).
KEYWORDS = [
    ("smoke", r"\b(fum\w*|arde\w*|ars[ae]?\b|incendi\w*|foc\w*)"),
    ("dust", r"\b(praf\w*|pulber\w*)"),
    ("flooding", r"\b(inunda\w*|balt\w*|baltoac\w*|apa pe (strada|trotuar))"),
    ("lighting", r"\b(iluminat\w*|felinar\w*|bec(uri)? (ars|stricat)\w*|intuneric)"),
    (
        "obstruction",
        r"\b(copac\w* (cazut|rupt)\w*|crengi|blocat\w*|obstacol\w*|masin\w* parcat\w* pe trotuar)",
    ),
    (
        "infrastructure",
        r"\b(groap\w*|gropi|trotuar\w* (spart|rupt|distrus)\w*|asfalt\w*|capac\w*|semafor\w*|bordur\w*)",
    ),
    ("construction", r"\b(santier\w*|demolar\w*|constructi\w*|lucrar\w*)"),
    ("machinery", r"\b(utilaj\w*|excavator\w*|buldozer\w*|macara\w*)"),
    ("traffic", r"\b(trafic\w*|ambuteiaj\w*|coloana de masini|aglomera\w*)"),
    ("industrial", r"\b(industr\w*|fabric\w*|uzin\w*|miros\w*|mirose\w*|chimic\w*)"),
]
SEVERE = (
    r"\b(periculos\w*|grav\w*|urgent\w*|accident\w*|ranit\w*|foarte (mult|mare|gros)\w*|enorm\w*"
    r"|nu se (mai )?vede)"
)
MINOR = r"\b(putin|usor|mica?)\b"
QUESTION_START = r"^(cum|unde|cand|care|ce|cat|e bine|este bine|pot|merita|se poate|exista|sunt)\b"
AIR_WORDS = (
    r"\b(aer\w*|polua\w*|calitat\w*|respir\w*|alerg\w*|ies\b|iesi\w*|plimba\w*|pm ?2|pm ?10|particul\w*)"
)
PREAMBLE = re.compile(
    r"^\s*(?:(?:salut|buna(?: ziua)?|hei)[,!. ]+)?(?:te rog[,: ]+)?"
    r"(?:(?:vreau|doresc|as vrea)\s+(?:sa\s+)?)?"
    r"(?:(?:raportez|raporteaza|semnalez|semnaleaza|anunt)\s+(?:ca\s+)?)?",
)


def category_of(text: str) -> str | None:
    words = plain(text)
    return next((category for category, pattern in KEYWORDS if re.search(pattern, words)), None)


def is_question(text: str) -> bool:
    words = plain(text).strip()
    return words.endswith("?") or bool(re.match(QUESTION_START, words))


def intent(text: str) -> str:
    """report | air_question | other"""
    words = plain(text)
    if is_question(text):
        return "air_question" if re.search(AIR_WORDS, words) else "other"
    if category_of(text):
        return "report"
    return "air_question" if re.search(AIR_WORDS, words) else "other"


def severity_of(text: str) -> int:
    words = plain(text)
    if re.search(SEVERE, words):
        return 3
    if re.search(MINOR, words):
        return 1
    return 2


def description_of(text: str) -> str:
    """The user's words without greetings/preamble, as a tidy sentence."""
    text = unicodedata.normalize("NFC", text).strip()
    cut = PREAMBLE.match(plain(text)).end()
    body = text[cut:] if len(text) - cut >= 5 else text
    body = re.sub(r"\s+", " ", body).strip(" ,;:-")
    if not body:
        return text
    body = body[0].upper() + body[1:]
    return body if body[-1] in ".!?" else body + "."


def extract(text: str) -> tuple[str, int, str]:
    category = category_of(text) or "other"
    return category, severity_of(text), description_of(text)


def category_label(category: str) -> str:
    return CATEGORIES[category].label
