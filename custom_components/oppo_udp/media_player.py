"""Media player platform for Oppo UDP-20X."""

from __future__ import annotations

import contextlib
from datetime import datetime
import logging

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import CALLBACK_TYPE, HassJob, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import (
    CONF_MODEL,
    DEFAULT_PORT,
    DOMAIN,
    INPUT_SOURCES_UDP203,
    INPUT_SOURCES_UDP205,
    MODEL_UDP205,
)
from .oppo_client import OppoClient, PlaybackStatus, PowerState

_LOGGER = logging.getLogger(__name__)


PLAYBACK_TO_STATE = {
    PlaybackStatus.PLAY: MediaPlayerState.PLAYING,
    PlaybackStatus.PAUSE: MediaPlayerState.PAUSED,
    PlaybackStatus.STOP: MediaPlayerState.IDLE,
    PlaybackStatus.FAST_FORWARD: MediaPlayerState.PLAYING,
    PlaybackStatus.FAST_REWIND: MediaPlayerState.PLAYING,
    PlaybackStatus.SLOW_FORWARD: MediaPlayerState.PLAYING,
    PlaybackStatus.SLOW_REWIND: MediaPlayerState.PLAYING,
    PlaybackStatus.STEP: MediaPlayerState.PAUSED,
    PlaybackStatus.HOME_MENU: MediaPlayerState.IDLE,
    PlaybackStatus.MEDIA_CENTER: MediaPlayerState.IDLE,
    PlaybackStatus.SCREEN_SAVER: MediaPlayerState.IDLE,
    PlaybackStatus.DISC_MENU: MediaPlayerState.IDLE,
    PlaybackStatus.NO_DISC: MediaPlayerState.IDLE,
    PlaybackStatus.LOADING: MediaPlayerState.BUFFERING,
    PlaybackStatus.OPEN: MediaPlayerState.IDLE,
    PlaybackStatus.CLOSE: MediaPlayerState.IDLE,
    PlaybackStatus.SETUP: MediaPlayerState.IDLE,
}

STREAMING_PLAYBACK_TO_STATE = {
    "play": MediaPlayerState.PLAYING,
    "pause": MediaPlayerState.PAUSED,
    "stop": MediaPlayerState.IDLE,
    "fast_forward": MediaPlayerState.PLAYING,
    "fast_rewind": MediaPlayerState.PLAYING,
    "slow_forward": MediaPlayerState.PLAYING,
    "slow_rewind": MediaPlayerState.PLAYING,
    "step": MediaPlayerState.PAUSED,
    "home_menu": MediaPlayerState.IDLE,
    "media_center": MediaPlayerState.IDLE,
    "screen_saver": MediaPlayerState.IDLE,
    "disc_menu": MediaPlayerState.IDLE,
    "no_disc": MediaPlayerState.IDLE,
    "loading": MediaPlayerState.BUFFERING,
    "open": MediaPlayerState.IDLE,
    "close": MediaPlayerState.IDLE,
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Oppo UDP-20X media player from a config entry."""
    host = config_entry.data[CONF_HOST]
    port = config_entry.data.get(CONF_PORT, DEFAULT_PORT)
    name = config_entry.data.get(CONF_NAME, "Oppo UDP-20X")
    model = config_entry.data.get(CONF_MODEL, "UDP-203")

    client = OppoClient(host, port=port)
    entity = OppoUDPMediaPlayer(client, name, model, config_entry.entry_id)
    async_add_entities([entity])


class OppoUDPMediaPlayer(MediaPlayerEntity):
    """Representation of an Oppo UDP-20X media player."""

    _attr_device_class = MediaPlayerDeviceClass.RECEIVER
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        client: OppoClient,
        name: str,
        model: str,
        entry_id: str,
    ) -> None:
        """Initialize the Oppo UDP-20X media player."""
        self._client = client
        self._attr_name = name
        self._attr_unique_id = f"oppo_udp_{entry_id}"
        self._model = model
        self._entry_id = entry_id

        # State
        self._power_state = PowerState.UNKNOWN
        self._playback_status = PlaybackStatus.UNKNOWN
        self._volume_level: float | None = None
        self._is_muted: bool = False
        self._media_title: str | None = None
        self._media_album: str | None = None
        self._media_artist: str | None = None
        self._media_position: int | None = None
        self._media_duration: int | None = None
        self._current_source: str | None = None
        self._disc_type: str | None = None
        self._audio_type: str | None = None
        self._subtitle_type: str | None = None
        self._streaming_active = False
        self._unsub_reconnect: CALLBACK_TYPE | None = None
        self._last_title: int | None = None
        self._last_chapter: int | None = None

        # Input sources based on model
        if model == MODEL_UDP205:
            self._source_list = list(INPUT_SOURCES_UDP205.keys())
            self._source_map = INPUT_SOURCES_UDP205
        else:
            self._source_list = list(INPUT_SOURCES_UDP203.keys())
            self._source_map = INPUT_SOURCES_UDP203

    @property
    def device_info(self):
        """Return device info."""
        return {
            "identifiers": {(DOMAIN, self._client.host)},
            "name": self._attr_name,
            "manufacturer": "Oppo Digital",
            "model": self._model,
        }

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        """Return the supported features."""
        return (
            MediaPlayerEntityFeature.TURN_ON
            | MediaPlayerEntityFeature.TURN_OFF
            | MediaPlayerEntityFeature.PLAY
            | MediaPlayerEntityFeature.PAUSE
            | MediaPlayerEntityFeature.STOP
            | MediaPlayerEntityFeature.NEXT_TRACK
            | MediaPlayerEntityFeature.PREVIOUS_TRACK
            | MediaPlayerEntityFeature.VOLUME_SET
            | MediaPlayerEntityFeature.VOLUME_STEP
            | MediaPlayerEntityFeature.VOLUME_MUTE
            | MediaPlayerEntityFeature.SELECT_SOURCE
        )

    @property
    def state(self) -> MediaPlayerState | None:
        """Return the state of the player."""
        if self._power_state == PowerState.OFF:
            return MediaPlayerState.OFF
        if self._power_state == PowerState.UNKNOWN:
            return None
        return PLAYBACK_TO_STATE.get(self._playback_status, MediaPlayerState.IDLE)

    @property
    def volume_level(self) -> float | None:
        """Return volume level (0..1)."""
        return self._volume_level

    @property
    def is_volume_muted(self) -> bool:
        """Return True if volume is muted."""
        return self._is_muted

    @property
    def media_title(self) -> str | None:
        """Return the media title."""
        return self._media_title

    @property
    def media_album_name(self) -> str | None:
        """Return the media album."""
        return self._media_album

    @property
    def media_artist(self) -> str | None:
        """Return the media artist."""
        return self._media_artist

    @property
    def media_position(self) -> int | None:
        """Return the media position in seconds."""
        return self._media_position

    @property
    def media_duration(self) -> int | None:
        """Return the media duration in seconds."""
        return self._media_duration

    @property
    def media_content_type(self) -> MediaType | None:
        """Return the content type."""
        if self._disc_type in ("cdda", "sacd", "dvd-audio"):
            return MediaType.MUSIC
        if self._disc_type in ("bd-mv", "dvd-video", "uhbd", "data-disc"):
            return MediaType.VIDEO
        return None

    @property
    def source(self) -> str | None:
        """Return current source."""
        return self._current_source

    @property
    def source_list(self) -> list[str]:
        """Return the available sources."""
        return self._source_list

    @property
    def extra_state_attributes(self):
        """Return extra state attributes."""
        attrs = {}
        if self._disc_type:
            attrs["disc_type"] = self._disc_type
        if self._audio_type:
            attrs["audio_type"] = self._audio_type
        return attrs

    async def async_added_to_hass(self) -> None:
        """Run when entity is added to hass."""
        await super().async_added_to_hass()
        await self._connect_and_stream()

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity is removed from hass."""
        self._reconnect_cancel()
        await self._client.stop_streaming()
        await self._client.disconnect()

    async def _connect_and_stream(self) -> None:
        """Connect and start streaming updates, schedule reconnect on failure."""
        if await self._client.connect():
            # Query initial state
            await self._fetch_initial_state()
            # Start streaming with disconnect handler
            await self._client.start_streaming(
                self._handle_streaming_event,
                on_disconnect=self._handle_disconnect,
            )
            self._streaming_active = True
        else:
            # Schedule a reconnection attempt
            self._schedule_reconnect()

    async def _fetch_initial_state(self) -> None:
        """Fetch full state snapshot from the player after connecting."""
        try:
            self._power_state = await self._client.query_power_status()
            if self._power_state == PowerState.ON:
                await self._poll_powered_on_state()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Error fetching initial state", exc_info=True)
        self.async_write_ha_state()

    async def _poll_powered_on_state(self) -> None:
        """Poll all state when the player is powered on."""
        self._playback_status = await self._client.query_playback_status()

        volume, muted = await self._client.query_volume()
        self._is_muted = muted
        if volume is not None:
            self._volume_level = volume / 100.0

        _source, raw = await self._client.query_input_source()
        if raw:
            self._current_source = self._map_input_source_response(raw)

        self._disc_type = (await self._client.query_disc_type()).value

        # Only poll active playback details if actually playing/paused with a
        # known disc type (querying with unknown/data disc can cause issues)
        if self._playback_status in (
            PlaybackStatus.PLAY,
            PlaybackStatus.PAUSE,
        ) and self._disc_type not in ("unknown", "unknown-disc", "data-disc"):
            await self._poll_active_playback_state()

    async def _poll_active_playback_state(self) -> None:
        """Poll details only available during active playback."""
        is_movie = self._disc_type in ("bd-mv", "dvd-video", "uhbd")

        # Use chapter time for video discs, track/title time for audio discs
        if is_movie:
            elapsed = await self._client.query_chapter_elapsed_time()
            remaining = await self._client.query_chapter_remaining_time()
        else:
            elapsed = await self._client.query_track_elapsed_time()
            remaining = await self._client.query_track_remaining_time()
        if elapsed is not None:
            self._media_position = elapsed
        if elapsed is not None and remaining is not None:
            self._media_duration = elapsed + remaining

        # If elapsed is 0, we're likely at a title/menu screen — querying
        # further details can produce errors and lock up the player.
        if not elapsed or not remaining:
            return

        # Track metadata (only available/relevant for audio discs)
        if not is_movie:
            self._media_title = await self._client.query_track_name()
            self._media_album = await self._client.query_track_album()
            self._media_artist = await self._client.query_track_performer()

        # Audio type (always available during active playback)
        self._audio_type = await self._client.query_audio_type()

        # Subtitle info (only relevant for video discs)
        if is_movie:
            self._subtitle_type = await self._client.query_subtitle_type()

    def _schedule_reconnect(self) -> None:
        """Schedule a reconnection attempt."""
        self._reconnect_cancel()
        self._unsub_reconnect = async_call_later(
            self.hass,
            30,
            HassJob(self._reconnect_callback),
        )

    def _reconnect_cancel(self) -> None:
        """Cancel any pending reconnection."""
        if self._unsub_reconnect is not None:
            self._unsub_reconnect()
            self._unsub_reconnect = None

    async def _reconnect_callback(self, _now: datetime) -> None:
        """Attempt to reconnect."""
        self._unsub_reconnect = None
        if not self._client.connected:
            await self._connect_and_stream()

    @callback
    def _handle_disconnect(self) -> None:
        """Handle connection loss — mark unavailable and schedule reconnect."""
        self._streaming_active = False
        self._power_state = PowerState.UNKNOWN
        self.async_write_ha_state()
        self._schedule_reconnect()

    @callback
    def _handle_streaming_event(self, event: tuple[str, ...]) -> None:
        """Handle a streaming event from the player."""
        if not event:
            return

        event_type = event[0]

        if event_type == "power":
            if event[1] == "on":
                self._power_state = PowerState.ON
                # Player turned on — rebuild state
                self.hass.async_create_task(self._rebuild_snapshot())
            else:
                self._power_state = PowerState.OFF
                self._clear_playback_state()

        elif event_type == "playback":
            prev_status = self._playback_status
            self._playback_status = self._streaming_playback_to_enum(event[1])
            # Transition from non-active to active playback — full rebuild
            was_active = prev_status in (PlaybackStatus.PLAY, PlaybackStatus.PAUSE)
            is_active = self._playback_status in (
                PlaybackStatus.PLAY,
                PlaybackStatus.PAUSE,
            )
            if is_active and not was_active:
                self.hass.async_create_task(self._rebuild_snapshot())
                return
            if not is_active:
                self._clear_playback_state()

        elif event_type == "volume":
            if event[1] == "mute":
                self._is_muted = True
            else:
                self._is_muted = False
                with contextlib.suppress(ValueError):
                    self._volume_level = int(event[1]) / 100.0

        elif event_type == "disc_type":
            self._disc_type = event[1]
            # Disc change invalidates everything — rebuild
            self.hass.async_create_task(self._rebuild_snapshot())
            return

        elif event_type == "input_source":
            self._current_source = self._map_input_source_response(event[1])
            # Source change invalidates track metadata — rebuild
            self.hass.async_create_task(self._rebuild_snapshot())
            return

        elif event_type == "audio_type":
            self._audio_type = event[1]

        elif event_type == "subtitle_type":
            self._subtitle_type = event[1]

        elif event_type == "time_code":
            self._handle_time_code_event(event[1])
            return  # _handle_time_code_event calls async_write_ha_state if needed

        self.async_write_ha_state()

    def _handle_time_code_event(self, value: str) -> None:
        """Handle a streaming time code event, rebuild on title/chapter change."""
        parts = value.split(" ")
        if len(parts) < 4:
            return

        with contextlib.suppress(ValueError):
            title = int(parts[0])
            chapter = int(parts[1])

            # Title or chapter changed — rebuild snapshot with fresh metadata
            if title != self._last_title or chapter != self._last_chapter:
                self._last_title = title
                self._last_chapter = chapter
                self.hass.async_create_task(self._rebuild_snapshot())
                return

        # Same title/chapter — just update position
        self._parse_time_code_event(value)
        self.async_write_ha_state()

    def _clear_playback_state(self) -> None:
        """Clear playback-specific state fields."""
        self._playback_status = PlaybackStatus.UNKNOWN
        self._media_position = None
        self._media_duration = None
        self._media_title = None
        self._media_album = None
        self._media_artist = None
        self._audio_type = None
        self._subtitle_type = None
        self._last_title = None
        self._last_chapter = None

    async def _rebuild_snapshot(self) -> None:
        """Re-poll all state from the player (called on significant changes)."""
        try:
            await self._poll_powered_on_state()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Error rebuilding snapshot", exc_info=True)
        self.async_write_ha_state()

    def _streaming_playback_to_enum(self, status: str) -> PlaybackStatus:
        """Convert streaming playback string to PlaybackStatus enum."""
        mapping = {
            "play": PlaybackStatus.PLAY,
            "pause": PlaybackStatus.PAUSE,
            "stop": PlaybackStatus.STOP,
            "fast_forward": PlaybackStatus.FAST_FORWARD,
            "fast_rewind": PlaybackStatus.FAST_REWIND,
            "slow_forward": PlaybackStatus.SLOW_FORWARD,
            "slow_rewind": PlaybackStatus.SLOW_REWIND,
            "step": PlaybackStatus.STEP,
            "home_menu": PlaybackStatus.HOME_MENU,
            "media_center": PlaybackStatus.MEDIA_CENTER,
            "screen_saver": PlaybackStatus.SCREEN_SAVER,
            "disc_menu": PlaybackStatus.DISC_MENU,
            "no_disc": PlaybackStatus.NO_DISC,
            "loading": PlaybackStatus.LOADING,
            "open": PlaybackStatus.OPEN,
            "close": PlaybackStatus.CLOSE,
        }
        return mapping.get(status, PlaybackStatus.UNKNOWN)

    def _map_input_source_response(self, raw: str) -> str | None:
        """Map a raw input source response to a friendly name."""
        source_response_map = {
            "0 BD-PLAYER": "Blu-Ray Player",
            "1 HDMI-IN": "HDMI In",
            "2 ARC-HDMI-OUT": "ARC HDMI Out",
            "3 OPTICAL-IN": "Optical",
            "4 COAXIAL-IN": "Coaxial",
            "5 USB-AUDIO-IN": "USB Audio",
        }
        return source_response_map.get(raw, raw)

    def _parse_time_code_event(self, value: str) -> None:
        """Parse a streaming time code event: 'TT CC T HH:MM:SS'."""
        parts = value.split(" ")
        if len(parts) < 4:
            return
        time_type = parts[2] if len(parts) > 2 else ""
        time_str = parts[3] if len(parts) > 3 else ""
        seconds = self._parse_time_str(time_str)
        if seconds is None:
            return

        if time_type == "E":  # Total elapsed
            self._media_position = seconds
        elif time_type == "R":  # Total remaining
            if self._media_position is not None:
                self._media_duration = self._media_position + seconds
            else:
                self._media_duration = seconds
        elif time_type == "T":  # Title/track elapsed
            self._media_position = seconds
        elif time_type == "X" and self._media_position is not None:  # Title remaining
            self._media_duration = self._media_position + seconds
        elif time_type == "C":  # Chapter elapsed
            self._media_position = seconds
        elif time_type == "K" and self._media_position is not None:  # Chapter remaining
            self._media_duration = self._media_position + seconds

    @staticmethod
    def _parse_time_str(time_str: str) -> int | None:
        """Parse HH:MM:SS to seconds."""
        parts = time_str.split(":")
        if len(parts) != 3:
            return None
        try:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except ValueError:
            return None

    # --- Commands ---

    async def async_turn_on(self) -> None:
        """Turn the player on."""
        await self._client.power_on()

    async def async_turn_off(self) -> None:
        """Turn the player off."""
        await self._client.power_off()

    async def async_media_play(self) -> None:
        """Send play command."""
        await self._client.play()

    async def async_media_pause(self) -> None:
        """Send pause command."""
        await self._client.pause()

    async def async_media_stop(self) -> None:
        """Send stop command."""
        await self._client.stop()

    async def async_media_next_track(self) -> None:
        """Send next track command."""
        await self._client.next_track()

    async def async_media_previous_track(self) -> None:
        """Send previous track command."""
        await self._client.previous_track()

    async def async_set_volume_level(self, volume: float) -> None:
        """Set volume level (0..1)."""
        vol_int = int(volume * 100)
        result = await self._client.set_volume(vol_int)
        if result is not None:
            self._volume_level = result / 100.0

    async def async_volume_up(self) -> None:
        """Turn volume up."""
        result = await self._client.volume_up()
        if result is not None:
            self._volume_level = result / 100.0

    async def async_volume_down(self) -> None:
        """Turn volume down."""
        result = await self._client.volume_down()
        if result is not None:
            self._volume_level = result / 100.0

    async def async_mute_volume(self, mute: bool) -> None:
        """Mute/unmute the volume."""
        result = await self._client.mute_toggle()
        if result is not None:
            self._is_muted = result

    async def async_select_source(self, source: str) -> None:
        """Select an input source."""
        source_id = self._source_map.get(source)
        if source_id is not None:
            await self._client.set_input_source(source_id)
            self._current_source = source
