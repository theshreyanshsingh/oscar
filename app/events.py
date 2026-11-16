"""In-process publish/subscribe for live dashboard updates (sent over Server-Sent Events)."""

import asyncio


class EventBus:
    def __init__(self, queue_size: int = 500):
        self._queue_size = queue_size
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: dict) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A dashboard tab that stopped reading; it reloads state on reconnect.
                pass
