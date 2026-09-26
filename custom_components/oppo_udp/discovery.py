"""Auto-discovery for Oppo and Magnetar players."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
import logging
import re
import socket
from typing import TYPE_CHECKING, override

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers import discovery_flow
from homeassistant.helpers.event import async_track_time_interval

from .const import CONF_MODEL, DOMAIN, MODEL_MAGNETAR

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

DISCOVERY_ADDRESS = "239.255.255.251"
DISCOVERY_PORT = 7624
DISCOVERY_BROADCAST_ADDRESS = "255.255.255.255"

_NOTIFY_PATTERN = re.compile(
    r"Notify:\s*OPPO Player Start.*?"
    + r"Server IP:\s*(?P<host>\S+).*?"
    + r"Server Port:\s*(?P<port>\d+).*?"
    + r"Server Name:\s*(?P<name>.+)",
    re.DOTALL,
)

# Legacy "OREMOTE" discovery: active probe + marker-delimited reply, used by
# every pre-UDP-20X model (and still answered by the UDP-20X itself).
_OREMOTE_LOGIN_MESSAGE = b"NOTIFY OREMOTE LOGIN"
_OREMOTE_MARKER = "REPORT ADDRESS TO OREMOTE:"
# The legacy players don't self-announce, so they need to be actively
# re-probed periodically to catch ones that were off or joined the network
# after startup. Matches the Magnetar/SSDP rescan cadence below.
_OREMOTE_PROBE_INTERVAL = timedelta(minutes=10)


def _parse_oremote_reply(message: str) -> dict[str, str | int] | None:
    """Parse a legacy OREMOTE discovery reply.

    Format: "<type>_<name>_REPORT ADDRESS TO OREMOTE:<ip>:<port>\\0" - not a
    clean CSV, so this searches for the marker string rather than splitting
    on fixed field offsets, mirroring the reference Android app's parser.
    """
    message = message.rstrip("\x00")
    marker_index = message.find(_OREMOTE_MARKER)
    if marker_index == -1:
        return None

    head = message[: marker_index - 1]  # drop the separator right before the marker
    first_underscore = head.find("_")
    name = head if first_underscore == -1 else head[first_underscore + 1 :]

    tail = message[marker_index + len(_OREMOTE_MARKER) :].lstrip()
    host, _sep, port_text = tail.partition(":")
    if not host or not port_text.isdigit():
        return None

    return {CONF_HOST: host, CONF_PORT: int(port_text), CONF_NAME: name or host}


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    """Parse incoming OPPO UDP-20X broadcasts and legacy OREMOTE replies."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    @staticmethod
    def _parse(data: bytes) -> dict[str, str | int] | None:
        message = data.decode("ascii", errors="ignore")
        match = _NOTIFY_PATTERN.search(message)
        if match:
            return {
                CONF_HOST: match.group("host"),
                CONF_PORT: int(match.group("port")),
                CONF_NAME: match.group("name").strip(),
            }
        return _parse_oremote_reply(message)

    @override
    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        parsed = self._parse(data)
        if parsed is None:
            return
        discovery_flow.async_create_flow(
            self._hass,
            DOMAIN,
            context={"source": SOURCE_INTEGRATION_DISCOVERY},
            data=parsed,
        )

    @override
    def error_received(self, exc: Exception) -> None:
        _LOGGER.debug("OPPO discovery socket error", exc_info=exc)


def _create_multicast_socket() -> socket.socket:
    """Bind a socket to the OPPO discovery port and multicast group.

    Also enables broadcast, since the same socket sends the active legacy
    "NOTIFY OREMOTE LOGIN" probe - the player replies to the exact socket
    it was queried on, so probing and listening must share one socket.

    Runs in the executor: socket creation and group membership are blocking
    calls on some platforms.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", DISCOVERY_PORT))
    membership_request = socket.inet_aton(DISCOVERY_ADDRESS) + socket.inet_aton("0.0.0.0")  # noqa: S104
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership_request)
    sock.setblocking(False)  # noqa: FBT003 - socket.setblocking only takes a positional bool
    return sock


async def async_start_oppo_discovery(hass: HomeAssistant) -> None:
    """Start Oppo discovery: listen for the UDP-20X broadcast and actively probe for legacy OREMOTE players.

    Best-effort: another process holding the discovery port, or a platform
    without multicast support, should not prevent the rest of the
    integration (including manual setup) from working.
    """
    try:
        sock = await hass.async_add_executor_job(_create_multicast_socket)
    except OSError:
        _LOGGER.debug("Unable to bind OPPO discovery socket", exc_info=True)
        return

    loop = asyncio.get_running_loop()
    try:
        transport, _ = await loop.create_datagram_endpoint(lambda: _DiscoveryProtocol(hass), sock=sock)
    except OSError:
        _LOGGER.debug("Unable to start OPPO discovery listener", exc_info=True)
        with contextlib.suppress(OSError):
            sock.close()
        return

    def _stop(_event: object) -> None:
        transport.close()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop)

    def _probe_oremote(_now: datetime | None = None) -> None:
        transport.sendto(_OREMOTE_LOGIN_MESSAGE, (DISCOVERY_BROADCAST_ADDRESS, DISCOVERY_PORT))

    _probe_oremote()
    async_track_time_interval(
        hass,
        _probe_oremote,
        _OREMOTE_PROBE_INTERVAL,
        name="Oppo legacy discovery probe",
        cancel_on_shutdown=True,
    )


# --- Magnetar (active SSDP-style probe) ---

SSDP_ADDRESS = "239.255.255.250"
SSDP_PORT = 1900
SSDP_BROADCAST_ADDRESS = "255.255.255.255"
_MAGNETAR_TOKEN = b"MAGNETAR"

_SEARCH_BURST_COUNT = 3
_SEARCH_BURST_INTERVAL = 0.5
_SEARCH_LISTEN_WINDOW = 5.0
# Matches the rescan cadence homeassistant.components.ssdp uses for its own
# periodic M-SEARCH burst.
_RESCAN_INTERVAL = timedelta(minutes=10)

_M_SEARCH_MESSAGE = (
    'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\nMX: 3\r\nST: ssdp:all\r\n\r\n'
).encode("ascii")


class _MagnetarSearchProtocol(asyncio.DatagramProtocol):
    """Collect the addresses of any replies that identify as a Magnetar player."""

    def __init__(self) -> None:
        self.found_hosts: set[str] = set()

    @override
    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if _MAGNETAR_TOKEN in data.upper():
            self.found_hosts.add(addr[0])

    @override
    def error_received(self, exc: Exception) -> None:
        _LOGGER.debug("Magnetar discovery socket error", exc_info=exc)


async def _async_scan_for_magnetar(hass: HomeAssistant) -> None:
    """Send one SSDP M-SEARCH burst and report any Magnetar replies found."""
    loop = asyncio.get_running_loop()
    try:
        transport, protocol = await loop.create_datagram_endpoint(
            _MagnetarSearchProtocol,
            local_addr=("0.0.0.0", 0),  # noqa: S104 - ephemeral port to receive SSDP replies on any interface
            allow_broadcast=True,
        )
    except OSError:
        _LOGGER.debug("Unable to start Magnetar discovery socket", exc_info=True)
        return

    try:
        for _ in range(_SEARCH_BURST_COUNT):
            transport.sendto(_M_SEARCH_MESSAGE, (SSDP_ADDRESS, SSDP_PORT))
            transport.sendto(_M_SEARCH_MESSAGE, (SSDP_BROADCAST_ADDRESS, SSDP_PORT))
            await asyncio.sleep(_SEARCH_BURST_INTERVAL)
        await asyncio.sleep(_SEARCH_LISTEN_WINDOW - _SEARCH_BURST_COUNT * _SEARCH_BURST_INTERVAL)
    finally:
        transport.close()

    for host in protocol.found_hosts:
        discovery_flow.async_create_flow(
            hass,
            DOMAIN,
            context={"source": SOURCE_INTEGRATION_DISCOVERY},
            data={CONF_HOST: host, CONF_MODEL: MODEL_MAGNETAR},
        )


def start_magnetar_discovery(hass: HomeAssistant) -> None:
    """Start periodic active discovery of Magnetar players.

    Unlike the UDP-20X broadcast, Magnetar players never announce
    themselves - they only reply to an M-SEARCH - so this actively probes
    once at startup and then on a periodic cadence, rather than passively
    listening. The scan itself (a multi-second burst-and-listen) runs as a
    background task so it never delays integration setup.
    """

    async def _rescan(_now: datetime) -> None:
        await _async_scan_for_magnetar(hass)

    hass.async_create_background_task(_async_scan_for_magnetar(hass), "Magnetar discovery scan", eager_start=True)
    async_track_time_interval(hass, _rescan, _RESCAN_INTERVAL, name="Magnetar discovery scan", cancel_on_shutdown=True)
