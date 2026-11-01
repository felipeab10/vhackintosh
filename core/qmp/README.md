# QMP broker

`QmpBroker` is a synchronous, threaded transport intended to be instantiated
once per managed VM. It owns the VM's one QMP Unix socket connection and one
background reader thread. It does not replace the short-lived legacy clients
in `core.passthrough`, `core.power`, or other modules yet.

```python
from core.qmp import QmpBroker

qmp = QmpBroker("/run/vhackintosh/tahoe/qmp.sock")
qmp.connect()  # greeting + qmp_capabilities
try:
    status = qmp.command("query-status")
    event = qmp.get_event(timeout=1.0)  # queue.Empty if none arrived
finally:
    qmp.close()
```

Callers may invoke `command(name, arguments=None, timeout=None)` from multiple
threads. Each command gets a unique integer ID; the reader correlates replies
while placing `event` messages into a bounded queue. When the queue fills, the
oldest event is discarded and `dropped_events` increments. Lines are capped at
1 MiB by default (`max_message_bytes` can adjust that bound).

Timeouts and transport/protocol errors raise `QmpError`. Timed out commands
are never retried because their effect is unknown. A dead connection stays
disconnected until an owner verifies the VM/process state and explicitly calls
`reconnect()`, which performs a new greeting and capability negotiation.
This transport does not arbitrate with `reims-ctl` readiness or legacy QMP
clients; integration must establish that ownership/handoff separately.
