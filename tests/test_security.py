from types import SimpleNamespace

from app.ids import uuid7
from app.security import RateLimiter, client_ip

TRUSTED = ["10.0.0.0/8"]


def request(peer: str, headers: dict[str, str] | None = None):
    return SimpleNamespace(
        client=SimpleNamespace(host=peer), headers={k.lower(): v for k, v in (headers or {}).items()}
    )


def test_client_ip_ignores_headers_from_untrusted_peer():
    assert client_ip(request("198.51.100.7", {"CF-Connecting-IP": "1.2.3.4"}), TRUSTED) == "198.51.100.7"


def test_client_ip_prefers_cloudflare_header_behind_trusted_proxy():
    req = request("10.0.0.5", {"CF-Connecting-IP": "1.2.3.4", "X-Forwarded-For": "5.6.7.8"})
    assert client_ip(req, TRUSTED) == "1.2.3.4"


def test_client_ip_takes_last_untrusted_forwarded_hop():
    req = request("10.0.0.5", {"X-Forwarded-For": "6.6.6.6, 1.2.3.4, 10.0.0.9"})
    assert client_ip(req, TRUSTED) == "1.2.3.4"


def test_rate_limiter_blocks_after_limit_and_reports_wait():
    limiter = RateLimiter()
    assert limiter.hit("k", 2, 60) is None
    assert limiter.hit("k", 2, 60) is None
    wait = limiter.hit("k", 2, 60)
    assert wait is not None and 0 < wait <= 60
    assert limiter.hit("other", 2, 60) is None


def test_uuid7_is_version_7_and_time_ordered():
    first, second = uuid7(), uuid7()
    assert first.version == 7
    assert first.variant == "specified in RFC 4122"
    assert first.int >> 80 <= second.int >> 80
