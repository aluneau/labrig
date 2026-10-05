"""In-process event bus: libvirt / task events -> Server-Sent Events clients

Events are published from libvirt's event thread and task threads, and
delivered to asyncio queues (one per connected browser).
"""
import asyncio
import json
import logging
import threading
from typing import Any, Dict, Set, Tuple

logger = logging.getLogger(__name__)


class EventBus:
    def __init__(self):
        self._subscribers: Set[Tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        with self._lock:
            self._subscribers.add((asyncio.get_running_loop(), queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers = {s for s in self._subscribers if s[1] is not queue}

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def publish(self, event: Dict[str, Any]) -> None:
        """Thread-safe: may be called from any thread"""
        data = json.dumps(event, default=str)
        with self._lock:
            subscribers = list(self._subscribers)
        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(self._put, queue, data)
            except RuntimeError:  # loop closed
                self.unsubscribe(queue)

    @staticmethod
    def _put(queue: asyncio.Queue, data: str) -> None:
        if queue.full():  # slow client: drop the oldest event
            queue.get_nowait()
        queue.put_nowait(data)


event_bus = EventBus()
