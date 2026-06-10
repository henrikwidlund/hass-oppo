"""Media player platform for Oppo UDP-20X."""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any, override

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    RepeatMode as HARepeatMode,
)
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import CALLBACK_TYPE, HassJob, HomeAssistant, callback
from homeassistant.helpers import entity_platform
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .const import CONF_MODEL, DEFAULT_PORT, DOMAIN, INPUT_SOURCES_UDP203, INPUT_SOURCES_UDP205, MODEL_UDP205
from .oppo_client import OppoClient, PlaybackStatus, PowerState, RepeatMode

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.entity_platform import AddEntitiesCallback


_OPPO_TO_HA_REPEAT: dict[RepeatMode, HARepeatMode] = {
    RepeatMode.OFF: HARepeatMode.OFF,
    RepeatMode.CHAPTER: HARepeatMode.ONE,
    RepeatMode.TITLE: HARepeatMode.ONE,
    RepeatMode.ALL: HARepeatMode.ALL,
    RepeatMode.SHUFFLE: HARepeatMode.OFF,
    RepeatMode.RANDOM: HARepeatMode.OFF,
}

_AUDIO_DISC_TYPES = frozenset({"cdda", "sacd", "dvd-audio"})


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


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
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

    platform = entity_platform.async_get_current_platform()
    for service_name, method in _ENTITY_SERVICES:
        platform.async_register_entity_service(service_name, None, method)


_ENTITY_SERVICES: tuple[tuple[str, str], ...] = (
    ("dimmer", "async_dimmer"),
    ("pure_audio_toggle", "async_pure_audio_toggle"),
    ("info_toggle", "async_info_toggle"),
    ("audio_language_toggle", "async_audio_language_toggle"),
    ("subtitle_toggle", "async_subtitle_toggle"),
    ("zoom", "async_zoom"),
)


class OppoUDPMediaPlayer(MediaPlayerEntity):
    """Representation of an Oppo UDP-20X media player."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_translation_key = "oppo_udp"

    def __init__(
        self,
        client: OppoClient,
        name: str,
        model: str,
        entry_id: str,
    ) -> None:
        """Initialize the Oppo UDP-20X media player."""
        self._client = client
        self._name = name
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
        self._media_position_updated_at: datetime | None = None
        self._media_duration: int | None = None
        self._current_source: str | None = None
        self._disc_type: str | None = None
        self._audio_type: str | None = None
        self._subtitle_type: str | None = None
        self._aspect_ratio: str | None = None
        self._three_d: str | None = None
        self._video_resolution: str | None = None
        self._repeat: HARepeatMode = HARepeatMode.OFF
        self._streaming_active = False
        self._unsub_reconnect: CALLBACK_TYPE | None = None
        self._last_title: int | None = None
        self._rebuild_in_progress = False

        # Input sources based on model
        if model == MODEL_UDP205:
            self._source_list = list(INPUT_SOURCES_UDP205.keys())
            self._source_map = INPUT_SOURCES_UDP205
        else:
            self._source_list = list(INPUT_SOURCES_UDP203.keys())
            self._source_map = INPUT_SOURCES_UDP203

    @property
    @override
    def device_info(self) -> DeviceInfo | None:  # pyright: ignore [reportIncompatibleVariableOverride]
        """Return device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._client.host)},
            name=self._name,
            manufacturer="Oppo Digital",
            model=self._model,
        )

    @property
    @override
    def supported_features(self) -> MediaPlayerEntityFeature:  # pyright: ignore [reportIncompatibleVariableOverride]
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
            | MediaPlayerEntityFeature.REPEAT_SET
        )

    @property
    @override
    def available(self) -> bool:  # pyright: ignore [reportIncompatibleVariableOverride]
        """Return if the entity is currently available."""
        return self._client.connected

    @property
    @override
    def state(self) -> MediaPlayerState | None:  # pyright: ignore [reportIncompatibleVariableOverride]
        """Return the state of the player."""
        if self._power_state == PowerState.OFF:
            return MediaPlayerState.OFF
        if self._power_state == PowerState.UNKNOWN:
            return None
        return PLAYBACK_TO_STATE.get(self._playback_status, MediaPlayerState.IDLE)

    @property
    @override
    def volume_level(self) -> float | None:  # pyright: ignore [reportIncompatibleVariableOverride]
        """Return volume level (0..1)."""
        return self._volume_level

    @property
    @override
    def is_volume_muted(self) -> bool:  # pyright: ignore [reportIncompatibleVariableOverride]
        """Return True if volume is muted."""
        return self._is_muted

    @property
    @override
    def media_title(self) -> str | None:  # pyright: ignore [reportIncompatibleVariableOverride]
        """Return the media title."""
        return self._media_title

    @property
    @override
    def media_album_name(self) -> str | None:  # pyright: ignore [reportIncompatibleVariableOverride]
        """Return the media album."""
        return self._media_album

    @property
    @override
    def media_artist(self) -> str | None:  # pyright: ignore [reportIncompatibleVariableOverride]
        """Return the media artist."""
        return self._media_artist

    @property
    @override
    def media_position(self) -> int | None:  # pyright: ignore [reportIncompatibleVariableOverride]
        """Return the media position in seconds."""
        return self._media_position

    @property
    @override
    def media_position_updated_at(self) -> datetime | None:  # pyright: ignore [reportIncompatibleVariableOverride]
        """Return when media_position was last updated."""
        return self._media_position_updated_at

    @property
    @override
    def media_duration(self) -> int | None:  # pyright: ignore [reportIncompatibleVariableOverride]
        """Return the media duration in seconds."""
        return self._media_duration

    @property
    @override
    def media_content_type(self) -> MediaType | None:  # pyright: ignore [reportIncompatibleVariableOverride]
        """Return the content type."""
        if self._disc_type in ("cdda", "sacd", "dvd-audio"):
            return MediaType.MUSIC
        if self._disc_type in ("bd-mv", "dvd-video", "uhbd", "data-disc"):
            return MediaType.VIDEO
        return None

    @property
    @override
    def source(self) -> str | None:  # pyright: ignore [reportIncompatibleVariableOverride]
        """Return current source."""
        return self._current_source

    @property
    @override
    def source_list(self) -> list[str]:  # pyright: ignore [reportIncompatibleVariableOverride]
        """Return the available sources."""
        return self._source_list

    @property
    @override
    def repeat(self) -> HARepeatMode | None:  # pyright: ignore [reportIncompatibleVariableOverride]
        """Return current repeat mode."""
        return self._repeat

    @property
    @override
    def extra_state_attributes(self) -> Mapping[str, Any] | None:  # pyright: ignore [reportIncompatibleVariableOverride]
        """Return extra state attributes."""
        attrs: dict[str, str] = {}
        if self._disc_type:
            attrs["disc_type"] = self._disc_type
        if self._audio_type:
            attrs["audio_type"] = self._audio_type
        if self._subtitle_type:
            attrs["subtitle_type"] = self._subtitle_type
        if self._aspect_ratio:
            attrs["aspect_ratio"] = self._aspect_ratio
        if self._three_d:
            attrs["three_d"] = self._three_d
        if self._video_resolution:
            attrs["video_resolution"] = self._video_resolution
        return attrs

    @override
    async def async_added_to_hass(self) -> None:
        """Run when entity is added to hass."""
        await super().async_added_to_hass()
        await self._connect_and_stream()

    @override
    async def async_will_remove_from_hass(self) -> None:
        """Run when entity is removed from hass."""
        self._reconnect_cancel()
        await self._client.stop_streaming()
        await self._client.disconnect()

    async def _connect_and_stream(self) -> None:
        """Connect and start streaming updates, schedule reconnect on failure."""
        if not await self._client.connect():
            self._schedule_reconnect()
            return

        # Query initial state
        await self._fetch_initial_state()
        # Start streaming with disconnect handler
        if not await self._client.start_streaming(
            self._handle_streaming_event,
            on_disconnect=self._handle_disconnect,
        ):
            # Verbose mode could not be enabled — treat as disconnect.
            self._streaming_active = False
            self._schedule_reconnect()
            return
        self._streaming_active = True

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

        repeat_mode = await self._client.query_repeat_mode()
        # Fall back to OFF if the player returned an unknown or unmapped mode
        # so we don't leave a stale value visible in the UI.
        self._repeat = _OPPO_TO_HA_REPEAT.get(repeat_mode, HARepeatMode.OFF)

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

        # For movie discs use total elapsed/remaining (QEL/QRE) which matches
        # the streaming @UTC time code (E/R) and reports full movie progress.
        # For audio discs use track elapsed/remaining (QTE/QTR).
        if is_movie:
            elapsed = await self._client.query_total_elapsed_time()
            remaining = await self._client.query_total_remaining_time()
        else:
            elapsed = await self._client.query_track_elapsed_time()
            remaining = await self._client.query_track_remaining_time()
        if elapsed is not None:
            self._set_media_position(elapsed)
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
        # Skip if duration is less than 60s (most likely title screen)
        if is_movie and (duration := self._media_duration) is not None and duration >= 60:
            self._subtitle_type = await self._client.query_subtitle_type()
        else:
            self._subtitle_type = None

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
    def _handle_streaming_event(self, event: tuple[str, str]) -> None:
        """Handle a streaming event from the player."""
        event_type = event[0]

        if event_type == "power":
            if event[1] == "on":
                self._power_state = PowerState.ON
                # Write immediately so the UI reflects power state without
                # waiting for the rebuild snapshot to complete.
                self.async_write_ha_state()
                self._schedule_rebuild_snapshot()
                return
            self._power_state = PowerState.OFF
            self._clear_all_state()

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
                # Push play/pause state immediately; rebuild fills in metadata.
                self.async_write_ha_state()
                self._schedule_rebuild_snapshot()
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
            self._schedule_rebuild_snapshot()
            return

        elif event_type == "input_source":
            self._current_source = self._map_input_source_response(event[1])
            # Source change invalidates track metadata — rebuild
            self._schedule_rebuild_snapshot()
            return

        elif event_type == "audio_type":
            self._audio_type = event[1]

        elif event_type == "subtitle_type":
            self._subtitle_type = event[1]

        elif event_type == "aspect_ratio":
            self._aspect_ratio = event[1].lower()

        elif event_type == "three_d":
            self._three_d = event[1]

        elif event_type == "video_resolution":
            self._video_resolution = event[1]

        elif event_type == "time_code":
            self._handle_time_code_event(event[1])
            return  # _handle_time_code_event calls async_write_ha_state if needed

        self.async_write_ha_state()

    def _schedule_rebuild_snapshot(self) -> None:
        """Schedule a rebuild task, deduping concurrent requests."""
        if self._rebuild_in_progress:
            return
        self._rebuild_in_progress = True
        self.hass.async_create_task(self._rebuild_snapshot(), "oppo_udp_rebuild_snapshot")

    def _handle_time_code_event(self, value: str) -> None:
        """Handle a streaming time code event, rebuild on title change."""
        parts = value.split(" ")
        if len(parts) < 4:
            return

        with contextlib.suppress(ValueError):
            title = int(parts[0])

            # Title changed — rebuild snapshot with fresh metadata.
            if title != self._last_title:
                self._last_title = title
                # Apply the current sample immediately to avoid a visible
                # position freeze/jump while waiting for the rebuild.
                self._parse_time_code_event(value)
                self.async_write_ha_state()
                self._schedule_rebuild_snapshot()
                return

        # Same title — just update position
        self._parse_time_code_event(value)
        self.async_write_ha_state()

    def _clear_playback_state(self) -> None:
        """Clear playback-specific state fields."""
        self._playback_status = PlaybackStatus.UNKNOWN
        self._media_position = None
        self._media_position_updated_at = None
        self._media_duration = None
        self._media_title = None
        self._media_album = None
        self._media_artist = None
        self._audio_type = None
        self._subtitle_type = None
        self._last_title = None

    def _clear_all_state(self) -> None:
        """Clear all state (used on power off)."""
        self._clear_playback_state()
        self._disc_type = None
        self._volume_level = None
        self._is_muted = False
        self._current_source = None
        self._aspect_ratio = None
        self._three_d = None
        self._video_resolution = None
        self._repeat = HARepeatMode.OFF

    async def _rebuild_snapshot(self) -> None:
        """Re-poll all state from the player (called on significant changes)."""
        try:
            await self._poll_powered_on_state()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Error rebuilding snapshot", exc_info=True)
        finally:
            self._rebuild_in_progress = False
        self.async_write_ha_state()

    @staticmethod
    def _streaming_playback_to_enum(status: str) -> PlaybackStatus:
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

    @staticmethod
    def _map_input_source_response(raw: str) -> str | None:
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
        time_type = parts[2]
        time_str = parts[3]
        seconds = self._parse_time_str(time_str)
        if seconds is None:
            return

        if time_type == "E":  # Total elapsed
            self._set_media_position(seconds)
        elif time_type == "R":  # Total remaining
            if self._media_position is not None:
                self._media_duration = self._media_position + seconds
            else:
                self._media_duration = seconds
        elif time_type == "T":  # Title/track elapsed
            self._set_media_position(seconds)
        elif time_type == "X" and self._media_position is not None:  # Title remaining
            self._media_duration = self._media_position + seconds

    def _set_media_position(self, seconds: int) -> None:
        """Update position and timestamp together for HA progress interpolation."""
        self._media_position = seconds
        self._media_position_updated_at = dt_util.utcnow()

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

    @override
    async def async_turn_on(self) -> None:
        """Turn the player on."""
        await self._client.power_on()

    @override
    async def async_turn_off(self) -> None:
        """Turn the player off."""
        if await self._client.power_off():
            self._power_state = PowerState.OFF
            self._clear_all_state()
            self.async_write_ha_state()

    @override
    async def async_media_play(self) -> None:
        """Send play command."""
        await self._client.play_pause_toggle()

    @override
    async def async_media_pause(self) -> None:
        """Send pause command."""
        await self._client.play_pause_toggle()

    @override
    async def async_media_stop(self) -> None:
        """Send stop command."""
        await self._client.stop()

    @override
    async def async_media_next_track(self) -> None:
        """Send next track command."""
        await self._client.next_track()

    @override
    async def async_media_previous_track(self) -> None:
        """Send previous track command."""
        await self._client.previous_track()

    @override
    async def async_set_volume_level(self, volume: float) -> None:
        """Set volume level (0..1)."""
        vol_int = int(volume * 100)
        result = await self._client.set_volume(vol_int)
        if result is not None:
            self._volume_level = result / 100.0

    @override
    async def async_volume_up(self) -> None:
        """Turn volume up."""
        result = await self._client.volume_up()
        if result is not None:
            self._volume_level = result / 100.0

    @override
    async def async_volume_down(self) -> None:
        """Turn volume down."""
        result = await self._client.volume_down()
        if result is not None:
            self._volume_level = result / 100.0

    @override
    async def async_mute_volume(self, mute: bool) -> None:
        """Mute/unmute the volume."""
        # Only toggle if the desired state differs from current
        if mute == self._is_muted:
            return
        result = await self._client.mute_toggle()
        if result is not None:
            self._is_muted = result

    @override
    async def async_select_source(self, source: str) -> None:
        """Select an input source."""
        source_id = self._source_map.get(source)
        if source_id is None:
            return
        raw = await self._client.set_input_source(source_id)
        if raw is None:
            return
        mapped = self._map_input_source_response(raw)
        if mapped is not None:
            self._current_source = mapped

    async def async_dimmer(self) -> None:
        """Cycle the front-panel dimmer."""
        await self._client.dimmer()

    async def async_pure_audio_toggle(self) -> None:
        """Toggle Pure Audio mode."""
        await self._client.pure_audio_toggle()

    async def async_info_toggle(self) -> None:
        """Show/hide on-screen display."""
        await self._client.info_toggle()

    async def async_audio_language_toggle(self) -> None:
        """Cycle audio language/channel."""
        await self._client.audio_language_toggle()

    async def async_subtitle_toggle(self) -> None:
        """Cycle subtitle language."""
        await self._client.subtitle_toggle()

    async def async_zoom(self) -> None:
        """Cycle zoom / aspect-ratio mode."""
        await self._client.zoom()

    @override
    async def async_set_repeat(self, repeat: HARepeatMode) -> None:
        """Set repeat mode."""
        if repeat == HARepeatMode.OFF:
            oppo_mode = RepeatMode.OFF
        elif repeat == HARepeatMode.ALL:
            oppo_mode = RepeatMode.ALL
        else:
            # HARepeatMode.ONE — Oppo distinguishes chapter (video) from title/track (audio).
            oppo_mode = RepeatMode.TITLE if self._disc_type in _AUDIO_DISC_TYPES else RepeatMode.CHAPTER
        new_mode = await self._client.set_repeat_mode(oppo_mode)
        mapped = _OPPO_TO_HA_REPEAT.get(new_mode)
        if mapped is not None:
            self._repeat = mapped
