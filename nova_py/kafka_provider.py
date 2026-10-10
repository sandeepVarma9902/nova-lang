# kafka_provider.py — Kafka wire-protocol stream provider for Nova (v2).
#
# Implements the StreamProvider interface from streams.py against a real
# Kafka-compatible broker (Kafka, Redpanda, ...) using ONLY the Python
# standard library: raw TCP sockets + struct, hand-rolled Kafka protocol
# framing. No kafka-python, no confluent-kafka, no third-party deps.
#
# Protocol subset implemented (verified against
# https://kafka.apache.org/protocol.html):
#   ApiVersions v0   (key 18) — version negotiation
#   Metadata    v1   (key 3)  — find the leader for a topic/partition
#   Produce     v2   (key 0)  — publish, acks=1, v2 record batches (magic 2)
#   Fetch       v4   (key 1)  — consume, READ_UNCOMMITTED
#
# Deliberately NOT implemented: consumer groups (OffsetCommit), SASL/TLS,
# transactions, compression, multi-partition fan-out, CreateTopics.
# See docs/v2-kafka.md for the honest accounting.
#
# Selection: NOVA_STREAMS=kafka (see streams.select_stream_provider).
# Config:    NOVA_KAFKA_BOOTSTRAP (required), NOVA_KAFKA_CLIENT_ID,
#            NOVA_KAFKA_CONNECT_TIMEOUT_MS, NOVA_KAFKA_REQUEST_TIMEOUT_MS,
#            NOVA_KAFKA_FETCH_MAX_WAIT_MS, NOVA_KAFKA_FETCH_MAX_BYTES,
#            NOVA_KAFKA_PARTITION.

import os
import socket
import struct
import threading
import time

from .streams import StreamProvider

# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------

# A few broker error codes we translate into readable messages.
_ERROR_NAMES = {
    -1: "UNKNOWN",
    0: "NONE",
    1: "OFFSET_OUT_OF_RANGE",
    2: "CORRUPT_MESSAGE",
    3: "UNKNOWN_TOPIC_OR_PARTITION",
    6: "NOT_LEADER_FOR_PARTITION",
    7: "REQUEST_TIMED_OUT",
    9: "UNKNOWN_TOPIC_OR_PART",
    10: "INVALID_REQUIRED_ACKS",
    14: "NOT_COORDINATOR_FOR_GROUP",
    15: "INVALID_TOPIC_EXCEPTION",
    17: "INVALID_PARTITIONS",
    29: "TOPIC_AUTHORIZATION_FAILED",
    30: "GROUP_AUTHORIZATION_FAILED",
    35: "UNSUPPORTED_VERSION",
    41: "INVALID_COMMIT_OFFSET_SIZE",
    60: "UNKNOWN_PRODUCER_ID",
}


def error_name(code):
    return _ERROR_NAMES.get(code, "ERROR_%d" % code)


class KafkaError(RuntimeError):
    """A broker-reported or client-side Kafka failure."""


class KafkaProtocolError(KafkaError):
    """Framing/decoding violated the protocol (bad bytes, id mismatch)."""


# ---------------------------------------------------------------------------
# CRC32C (Castagnoli) — Kafka record batches use CRC32C, which is NOT the
# zlib/IEEE CRC32, so we roll our own table-driven implementation.
# ---------------------------------------------------------------------------

def _build_crc32c_table():
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0x82F63B78  # reflected 0x1EDC6F41
            else:
                crc >>= 1
        table.append(crc & 0xFFFFFFFF)
    return table


_CRC32C_TABLE = _build_crc32c_table()


def crc32c(data):
    """CRC32C (Castagnoli) checksum. Check vector: b"123456789" -> 0xE3069283."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc = _CRC32C_TABLE[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


# ---------------------------------------------------------------------------
# protocol primitives: zig-zag varints, strings, byte arrays
# ---------------------------------------------------------------------------

class _Writer:
    def __init__(self):
        self.buf = bytearray()

    # -- fixed width ----------------------------------------------------
    def i8(self, v):
        self.buf += struct.pack(">b", v)
        return self

    def i16(self, v):
        self.buf += struct.pack(">h", v)
        return self

    def i32(self, v):
        self.buf += struct.pack(">i", v)
        return self

    def i64(self, v):
        self.buf += struct.pack(">q", v)
        return self

    def u32(self, v):
        self.buf += struct.pack(">I", v)
        return self

    def boolean(self, v):
        self.buf += b"\x01" if v else b"\x00"
        return self

    def raw(self, data):
        self.buf += data
        return self

    # -- varints (zig-zag, like protobuf) -------------------------------
    @staticmethod
    def _encode_varint_unsigned(u):
        out = bytearray()
        while u > 0x7F:
            out.append((u & 0x7F) | 0x80)
            u >>= 7
        out.append(u & 0x7F)
        return bytes(out)

    def varint(self, v):
        zz = ((v << 1) ^ (v >> 31)) & 0xFFFFFFFF
        self.buf += self._encode_varint_unsigned(zz)
        return self

    def varlong(self, v):
        zz = ((v << 1) ^ (v >> 63)) & 0xFFFFFFFFFFFFFFFF
        self.buf += self._encode_varint_unsigned(zz)
        return self

    # -- strings / bytes ------------------------------------------------
    @staticmethod
    def _utf8(s):
        if isinstance(s, bytes):
            return s
        return str(s).encode("utf-8")

    def string(self, s):
        b = self._utf8(s)
        self.i16(len(b))
        self.buf += b
        return self

    def nullable_string(self, s):
        if s is None:
            self.i16(-1)
        else:
            self.string(s)
        return self

    def bytes_field(self, b):
        b = bytes(b)
        self.i32(len(b))
        self.buf += b
        return self

    def nullable_bytes(self, b):
        if b is None:
            self.i32(-1)
        else:
            self.bytes_field(b)
        return self

    def nullable_varbytes(self, b):
        """Varint-length byte array, -1 (zig-zag) for null; used in records."""
        if b is None:
            self.varint(-1)
        else:
            b = bytes(b)
            self.varint(len(b))
            self.buf += b
        return self

    def array(self, items, encode_item):
        self.i32(len(items))
        for it in items:
            encode_item(it)
        return self

    def get(self):
        return bytes(self.buf)


class _Reader:
    def __init__(self, data):
        self.buf = data
        self.pos = 0

    def remaining(self):
        return len(self.buf) - self.pos

    def _take(self, n):
        if self.pos + n > len(self.buf):
            raise KafkaProtocolError(
                "truncated message: need %d bytes, %d remain"
                % (n, self.remaining()))
        out = self.buf[self.pos:self.pos + n]
        self.pos += n
        return out

    def i8(self):
        return struct.unpack(">b", self._take(1))[0]

    def i16(self):
        return struct.unpack(">h", self._take(2))[0]

    def i32(self):
        return struct.unpack(">i", self._take(4))[0]

    def i64(self):
        return struct.unpack(">q", self._take(8))[0]

    def u32(self):
        return struct.unpack(">I", self._take(4))[0]

    def boolean(self):
        return self.i8() != 0

    @staticmethod
    def _decode_varint_unsigned(data, pos):
        shift = 0
        result = 0
        while True:
            if pos >= len(data):
                raise KafkaProtocolError("truncated varint")
            byte = data[pos]
            pos += 1
            result |= (byte & 0x7F) << shift
            if not (byte & 0x80):
                break
            shift += 7
            if shift >= 70:
                raise KafkaProtocolError("varint too long")
        return result, pos

    def _varint_zz(self, bits):
        u, self.pos = self._decode_varint_unsigned(self.buf, self.pos)
        return (u >> 1) ^ -(u & 1)

    def varint(self):
        return self._varint_zz(32)

    def varlong(self):
        return self._varint_zz(64)

    def string(self):
        n = self.i16()
        if n < 0:
            raise KafkaProtocolError("negative length for non-nullable string")
        return self._take(n)

    def nullable_string(self):
        n = self.i16()
        if n < 0:
            return None
        return self._take(n)

    def bytes_field(self):
        n = self.i32()
        if n < 0:
            raise KafkaProtocolError(
                "negative length for non-nullable bytes")
        return self._take(n)

    def nullable_bytes(self):
        n = self.i32()
        if n < 0:
            return None
        return self._take(n)

    def nullable_varbytes(self):
        n = self.varint()
        if n < 0:
            return None
        return self._take(n)


# ---------------------------------------------------------------------------
# v2 record batches (message format magic=2)
# ---------------------------------------------------------------------------

def encode_record_batch(records, base_offset=-1, timestamp_ms=None):
    """Encode one v2 RecordBatch holding `records` = [(key, value), ...].

    key/value are bytes or None. Returns the batch bytes (without the
    surrounding NULLABLE_RECORDS length prefix).
    """
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)
    inner = _Writer()
    inner.i32(-1)          # partition leader epoch (unknown)
    inner.i8(2)            # magic = 2
    inner.u32(0)           # crc placeholder, patched below
    inner.i16(0)           # attributes: no compression, CreateTime
    inner.i32(len(records) - 1)  # last offset delta
    inner.i64(timestamp_ms)      # first timestamp
    inner.i64(timestamp_ms)      # max timestamp
    inner.i64(-1)          # producer id (non-idempotent)
    inner.i16(-1)          # producer epoch
    inner.i32(-1)          # base sequence
    inner.i32(len(records))
    for i, (key, value) in enumerate(records):
        rec = _Writer()
        rec.i8(0)              # attributes
        rec.varlong(0)         # timestamp delta
        rec.varint(i)          # offset delta
        rec.nullable_varbytes(key)
        rec.nullable_varbytes(value)
        rec.varint(-1)         # headers: null
        rb = rec.get()
        inner.varint(len(rb))
        inner.raw(rb)
    content = bytearray(inner.get())
    # CRC32C covers everything from attributes to the end of the batch.
    crc = crc32c(bytes(content[9:]))
    struct.pack_into(">I", content, 5, crc)
    out = _Writer()
    out.i64(base_offset)
    out.i32(len(content))   # batch length: bytes after this field
    out.raw(bytes(content))
    return out.get()


def decode_record_batches(data):
    """Decode concatenated v2 RecordBatches -> [(offset, key, value), ...].

    Returns offsets as absolute (base_offset + offset_delta).
    Raises KafkaProtocolError on non-v2 batches or corrupt framing.
    """
    records = []
    r = _Reader(data)
    while r.remaining() > 0:
        if r.remaining() < 12:
            raise KafkaProtocolError(
                "truncated record batch header (%d bytes left)"
                % r.remaining())
        base_offset = r.i64()
        batch_len = r.i32()
        if batch_len < 0 or batch_len > r.remaining():
            raise KafkaProtocolError(
                "bad record batch length %d" % batch_len)
        batch = r._take(batch_len)
        b = _Reader(batch)
        b.i32()                    # partition leader epoch
        magic = b.i8()
        if magic != 2:
            raise KafkaProtocolError(
                "unsupported record batch magic %d (only v2 supported)"
                % magic)
        crc_start = b.pos + 4      # crc field itself is not checksummed
        stored_crc = b.u32()
        computed_crc = crc32c(batch[crc_start:])
        if stored_crc != computed_crc:
            raise KafkaProtocolError(
                "record batch CRC mismatch (corrupt data)")
        b.i16()                    # attributes
        b.i32()                    # last offset delta
        b.i64()                    # first timestamp
        b.i64()                    # max timestamp
        b.i64()                    # producer id
        b.i16()                    # producer epoch
        b.i32()                    # base sequence
        count = b.i32()
        for _ in range(count):
            b.varint()             # record length (framing only)
            b.i8()                 # attributes
            b.varlong()            # timestamp delta
            offset_delta = b.varint()
            key = b.nullable_varbytes()
            value = b.nullable_varbytes()
            n_headers = b.varint()
            for _ in range(max(n_headers, 0)):
                b.nullable_varbytes()  # header key
                b.nullable_varbytes()  # header value
            records.append((base_offset + offset_delta, key, value))
    return records


# ---------------------------------------------------------------------------
# request builders (each returns the full length-prefixed frame)
# ---------------------------------------------------------------------------

API_PRODUCE = 0
API_FETCH = 1
API_METADATA = 3
API_APIVERSIONS = 18


def frame_request(api_key, api_version, correlation_id, client_id, body):
    """Wrap a request body in the v1 request header + size prefix."""
    w = _Writer()
    w.i16(api_key)
    w.i16(api_version)
    w.i32(correlation_id)
    w.nullable_string(client_id)
    w.raw(body)
    payload = w.get()
    return struct.pack(">i", len(payload)) + payload


def build_api_versions_request(correlation_id, client_id):
    """ApiVersions v0: empty body, v1 header (client_id included)."""
    return frame_request(API_APIVERSIONS, 0, correlation_id, client_id, b"")


def build_metadata_request(correlation_id, client_id, topics):
    """Metadata v1: array of topic name strings."""
    w = _Writer()
    w.array(topics, lambda t: w.string(t))
    return frame_request(API_METADATA, 1, correlation_id, client_id, w.get())


def build_produce_request(correlation_id, client_id, topic, partition,
                          records, timeout_ms=30000, acks=1):
    """Produce v2: one topic, one partition, a v2 record batch payload."""
    batch = encode_record_batch(records)
    w = _Writer()
    w.nullable_string(None)   # transactional_id
    w.i16(acks)
    w.i32(timeout_ms)
    w.i32(1)                  # one topic
    w.string(topic)
    w.i32(1)                  # one partition
    w.i32(partition)
    w.nullable_bytes(batch)   # NULLABLE_RECORDS
    return frame_request(API_PRODUCE, 2, correlation_id, client_id, w.get())


def build_fetch_request(correlation_id, client_id, topic, partition,
                        fetch_offset, max_bytes=1048576, max_wait_ms=1000,
                        min_bytes=1):
    """Fetch v4: READ_UNCOMMITTED (isolation_level=0), single partition."""
    w = _Writer()
    w.i32(-1)                 # replica_id (-1 = normal consumer)
    w.i32(max_wait_ms)
    w.i32(min_bytes)
    w.i32(max_bytes)
    w.i8(0)                   # isolation_level: READ_UNCOMMITTED
    w.i32(1)                  # one topic
    w.string(topic)
    w.i32(1)                  # one partition
    w.i32(partition)
    w.i64(fetch_offset)
    w.i32(max_bytes)          # per-partition max bytes
    return frame_request(API_FETCH, 4, correlation_id, client_id, w.get())


# ---------------------------------------------------------------------------
# response parsers (input: body AFTER the v0 response header correlation_id)
# ---------------------------------------------------------------------------

def parse_api_versions_response(body):
    """-> {api_key: (min_version, max_version)}"""
    r = _Reader(body)
    err = r.i16()
    if err != 0:
        raise KafkaError("ApiVersions failed: %s" % error_name(err))
    n = r.i32()
    versions = {}
    for _ in range(n):
        # NOTE: read into locals first — in d[k()] = (v(), w()) the RHS
        # is evaluated before the subscript, which would scramble order.
        api_key = r.i16()
        lo = r.i16()
        hi = r.i16()
        versions[api_key] = (lo, hi)
    return versions


def parse_metadata_response(body):
    """Metadata v1 -> {"brokers": {id: (host, port)},
    "topics": {name: {"error": code, "partitions": {idx: {"error", "leader"}}}}}"""
    r = _Reader(body)
    brokers = {}
    for _ in range(r.i32()):
        node_id = r.i32()
        host = r.string().decode("utf-8")
        port = r.i32()
        r.nullable_string()   # rack
        brokers[node_id] = (host, port)
    r.i32()                   # controller_id
    topics = {}
    for _ in range(r.i32()):
        err = r.i16()
        name = r.string().decode("utf-8")
        r.boolean()           # is_internal
        partitions = {}
        for _ in range(r.i32()):
            perr = r.i16()
            idx = r.i32()
            leader = r.i32()
            for _ in range(r.i32()):
                r.i32()       # replica nodes
            for _ in range(r.i32()):
                r.i32()       # isr nodes
            partitions[idx] = {"error": perr, "leader": leader}
        topics[name] = {"error": err, "partitions": partitions}
    return {"brokers": brokers, "topics": topics}


def parse_produce_response(body):
    """Produce v2 -> {(topic, partition): (error_code, base_offset)}"""
    r = _Reader(body)
    out = {}
    for _ in range(r.i32()):
        topic = r.string().decode("utf-8")
        for _ in range(r.i32()):
            idx = r.i32()
            err = r.i16()
            base_offset = r.i64()
            r.i64()           # log_append_time_ms
            out[(topic, idx)] = (err, base_offset)
    r.i32()                   # throttle_time_ms
    return out


def parse_fetch_response(body):
    """Fetch v4 -> {topic: {partition: {"error", "high_watermark",
    "records": [(offset, key, value), ...]}}}"""
    r = _Reader(body)
    r.i32()                   # throttle_time_ms
    out = {}
    for _ in range(r.i32()):
        topic = r.string().decode("utf-8")
        parts = {}
        for _ in range(r.i32()):
            idx = r.i32()
            err = r.i16()
            hw = r.i64()
            r.i64()           # last_stable_offset
            n_aborted = r.i32()
            for _ in range(max(n_aborted, 0)):
                r.i64()       # producer_id
                r.i64()       # first_offset
            raw = r.nullable_bytes()
            records = decode_record_batches(raw) if raw else []
            parts[idx] = {"error": err, "high_watermark": hw,
                          "records": records}
        out[topic] = parts
    return out


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------

def _default_socket_factory(host, port, timeout_s):
    sock = socket.create_connection((host, port), timeout=timeout_s)
    sock.settimeout(timeout_s)
    return sock


class BrokerConnection:
    """One TCP connection to one broker; strict request/response pairing."""

    def __init__(self, host, port, timeout_s=10.0, socket_factory=None):
        self.host = host
        self.port = port
        factory = socket_factory or _default_socket_factory
        try:
            self._sock = factory(host, port, timeout_s)
        except OSError as e:
            raise KafkaError(
                "cannot connect to Kafka broker %s:%d: %s"
                % (host, port, e))
        self._closed = False

    def _read_exact(self, n):
        chunks = []
        remaining = n
        while remaining > 0:
            try:
                chunk = self._sock.recv(remaining)
            except socket.timeout:
                raise KafkaError(
                    "timed out waiting for broker %s:%d"
                    % (self.host, self.port))
            except OSError as e:
                raise KafkaError(
                    "lost connection to broker %s:%d: %s"
                    % (self.host, self.port, e))
            if not chunk:
                raise KafkaError(
                    "broker %s:%d closed the connection"
                    % (self.host, self.port))
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def roundtrip(self, correlation_id, framed_request):
        """Send one framed request; return a _Reader over the response body
        (positioned just after the response header's correlation_id)."""
        if self._closed:
            raise KafkaError("connection to %s:%d is closed"
                             % (self.host, self.port))
        try:
            self._sock.sendall(framed_request)
        except OSError as e:
            raise KafkaError("failed to send to broker %s:%d: %s"
                             % (self.host, self.port, e))
        (size,) = struct.unpack(">i", self._read_exact(4))
        if size < 4 or size > 100 * 1024 * 1024:
            raise KafkaProtocolError(
                "implausible response size %d from %s:%d"
                % (size, self.host, self.port))
        body = self._read_exact(size)
        r = _Reader(body)
        got = r.i32()
        if got != correlation_id:
            raise KafkaProtocolError(
                "correlation id mismatch: sent %d, got %d"
                % (correlation_id, got))
        return r

    def close(self):
        self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# config (env-driven)
# ---------------------------------------------------------------------------

class KafkaConfig:
    def __init__(self, bootstrap, client_id="nova", connect_timeout_s=10.0,
                 request_timeout_s=30.0, fetch_max_wait_ms=1000,
                 fetch_max_bytes=1048576, partition=0):
        self.bootstrap = bootstrap            # [(host, port), ...]
        self.client_id = client_id
        self.connect_timeout_s = connect_timeout_s
        self.request_timeout_s = request_timeout_s
        self.fetch_max_wait_ms = fetch_max_wait_ms
        self.fetch_max_bytes = fetch_max_bytes
        self.partition = partition

    @classmethod
    def from_env(cls, env=None):
        env = env if env is not None else os.environ
        raw = env.get("NOVA_KAFKA_BOOTSTRAP")
        if not raw:
            raise RuntimeError(
                "NOVA_STREAMS=kafka requires NOVA_KAFKA_BOOTSTRAP to be set, "
                "e.g. NOVA_KAFKA_BOOTSTRAP=localhost:9092 "
                "(comma-separated host:port for more brokers). "
                "See docs/v2-kafka.md for the docker-compose walkthrough.")

        def num(name, default):
            try:
                return int(env.get(name, default))
            except ValueError:
                raise RuntimeError(
                    "%s must be an integer (got %r)" % (name, env.get(name)))

        bootstrap = []
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            if ":" not in entry:
                raise RuntimeError(
                    "bad NOVA_KAFKA_BOOTSTRAP entry %r: want host:port"
                    % entry)
            host, _, port_s = entry.rpartition(":")
            try:
                port = int(port_s)
            except ValueError:
                raise RuntimeError(
                    "bad NOVA_KAFKA_BOOTSTRAP entry %r: port %r is not "
                    "an integer" % (entry, port_s))
            bootstrap.append((host, port))
        if not bootstrap:
            raise RuntimeError(
                "NOVA_KAFKA_BOOTSTRAP is set but contains no host:port "
                "entries")
        return cls(
            bootstrap=bootstrap,
            client_id=env.get("NOVA_KAFKA_CLIENT_ID", "nova"),
            connect_timeout_s=num("NOVA_KAFKA_CONNECT_TIMEOUT_MS", 10000)
            / 1000.0,
            request_timeout_s=num("NOVA_KAFKA_REQUEST_TIMEOUT_MS", 30000)
            / 1000.0,
            fetch_max_wait_ms=num("NOVA_KAFKA_FETCH_MAX_WAIT_MS", 1000),
            fetch_max_bytes=num("NOVA_KAFKA_FETCH_MAX_BYTES", 1048576),
            partition=num("NOVA_KAFKA_PARTITION", 0),
        )


# ---------------------------------------------------------------------------
# the provider
# ---------------------------------------------------------------------------

_RETRIABLE = {3, 6}  # UNKNOWN_TOPIC_OR_PARTITION, NOT_LEADER_FOR_PARTITION


class KafkaStreamProvider(StreamProvider):
    """StreamProvider backed by a real Kafka-compatible broker.

    One partition per topic (NOVA_KAFKA_PARTITION, default 0) — Nova's
    ordered-log contract maps cleanly onto a single partition's log.
    Offsets committed via commit() are tracked client-side (in memory);
    no broker-side consumer-group OffsetCommit is performed.
    """
    name = "kafka"

    def __init__(self, config=None, socket_factory=None):
        self.config = config or KafkaConfig.from_env()
        self._socket_factory = socket_factory
        self._lock = threading.Lock()
        self._corr = 0
        self._conns = {}        # (host, port) -> BrokerConnection
        self._version_checked = set()  # (host, port) already negotiated
        self._leaders = {}       # (topic, partition) -> (host, port)
        self._committed = {}     # (topic, group) -> offset
        self._pending = {}       # (topic, group) -> [(offset,key,value)]

    # -- plumbing ------------------------------------------------------
    def _next_corr(self):
        self._corr += 1
        return self._corr

    def _conn(self, host, port):
        key = (host, port)
        conn = self._conns.get(key)
        if conn is None:
            conn = BrokerConnection(
                host, port,
                timeout_s=self.config.request_timeout_s,
                socket_factory=self._socket_factory)
            self._conns[key] = conn
        return conn

    def _check_versions(self, conn):
        """Negotiate once per bootstrap connection; fail fast and loud."""
        cid = self._next_corr()
        r = conn.roundtrip(
            cid, build_api_versions_request(cid, self.config.client_id))
        versions = parse_api_versions_response(r.buf[r.pos:])
        need = {API_PRODUCE: 2, API_FETCH: 4, API_METADATA: 1}
        for api, want in need.items():
            got = versions.get(api)
            if got is None or not (got[0] <= want <= got[1]):
                raise KafkaError(
                    "broker does not support the API versions this provider "
                    "needs (api %d: need v%d, broker offers %s)" %
                    (api, want, got))
        return versions

    def _refresh_metadata(self, topics):
        """Ask each bootstrap broker for metadata until one answers."""
        last_err = None
        for host, port in self.config.bootstrap:
            try:
                conn = self._conn(host, port)
                if (host, port) not in self._version_checked:
                    self._check_versions(conn)
                    self._version_checked.add((host, port))
                cid = self._next_corr()
                r = conn.roundtrip(
                    cid, build_metadata_request(
                        cid, self.config.client_id, list(topics)))
                meta = parse_metadata_response(r.buf[r.pos:])
            except KafkaError as e:
                last_err = e
                continue
            brokers = meta["brokers"]
            for topic, tmeta in meta["topics"].items():
                if tmeta["error"] != 0:
                    raise KafkaError(
                        "metadata for topic %r failed: %s "
                        "(create the topic first, or enable the broker's "
                        "auto.create.topics.enable)" %
                        (topic, error_name(tmeta["error"])))
                for idx, pmeta in tmeta["partitions"].items():
                    if pmeta["error"] != 0:
                        raise KafkaError(
                            "metadata for topic %r partition %d failed: %s"
                            % (topic, idx, error_name(pmeta["error"])))
                    leader = pmeta["leader"]
                    if leader < 0 or leader not in brokers:
                        raise KafkaError(
                            "no leader available for topic %r partition %d "
                            "right now" % (topic, idx))
                    self._leaders[(topic, idx)] = brokers[leader]
            return
        raise KafkaError(
            "could not fetch metadata from any bootstrap server %s: %s" %
            ([h for h, _ in self.config.bootstrap], last_err))

    def _leader_conn(self, topic):
        key = (topic, self.config.partition)
        leader = self._leaders.get(key)
        if leader is None:
            self._refresh_metadata({topic})
            leader = self._leaders.get(key)
        if leader is None:
            raise KafkaError(
                "topic %r has no partition %d (topic may have fewer "
                "partitions than expected)" % (topic, self.config.partition))
        return self._conn(*leader)

    def close(self):
        with self._lock:
            for conn in self._conns.values():
                conn.close()
            self._conns.clear()
            self._version_checked.clear()
            self._leaders.clear()

    # -- StreamProvider interface --------------------------------------
    @staticmethod
    def _to_bytes(v, what):
        if v is None or isinstance(v, bytes):
            return v
        if isinstance(v, str):
            return v.encode("utf-8")
        raise KafkaError(
            "%s must be a String (got %r); this provider serializes "
            "strings as UTF-8" % (what, type(v).__name__))

    @staticmethod
    def _from_bytes(b):
        if b is None:
            return None
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            return b  # non-UTF8 payloads pass through as raw bytes

    def publish(self, topic, key, value):
        key_b = self._to_bytes(key, "key")
        value_b = self._to_bytes(value, "value")
        with self._lock:
            for attempt in (0, 1):
                conn = self._leader_conn(topic)
                cid = self._next_corr()
                frame = build_produce_request(
                    cid, self.config.client_id, topic,
                    self.config.partition, [(key_b, value_b)],
                    timeout_ms=int(self.config.request_timeout_s * 1000))
                r = conn.roundtrip(cid, frame)
                results = parse_produce_response(r.buf[r.pos:])
                err, base_offset = results[(topic, self.config.partition)]
                if err in _RETRIABLE and attempt == 0:
                    self._leaders.pop((topic, self.config.partition), None)
                    continue
                if err != 0:
                    raise KafkaError(
                        "produce to %r failed: %s" % (topic, error_name(err)))
                return base_offset
            raise KafkaError("produce to %r failed after metadata refresh"
                             % topic)

    def _fetch_from(self, topic, fetch_offset):
        with self._lock:
            for attempt in (0, 1):
                conn = self._leader_conn(topic)
                cid = self._next_corr()
                frame = build_fetch_request(
                    cid, self.config.client_id, topic,
                    self.config.partition, fetch_offset,
                    max_bytes=self.config.fetch_max_bytes,
                    max_wait_ms=self.config.fetch_max_wait_ms)
                r = conn.roundtrip(cid, frame)
                parsed = parse_fetch_response(r.buf[r.pos:])
                part = parsed[topic][self.config.partition]
                if part["error"] in _RETRIABLE and attempt == 0:
                    self._leaders.pop((topic, self.config.partition), None)
                    continue
                if part["error"] == 1:  # OFFSET_OUT_OF_RANGE
                    return []
                if part["error"] != 0:
                    raise KafkaError(
                        "fetch from %r failed: %s"
                        % (topic, error_name(part["error"])))
                return [(off, self._from_bytes(k), self._from_bytes(v))
                        for off, k, v in part["records"]
                        if off >= fetch_offset]
            raise KafkaError("fetch from %r failed after metadata refresh"
                             % topic)

    def poll(self, topic, group):
        gkey = (topic, group)
        with self._lock:
            buf = self._pending.setdefault(gkey, [])
            if buf:
                return buf.pop(0)
            pos = self._committed.get(gkey, -1) + 1
        # Network I/O outside the lock; _fetch_from takes the lock itself.
        recs = self._fetch_from(topic, pos)
        with self._lock:
            buf = self._pending.setdefault(gkey, [])
            buf.extend(recs)
            if buf:
                return buf.pop(0)
            return None

    def commit(self, topic, group, offset):
        # Client-side only: broker OffsetCommit (consumer groups) is not
        # implemented. Mirrors the memory provider's advance-only rule.
        gkey = (topic, group)
        with self._lock:
            if offset > self._committed.get(gkey, -1):
                self._committed[gkey] = offset
                self._pending[gkey] = [rec for rec in self._pending[gkey]
                                       if rec[0] > offset]
