"""Shared TCP client scaffolding for a persistent connection with a background streaming reader.

Both ``OppoClient`` and ``MagnetarClient`` hold a persistent TCP control
socket and run a background reader task that turns incoming bytes into typed
events, decoupled from callback dispatch via a queue and a separate
dispatcher task -- the same architecture, just parsing a different wire
format. This module owns that shared scaffolding (connection teardown, task
lifecycle, the queue/dispatcher pair); each subclass supplies its own framing
for outbound commands and implements ``_streaming_loop`` for its own inbound
format, calling ``_finalize_streaming_loop`` from its ``finally`` block.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

_LOGGER = logging.getLogger(__name__)

DEFAULT_STREAM_EVENT_QUEUE_SIZE = 128


class StreamingTcpClient[EventT]:
    """Persistent TCP client with connection teardown and a background streaming reader."""

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False
        self._streaming_task: asyncio.Task[None] | None = None
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._streaming_callbacks: list[Callable[[EventT], None]] = []
        self._disconnect_callback: Callable[[], None] | None = None
        self._stop_streaming_requested = False
        self._event_queue: asyncio.Queue[EventT] | None = None

    @property
    def host(self) -> str:
        """Return the host address."""
        return self._host

    @property
    def connected(self) -> bool:
        """Return True if connected."""
        return self._connected and self._writer is not None

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
        except Exception:
            _LOGGER.debug("Error closing writer during teardown", exc_info=True)

    @staticmethod
    async def _cancel_task(task: asyncio.Task[None] | None) -> None:
        """Cancel a task and wait for completion."""
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _clear_event_queue(self) -> None:
        """Drop any queued events and release the queue."""
        queue = self._event_queue
        if queue is None:
            return
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._event_queue = None

    def start_streaming(
        self,
        callback: Callable[[EventT], None],
        on_disconnect: Callable[[], None] | None = None,
    ) -> None:
        """Start the background reader and dispatcher tasks.

        Reader (``_streaming_loop``, implemented by the subclass) parses
        incoming data and enqueues events; dispatcher calls callbacks. This
        keeps socket reads decoupled from callback speed.

        Args:
            callback: Called with each streaming event.
            on_disconnect: Optional callback called when the connection is lost.
        """
        self._disconnect_callback = on_disconnect
        self._stop_streaming_requested = False
        # Keep only the active subscriber callback to avoid duplicated events
        # after reconnect cycles.
        self._streaming_callbacks = [callback]

        if self._event_queue is None:
            self._event_queue = asyncio.Queue(maxsize=DEFAULT_STREAM_EVENT_QUEUE_SIZE)

        if self._dispatcher_task is None or self._dispatcher_task.done():
            self._dispatcher_task = asyncio.create_task(self._dispatch_streaming_events())

        if self._streaming_task and not self._streaming_task.done():
            return
        self._streaming_task = asyncio.create_task(self._streaming_loop())

    async def stop_streaming(self) -> None:
        """Stop streaming updates."""
        self._stop_streaming_requested = True

        await self._cancel_task(self._streaming_task)
        self._streaming_task = None

        await self._cancel_task(self._dispatcher_task)
        self._dispatcher_task = None

        self._clear_event_queue()
        self._streaming_callbacks.clear()

    def _enqueue_streaming_event(self, event: EventT) -> None:
        """Enqueue event without blocking the socket reader."""
        queue = self._event_queue
        if queue is None:
            return

        if not queue.full():
            queue.put_nowait(event)
            return

        # Keep freshest telemetry under load.
        with contextlib.suppress(asyncio.QueueEmpty):
            queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(event)

    async def _dispatch_streaming_events(self) -> None:
        """Drain queued events and invoke callbacks."""
        queue = self._event_queue
        if queue is None:
            return
        try:
            while True:
                event = await queue.get()
                for cb in self._streaming_callbacks:
                    try:
                        cb(event)
                    except Exception:
                        _LOGGER.exception("Error in streaming callback")
        except asyncio.CancelledError:
            _LOGGER.debug("Streaming event dispatcher task cancelled")
            raise

    async def _streaming_loop(self) -> None:
        """Background loop reading streaming events from the player.

        Must be implemented by subclasses, which should call
        ``_finalize_streaming_loop`` from their own ``finally`` block (after
        draining any subclass-specific pending state, e.g. a command/response
        future).
        """
        raise NotImplementedError

    async def _finalize_streaming_loop(self) -> None:
        """Tear down connection/dispatcher state after the reader loop exits."""
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
                except Exception:
                    _LOGGER.debug("Error closing writer after streaming disconnect", exc_info=True)

            # Unexpected reader loop exit should tear down dispatcher + queue
            # because stop_streaming() is not called on this path.
            await self._cancel_task(self._dispatcher_task)
            self._dispatcher_task = None
            self._clear_event_queue()

        # Mark reader task as not running.
        self._streaming_task = None

        # Notify the caller that the connection was lost.
        if not self._stop_streaming_requested and self._disconnect_callback is not None:
            try:
                self._disconnect_callback()
            except Exception:
                _LOGGER.exception("Error in disconnect callback")
