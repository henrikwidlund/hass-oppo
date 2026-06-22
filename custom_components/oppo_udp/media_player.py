"""Media player platform for Oppo UDP-20X."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
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

# Player ACKs PON before it can reliably accept SVM. Wait this long after
# a power-on transition before sending the verbose-mode command.
_VERBOSE_MODE_POWER_ON_DELAY = 1.0

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.entity_platform import AddEntitiesCallback


@dataclass
class _Snapshot:
    """All entity state populated from the player."""

    power_state: PowerState = PowerState.UNKNOWN
    playback_status: PlaybackStatus = PlaybackStatus.UNKNOWN
    volume_level: float | None = None
    is_muted: bool = False
    media_title: str | None = None
    media_album: str | None = None
    media_artist: str | None = None
    media_position: int | None = None
    media_position_updated_at: datetime | None = None
    media_duration: int | None = None
    current_source: str | None = None
    disc_type: str | None = None
    audio_type: str | None = None
    subtitle_type: str | None = None
    aspect_ratio: str | None = None
    three_d: str | None = None
    hdr_status: str | None = None
    video_resolution: str | None = None
    repeat: HARepeatMode = HARepeatMode.OFF
    shuffle: bool = False


_OPPO_TO_HA_REPEAT: dict[RepeatMode, HARepeatMode] = {
    RepeatMode.OFF: HARepeatMode.OFF,
    RepeatMode.CHAPTER: HARepeatMode.ONE,
    RepeatMode.TITLE: HARepeatMode.ONE,
    RepeatMode.ALL: HARepeatMode.ALL,
    # Shuffle and Random are surfaced via the separate `shuffle` property.
    RepeatMode.SHUFFLE: HARepeatMode.OFF,
    RepeatMode.RANDOM: HARepeatMode.OFF,
}

_OPPO_SHUFFLE_MODES: frozenset[RepeatMode] = frozenset({RepeatMode.SHUFFLE, RepeatMode.RANDOM})

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


_SERVICES_REGISTERED_KEY = f"{DOMAIN}_services_registered"


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

    # Entity services are global to the integration. Register them only once
    # so a reload or a second config entry does not raise.
    if not hass.data.get(_SERVICES_REGISTERED_KEY):
        platform = entity_platform.async_get_current_platform()
        for service_name, method in _ENTITY_SERVICES:
            platform.async_register_entity_service(service_name, None, method)
        hass.data[_SERVICES_REGISTERED_KEY] = True


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

        # All player-derived state lives in a single snapshot object so a
        # rebuild can atomically swap it out without leaving any field stale.
        self._snapshot = _Snapshot()
        self._streaming_active = False
        self._unsub_reconnect: CALLBACK_TYPE | None = None
        self._rebuild_in_progress = False
        self._rebuild_pending = False
        # Title/chapter from the most recent @UTC frame, for detecting the
        # content change that warrants a metadata rebuild. Kept off ``_snapshot``
        # so a rebuild's atomic swap can't reset it — that would make the next
        # frame look like a change and trigger an endless rebuild loop. Reset
        # only on playback invalidation.
        self._last_progress_title: int | None = None
        self._last_progress_chapter: int | None = None
        # Pending verbose-mode task, kept so unload / a fresh power-on
        # transition can cancel the previous delayed SVM send.
        self._verbose_mode_task: asyncio.Task[None] | None = None

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
            | MediaPlayerEntityFeature.SHUFFLE_SET
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
        if self._snapshot.power_state == PowerState.OFF:
            return MediaPlayerState.OFF
        if self._snapshot.power_state == PowerState.UNKNOWN:
            return None
        return PLAYBACK_TO_STATE.get(self._snapshot.playback_status, MediaPlayerState.IDLE)

    @property
    @override
    def volume_level(self) -> float | None:  # pyright: ignore [reportIncompatibleVariableOverride]
        """Return volume level (0..1)."""
        return self._snapshot.volume_level

    @property
    @override
    def is_volume_muted(self) -> bool:  # pyright: ignore [reportIncompatibleVariableOverride]
        """Return True if volume is muted."""
        return self._snapshot.is_muted

    @property
    @override
    def media_title(self) -> str | None:  # pyright: ignore [reportIncompatibleVariableOverride]
        """Return the media title."""
        return self._snapshot.media_title

    @property
    @override
    def media_album_name(self) -> str | None:  # pyright: ignore [reportIncompatibleVariableOverride]
        """Return the media album."""
        return self._snapshot.media_album

    @property
    @override
    def media_artist(self) -> str | None:  # pyright: ignore [reportIncompatibleVariableOverride]
        """Return the media artist."""
        return self._snapshot.media_artist

    @property
    @override
    def media_position(self) -> int | None:  # pyright: ignore [reportIncompatibleVariableOverride]
        """Return the media position in seconds."""
        return self._snapshot.media_position

    @property
    @override
    def media_position_updated_at(self) -> datetime | None:  # pyright: ignore [reportIncompatibleVariableOverride]
        """Return when media_position was last updated."""
        return self._snapshot.media_position_updated_at

    @property
    @override
    def media_duration(self) -> int | None:  # pyright: ignore [reportIncompatibleVariableOverride]
        """Return the media duration in seconds."""
        return self._snapshot.media_duration

    @property
    @override
    def media_content_type(self) -> MediaType | None:  # pyright: ignore [reportIncompatibleVariableOverride]
        """Return the content type."""
        if self._snapshot.disc_type in ("cdda", "sacd", "dvd-audio"):
            return MediaType.MUSIC
        if self._snapshot.disc_type in ("bd-mv", "dvd-video", "uhbd", "data-disc"):
            return MediaType.VIDEO
        return None

    @property
    @override
    def source(self) -> str | None:  # pyright: ignore [reportIncompatibleVariableOverride]
        """Return current source."""
        return self._snapshot.current_source

    @property
    @override
    def source_list(self) -> list[str]:  # pyright: ignore [reportIncompatibleVariableOverride]
        """Return the available sources."""
        return self._source_list

    @property
    @override
    def repeat(self) -> HARepeatMode | None:  # pyright: ignore [reportIncompatibleVariableOverride]
        """Return current repeat mode."""
        return self._snapshot.repeat

    @property
    @override
    def shuffle(self) -> bool | None:  # pyright: ignore [reportIncompatibleVariableOverride]
        """Return True when shuffle/random playback is active."""
        return self._snapshot.shuffle

    @property
    @override
    def extra_state_attributes(self) -> Mapping[str, Any] | None:  # pyright: ignore [reportIncompatibleVariableOverride]
        """Return extra state attributes."""
        attrs: dict[str, str] = {}
        if self._snapshot.disc_type:
            attrs["disc_type"] = self._snapshot.disc_type
        if self._snapshot.audio_type:
            attrs["audio_type"] = self._snapshot.audio_type
        if self._snapshot.subtitle_type:
            attrs["subtitle_type"] = self._snapshot.subtitle_type
        if self._snapshot.aspect_ratio:
            attrs["aspect_ratio"] = self._snapshot.aspect_ratio
        if self._snapshot.three_d:
            attrs["three_d"] = self._snapshot.three_d
        if self._snapshot.hdr_status:
            attrs["hdr_status"] = self._snapshot.hdr_status
        if self._snapshot.video_resolution:
            attrs["video_resolution"] = self._snapshot.video_resolution
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
        # Snapshot the task before clearing so we can await its teardown
        # below — `_cancel_verbose_mode_task` issues the cancel and drops
        # the reference; the done-callback drains any exception.
        pending_verbose = self._verbose_mode_task
        self._cancel_verbose_mode_task()
        if pending_verbose is not None and not pending_verbose.done():
            with contextlib.suppress(asyncio.CancelledError):
                await pending_verbose
        await self._client.stop_streaming()
        await self._client.disconnect()

    async def _connect_and_stream(self) -> None:
        """Connect and start streaming updates, schedule reconnect on failure."""
        if not await self._client.connect():
            self._schedule_reconnect()
            return

        # Query initial state
        await self._fetch_initial_state()
        # Start the reader / dispatcher so streaming events can be received
        # even before we send the SVM command.
        self._client.start_streaming(
            self._handle_streaming_event,
            on_disconnect=self._handle_disconnect,
        )
        self._streaming_active = True
        # Verbose mode only makes sense to send while the player is on. If
        # it's off now, the UPW=on streaming event (or a future TURN_ON) will
        # trigger the SVM 3 command — provided verbose mode was set on a prior
        # session, the player retains it across power cycles and will emit
        # events again.
        #
        # `_fetch_initial_state` swallows errors without resetting the
        # snapshot, so a transient failure can leave `power_state` at any
        # stale value (UNKNOWN on first connect, OFF/etc. on later
        # reconnects). Whenever we cannot confirm ON, re-query once so we
        # don't skip the verbose-mode bootstrap and end up with no streaming
        # events to recover.
        if self._snapshot.power_state != PowerState.ON:
            try:
                fresh_power = await self._client.query_power_status()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Power status re-query failed", exc_info=True)
            else:
                self._snapshot.power_state = fresh_power
                self.async_write_ha_state()
        if self._snapshot.power_state == PowerState.ON:
            self._schedule_ensure_verbose_mode()
        else:
            # Cannot confirm ON — make sure no leftover task from an earlier
            # cycle keeps sleeping toward a now-pointless SVM send.
            self._cancel_verbose_mode_task()

    async def _fetch_initial_state(self) -> None:
        """Fetch a full snapshot from the player after connecting."""
        try:
            self._snapshot = await self._build_snapshot()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Error fetching initial state", exc_info=True)
        self.async_write_ha_state()

    async def _build_snapshot(self) -> _Snapshot:
        """Build a fresh snapshot by querying the player.

        Constructs a new ``_Snapshot`` locally and only assigns into it.
        Callers swap the returned snapshot in atomically so any field the
        rebuild does not populate is reset to its dataclass default — no
        stale carry-over from the previous snapshot.
        """
        snapshot = _Snapshot()
        snapshot.power_state = await self._client.query_power_status()
        if snapshot.power_state == PowerState.ON:
            await self._populate_powered_on(snapshot)
        return snapshot

    async def _populate_powered_on(self, snapshot: _Snapshot) -> None:
        """Populate the supplied snapshot with the player's current state."""
        snapshot.playback_status = await self._client.query_playback_status()

        volume, muted = await self._client.query_volume()
        snapshot.is_muted = muted
        if volume is not None:
            snapshot.volume_level = volume / 100.0

        _source, raw = await self._client.query_input_source()
        if raw:
            snapshot.current_source = self._map_input_source_response(raw)

        snapshot.disc_type = (await self._client.query_disc_type()).value

        # HDMI output resolution is reported by the player whenever it is on,
        # not just during active playback.
        snapshot.video_resolution = await self._client.query_hdmi_resolution()

        # Only poll active playback details (and repeat/HDR) if actually
        # playing/paused with a known disc type — querying repeat or playback
        # sensors at the home menu can return stale or error responses.
        if snapshot.playback_status in (
            PlaybackStatus.PLAY,
            PlaybackStatus.PAUSE,
        ) and snapshot.disc_type not in ("unknown", "unknown-disc", "data-disc"):
            await self._populate_active_playback(snapshot)

    async def _populate_active_playback(self, snapshot: _Snapshot) -> None:
        """Populate fields only available during active playback."""
        is_movie = snapshot.disc_type in ("bd-mv", "dvd-video", "uhbd")

        # Repeat / shuffle are only meaningful with active playback.
        # Oppo reports them in the same query — Shuffle/Random surface
        # as ``shuffle=True`` with repeat falling back to OFF.
        repeat_mode = await self._client.query_repeat_mode()
        snapshot.repeat = _OPPO_TO_HA_REPEAT.get(repeat_mode, HARepeatMode.OFF)
        snapshot.shuffle = repeat_mode in _OPPO_SHUFFLE_MODES

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
            self._set_media_position(snapshot, elapsed)
        if elapsed is not None and remaining is not None:
            snapshot.media_duration = elapsed + remaining

        # If elapsed is 0, we're likely at a title/menu screen — querying
        # further details can produce errors and lock up the player.
        if not elapsed or not remaining:
            return

        # Track metadata (only available/relevant for audio discs)
        if not is_movie:
            snapshot.media_title = await self._client.query_track_name()
            snapshot.media_album = await self._client.query_track_album()
            snapshot.media_artist = await self._client.query_track_performer()

        # Audio type (always available during active playback)
        snapshot.audio_type = await self._client.query_audio_type()

        # Subtitle info (only relevant for video discs)
        # Skip if duration is less than 60s (most likely title screen)
        if is_movie and (duration := snapshot.media_duration) is not None and duration >= 60:
            snapshot.subtitle_type = await self._client.query_subtitle_type()

        # Video-only attributes — a snapshot rebuild fully refreshes
        # these fields instead of relying on the next streaming event.
        if not is_movie:
            return
        snapshot.aspect_ratio = await self._client.query_aspect_ratio()
        # 3D is only meaningful on Blu-Ray movie discs.
        if snapshot.disc_type == "bd-mv":
            snapshot.three_d = await self._client.query_three_d_status()
        # HDR is only meaningful on Ultra HD Blu-Ray discs.
        if snapshot.disc_type == "uhbd":
            snapshot.hdr_status = await self._client.query_hdr_status()

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
        # Disconnect invalidates any in-flight delayed SVM send — drop it so
        # it can't fire against a freshly reconnected (and possibly off)
        # player.
        self._cancel_verbose_mode_task()
        # Drop the whole snapshot so HA does not show stale media data while
        # we are disconnected, and re-arm the @UTC progress cursor. Reconnect
        # rebuilds via `_fetch_initial_state`, but that swallows errors — if it
        # fails, the snapshot stays empty and a stale cursor would let the first
        # post-reconnect time-code frame look "unchanged" and skip the rebuild,
        # leaving metadata empty until some other invalidating event.
        self._snapshot = _Snapshot()
        self._reset_progress_cursor()
        self.async_write_ha_state()
        self._schedule_reconnect()

    @callback
    def _handle_streaming_event(self, event: tuple[str, str]) -> None:
        """Handle a streaming event from the player."""
        event_type = event[0]

        if event_type == "power":
            if event[1] == "on":
                # Reset every field so any leftover from before the power
                # cycle is gone while the rebuild runs. Write immediately so
                # the UI reflects ON without waiting for the rebuild to land.
                self._snapshot = _Snapshot(power_state=PowerState.ON)
                self._reset_progress_cursor()
                self.async_write_ha_state()
                # The player only reliably accepts SVM while on. Catch the case where
                # it was powered up externally and verbose mode might have been reverted.
                self._schedule_ensure_verbose_mode()
                self._schedule_rebuild_snapshot()
                return
            # Power-off transition — drop any in-flight delayed SVM send so it
            # doesn't fire against a player that's no longer on.
            self._cancel_verbose_mode_task()
            self._snapshot = _Snapshot(power_state=PowerState.OFF)
            self._reset_progress_cursor()

        elif event_type == "playback":
            prev_status = self._snapshot.playback_status
            self._snapshot.playback_status = self._streaming_playback_to_enum(event[1])
            # Transition from non-active to active playback — full rebuild
            was_active = prev_status in (PlaybackStatus.PLAY, PlaybackStatus.PAUSE)
            is_active = self._snapshot.playback_status in (
                PlaybackStatus.PLAY,
                PlaybackStatus.PAUSE,
            )
            if is_active and not was_active:
                # Push play/pause state immediately; rebuild fills in metadata.
                self.async_write_ha_state()
                self._schedule_rebuild_snapshot()
                return
            if not is_active:
                self._clear_playback_metadata()

        elif event_type == "volume":
            if event[1] == "mute":
                self._snapshot.is_muted = True
            else:
                self._snapshot.is_muted = False
                with contextlib.suppress(ValueError):
                    self._snapshot.volume_level = int(event[1]) / 100.0

        elif event_type == "disc_type":
            self._snapshot.disc_type = event[1]
            # Disc change invalidates everything tied to the previous disc:
            # playback metadata (title/album/artist, audio/subtitle types,
            # repeat/shuffle/HDR, position/duration) and video pipeline
            # attributes. Preserve playback_status — its own streaming event
            # will deliver the new value.
            self._handle_invalidating_change()
            return

        elif event_type == "input_source":
            self._snapshot.current_source = self._map_input_source_response(event[1])
            # Source change can invalidate the entire playback domain —
            # same scope as a disc change.
            self._handle_invalidating_change()
            return

        elif event_type == "audio_type":
            self._snapshot.audio_type = event[1]

        elif event_type == "subtitle_type":
            self._snapshot.subtitle_type = event[1]

        elif event_type == "aspect_ratio":
            self._snapshot.aspect_ratio = event[1].lower()

        elif event_type == "three_d":
            self._snapshot.three_d = event[1]

        elif event_type == "video_resolution":
            self._snapshot.video_resolution = event[1]
            # Player can renegotiate HDMI mid-playback (HDR↔SDR, Dolby Vision
            # fallback). Re-query HDR while a UHD disc is actively playing.
            if self._is_uhd_active_playback():
                self.hass.async_create_task(self._refresh_hdr(), name=f"oppo_udp_refresh_hdr[{self._client.host}]")

        elif event_type == "time_code":
            self._handle_time_code_event(event[1])
            return  # _handle_time_code_event calls async_write_ha_state if needed

        self.async_write_ha_state()

    def _is_uhd_active_playback(self) -> bool:
        """True when a UHD Blu-Ray is actively playing/paused."""
        return self._snapshot.disc_type == "uhbd" and self._snapshot.playback_status in (
            PlaybackStatus.PLAY,
            PlaybackStatus.PAUSE,
        )

    def _schedule_ensure_verbose_mode(self) -> None:
        """Schedule the delayed SVM send, replacing any pending one.

        Multiple power-on signals can arrive back-to-back (initial connect,
        UPW=on streaming event, an explicit ``turn_on``). Cancel any
        in-flight verbose-mode task before starting a new one so we never
        end up with two delayed SVM sends racing.
        """
        self._cancel_verbose_mode_task()
        task = self.hass.async_create_task(
            self._ensure_verbose_mode(),
            name=f"oppo_udp_ensure_verbose_mode[{self._client.host}]",
        )
        # Consume the task result so a cancellation (or any stray exception
        # that escaped the body) does not surface as
        # "Task exception was never retrieved" in the event loop logs.
        task.add_done_callback(self._handle_verbose_mode_task_done)
        self._verbose_mode_task = task

    def _cancel_verbose_mode_task(self) -> None:
        """Cancel any pending verbose-mode task.

        The task drains itself via ``_handle_verbose_mode_task_done`` once
        cancellation lands, so callers in sync contexts don't need to await
        it. ``async_will_remove_from_hass`` awaits it explicitly to make
        teardown deterministic.
        """
        task = self._verbose_mode_task
        if task is not None and not task.done():
            task.cancel()
        self._verbose_mode_task = None

    def _handle_verbose_mode_task_done(self, task: asyncio.Task[None]) -> None:
        """Drain the verbose-mode task's result to keep the loop quiet."""
        with contextlib.suppress(asyncio.CancelledError):
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                _LOGGER.debug("Verbose-mode task raised for host %s", self._client.host, exc_info=exc)

    async def _ensure_verbose_mode(self) -> None:
        """Send ``SVM 3`` to enable detailed streaming updates.

        Only safe to call while the player is on — the player ignores ``SVM``
        when powered off and ``QVM`` cannot be relied on to report the
        retained mode either. Sending the command immediately after a PON
        ACK is too aggressive: the player needs a moment after power-on
        before it will reliably honor SVM 3, so we wait first. Failures are
        logged at debug level and otherwise swallowed: the next power-on /
        turn-on event will retry.
        """
        try:
            await asyncio.sleep(_VERBOSE_MODE_POWER_ON_DELAY)
            # Recheck after the wait — if the player went off again during
            # the delay, skip the SVM send.
            if self._snapshot.power_state == PowerState.ON and not await self._client.set_verbose_mode(3):
                _LOGGER.debug("Failed to enable verbose mode")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Error enabling verbose mode", exc_info=True)

    async def _refresh_hdr(self) -> None:
        """Query HDR status and update the snapshot in place."""
        try:
            self._snapshot.hdr_status = await self._client.query_hdr_status()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Error refreshing HDR status", exc_info=True)
            return
        self.async_write_ha_state()

    def _handle_invalidating_change(self) -> None:
        """Apply the common cleanup for disc/input-source streaming events.

        Drops stale playback metadata and video attributes immediately so the
        UI doesn't keep showing old values while the async rebuild runs.
        """
        self._clear_playback_metadata()
        self._clear_video_state()
        self.async_write_ha_state()
        self._schedule_rebuild_snapshot()

    def _schedule_rebuild_snapshot(self) -> None:
        """Schedule a rebuild task, coalescing concurrent requests.

        A request that arrives while a rebuild is in flight is not dropped —
        it sets a pending flag, and one additional rebuild is run when the
        current rebuild finishes so any state-invalidating event observed
        mid-rebuild is still reflected in the final snapshot.
        """
        if self._rebuild_in_progress:
            self._rebuild_pending = True
            return
        self._rebuild_in_progress = True
        self.hass.async_create_task(self._rebuild_snapshot(), name=f"oppo_udp_rebuild_snapshot[{self._client.host}]")

    def _handle_time_code_event(self, value: str) -> None:
        """Handle a streaming time code event: ``<title> <chapter> <type> HH:MM:SS``.

        Applies the position from the stream, triggering a full metadata rebuild
        when the title (or, on audio discs, the track) changes.
        """
        parts = value.split()
        if len(parts) < 4:
            return

        # Title is the content-change key; the chapter only matters on audio
        # discs (where it is the track number — the title stays 001). Parse them
        # independently so a malformed chapter can't suppress title-change
        # detection. Both are digit strings per the protocol.
        title = int(parts[0]) if parts[0].isdigit() else None
        chapter = int(parts[1]) if parts[1].isdigit() else None
        if title is not None:
            # Video chapters increment routinely and their metadata has its own
            # events, so only a title change (or an audio track change) rebuilds.
            is_audio = self._snapshot.disc_type in _AUDIO_DISC_TYPES
            chapter_changed = is_audio and chapter is not None and chapter != self._last_progress_chapter
            if title != self._last_progress_title or chapter_changed:
                self._last_progress_title = title
                if chapter is not None:
                    self._last_progress_chapter = chapter
                # Apply the current sample immediately to avoid a visible
                # position freeze/jump while waiting for the rebuild.
                self._parse_time_code_event(value)
                self.async_write_ha_state()
                self._schedule_rebuild_snapshot()
                return

        # No content change — apply the frame, writing state only if the
        # position or duration moved.
        if self._parse_time_code_event(value):
            self.async_write_ha_state()

    def _clear_playback_metadata(self) -> None:
        """Clear fields tied to the currently-playing content.

        Shared by playback-stop, disc-change and input-source-change handlers —
        all three invalidate position, track metadata, repeat/shuffle and HDR.
        ``playback_status`` is intentionally preserved: callers either keep the
        streaming-supplied value or leave the previous one in place. Video
        attributes (aspect ratio / 3D / HDMI resolution) are cleared by
        ``_clear_video_state`` on the disc/source paths only — the player
        keeps reporting them across playback-active transitions.
        """
        self._snapshot.media_position = None
        self._snapshot.media_position_updated_at = None
        self._snapshot.media_duration = None
        self._snapshot.media_title = None
        self._snapshot.media_album = None
        self._snapshot.media_artist = None
        self._snapshot.audio_type = None
        self._snapshot.subtitle_type = None
        self._snapshot.repeat = HARepeatMode.OFF
        self._snapshot.shuffle = False
        self._snapshot.hdr_status = None
        # Playback domain invalidated — re-arm the @UTC change detector so the
        # next time-code event rebuilds against fresh metadata.
        self._reset_progress_cursor()

    def _reset_progress_cursor(self) -> None:
        """Re-arm the streaming @UTC title/chapter change detector."""
        self._last_progress_title = None
        self._last_progress_chapter = None

    def _clear_video_state(self) -> None:
        """Clear video-only attributes (aspect ratio, 3D, HDR, HDMI resolution)."""
        self._snapshot.aspect_ratio = None
        self._snapshot.three_d = None
        self._snapshot.hdr_status = None
        self._snapshot.video_resolution = None

    def _streaming_event_invalidated_rebuild(self, new_snapshot: _Snapshot) -> bool:
        """Return True if a streaming event made the rebuild result stale.

        Streaming events mutate ``self._snapshot`` in place while the async
        rebuild is in flight. If the rebuild was building a powered-on
        snapshot but the player flipped to OFF (or the connection dropped
        and ``_handle_disconnect`` reset everything) we must not overwrite
        that more recent state with the stale rebuild.
        """
        current = self._snapshot.power_state
        return current != new_snapshot.power_state and current in (PowerState.OFF, PowerState.UNKNOWN)

    async def _rebuild_snapshot(self) -> None:
        """Re-poll all state from the player and swap snapshots atomically."""
        new_snapshot: _Snapshot | None = None
        try:
            new_snapshot = await self._build_snapshot()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Error rebuilding snapshot", exc_info=True)
        finally:
            self._rebuild_in_progress = False

        # If an invalidating streaming event arrived during the rebuild, the
        # result we just built is already stale — skip the swap entirely and
        # let the follow-up rebuild produce the snapshot that gets applied.
        if self._rebuild_pending:
            self._rebuild_pending = False
            self._schedule_rebuild_snapshot()
            return

        if new_snapshot is not None and not self._streaming_event_invalidated_rebuild(new_snapshot):
            # Atomic swap — any field the rebuild didn't populate falls back to
            # its dataclass default, so no stale value can survive.
            self._snapshot = new_snapshot
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

    def _parse_time_code_event(self, value: str) -> bool:
        """Parse a streaming time code event: '<title> <chapter> <type> HH:MM:SS'.

        Returns True only if the media position or duration changed, so the
        caller can skip a redundant state write for an unchanged value.
        """
        parts = value.split()
        if len(parts) < 4:
            return False
        time_type = parts[2]
        seconds = self._parse_time_str(parts[3])
        if seconds is None:
            return False

        snapshot = self._snapshot
        # Elapsed time (E total / T title / C chapter-track) -> media position.
        if time_type in ("E", "T", "C"):
            if snapshot.media_position == seconds:
                return False
            self._set_media_position(snapshot, seconds)
            return True
        # Remaining time -> total duration (elapsed + remaining). Title/chapter
        # remaining (X/K) is only a valid duration once the elapsed position is
        # known; total remaining (R) also stands alone as a fallback before then.
        if time_type == "R":
            duration = snapshot.media_position + seconds if snapshot.media_position is not None else seconds
        elif time_type in ("X", "K") and snapshot.media_position is not None:
            duration = snapshot.media_position + seconds
        else:
            return False
        if snapshot.media_duration == duration:
            return False
        snapshot.media_duration = duration
        return True

    @staticmethod
    def _set_media_position(snapshot: _Snapshot, seconds: int) -> None:
        """Update position and timestamp together for HA progress interpolation."""
        snapshot.media_position = seconds
        snapshot.media_position_updated_at = dt_util.utcnow()

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
        """Turn the player on and re-enable verbose streaming updates."""
        if not await self._client.power_on():
            return
        # PON ACK means the player accepted the power-on, but it needs a
        # short grace period before it reliably honors SVM 3. Fire and forget
        # so the service call returns immediately; `_ensure_verbose_mode`
        # waits internally before sending the command.
        self._schedule_ensure_verbose_mode()

    @override
    async def async_turn_off(self) -> None:
        """Turn the player off."""
        if await self._client.power_off():
            # Drop any in-flight delayed SVM send — the player is going off
            # and the deferred command would otherwise fire against it.
            self._cancel_verbose_mode_task()
            self._snapshot = _Snapshot(power_state=PowerState.OFF)
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
            self._snapshot.volume_level = result / 100.0

    @override
    async def async_volume_up(self) -> None:
        """Turn volume up."""
        result = await self._client.volume_up()
        if result is not None:
            self._snapshot.volume_level = result / 100.0

    @override
    async def async_volume_down(self) -> None:
        """Turn volume down."""
        result = await self._client.volume_down()
        if result is not None:
            self._snapshot.volume_level = result / 100.0

    @override
    async def async_mute_volume(self, mute: bool) -> None:
        """Mute/unmute the volume."""
        # Only toggle if the desired state differs from current
        if mute == self._snapshot.is_muted:
            return
        result = await self._client.mute_toggle()
        if result is not None:
            self._snapshot.is_muted = result

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
            self._snapshot.current_source = mapped

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
        """Set repeat mode (clears shuffle on the player)."""
        if repeat == HARepeatMode.OFF:
            oppo_mode = RepeatMode.OFF
        elif repeat == HARepeatMode.ALL:
            oppo_mode = RepeatMode.ALL
        else:
            # HARepeatMode.ONE — Oppo distinguishes chapter (video) from title/track (audio).
            oppo_mode = RepeatMode.TITLE if self._snapshot.disc_type in _AUDIO_DISC_TYPES else RepeatMode.CHAPTER
        new_mode = await self._client.set_repeat_mode(oppo_mode)
        self._apply_repeat_mode_response(new_mode)

    @override
    async def async_set_shuffle(self, shuffle: bool) -> None:
        """Set shuffle on/off (sends ``SRP SHF`` or ``SRP OFF``)."""
        if shuffle == self._snapshot.shuffle:
            return
        # The Oppo player has one combined repeat/shuffle setting, so enabling
        # shuffle clears the current repeat mode and vice versa.
        target = RepeatMode.SHUFFLE if shuffle else RepeatMode.OFF
        new_mode = await self._client.set_repeat_mode(target)
        self._apply_repeat_mode_response(new_mode)

    def _apply_repeat_mode_response(self, new_mode: RepeatMode) -> None:
        """Update repeat/shuffle from a SRP response and push state."""
        mapped = _OPPO_TO_HA_REPEAT.get(new_mode)
        if mapped is None:
            return
        self._snapshot.repeat = mapped
        self._snapshot.shuffle = new_mode in _OPPO_SHUFFLE_MODES
        # No streaming event reports repeat changes, so push state ourselves.
        self.async_write_ha_state()
