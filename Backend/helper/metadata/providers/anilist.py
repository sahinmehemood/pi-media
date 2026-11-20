"""AniList GraphQL metadata provider (Librarian plugin file, DESIGN §7).

AniList is the identity authority for anime and donghua. Its GraphQL
API is unauthenticated for reads, so this client needs no API key.
It follows the tree's provider pattern (mirrors kitsu.py): a
module-global httpx.AsyncClient created lazily by _get_client().

Only the two operations the Librarian gateway uses are exposed:
search_media() (one Page of title-bridge results) and media_by_id()
(the cross-ID single fetch). Both raise AnilistTransportError on any
failure to produce a well-formed response, so the gateway can
classify the leg ``down`` instead of mistaking an outage for a
zero-result ``empty`` (DESIGN §7 leg classification).
"""
from __future__ import annotations

import asyncio
from typing import Optional

import httpx

ANILIST_URL = "https://graphql.anilist.co"

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()

_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "Telegram-Stremio (+https://github.com/weebzone/Telegram-Stremio)",
}

# Pinned selection set (DESIGN §7 / Phase 2 plan §4): identity,
# title bridge and relations — id, idMal, format, countryOfOrigin,
# seasonYear, title {romaji english native}, synonyms, relations
# (edges' relationType and related node's id + title). Relations
# are data only: the gateway must not use them as join keys.
# externalLinks are deliberately NOT requested.
_MEDIA_FIELDS = """
    id
    idMal
    format
    countryOfOrigin
    seasonYear
    title { romaji english native }
    synonyms
    relations { edges { relationType node { id title { romaji english native } } } }
"""


class AnilistTransportError(Exception):
    """AniList could not produce a well-formed response.

    Carries ``status`` (the HTTP status when known, else None) and
    ``retry_after`` (seconds parsed from a 429's Retry-After header
    when present and numeric, else None). The gateway maps this to
    leg state ``down`` and surfaces ``retry_after`` for a 429.
    """

    def __init__(self, message: str, status: Optional[int] = None,
                 retry_after: Optional[int] = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


async def _get_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(
                timeout=20.0, follow_redirects=True, headers=_HEADERS
            )
        return _client


def _parse_retry_after(resp) -> Optional[int]:
    raw = None
    try:
        raw = resp.headers.get("Retry-After")
    except Exception:
        raw = None
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


async def _post(query: str, variables: dict) -> dict:
    """POST one GraphQL operation; return its ``data`` object.

    Raises AnilistTransportError on a transport error, a timeout, a
    non-200 status, malformed JSON, or a response with no usable
    ``data`` (GraphQL errors with no data are a schema failure). A
    200 with partial GraphQL errors AND usable data returns the data.
    """
    try:
        client = await _get_client()
        resp = await client.post(
            ANILIST_URL, json={"query": query, "variables": variables}
        )
    except Exception as e:
        raise AnilistTransportError(f"AniList request failed: {e}") from e
    if resp.status_code != 200:
        retry_after = _parse_retry_after(resp) if resp.status_code == 429 else None
        raise AnilistTransportError(
            f"AniList HTTP {resp.status_code}",
            status=resp.status_code,
            retry_after=retry_after,
        )
    try:
        body = resp.json()
    except Exception as e:
        raise AnilistTransportError(
            "AniList returned malformed JSON", status=resp.status_code
        ) from e
    if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
        raise AnilistTransportError(
            "AniList response has no data", status=resp.status_code
        )
    return body["data"]


async def search_media(name: str, *, format: Optional[str] = None,
                       country: Optional[str] = None,
                       media_kind: str = "ANIME") -> list:
    """One Page(page=1, perPage=10) search; returns the raw Media dicts.

    Exactly one page is fetched — no pagination loop. The ``format``
    and ``countryOfOrigin`` filters are omitted from the operation
    entirely when not given (an explicit null filter is not the same
    as an absent one). A well-formed empty Page returns [].
    """
    if media_kind != "ANIME":
        raise ValueError(f"unsupported AniList media kind: {media_kind!r}")
    args = ["search: $search", "type: ANIME", "sort: SEARCH_MATCH"]
    variables: dict = {"search": name}
    declarations = ["$search: String"]
    if format is not None:
        args.append("format: $format")
        variables["format"] = format
        declarations.append("$format: MediaFormat")
    if country is not None:
        args.append("countryOfOrigin: $country")
        variables["country"] = country
        declarations.append("$country: CountryCode")
    query = (
        f"query ({', '.join(declarations)}) {{\n"
        f"  Page(page: 1, perPage: 10) {{\n"
        f"    media({', '.join(args)}) {{{_MEDIA_FIELDS}  }}\n"
        f"  }}\n"
        f"}}"
    )
    data = await _post(query, variables)
    page = data.get("Page")
    if not isinstance(page, dict) or not isinstance(page.get("media"), list):
        raise AnilistTransportError("AniList search response failed schema validation")
    media = page["media"]
    if not all(isinstance(m, dict) for m in media):
        raise AnilistTransportError("AniList search response failed schema validation")
    return media


async def media_by_id(anilist_id: int) -> Optional[dict]:
    """Fetch one Media by AniList ID for cross-ID resolution.

    Returns the Media dict, or None on a well-formed not-found;
    raises AnilistTransportError on the same failure set as
    search_media().
    """
    query = (
        "query ($id: Int) {\n"
        f"  Media(id: $id, type: ANIME) {{{_MEDIA_FIELDS}  }}\n"
        "}"
    )
    data = await _post(query, {"id": int(anilist_id)})
    media = data.get("Media")
    if media is None:
        return None
    if not isinstance(media, dict):
        raise AnilistTransportError("AniList media response failed schema validation")
    return media
