"""TCP client for communicating with Magnetar players.

Most Magnetar commands are fire-and-forget: they are answered with the
literal string ``ack`` and carry no state. However, sending the ``APP``
command right after connecting (undocumented) switches the player
into pushing unsolicited ``<message>...</message>`` XML blocks with real
playback and volume state on the same connection. This client sends that
identify command and parses those pushes; every other command is still the
plain fire-and-forget ``#CODE`` the official manual documents.

Commands are framed as ``#<CODE>`` terminated with CR+LF (``\\r\\n``) and sent
to the player's fixed listening port (8102).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import html
import logging
import re
import socket
from typing import TYPE_CHECKING, override

import defusedxml.ElementTree as DefusedET

from .const import MAGNETAR_PORT
from .oppo_client import PowerState, enable_tcp_keepalive
from .streaming_client import StreamingTcpClient

if TYPE_CHECKING:
    from xml.etree.ElementTree import Element

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 3.0
COMMAND_INTERVAL = 0.1  # 100ms between commands (rate limiting)

# Wake-on-LAN magic packets are broadcast to the discard port.
_WOL_PORT = 9
_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")

# Sent right after connecting; this is what makes the player start pushing
# <message> updates on this connection (see module docstring).
_IDENTIFY_COMMAND = "APP"

_MESSAGE_OPEN = "<message>"
_MESSAGE_CLOSE = "</message>"
_READ_CHUNK_SIZE = 4096
# The reference app has no such cap and just keeps appending until a
# self-contained chunk resets the buffer; this guards against an unbounded
# growth if the player ever sends something that never closes.
_MAX_PUSH_BUFFER_SIZE = 65536


def parse_mac(mac: str) -> bytes | None:
    """Return the 6 raw bytes of a MAC address, or None if malformed."""
    if not _MAC_RE.match(mac.strip()):
        return None
    hex_only = mac.strip().replace(":", "").replace("-", "")
    return bytes.fromhex(hex_only)


def _build_magic_packet(mac_bytes: bytes) -> bytes:
    """Build a Wake-on-LAN magic packet: 6x 0xFF followed by 16x the MAC."""
    return b"\xff" * 6 + mac_bytes * 16


@dataclass(frozen=True)
class MagnetarPlayState:
    """Now-playing state pushed by the player (``UpdatePlayState``).

    Field presence depends on ``media_type``:
    cd -> track_title/channel/frequency; sacd -> + disc_artist
    /disc_title; bd/vcd/dvd/video -> file_name/hdr/four_k/color_space/
    deep_color/frame_rate; audio -> file_name/channel/frequency/artist/title.
    """

    media_type: str
    state: str
    curr_idx: str
    total_count: str
    curr_time: str
    total_time: str
    repeat_mode: str
    track_title: str | None = None
    channel: str | None = None
    frequency: str | None = None
    disc_artist: str | None = None
    disc_title: str | None = None
    file_name: str | None = None
    hdr: str | None = None
    four_k: str | None = None
    color_space: str | None = None
    deep_color: str | None = None
    frame_rate: str | None = None
    artist: str | None = None
    title: str | None = None


@dataclass(frozen=True)
class MagnetarVolumeUpdate:
    """Volume/mute state pushed by the player (``UpdateVolume``)."""

    muted: bool
    volume: int | None = None


MagnetarPushEvent = MagnetarPlayState | MagnetarVolumeUpdate


def _text(data: Element, tag: str) -> str | None:
    """Return a child element's text, or None if missing/empty.

    The player's own XML serializer double-escapes entities (confirmed
    against a real capture: literal ``&amp;amp;`` on the wire, which decodes
    to ``&amp;`` after normal XML parsing instead of ``&``). A second
    ``html.unescape`` pass resolves that; it's a no-op for text that was only
    escaped once, since a lone ``&`` from a correct single decode never
    matches another entity pattern.
    """
    value = data.findtext(tag)
    if not value:
        return None
    return html.unescape(value)


def _parse_play_state(data: Element) -> MagnetarPlayState | None:
    """Build a MagnetarPlayState from an ``UpdatePlayState`` message's ``<data>`` element."""
    media = data.find("media")
    media_type = media.get("type") if media is not None else None
    if not media_type:
        return None
    return MagnetarPlayState(
        media_type=media_type,
        state=_text(data, "state") or "",
        curr_idx=_text(data, "curr_idx") or "",
        total_count=_text(data, "total_count") or "",
        curr_time=_text(data, "curr_time") or "",
        total_time=_text(data, "total_time") or "",
        repeat_mode=_text(data, "repeat_mode") or "",
        track_title=_text(data, "track_title"),
        channel=_text(data, "channel"),
        frequency=_text(data, "frequency"),
        disc_artist=_text(data, "disc_artist"),
        disc_title=_text(data, "disc_title"),
        file_name=_text(data, "file_name"),
        hdr=_text(data, "hdr"),
        four_k=_text(data, "four_k"),
        color_space=_text(data, "color_space"),
        deep_color=_text(data, "deep_color"),
        frame_rate=_text(data, "frame_rate"),
        artist=_text(data, "artist"),
        title=_text(data, "title"),
    )


def _parse_volume_update(data: Element) -> MagnetarVolumeUpdate:
    """Build a MagnetarVolumeUpdate from an ``UpdateVolume`` message's ``<data>`` element."""
    mute_text = (_text(data, "mute") or "").strip().lower()
    volume_text = _text(data, "volume")
    volume = int(volume_text) if volume_text is not None and volume_text.isdigit() else None
    return MagnetarVolumeUpdate(muted=mute_text == "true", volume=volume)


def _parse_push_message(xml_text: str) -> MagnetarPushEvent | None:
    """Parse one ``<message>...</message>`` block.

    Confirmed against a real player capture: pushes are wrapped the same way
    as outgoing commands - ``<message><from>...</from><to>...</to>
    <operation><cmd>X</cmd><data>...</data></operation></message>`` - not
    flat under ``<message>``.

    Returns None for message types with no media_player equivalent (the
    player's file-browser/setup-menu commands) or when the block fails to
    parse - never raises, so one bad or uninteresting message can't take
    down the reader loop.
    """
    try:
        root = DefusedET.fromstring(xml_text)
    except Exception:
        _LOGGER.debug("Failed to parse Magnetar push message: %r", xml_text, exc_info=True)
        return None

    operation = root.find("operation")
    if operation is None:
        return None
    data = operation.find("data")
    if data is None:
        return None

    cmd = operation.findtext("cmd")
    if cmd == "UpdatePlayState":
        return _parse_play_state(data)
    if cmd == "UpdateVolume":
        return _parse_volume_update(data)
    return None


def _extract_message(buffer: str) -> tuple[str | None, str]:
    """Pull one complete ``<message>...</message>`` span out of ``buffer``.

    Returns ``(message_xml, remaining_buffer)``; ``message_xml`` is None when
    the buffer doesn't yet contain a complete message.
    """
    start = buffer.find(_MESSAGE_OPEN)
    if start == -1:
        return None, buffer
    end = buffer.find(_MESSAGE_CLOSE, start)
    if end == -1:
        return None, buffer
    end += len(_MESSAGE_CLOSE)
    return buffer[start:end], buffer[end:]


class MagnetarClient(StreamingTcpClient[MagnetarPushEvent]):
    """TCP client for Magnetar players."""

    def __init__(self, host: str, mac: str, port: int = MAGNETAR_PORT) -> None:
        """Initialize the client."""
        super().__init__(host, port)
        self._mac = mac
        self._lock = asyncio.Lock()
        self._last_command_time: float = 0.0

    async def connect(self) -> bool:
        """Open the control connection to the player."""
        if self._connected and self._writer is not None:
            return True
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=DEFAULT_TIMEOUT,
            )
            raw_sock = self._writer.get_extra_info("socket")
            if raw_sock is not None:
                raw_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                enable_tcp_keepalive(raw_sock)
            self._connected = True
            _LOGGER.debug("Connected to Magnetar player at %s:%s", self._host, self._port)
        except OSError:
            _LOGGER.debug("Failed to connect to Magnetar player at %s:%s", self._host, self._port, exc_info=True)
            await self._teardown_connection()
            return False
        else:
            return True

    async def disconnect(self) -> None:
        """Close the control connection."""
        self._stop_streaming_requested = True
        await self._cancel_task(self._streaming_task)
        self._streaming_task = None
        await self._cancel_task(self._dispatcher_task)
        self._dispatcher_task = None
        self._clear_event_queue()
        self._streaming_callbacks.clear()
        await self._teardown_connection()

    async def send_wake_on_lan(self) -> bool:
        """Broadcast a Wake-on-LAN magic packet to the configured MAC.

        Returns False on a malformed MAC or send error -
        the caller still attempts the power command regardless.
        """
        mac_bytes = parse_mac(self._mac)
        if mac_bytes is None:
            _LOGGER.warning("Cannot send Wake-on-LAN: malformed MAC address %r", self._mac)
            return False
        packet = _build_magic_packet(mac_bytes)
        try:
            loop = asyncio.get_running_loop()
            transport, _ = await loop.create_datagram_endpoint(
                asyncio.DatagramProtocol,
                remote_addr=("255.255.255.255", _WOL_PORT),
                allow_broadcast=True,
            )
            try:
                transport.sendto(packet)
            finally:
                transport.close()
        except OSError:
            _LOGGER.debug("Failed to send Wake-on-LAN packet", exc_info=True)
            return False
        else:
            return True

    async def _send_command(self, command: str) -> bool:
        """Send a command and confirm the write succeeded.

        The player replies with ``ack``; the response carries no state, so it
        is drained only to detect a dropped connection.
        """
        async with self._lock:
            if not self._connected and not await self.connect():
                return False

            now = asyncio.get_running_loop().time()
            elapsed = now - self._last_command_time
            if elapsed < COMMAND_INTERVAL:
                await asyncio.sleep(COMMAND_INTERVAL - elapsed)

            if self._writer is None:
                self._connected = False
                return False

            try:
                self._writer.write(f"#{command}\r\n".encode("ascii"))
                await self._writer.drain()
                self._last_command_time = asyncio.get_running_loop().time()
            except OSError:
                _LOGGER.debug("Error sending Magnetar command %s", command, exc_info=True)
                await self._teardown_connection()
                return False
            return True

    # --- Metadata push ---

    async def enable_metadata_push(self) -> bool:
        """Identify as an app so the player starts pushing state updates.

        Sends ``#APP`` - see the module docstring. Only enables the push;
        the caller is responsible for starting the reader via ``start_streaming``.
        """
        return await self._send_command(_IDENTIFY_COMMAND)

    # start_streaming/stop_streaming/_enqueue_streaming_event/
    # _dispatch_streaming_events are inherited from StreamingTcpClient; only
    # the reader loop below (parsing this protocol's own <message> format) is
    # specific to Magnetar.

    def _process_chunk(self, buffer: str, chunk: bytes) -> str:
        """Fold one read chunk into the message buffer, dispatching complete messages.

        Returns the (possibly still partial) remaining buffer.
        """
        text = chunk.decode("utf-8", errors="replace")
        # A self-contained chunk (both tags present) resets the buffer,
        # otherwise a split message keeps accumulating.
        buffer = text if _MESSAGE_OPEN in text and _MESSAGE_CLOSE in text else buffer + text

        if len(buffer) > _MAX_PUSH_BUFFER_SIZE:
            _LOGGER.warning(
                "Magnetar push buffer exceeded %d bytes without a complete message, discarding",
                _MAX_PUSH_BUFFER_SIZE,
            )
            return ""

        while True:
            message_xml, buffer = _extract_message(buffer)
            if message_xml is None:
                return buffer
            try:
                event = _parse_push_message(message_xml)
            except Exception:
                _LOGGER.exception("Error parsing Magnetar push message")
                continue
            if event is not None:
                self._enqueue_streaming_event(event)

    @override
    async def _streaming_loop(self) -> None:
        """Background loop reading and parsing push messages from the player."""
        buffer = ""
        try:
            while self._connected and self._reader:
                try:
                    chunk = await self._reader.read(_READ_CHUNK_SIZE)
                except asyncio.CancelledError:
                    raise
                except OSError:
                    _LOGGER.debug("Magnetar streaming connection lost")
                    self._connected = False
                    break

                if not chunk:
                    _LOGGER.debug("Magnetar streaming connection closed by peer")
                    self._connected = False
                    break

                buffer = self._process_chunk(buffer, chunk)
        finally:
            await self._finalize_streaming_loop()

    # --- Power ---
    #
    # Power commands are fire-and-forget: Wake-on-LAN powers the player up, and
    # the follow-up command's result is ignored on purpose. A deep-sleeping
    # player takes ~30s before it accepts TCP (so the send may fail even though
    # WoL is waking it), and an already-on player returns nothing meaningful.
    # Either way the assumed resulting PowerState is returned.

    async def power_on(self) -> PowerState:
        """Wake the player (WoL) and turn it on. Returns the assumed state."""
        await self.send_wake_on_lan()
        await self._send_command("PON")
        return PowerState.ON

    async def power_off(self) -> PowerState:
        """Turn the player off. Returns the assumed state."""
        await self._send_command("POF")
        return PowerState.OFF

    # --- Playback ---

    async def play(self) -> bool:
        """Start playback."""
        return await self._send_command("PLA")

    async def pause(self) -> bool:
        """Pause playback."""
        return await self._send_command("PAU")

    async def stop(self) -> bool:
        """Stop playback."""
        return await self._send_command("STP")

    async def next_track(self) -> bool:
        """Skip to next track/chapter."""
        return await self._send_command("NXT")

    async def previous_track(self) -> bool:
        """Skip to previous track/chapter."""
        return await self._send_command("PRE")

    async def fast_forward(self) -> bool:
        """Fast forward."""
        return await self._send_command("FWD")

    async def fast_reverse(self) -> bool:
        """Fast reverse."""
        return await self._send_command("REV")

    # --- Volume ---

    async def volume_up(self) -> bool:
        """Raise volume."""
        return await self._send_command("VUP")

    async def volume_down(self) -> bool:
        """Lower volume."""
        return await self._send_command("VDN")

    async def mute_toggle(self) -> bool:
        """Toggle mute."""
        return await self._send_command("MUT")

    # --- Tray ---

    async def eject_toggle(self) -> bool:
        """Toggle tray open/close."""
        return await self._send_command("EJT")

    # --- Shared front-panel / OSD commands ---

    async def dimmer(self) -> bool:
        """Cycle front-panel display brightness."""
        return await self._send_command("DIM")

    async def pure_audio_toggle(self) -> bool:
        """Toggle Pure Tone mode (disables video output)."""
        return await self._send_command("PUR")

    async def info_toggle(self) -> bool:
        """Show/hide the on-screen display."""
        return await self._send_command("OSD")

    async def audio_language_toggle(self) -> bool:
        """Change audio track / language."""
        return await self._send_command("AUD")

    async def subtitle_toggle(self) -> bool:
        """Change subtitle language."""
        return await self._send_command("SUB")

    async def zoom(self) -> bool:
        """Cycle zoom / aspect-ratio mode."""
        return await self._send_command("ZOM")
