# streams.py — Nova stream providers (pub/sub transport for `stream` decls).
#
# v1 ships the in-memory provider: a single-process, thread-safe,
# Kafka-style append log. Each topic is an ordered log with integer
# offsets; each consumer group tracks its own committed offset, so
# groups make independent progress over the same topic — the same
# contract a real broker offers, without the broker.
#
# A Kafka/Redpanda provider is the v2 hook: same four-method interface,
# real broker underneath. Nothing in the interpreter changes.

import threading


class StreamProvider:
    """Transport contract every stream provider implements."""
    name = "base"

    def publish(self, topic, key, value):
        """Append a record; returns its offset."""
        raise NotImplementedError

    def poll(self, topic, group):
        """Next uncommitted (offset, key, value) for the group, or None."""
        raise NotImplementedError

    def commit(self, topic, group, offset):
        """Mark offset as consumed for the group."""
        raise NotImplementedError


class MemoryStreamProvider(StreamProvider):
    """Deterministic in-process provider. The v1 default."""
    name = "memory"

    def __init__(self):
        self._lock = threading.Lock()
        self._logs = {}       # topic -> [(offset, key, value), ...]
        self._committed = {}  # (topic, group) -> last committed offset

    def publish(self, topic, key, value):
        with self._lock:
            log = self._logs.setdefault(topic, [])
            offset = len(log)
            log.append((offset, key, value))
            return offset

    def poll(self, topic, group):
        with self._lock:
            log = self._logs.get(topic, [])
            pos = self._committed.get((topic, group), -1) + 1
            if pos < len(log):
                return log[pos]
            return None

    def commit(self, topic, group, offset):
        with self._lock:
            if offset > self._committed.get((topic, group), -1):
                self._committed[(topic, group)] = offset


def select_stream_provider():
    """Pick the stream provider. NOVA_STREAMS=memory (default) is the
    deterministic in-process provider; NOVA_STREAMS=kafka selects the
    v2 broker-backed KafkaStreamProvider (needs NOVA_KAFKA_BOOTSTRAP)."""
    import os
    want = os.environ.get("NOVA_STREAMS", "memory")
    if want == "memory":
        return MemoryStreamProvider()
    if want == "kafka":
        from .kafka_provider import KafkaStreamProvider
        return KafkaStreamProvider()
    raise RuntimeError(
        "unknown stream provider '%s' (want 'memory' or 'kafka')" % want)
