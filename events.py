"""
Simple event hook system for Hub module decoupling.

Messaging emits events. Trust, analytics, tokens, and operator
integrations subscribe. The dependency arrow points up, not down.
"""


class EventHook:
    """Synchronous event hook — list of callbacks fired in order."""

    __slots__ = ("_subscribers",)

    def __init__(self):
        self._subscribers = []

    def subscribe(self, callback):
        """Register a callback. Returns the callback for use as a decorator."""
        self._subscribers.append(callback)
        return callback

    def fire(self, *args, **kwargs):
        """Call all subscribers. Exceptions in one subscriber don't block others."""
        results = []
        for cb in self._subscribers:
            try:
                result = cb(*args, **kwargs)
                if result is not None:
                    results.append(result)
            except Exception as e:
                print(f"[EVENT] Subscriber {cb.__name__} failed: {e}")
        return results

    def __len__(self):
        return len(self._subscribers)

    def clear(self):
        """Remove all subscribers. Useful for testing."""
        self._subscribers.clear()
