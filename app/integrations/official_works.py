import hashlib
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from defusedxml import ElementTree

from app.integrations.http import ProviderError


class OfficialWorksProvider:
    """Allowlisted RSS acquisition. Uncertain content requires human review before scoring."""

    name = "official_works"

    def __init__(self, settings, http):
        self.settings, self.http = settings, http

    def allowed(self, url):
        parsed = urlparse(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == self.settings.official_works_allowed_host
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
        )

    async def fetch(self):
        url = self.settings.official_works_feed_url
        if not url or not self.allowed(url):
            raise ProviderError("missing_verified_official_feed")
        try:
            response = await self.http.client.get(url)
            response.raise_for_status()
            return self.parse(response.text)
        except Exception:
            raise ProviderError("official_feed_unavailable_or_invalid") from None

    def parse(self, content):
        root = ElementTree.fromstring(content)
        results = []
        for item in root.findall(".//item"):
            link = item.findtext("link", "").strip()
            if not self.allowed(link):
                continue
            title = BeautifulSoup(item.findtext("title", ""), "html.parser").get_text(" ", strip=True)
            description = BeautifulSoup(item.findtext("description", ""), "html.parser").get_text(
                " ", strip=True
            )
            try:
                published = parsedate_to_datetime(item.findtext("pubDate", ""))
            except (TypeError, ValueError):
                published = None
            results.append(
                {
                    "source_id": hashlib.sha256(link.encode()).hexdigest(),
                    "title": title[:500],
                    "description": description[:10000],
                    "original_url": link,
                    "published_at": published,
                    "retrieved_at": datetime.now(UTC),
                    "verification": "needs_review",
                    "status": "unknown",
                    "provenance": {
                        "source": self.settings.official_works_allowed_host,
                        "category": "OFFICIAL_PLANNED_WORK",
                        "limitation": "Announcement candidate: dates, location and traffic relevance require administrative review",
                    },
                }
            )
        return results
