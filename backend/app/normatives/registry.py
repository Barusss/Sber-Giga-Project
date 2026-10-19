"""Read live official Rosstandart standard cards. Fail closed on ambiguity.

No generic URLs are fetched: both search and detail requests are restricted
to https://protect.gost.ru, and redirects are disabled. Public HTML is untrusted.
"""
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel

from app.normatives.extraction import normalize_code

BASE_URL = "https://protect.gost.ru"
DETAIL_PATH = re.compile(r"^/gost/details/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
EXACT_GOST = re.compile(
    r"^ГОСТ(?: Р)?(?: (?:ИСО|МЭК|ISO|IEC))? \d+(?:\.\d+){0,5}-\d{2,4}$"
)
STATUS_MAP = {
    "Действует": "active",
    "Действует только в РФ": "active",
    "Заменен": "obsolete",
    "Отменен": "obsolete",
    "Срок действия истек": "obsolete",
    "Утратил силу в РФ": "obsolete",
    "Принят": "unknown",
    "Действует с ограничением": "unknown",
}
STATUS_UNKNOWN = "unknown"
MAX_HTML_BYTES = 2_000_000


class RegistryError(Exception):
    """Official site unreachable, changed layout or inconsistent content."""


class RegistryResult(BaseModel):
    code: str
    status: str = STATUS_UNKNOWN
    registry_status: str | None = None
    title: str | None = None
    replacement: str | None = None
    source_url: str | None = None
    checked_at: datetime | None = None
    reason: str | None = None


def detail_path(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "protect.gost.ru":
        return None
    if parsed.params or parsed.query or parsed.fragment:
        return None
    if not DETAIL_PATH.fullmatch(parsed.path):
        return None
    return parsed.path


def parse_search(html: str, requested_code: str) -> str | None:
    """Use only an exact designation's link; never take the first fuzzy hit."""
    soup = BeautifulSoup(html, "html.parser")
    found = set()
    for anchor in soup.select("a[href]"):
        raw_href = anchor.get("href", "")
        path = raw_href if raw_href.startswith("/") else detail_path(raw_href)
        if not path or not DETAIL_PATH.fullmatch(path):
            continue
        if normalize_code(anchor.get_text(" ", strip=True)) == requested_code:
            found.add(path)
    if len(found) == 1:
        return next(iter(found))
    return None


def parse_detail(html: str, code: str, source_url: str) -> RegistryResult:
    """Require an exact H1, exact registered designation and known status."""
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find("h1")
    if heading is None or normalize_code(heading.get_text(" ", strip=True)) != code:
        raise RegistryError("Official card designation does not match the requested standard")

    # Structured DOM text; the status is before the 'Основные сведения' heading,
    # not in the explanatory footer legend.
    text = [re.sub(r"\s+", " ", part).strip()
            for part in soup.get_text("\n", strip=True).splitlines()]
    text = [part for part in text if part]
    try:
        pos = next(i for i, value in enumerate(text) if normalize_code(value) == code)
    except StopIteration as exc:
        raise RegistryError("Official card heading cannot be read") from exc

    status_candidates = [
        value for value in text[pos + 1:pos + 12]
        if value in STATUS_MAP
    ]
    if len(status_candidates) != 1:
        raise RegistryError("Official status missing or ambiguous")
    label = status_candidates[0]

    def field_value(name: str) -> str | None:
        matches = [i for i, value in enumerate(text) if value == name]
        if len(matches) != 1 or matches[0] + 1 >= len(text):
            return None
        value = text[matches[0] + 1]
        if value in {"-", "—", ""}:
            return None
        return value

    designation = field_value("Обозначение")
    if not designation or normalize_code(designation) != code:
        raise RegistryError("Official card registration differs from its heading")
    replacement = field_value("Обозначение заменяющего")
    return RegistryResult(
        code=code, status=STATUS_MAP[label], registry_status=label,
        title=field_value("Заглавие на русском языке"),
        replacement=replacement, source_url=source_url,
        checked_at=datetime.now(timezone.utc),
        reason=("restrictions_or_pending" if STATUS_MAP[label] == "unknown" else None),
    )


class RosstandartClient:
    def __init__(self, http_client: httpx.Client | None = None):
        self.http_client = http_client

    def _fetch(self, path: str, params: dict | None = None) -> str:
        if path != "/gost" and not DETAIL_PATH.fullmatch(path):
            raise RegistryError("Refused unexpected registry URL")
        url = BASE_URL + path
        try:
            if self.http_client is None:
                with httpx.Client(timeout=6.0, follow_redirects=False) as client:
                    response = client.get(url, params=params)
            else:
                response = self.http_client.get(url, params=params, timeout=6.0,
                                                follow_redirects=False)
        except httpx.RequestError as exc:
            raise RegistryError("Cannot reach official Rosstandart registry") from exc
        if response.status_code != 200:
            raise RegistryError("Official Rosstandart registry returned an unsuccessful status")
        if len(response.content) > MAX_HTML_BYTES:
            raise RegistryError("Official registry response too large")
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type:
            raise RegistryError("Official registry returned unexpected content type")
        return response.text

    def lookup(self, code: str, source_url: str | None = None) -> RegistryResult:
        code = normalize_code(code)
        if not EXACT_GOST.fullmatch(code):
            return RegistryResult(code=code, reason="unsupported_or_incomplete_designation")
        path = detail_path(source_url)
        try:
            if path is None:
                listing = self._fetch("/gost", params={"search": code})
                path = parse_search(listing, code)
            if path is None:
                return RegistryResult(code=code, reason="exact_official_card_not_found")
            url = BASE_URL + path
            detail = self._fetch(path)
            return parse_detail(detail, code, url)
        except RegistryError:
            return RegistryResult(code=code, reason="official_verification_unavailable")
