"""Persistent, per-VM QMP transport.

The broker is synchronous and owns one reader thread for its lifetime. Create
one :class:`QmpBroker` per managed VM; callers may issue commands from multiple
threads and consume asynchronous events with :meth:`get_event`.
"""

from .broker import QmpBroker, QmpError

__all__ = ["QmpBroker", "QmpError"]
