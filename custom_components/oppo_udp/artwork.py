"""Album artwork fetching via MusicBrainz and Cover Art Archive."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any
import urllib.parse

import aiohttp

from homeassistant.helpers.aiohttp_client import async_get_clientsession

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_MB_BASE = "https://musicbrainz.org/ws/2"
_CAA_BASE = "https://coverartarchive.org/release"
_CACHE_TTL = 10800.0  # 3 hours
_MB_RATE_LIMIT = 1.0  # MusicBrainz requires ≤ 1 req/sec
_SCORE_THRESHOLD = 80
_USER_AGENT = "hass-oppo/0.0.3 (https://github.com/henrikwidlund/hass-oppo)"
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


def _lucene_phrase(value: str) -> str:
    """Wrap value in a Lucene phrase so its contents are treated literally."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class AlbumArtworkService:
    """Fetch album cover URLs from MusicBrainz + Cover Art Archive."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._cache: dict[tuple[str | None, ...], tuple[str | None, float]] = {}
        self._lock = asyncio.Lock()
        self._last_mb_request: float = 0.0

    async def get_cover_url(
        self,
        artist: str,
        album: str | None,
        track: str | None,
    ) -> str | None:
        """Return a cover art URL, or None if not found/available."""
        if not artist or (not album and not track):
            return None

        cache_key: tuple[str | None, ...] = (artist, album) if album else (artist, None, track)
        now = time.monotonic()
        if cache_key in self._cache:
            cached_url, expires = self._cache[cache_key]
            if now < expires:
                return cached_url

        async with self._lock:
            now = time.monotonic()
            if cache_key in self._cache:
                cached_url, expires = self._cache[cache_key]
                if now < expires:
                    return cached_url

            result = await self._fetch(artist, album, track)
            self._cache[cache_key] = (result, time.monotonic() + _CACHE_TTL)
            return result

    async def _rate_limit_mb(self) -> None:
        elapsed = time.monotonic() - self._last_mb_request
        if elapsed < _MB_RATE_LIMIT:
            await asyncio.sleep(_MB_RATE_LIMIT - elapsed)
        self._last_mb_request = time.monotonic()

    async def _fetch(
        self,
        artist: str,
        album: str | None,
        track: str | None,
    ) -> str | None:
        session = async_get_clientsession(self._hass)
        release_ids = await self._get_release_ids(session, artist, album, track)
        for release_id in release_ids:
            cover = await self._check_cover_art(session, release_id)
            if cover is not None:
                return cover
        _LOGGER.debug("No cover found for artist=%r album=%r track=%r", artist, album, track)
        return None

    async def _get_release_ids(
        self,
        session: aiohttp.ClientSession,
        artist: str,
        album: str | None,
        track: str | None,
    ) -> list[str]:
        if album:
            query = f"artist:{_lucene_phrase(artist)} AND release:{_lucene_phrase(album)}"
            url = f"{_MB_BASE}/release/?query={urllib.parse.quote(query)}&fmt=json"
            data = await self._mb_get(session, url)
            if data is None:
                return []
            releases: list[dict[str, Any]] = data.get("releases", [])
            candidates = sorted(
                (r for r in releases if r.get("score", 0) > _SCORE_THRESHOLD),
                key=lambda r: r.get("score", 0),
                reverse=True,
            )
            return [r["id"] for r in candidates if "id" in r]

        if track is None:
            return []
        query = f"artist:{_lucene_phrase(artist)} AND recording:{_lucene_phrase(track)}"
        url = f"{_MB_BASE}/recording/?query={urllib.parse.quote(query)}&fmt=json"
        data = await self._mb_get(session, url)
        if data is None:
            return []
        recordings: list[dict[str, Any]] = data.get("recordings", [])
        ordered = sorted(
            (r for r in recordings if r.get("score", 0) > _SCORE_THRESHOLD),
            key=lambda r: r.get("score", 0),
            reverse=True,
        )
        release_ids: list[str] = []
        for rec in ordered:
            release_ids.extend(rid for rel in rec.get("releases", []) if (rid := rel.get("id")))
        return release_ids

    async def _mb_get(self, session: aiohttp.ClientSession, url: str) -> dict[str, Any] | None:
        await self._rate_limit_mb()
        try:
            async with session.get(
                url,
                headers={"User-Agent": _USER_AGENT},
                timeout=_REQUEST_TIMEOUT,
            ) as resp:
                if resp.status == 200:
                    result: dict[str, Any] = await resp.json()
                    return result
                _LOGGER.debug("MusicBrainz returned %s for %s", resp.status, url)
                return None
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Error querying MusicBrainz: %s", url, exc_info=True)
            return None

    @staticmethod
    async def _check_cover_art(session: aiohttp.ClientSession, release_id: str) -> str | None:
        url = f"{_CAA_BASE}/{release_id}/front-500"
        try:
            async with session.get(
                url,
                headers={"User-Agent": _USER_AGENT},
                timeout=_REQUEST_TIMEOUT,
                allow_redirects=True,
            ) as resp:
                if resp.ok:
                    return url
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Error checking cover art for release %s", release_id, exc_info=True)
        return None
