"""Client for the pre-20X players' proprietary HTTP JSON control API.

Reverse-engineered from the decompiled OPPO "BDP-10x MediaControl" Android
app: BDP-83/93/95/103/105 all run a small HTTP JSON API on TCP port 436,
entirely separate from the RS-232-over-IP text protocol already spoken by
``OppoClient``. ``GET /getmusicplayinfo`` is the one endpoint used here - it
returns an ``id3info`` object (title/album/artist/genre/year) for whatever is
currently playing, which fills the same hole UDP-20X already covers via its
own telnet ``QTN``/``QTA``/``QTP`` queries.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

import aiohttp

from homeassistant.helpers.aiohttp_client import async_get_clientsession

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_HTTP_METADATA_PORT = 436
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=3)


@dataclass(frozen=True)
class OppoHttpMusicInfo:
    """Track metadata parsed from a ``/getmusicplayinfo`` response."""

    title: str | None
    album: str | None
    artist: str | None


async def query_music_play_info(hass: HomeAssistant, host: str) -> OppoHttpMusicInfo | None:
    """Query the player's HTTP-436 JSON API for the currently playing track.

    Returns None on any failure (timeout, connection refused, malformed
    JSON) - firmware support for this endpoint is only confirmed on
    BDP-103/105, so a player that doesn't implement it must fail silently
    rather than break the snapshot rebuild.
    """
    session = async_get_clientsession(hass)
    url = f"http://{host}:{_HTTP_METADATA_PORT}/getmusicplayinfo"
    try:
        async with session.get(url, timeout=_REQUEST_TIMEOUT) as response:
            payload = await response.json(content_type=None)
    except aiohttp.ClientError, TimeoutError, ValueError:
        _LOGGER.debug("Failed to query %s", url, exc_info=True)
        return None

    id3info = payload.get("id3info")
    if not isinstance(id3info, dict):
        return None

    playinfo = payload.get("playinfo")
    dlna_filename = playinfo.get("dlna_filename") if isinstance(playinfo, dict) else None
    file_path = playinfo.get("file_path") if isinstance(playinfo, dict) else None

    return OppoHttpMusicInfo(
        title=id3info.get("title") or dlna_filename or _filename_from_path(file_path),
        album=id3info.get("album") or None,
        artist=id3info.get("artist") or None,
    )


def _filename_from_path(path: str | None) -> str | None:
    """Return the filename component of a player-side file path, or None.

    Untagged local files have no ``id3info.title`` - the official app falls
    back to the filename in that case (``getFileNameFromPath``: everything
    after the last ``/``), so we do the same rather than showing nothing.
    """
    if not path:
        return None
    return path.rsplit("/", 1)[-1] or None
