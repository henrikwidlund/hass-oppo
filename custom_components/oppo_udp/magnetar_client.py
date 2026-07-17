"""TCP client for communicating with Magnetar players.

Unlike the Oppo protocol, the Magnetar network-control interface is
fire-and-forget: every command is answered with the literal string ``ack`` and
the player exposes no query or unsolicited-status ("verbose") support. All
entity state is therefore tracked optimistically by the caller.

Commands are framed as ``#<CODE>`` terminated with CR+LF (``\\r\\n``) and sent
to the player's fixed listening port (8102).
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket

from .const import MAGNETAR_PORT
from .oppo_client import PowerState

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 3.0
COMMAND_INTERVAL = 0.1  # 100ms between commands (rate limiting)

# Wake-on-LAN magic packets are broadcast to the discard port.
_WOL_PORT = 9
_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")


def parse_mac(mac: str) -> bytes | None:
    """Return the 6 raw bytes of a MAC address, or None if malformed."""
    if not _MAC_RE.match(mac.strip()):
        return None
    hex_only = mac.strip().replace(":", "").replace("-", "")
    return bytes.fromhex(hex_only)


def _build_magic_packet(mac_bytes: bytes) -> bytes:
    """Build a Wake-on-LAN magic packet: 6x 0xFF followed by 16x the MAC."""
    return b"\xff" * 6 + mac_bytes * 16


class MagnetarClient:
    """Fire-and-forget TCP client for Magnetar players."""

    def __init__(self, host: str, mac: str, port: int = MAGNETAR_PORT) -> None:
        """Initialize the client."""
        self._host = host
        self._port = port
        self._mac = mac
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._last_command_time: float = 0.0
        self._connected = False

    @property
    def host(self) -> str:
        """Return the host address."""
        return self._host

    @property
    def connected(self) -> bool:
        """Return True if connected."""
        return self._connected and self._writer is not None

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
            self._connected = True
            _LOGGER.debug("Connected to Magnetar player at %s:%s", self._host, self._port)
        except OSError:
            _LOGGER.debug("Failed to connect to Magnetar player at %s:%s", self._host, self._port, exc_info=True)
            await self._teardown_connection()
            return False
        else:
            return True

    async def _teardown_connection(self) -> None:
        """Close the transport and clear stream references."""
        self._connected = False
        writer = self._writer
        self._writer = None
        self._reader = None
        if writer is None:
            return
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Error closing writer during teardown", exc_info=True)

    async def disconnect(self) -> None:
        """Close the control connection."""
        await self._teardown_connection()

    async def send_wake_on_lan(self) -> bool:
        """Broadcast a Wake-on-LAN magic packet to the configured MAC.

        Returns False on a malformed MAC or send error —
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
