"""TCP client for communicating with the Oppo UDP-20X player."""

from __future__ import annotations

import asyncio
import contextlib
from enum import StrEnum
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 23
DEFAULT_TIMEOUT = 3.0
COMMAND_INTERVAL = 0.1  # 100ms between commands (rate limiting)


class PowerState(StrEnum):
    """Power state of the player."""

    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"


class PlaybackStatus(StrEnum):
    """Playback status of the player."""

    PLAY = "play"
    PAUSE = "pause"
    STOP = "stop"
    STEP = "step"
    FAST_REWIND = "fast_rewind"
    FAST_FORWARD = "fast_forward"
    SLOW_FORWARD = "slow_forward"
    SLOW_REWIND = "slow_rewind"
    SETUP = "setup"
    HOME_MENU = "home_menu"
    MEDIA_CENTER = "media_center"
    SCREEN_SAVER = "screen_saver"
    DISC_MENU = "disc_menu"
    NO_DISC = "no_disc"
    LOADING = "loading"
    OPEN = "open"
    CLOSE = "close"
    UNKNOWN = "unknown"


class DiscType(StrEnum):
    """Disc type."""

    BLURAY_MOVIE = "bd-mv"
    DVD_VIDEO = "dvd-video"
    DVD_AUDIO = "dvd-audio"
    SACD = "sacd"
    CD_AUDIO = "cdda"
    DATA_DISC = "data-disc"
    ULTRA_HD_BLURAY = "uhbd"
    NO_DISC = "no-disc"
    UNKNOWN_DISC = "unknown-disc"
    UNKNOWN = "unknown"


class InputSource(StrEnum):
    """Input source."""

    BLURAY_PLAYER = "BD-PLAYER"
    HDMI_IN = "HDMI-IN"
    ARC_HDMI_OUT = "ARC-HDMI-OUT"
    OPTICAL = "OPTICAL-IN"
    COAXIAL = "COAXIAL-IN"
    USB_AUDIO = "USB-AUDIO-IN"
    UNKNOWN = "unknown"


class OppoClient:
    """TCP client for Oppo UDP-20X players."""

    def __init__(self, host: str, port: int = DEFAULT_PORT) -> None:
        """Initialize the client."""
        self._host = host
        self._port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._last_command_time: float = 0.0
        self._connected = False
        self._streaming_task: asyncio.Task[None] | None = None
        self._streaming_callbacks: list[Callable[[tuple[str, str]], None]] = []
        self._disconnect_callback: Callable[[], None] | None = None
        self._pending_response: asyncio.Future[str | None] | None = None
        self._stop_streaming_requested = False

    @property
    def host(self) -> str:
        """Return the host address."""
        return self._host

    @property
    def connected(self) -> bool:
        """Return True if connected."""
        return self._connected and self._writer is not None

    async def connect(self) -> bool:
        """Connect to the Oppo player."""
        if self._connected and self._writer is not None:
            return True

        try:
            self._reader, self._writer = await self._do_connect()
            # Disable Nagle's algorithm for immediate command delivery
            writer = self._writer
            import socket as socket_module  # noqa: PLC0415

            raw_sock = writer.get_extra_info("socket")
            if raw_sock is not None:
                raw_sock.setsockopt(socket_module.IPPROTO_TCP, socket_module.TCP_NODELAY, 1)
            self._connected = True
            _LOGGER.debug("Connected to Oppo player at %s:%s", self._host, self._port)
        except OSError:
            _LOGGER.exception(
                "Failed to connect to Oppo player at %s:%s",
                self._host,
                self._port,
            )
            self._connected = False
            return False
        else:
            return True

    async def _do_connect(
        self,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Attempt TCP connection with one retry on socket error."""
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=DEFAULT_TIMEOUT,
            )
        except OSError:
            # Network stack might not be ready — retry once after a short delay
            _LOGGER.debug("Connection failed, retrying in 500ms")
            await asyncio.sleep(0.5)
            return await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=DEFAULT_TIMEOUT,
            )

    async def disconnect(self) -> None:
        """Disconnect from the Oppo player."""
        self._stop_streaming_requested = True
        if self._streaming_task and not self._streaming_task.done():
            self._streaming_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._streaming_task
            self._streaming_task = None

        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Error closing writer during disconnect")
        self._writer = None
        self._reader = None
        self._connected = False

    async def _send_command(self, command: str) -> str | None:
        """Send a command and wait for the response."""
        async with self._lock:
            if not self._connected and not await self.connect():
                return None

            # Rate limiting
            now = asyncio.get_event_loop().time()
            elapsed = now - self._last_command_time
            if elapsed < COMMAND_INTERVAL:
                await asyncio.sleep(COMMAND_INTERVAL - elapsed)

            try:
                response = await self._send_command_core(command)

                # Retry once on OVERTIME error (player was busy)
                if response == "@ER OVERTIME":
                    await asyncio.sleep(0.05)
                    response = await self._send_command_core(command)

            except OSError:
                _LOGGER.exception("Error sending command %s", command)
                self._connected = False
                return None

            return response

    async def _send_command_core(self, command: str) -> str | None:
        """Send a single command attempt and read the response."""
        if self._writer is None:
            self._connected = False
            return None

        # Register pending command response before writing to avoid races with
        # fast replies being consumed by the streaming loop.
        use_streaming_response = bool(self._streaming_task and not self._streaming_task.done())
        pending_response: asyncio.Future[str | None] | None = None
        if use_streaming_response:
            loop = asyncio.get_event_loop()
            pending_response = loop.create_future()
            self._pending_response = pending_response

        cmd_bytes = f"#{command}\r".encode("ascii")
        try:
            self._writer.write(cmd_bytes)
            await self._writer.drain()
            self._last_command_time = asyncio.get_event_loop().time()
        except Exception:
            if self._pending_response is pending_response:
                self._pending_response = None
            raise

        # If streaming loop is active, use a future that it will complete
        if use_streaming_response and pending_response is not None:
            try:
                return await asyncio.wait_for(pending_response, timeout=DEFAULT_TIMEOUT)
            except TimeoutError:
                _LOGGER.debug("Command %s timed out waiting for response", command)
                return None
            finally:
                if self._pending_response is pending_response:
                    self._pending_response = None

        # No streaming loop — read directly
        return await asyncio.wait_for(
            self._read_response(),
            timeout=DEFAULT_TIMEOUT,
        )

    async def _read_response(self) -> str | None:
        """Read a response from the player."""
        if not self._reader:
            return None

        try:
            data = await self._reader.readuntil(b"\r")
            response = data.decode("ascii").strip()
            _LOGGER.debug("Received response: %s", response)

            # Handle streaming format: @CMD OK/ER ...
            if len(response) > 5 and response[0] == "@" and response[4] == " ":
                payload = response[5:]
                if payload.startswith(("OK", "ER")):
                    return "@" + payload

            # Handle legacy responses without '@' prefix (e.g. "OK CLOSE")
            if response.startswith(("OK", "ER")) and (len(response) == 2 or response[2] == " "):
                return "@" + response
        except asyncio.IncompleteReadError, OSError:
            self._connected = False
            return None
        else:
            return response

    @staticmethod
    def _parse_ok_response(response: str | None) -> str | None:
        """Parse an OK response and return the value after @OK."""
        if response is None:
            return None
        if response.startswith("@OK"):
            return response[4:] if len(response) > 4 else ""
        return None

    # --- Power commands ---

    async def power_on(self) -> bool:
        """Turn the player on."""
        response = self._parse_ok_response(await self._send_command("PON"))
        return response == "ON"

    async def power_off(self) -> bool:
        """Turn the player off."""
        response = self._parse_ok_response(await self._send_command("POF"))
        return response == "OFF"

    async def power_toggle(self) -> PowerState:
        """Toggle power."""
        response = self._parse_ok_response(await self._send_command("POW"))
        if response == "ON":
            return PowerState.ON
        if response == "OFF":
            return PowerState.OFF
        return PowerState.UNKNOWN

    # --- Playback commands ---

    async def play(self) -> bool:
        """Start playback."""
        response = await self._send_command("PLA")
        return response is not None and "@OK" in response

    async def pause(self) -> bool:
        """Pause playback."""
        response = await self._send_command("PAU")
        return response is not None and "@OK" in response

    async def stop(self) -> bool:
        """Stop playback."""
        response = await self._send_command("STP")
        return response is not None and "@OK" in response

    async def next_track(self) -> bool:
        """Skip to next track/chapter."""
        response = await self._send_command("NXT")
        return response is not None and "@OK" in response

    async def previous_track(self) -> bool:
        """Skip to previous track/chapter."""
        response = await self._send_command("PRE")
        return response is not None and "@OK" in response

    async def fast_forward(self) -> bool:
        """Fast forward."""
        response = await self._send_command("FWD")
        return response is not None and "@OK" in response

    async def fast_reverse(self) -> bool:
        """Fast reverse."""
        response = await self._send_command("REV")
        return response is not None and "@OK" in response

    # --- Volume commands ---

    async def volume_up(self) -> int | None:
        """Increase volume."""
        response = self._parse_ok_response(await self._send_command("VUP"))
        if response is not None:
            try:
                return int(response)
            except ValueError:
                pass
        return None

    async def volume_down(self) -> int | None:
        """Decrease volume."""
        response = self._parse_ok_response(await self._send_command("VDN"))
        if response is not None:
            try:
                return int(response)
            except ValueError:
                pass
        return None

    async def set_volume(self, volume: int) -> int | None:
        """Set volume (0-100)."""
        volume = max(0, min(100, volume))
        response = self._parse_ok_response(await self._send_command(f"SVL {volume}"))
        if response is not None:
            try:
                return int(response)
            except ValueError:
                pass
        return None

    async def mute_toggle(self) -> bool | None:
        """Toggle mute. Returns True if muted, False if unmuted, None on error."""
        response = self._parse_ok_response(await self._send_command("MUT"))
        if response == "MUTE":
            return True
        if response == "UNMUTE":
            return False
        return None

    # --- Navigation commands ---

    async def home(self) -> bool:
        """Go to home menu."""
        response = await self._send_command("HOM")
        return response is not None and "@OK" in response

    async def navigate_up(self) -> bool:
        """Navigate up."""
        response = await self._send_command("NUP")
        return response is not None and "@OK" in response

    async def navigate_down(self) -> bool:
        """Navigate down."""
        response = await self._send_command("NDN")
        return response is not None and "@OK" in response

    async def navigate_left(self) -> bool:
        """Navigate left."""
        response = await self._send_command("NLT")
        return response is not None and "@OK" in response

    async def navigate_right(self) -> bool:
        """Navigate right."""
        response = await self._send_command("NRT")
        return response is not None and "@OK" in response

    async def select(self) -> bool:
        """Select / Enter."""
        response = await self._send_command("SEL")
        return response is not None and "@OK" in response

    async def return_back(self) -> bool:
        """Return / Back."""
        response = await self._send_command("RET")
        return response is not None and "@OK" in response

    async def top_menu(self) -> bool:
        """Show top menu."""
        response = await self._send_command("TTL")
        return response is not None and "@OK" in response

    async def popup_menu(self) -> bool:
        """Show popup menu."""
        response = await self._send_command("MNU")
        return response is not None and "@OK" in response

    # --- Tray ---

    async def eject_toggle(self) -> str | None:
        """Toggle tray open/close. Returns 'OPEN' or 'CLOSE'."""
        return self._parse_ok_response(await self._send_command("EJT"))

    # --- Input source ---

    async def set_input_source(self, source_id: int) -> str | None:
        """Set input source by numeric id."""
        return self._parse_ok_response(await self._send_command(f"SIS {source_id}"))

    # --- Query commands ---

    async def query_power_status(self) -> PowerState:
        """Query power status."""
        response = self._parse_ok_response(await self._send_command("QPW"))
        if response == "ON":
            return PowerState.ON
        if response == "OFF":
            return PowerState.OFF
        return PowerState.UNKNOWN

    async def query_playback_status(self) -> PlaybackStatus:
        """Query current playback status."""
        response = self._parse_ok_response(await self._send_command("QPL"))
        if response is None:
            return PlaybackStatus.UNKNOWN
        status_map = {
            "PLAY": PlaybackStatus.PLAY,
            "PAUSE": PlaybackStatus.PAUSE,
            "STOP": PlaybackStatus.STOP,
            "STEP": PlaybackStatus.STEP,
            "FREV": PlaybackStatus.FAST_REWIND,
            "FFWD": PlaybackStatus.FAST_FORWARD,
            "SFWD": PlaybackStatus.SLOW_FORWARD,
            "SREV": PlaybackStatus.SLOW_REWIND,
            "SETUP": PlaybackStatus.SETUP,
            "HOME MENU": PlaybackStatus.HOME_MENU,
            "MEDIA CENTER": PlaybackStatus.MEDIA_CENTER,
            "SCREEN SAVER": PlaybackStatus.SCREEN_SAVER,
            "DISC MENU": PlaybackStatus.DISC_MENU,
            "NO DISC": PlaybackStatus.NO_DISC,
            "LOADING": PlaybackStatus.LOADING,
            "OPEN": PlaybackStatus.OPEN,
            "CLOSE": PlaybackStatus.CLOSE,
            "UNKNOW": PlaybackStatus.UNKNOWN,
            "UNKNOWN": PlaybackStatus.UNKNOWN,
        }
        return status_map.get(response, PlaybackStatus.UNKNOWN)

    async def query_volume(self) -> tuple[int | None, bool]:
        """Query volume. Returns (volume_level, is_muted)."""
        response = self._parse_ok_response(await self._send_command("QVL"))
        if response is None:
            return None, False
        if response == "MUTE":
            return None, True
        try:
            return int(response), False
        except ValueError:
            return None, False

    async def query_disc_type(self) -> DiscType:
        """Query disc type."""
        response = self._parse_ok_response(await self._send_command("QDT"))
        if response is None:
            return DiscType.UNKNOWN
        disc_map = {
            "BD-MV": DiscType.BLURAY_MOVIE,
            "DVD-VIDEO": DiscType.DVD_VIDEO,
            "DVD-AUDIO": DiscType.DVD_AUDIO,
            "SACD": DiscType.SACD,
            "CDDA": DiscType.CD_AUDIO,
            "DATA-DISC": DiscType.DATA_DISC,
            "UHBD": DiscType.ULTRA_HD_BLURAY,
            "NO-DISC": DiscType.NO_DISC,
            "UNKNOW-DISC": DiscType.UNKNOWN_DISC,
        }
        return disc_map.get(response, DiscType.UNKNOWN)

    async def query_input_source(self) -> tuple[InputSource, str | None]:
        """Query current input source. Returns (source_enum, raw_response)."""
        response = self._parse_ok_response(await self._send_command("QIS"))
        if response is None:
            return InputSource.UNKNOWN, None
        source_map = {
            "0 BD-PLAYER": InputSource.BLURAY_PLAYER,
            "1 HDMI-IN": InputSource.HDMI_IN,
            "2 ARC-HDMI-OUT": InputSource.ARC_HDMI_OUT,
            "3 OPTICAL-IN": InputSource.OPTICAL,
            "4 COAXIAL-IN": InputSource.COAXIAL,
            "5 USB-AUDIO-IN": InputSource.USB_AUDIO,
        }
        return source_map.get(response, InputSource.UNKNOWN), response

    async def query_track_elapsed_time(self) -> int | None:
        """Query track/title elapsed time in seconds."""
        response = self._parse_ok_response(await self._send_command("QTE"))
        return self._parse_time(response)

    async def query_track_remaining_time(self) -> int | None:
        """Query track/title remaining time in seconds."""
        response = self._parse_ok_response(await self._send_command("QTR"))
        return self._parse_time(response)

    async def query_chapter_elapsed_time(self) -> int | None:
        """Query chapter elapsed time in seconds."""
        response = self._parse_ok_response(await self._send_command("QCE"))
        return self._parse_time(response)

    async def query_chapter_remaining_time(self) -> int | None:
        """Query chapter remaining time in seconds."""
        response = self._parse_ok_response(await self._send_command("QCR"))
        return self._parse_time(response)

    async def query_total_elapsed_time(self) -> int | None:
        """Query total elapsed time in seconds."""
        response = self._parse_ok_response(await self._send_command("QEL"))
        return self._parse_time(response)

    async def query_total_remaining_time(self) -> int | None:
        """Query total remaining time in seconds."""
        response = self._parse_ok_response(await self._send_command("QRE"))
        return self._parse_time(response)

    async def query_track_name(self) -> str | None:
        """Query track name."""
        return self._parse_ok_response(await self._send_command("QTN"))

    async def query_track_album(self) -> str | None:
        """Query track album."""
        return self._parse_ok_response(await self._send_command("QTA"))

    async def query_track_performer(self) -> str | None:
        """Query track performer."""
        return self._parse_ok_response(await self._send_command("QTP"))

    async def query_audio_type(self) -> str | None:
        """Query audio type."""
        return self._parse_ok_response(await self._send_command("QAT"))

    async def query_subtitle_type(self) -> str | None:
        """Query subtitle type."""
        return self._parse_ok_response(await self._send_command("QST"))

    # --- Verbose mode ---

    async def set_verbose_mode(self, mode: int) -> bool:
        """Set verbose mode (0=off, 2=unsolicited updates, 3=detailed).

        Skips sending the SVM command if verbose mode is already set to the requested mode.
        """
        verbose_mode_response = self._parse_ok_response(await self._send_command("QVM"))
        if verbose_mode_response is not None:
            try:
                if int(verbose_mode_response) == mode:
                    return True
            except ValueError:
                pass

        response = self._parse_ok_response(await self._send_command(f"SVM {mode}"))
        return response is not None

    # --- Streaming updates ---

    async def start_streaming(
        self,
        callback: Callable[[tuple[str, str]], None],
        on_disconnect: Callable[[], None] | None = None,
    ) -> None:
        """Start receiving streaming updates from the player.

        First enables verbose mode 3 (detailed unsolicited status updates
        including playback progress), then starts a background reader loop
        that parses incoming events and calls the callback with parsed
        event tuples.

        Args:
            callback: Called with each streaming event tuple.
            on_disconnect: Optional callback called when the connection is lost.

        """
        self._disconnect_callback = on_disconnect
        self._stop_streaming_requested = False
        # Keep only the active subscriber callback to avoid duplicated events
        # after reconnect cycles.
        self._streaming_callbacks = [callback]
        await self.set_verbose_mode(3)
        if self._streaming_task and not self._streaming_task.done():
            return
        self._streaming_task = asyncio.create_task(self._streaming_loop())

    async def stop_streaming(self) -> None:
        """Stop streaming updates."""
        self._stop_streaming_requested = True
        if self._streaming_task and not self._streaming_task.done():
            self._streaming_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._streaming_task
        self._streaming_task = None
        self._streaming_callbacks.clear()

    async def _streaming_loop(self) -> None:
        """Background loop reading streaming events from the player."""
        try:
            while self._connected and self._reader:
                try:
                    data = await self._reader.readuntil(b"\r")
                    frame = data.decode("ascii").strip()
                    if not frame:
                        continue

                    # Check if this is a command response (for pending commands)
                    if self._try_complete_pending_response(frame):
                        continue

                    event = self._parse_streaming_frame(frame)
                    if event:
                        for cb in self._streaming_callbacks:
                            try:
                                cb(event)
                            except Exception:
                                _LOGGER.exception("Error in streaming callback")
                except asyncio.CancelledError:
                    raise
                except asyncio.IncompleteReadError, OSError:
                    _LOGGER.debug("Streaming connection lost")
                    self._connected = False
                    break
                except Exception:
                    _LOGGER.exception("Error in streaming loop")
                    await asyncio.sleep(1)
        finally:
            # Complete any pending command response with None
            if self._pending_response and not self._pending_response.done():
                self._pending_response.set_result(None)

            self._streaming_callbacks.clear()

            # On unexpected disconnect, explicitly close transport and clear
            # stream objects to avoid stale writer/reader references.
            if not self._stop_streaming_requested:
                writer = self._writer
                self._writer = None
                self._reader = None
                self._connected = False
                if writer is not None:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:  # noqa: BLE001
                        _LOGGER.debug("Error closing writer after streaming disconnect")

            # Notify the caller that the connection was lost
            if not self._stop_streaming_requested and self._disconnect_callback is not None:
                try:
                    self._disconnect_callback()
                except Exception:
                    _LOGGER.exception("Error in disconnect callback")

    def _try_complete_pending_response(self, frame: str) -> bool:
        """Try to dispatch a frame as a command response.

        Returns True if the frame was consumed as a command response.
        """
        if self._pending_response is None or self._pending_response.done():
            return False

        # Direct @OK/@ER response
        if frame.startswith(("@OK", "@ER")):
            self._pending_response.set_result(frame)
            return True

        # Legacy response without '@' prefix
        if frame.startswith(("OK", "ER")) and (len(frame) == 2 or frame[2] == " "):
            self._pending_response.set_result("@" + frame)
            return True

        # Streaming format command response: @CMD OK/ER ...
        if len(frame) > 5 and frame[0] == "@" and frame[4] == " ":
            payload = frame[5:]
            if payload.startswith(("OK", "ER")) and (len(payload) == 2 or payload[2] == " "):
                self._pending_response.set_result("@" + payload)
                return True

        return False

    def _parse_streaming_frame(self, frame: str) -> tuple[str, str] | None:
        """Parse a streaming frame into an event tuple.

        Returns a tuple like ('power', 'on'), ('playback', 'play'), etc.
        Returns None if the frame is not a recognized streaming event.
        """
        if len(frame) < 6 or frame[0] != "@" or frame[4] != " ":
            return None

        code = frame[1:4]
        value = frame[5:]

        if code == "UPW":
            state = "on" if value == "1" else "off"
            return "power", state

        if code == "UPL":
            status = self._parse_streaming_playback(value)
            return "playback", status

        if code == "UVL":
            if value == "MUT":
                return "volume", "mute"
            try:
                return "volume", str(int(value))
            except ValueError:
                return "volume", value

        if code == "UDT":
            disc_type = self._parse_streaming_disc_type(value)
            return "disc_type", disc_type

        if code == "UAT":
            return "audio_type", value

        if code == "UST":
            return "subtitle_type", value

        if code == "UIS":
            return "input_source", value

        if code == "UTC":
            return "time_code", value

        if code == "UVO":
            return "video_resolution", value

        return "unknown", f"{code} {value}"

    @staticmethod
    def _parse_streaming_playback(value: str) -> str:
        """Parse a streaming playback status update value."""
        status_map = {
            "DISC": "no_disc",
            "LOAD": "loading",
            "OPEN": "open",
            "CLOS": "close",
            "PLAY": "play",
            "PAUS": "pause",
            "STOP": "stop",
            "HOME": "home_menu",
            "MCTR": "media_center",
            "SCSV": "screen_saver",
            "MENU": "disc_menu",
        }
        if value in status_map:
            return status_map[value]
        if value.startswith("FFW"):
            return "fast_forward"
        if value.startswith(("FRV", "FRE")):
            return "fast_rewind"
        if value.startswith("SFW"):
            return "slow_forward"
        if value.startswith(("SRV", "SRE")):
            return "slow_rewind"
        if value in ("STPF", "STPR"):
            return "step"
        return "unknown"

    @staticmethod
    def _parse_streaming_disc_type(value: str) -> str:
        """Parse a streaming disc type update value (4-char codes)."""
        disc_type_map = {
            "UHBD": DiscType.ULTRA_HD_BLURAY.value,
            "BDMV": DiscType.BLURAY_MOVIE.value,
            "DVDV": DiscType.DVD_VIDEO.value,
            "DVDA": DiscType.DVD_AUDIO.value,
            "SACD": DiscType.SACD.value,
            "CDDA": DiscType.CD_AUDIO.value,
            "DATA": DiscType.DATA_DISC.value,
            "UNKW": DiscType.UNKNOWN_DISC.value,
        }
        return disc_type_map.get(value, DiscType.UNKNOWN.value)

    @staticmethod
    def _parse_time(response: str | None) -> int | None:
        """Parse a time response (HH:MM:SS) into total seconds."""
        if response is None:
            return None
        parts = response.split(":")
        if len(parts) != 3:
            return None
        try:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = int(parts[2])
            return hours * 3600 + minutes * 60 + seconds
        except ValueError:
            return None
