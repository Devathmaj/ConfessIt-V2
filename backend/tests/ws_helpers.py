"""Deterministic Redis pub/sub stand-ins for live WebSocket tests.

The endpoint subscribes through ``redis_service.subscribe_to_conversation`` and
blocks on ``pubsub.listen()`` inside a worker thread. A real Redis is not needed
to test what a socket does with a published message: the fake below lets the
test publish into the same subscription and still lets socket shutdown wake the
blocked listener thread.
"""

import json
import queue
import threading

from app.services.redis_service import redis_service


class FakePubSub:
    """A single subscription fed by the test, not by a Redis server."""

    def __init__(self):
        self._messages = queue.Queue()
        self._closed = threading.Event()

    def publish(self, payload):
        """Deliver one application message to the listening socket."""
        self._messages.put({"type": "message", "data": json.dumps(payload)})

    def listen(self):
        while not self._closed.is_set():
            try:
                message = self._messages.get(timeout=0.05)
            except queue.Empty:
                continue
            if message is None:
                return
            yield message

    def unsubscribe(self, channel):
        pass

    def close(self):
        # Wakes a listener blocked in Queue.get so the worker thread can exit
        # after the socket's own finally block unsubscribes.
        self._closed.set()
        self._messages.put(None)


def install_fake_pubsub(monkeypatch):
    """Route the endpoint's subscription to a fake and return it for publishing."""
    pubsub = FakePubSub()
    monkeypatch.setattr(
        redis_service, "subscribe_to_conversation", lambda channel: pubsub
    )
    return pubsub
