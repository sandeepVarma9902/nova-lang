#!/usr/bin/env python3
# test_kafka_provider.py — unit tests for nova_lang.kafka_provider.
#
# No broker needed: every test drives the provider with a FakeSocket that
# replays canned, protocol-correct response frames (built with the same
# framing rules the parser expects) and records the request bytes so the
# tests can assert on the exact wire encoding.
#
# Run: python3 tests/test_kafka_provider.py   (from the repo root)

import os
import struct
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from nova_lang import kafka_provider as kp
from nova_lang.kafka_provider import (
    KafkaConfig, KafkaError, KafkaProtocolError, KafkaStreamProvider,
    _Reader, _Writer, crc32c, decode_record_batches, encode_record_batch,
    build_api_versions_request, build_metadata_request,
    build_produce_request, build_fetch_request,
    parse_api_versions_response, parse_metadata_response,
    parse_produce_response, parse_fetch_response,
)
from nova_lang.streams import MemoryStreamProvider, select_stream_provider


# ---------------------------------------------------------------------------
# fake transport
# ---------------------------------------------------------------------------

class FakeSocket:
    """A socket double: replays queued full response frames, records sends.

    recv() returns at most `chunk` bytes per call to prove the client
    handles TCP fragmentation correctly.
    """

    def __init__(self, responses, chunk=7):
        self._responses = list(responses)
        self._incoming = b""
        self._chunk = chunk
        self.sent = []
        self.closed = False

    def sendall(self, data):
        self.sent.append(bytes(data))
        if self._responses:
            self._incoming += self._responses.pop(0)

    def recv(self, n):
        if not self._incoming and not self._responses:
            return b""  # peer closed
        take = min(n, self._chunk, len(self._incoming))
        out, self._incoming = self._incoming[:take], self._incoming[take:]
        return out

    def settimeout(self, t):
        pass

    def close(self):
        self.closed = True


def frame_response(correlation_id, body):
    payload = struct.pack(">i", correlation_id) + body
    return struct.pack(">i", len(payload)) + payload


def api_versions_body(entries=((18, 0, 3), (0, 0, 8), (1, 0, 13), (3, 0, 12))):
    w = _Writer()
    w.i16(0)
    w.i32(len(entries))
    for api, lo, hi in entries:
        w.i16(api).i16(lo).i16(hi)
    return w.get()


def metadata_body(topic, leader_id=1, broker_host="broker1", broker_port=9092,
                  partition=0, topic_error=0, n_partitions=1):
    w = _Writer()
    w.i32(1)                       # one broker
    w.i32(leader_id)
    w.string(broker_host)
    w.i32(broker_port)
    w.nullable_string(None)        # rack
    w.i32(leader_id)               # controller_id
    w.i32(1)                       # one topic
    w.i16(topic_error)
    w.string(topic)
    w.boolean(False)
    w.i32(n_partitions)
    for p in range(n_partitions):
        w.i16(0)
        w.i32(p)
        w.i32(leader_id)
        w.i32(1); w.i32(leader_id)  # replicas
        w.i32(1); w.i32(leader_id)  # isr
    return w.get()


def produce_body(topic, partition=0, error=0, base_offset=0):
    w = _Writer()
    w.i32(1)
    w.string(topic)
    w.i32(1)
    w.i32(partition)
    w.i16(error)
    w.i64(base_offset)
    w.i64(-1)                      # log_append_time_ms
    w.i32(0)                       # throttle_time_ms
    return w.get()


def fetch_body(topic, records, base_offset=0, partition=0, error=0,
               high_watermark=10):
    batch = encode_record_batch(records, base_offset=base_offset) \
        if records else b""
    w = _Writer()
    w.i32(0)                       # throttle_time_ms
    w.i32(1)
    w.string(topic)
    w.i32(1)
    w.i32(partition)
    w.i16(error)
    w.i64(high_watermark)
    w.i64(high_watermark)          # last_stable_offset
    w.i32(-1)                      # aborted_transactions: null
    w.nullable_bytes(batch if records else None)
    return w.get()


def make_provider(queues, **cfg):
    """queues: {(host, port): [response frames]}. Returns (provider, sockets)."""
    sockets = {}

    def factory(host, port, timeout):
        sock = FakeSocket(queues.get((host, port), []))
        sockets[(host, port)] = sock
        return sock

    config = KafkaConfig(bootstrap=[("bootstrap", 1)], **cfg)
    return KafkaStreamProvider(config=config, socket_factory=factory), sockets


def bootstrap_queues(topic="events"):
    """Canned bootstrap responses: ApiVersions (cid 1) + Metadata (cid 2)."""
    return {("bootstrap", 1): [
        frame_response(1, api_versions_body()),
        frame_response(2, metadata_body(topic)),
    ]}


def last_sent_frame(sock):
    """Parse the most recent request frame: (api_key, version, corr, body)."""
    frame = sock.sent[-1]
    (size,) = struct.unpack(">i", frame[:4])
    assert size == len(frame) - 4, "length prefix mismatch"
    r = _Reader(frame[4:])
    api_key, version, corr = r.i16(), r.i16(), r.i32()
    client_id = r.nullable_string()
    return api_key, version, corr, client_id, r


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------

class TestPrimitives(unittest.TestCase):
    def test_crc32c_check_vector(self):
        # Standard CRC32C check value.
        self.assertEqual(crc32c(b"123456789"), 0xE3069283)
        self.assertEqual(crc32c(b""), 0x00000000)

    def test_varint_roundtrip(self):
        for v in (0, 1, -1, 63, 64, 300, -300, 2**31 - 1, -(2**31),
                  2**63 - 1, -(2**63)):
            w = _Writer()
            w.varint(v) if abs(v) < 2**31 else w.varlong(v)
            r = _Reader(w.get())
            self.assertEqual(r.varint() if abs(v) < 2**31 else r.varlong(),
                             v, "varint roundtrip failed for %r" % v)

    def test_varint_signed_encoding(self):
        # -1 zig-zags to 1 -> single byte 0x01; 0 -> 0x00.
        self.assertEqual(_Writer().varint(-1).get(), b"\x01")
        self.assertEqual(_Writer().varint(0).get(), b"\x00")

    def test_string_roundtrip(self):
        w = _Writer()
        w.string("héllo").nullable_string(None).nullable_string("")
        r = _Reader(w.get())
        self.assertEqual(r.string(), "héllo".encode("utf-8"))
        self.assertIsNone(r.nullable_string())
        self.assertEqual(r.nullable_string(), b"")

    def test_truncated_raises(self):
        with self.assertRaises(KafkaProtocolError):
            _Reader(b"\x00\x01").i32()


# ---------------------------------------------------------------------------
# record batches
# ---------------------------------------------------------------------------

class TestRecordBatch(unittest.TestCase):
    def test_roundtrip(self):
        records = [(None, b"value-only"), (b"key", b"value"),
                   (b"k", None), (b"", b"")]
        batch = encode_record_batch(records, base_offset=41)
        got = decode_record_batches(batch)
        self.assertEqual(
            [(off, k, v) for off, k, v in got],
            [(41 + i, k, v) for i, (k, v) in enumerate(records)])

    def test_crc_is_verified_on_decode(self):
        batch = bytearray(encode_record_batch([(b"k", b"v")]))
        batch[-1] ^= 0xFF  # corrupt one payload byte
        with self.assertRaises(KafkaProtocolError):
            decode_record_batches(bytes(batch))

    def test_magic_is_checked(self):
        batch = bytearray(encode_record_batch([(b"k", b"v")]))
        batch[12 + 4] = 1  # magic byte sits at 12+4 (after baseOffset+len+epoch)
        with self.assertRaises(KafkaProtocolError):
            decode_record_batches(bytes(batch))

    def test_concatenated_batches(self):
        data = (encode_record_batch([(b"a", b"1")], base_offset=0) +
                encode_record_batch([(b"b", b"2"), (b"c", b"3")],
                                    base_offset=7))
        got = decode_record_batches(data)
        self.assertEqual([o for o, _, _ in got], [0, 7, 8])
        self.assertEqual([v for _, _, v in got], [b"1", b"2", b"3"])


# ---------------------------------------------------------------------------
# request encoding
# ---------------------------------------------------------------------------

class TestRequestEncoding(unittest.TestCase):
    def test_api_versions(self):
        api, ver, corr, cid, r = last_sent_frame(
            type("S", (), {"sent": [build_api_versions_request(7, "nova")]})())
        self.assertEqual((api, ver, corr), (18, 0, 7))
        self.assertEqual(cid, b"nova")
        self.assertEqual(r.remaining(), 0)  # empty body

    def test_metadata(self):
        frame = build_metadata_request(3, "nova", ["events", "other"])
        _, _, _, _, r = last_sent_frame(
            type("S", (), {"sent": [frame]})())
        self.assertEqual(r.i32(), 2)
        self.assertEqual(r.string(), b"events")
        self.assertEqual(r.string(), b"other")

    def test_produce(self):
        frame = build_produce_request(9, "nova", "events", 0,
                                      [(b"mykey", b"myval")])
        api, ver, corr, cid, r = last_sent_frame(
            type("S", (), {"sent": [frame]})())
        self.assertEqual((api, ver, corr), (0, 2, 9))
        self.assertIsNone(r.nullable_string())  # transactional_id
        self.assertEqual(r.i16(), 1)            # acks=1
        self.assertEqual(r.i32(), 30000)        # timeout_ms
        self.assertEqual(r.i32(), 1)
        self.assertEqual(r.string(), b"events")
        self.assertEqual(r.i32(), 1)
        self.assertEqual(r.i32(), 0)            # partition
        batch = r.nullable_bytes()
        self.assertTrue(batch)
        self.assertEqual(decode_record_batches(batch),
                         [(0 - 1, b"mykey", b"myval")])

    def test_fetch(self):
        frame = build_fetch_request(11, "nova", "events", 0, 123,
                                    max_bytes=65536, max_wait_ms=500)
        api, ver, corr, cid, r = last_sent_frame(
            type("S", (), {"sent": [frame]})())
        self.assertEqual((api, ver, corr), (1, 4, 11))
        self.assertEqual(r.i32(), -1)           # replica_id
        self.assertEqual(r.i32(), 500)          # max_wait_ms
        self.assertEqual(r.i32(), 1)            # min_bytes
        self.assertEqual(r.i32(), 65536)        # max_bytes
        self.assertEqual(r.i8(), 0)             # isolation_level
        self.assertEqual(r.i32(), 1)
        self.assertEqual(r.string(), b"events")
        self.assertEqual(r.i32(), 1)
        self.assertEqual(r.i32(), 0)            # partition
        self.assertEqual(r.i64(), 123)          # fetch_offset
        self.assertEqual(r.i32(), 65536)


# ---------------------------------------------------------------------------
# response parsing
# ---------------------------------------------------------------------------

class TestResponseParsing(unittest.TestCase):
    def test_api_versions(self):
        v = parse_api_versions_response(api_versions_body())
        self.assertEqual(v[0], (0, 8))
        self.assertEqual(v[18], (0, 3))

    def test_metadata(self):
        meta = parse_metadata_response(metadata_body("events"))
        self.assertEqual(meta["brokers"][1], ("broker1", 9092))
        t = meta["topics"]["events"]
        self.assertEqual(t["error"], 0)
        self.assertEqual(t["partitions"][0]["leader"], 1)

    def test_produce(self):
        out = parse_produce_response(produce_body("events", base_offset=42))
        self.assertEqual(out[("events", 0)], (0, 42))

    def test_fetch_with_records(self):
        body = fetch_body("events", [(b"k1", b"v1"), (None, b"v2")],
                          base_offset=5, high_watermark=7)
        parsed = parse_fetch_response(body)
        part = parsed["events"][0]
        self.assertEqual(part["error"], 0)
        self.assertEqual(part["high_watermark"], 7)
        self.assertEqual(
            [(o, k, v) for o, k, v in part["records"]],
            [(5, b"k1", b"v1"), (6, None, b"v2")])

    def test_fetch_empty(self):
        parsed = parse_fetch_response(fetch_body("events", []))
        self.assertEqual(parsed["events"][0]["records"], [])


# ---------------------------------------------------------------------------
# provider end-to-end against the fake transport
# ---------------------------------------------------------------------------

class TestProviderFlow(unittest.TestCase):
    def test_publish_returns_broker_offset(self):
        queues = bootstrap_queues()
        queues[("broker1", 9092)] = [frame_response(3, produce_body(
            "events", base_offset=7))]
        p, socks = make_provider(queues)
        try:
            off = p.publish("events", "k", "v")
            self.assertEqual(off, 7)
            # the produce request actually carried the key/value
            _, _, _, _, r = last_sent_frame(socks[("broker1", 9092)])
            r.nullable_string(); r.i16(); r.i32()
            r.i32(); r.string(); r.i32(); r.i32()
            batch = r.nullable_bytes()
            self.assertEqual(decode_record_batches(batch)[0][1:], (b"k", b"v"))
        finally:
            p.close()

    def test_publish_error_surfaces_named(self):
        # UNKNOWN_TOPIC_OR_PARTITION twice: retriable once, then raised.
        # Correlation ids are provider-global: 1 apiversions, 2 metadata,
        # 3 produce, 4 metadata (refresh), 5 produce (retry).
        queues = {("bootstrap", 1): [
            frame_response(1, api_versions_body()),
            frame_response(2, metadata_body("events")),
            frame_response(4, metadata_body("events")),
        ]}
        queues[("broker1", 9092)] = [
            frame_response(3, produce_body("events", error=3)),
            frame_response(5, produce_body("events", error=3)),
        ]
        p, _ = make_provider(queues)
        try:
            with self.assertRaises(KafkaError) as ctx:
                p.publish("events", "k", "v")
            self.assertIn("UNKNOWN_TOPIC_OR_PARTITION", str(ctx.exception))
        finally:
            p.close()

    def test_poll_commit_poll(self):
        # cids: 1 apiversions, 2 metadata, 3 fetch(g1), 4 fetch(g2), 5 fetch(g1)
        queues = bootstrap_queues()
        two = [(b"k1", b"v1"), (b"k2", b"v2")]
        queues[("broker1", 9092)] = [
            frame_response(3, fetch_body("events", two, base_offset=5)),
            frame_response(4, fetch_body("events", two, base_offset=5)),
            frame_response(5, fetch_body("events", [])),
        ]
        p, _ = make_provider(queues)
        try:
            rec = p.poll("events", "g1")
            self.assertEqual(rec, (5, "k1", "v1"))
            # second poll serves the prefetched record without a new fetch
            rec = p.poll("events", "g1")
            self.assertEqual(rec, (6, "k2", "v2"))
            p.commit("events", "g1", 5)
            p.commit("events", "g1", 6)
            # independent group starts from the beginning
            rec = p.poll("events", "g2")
            self.assertEqual(rec, (5, "k1", "v1"))
            # g1 is caught up: broker returns no records
            self.assertIsNone(p.poll("events", "g1"))
        finally:
            p.close()

    def test_correlation_id_mismatch_raises(self):
        queues = bootstrap_queues()
        queues[("broker1", 9092)] = [
            frame_response(999, produce_body("events", base_offset=1))]
        p, _ = make_provider(queues)
        try:
            with self.assertRaises(KafkaProtocolError):
                p.publish("events", "k", "v")
        finally:
            p.close()

    def test_unsupported_broker_versions_rejected(self):
        queues = {("bootstrap", 1): [
            frame_response(1, api_versions_body(
                entries=((0, 0, 1), (1, 0, 1), (3, 0, 0), (18, 0, 0)))),
        ]}
        p, _ = make_provider(queues)
        try:
            with self.assertRaises(KafkaError) as ctx:
                p.publish("events", "k", "v")
            self.assertIn("does not support", str(ctx.exception))
        finally:
            p.close()

    def test_non_string_value_rejected(self):
        p, _ = make_provider({})
        try:
            with self.assertRaises(KafkaError):
                p.publish("events", "k", {"not": "a string"})
        finally:
            p.close()


# ---------------------------------------------------------------------------
# config + selection
# ---------------------------------------------------------------------------

class TestConfigAndSelection(unittest.TestCase):
    def test_missing_bootstrap_is_a_clear_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            KafkaConfig.from_env({})
        self.assertIn("NOVA_KAFKA_BOOTSTRAP", str(ctx.exception))

    def test_bad_bootstrap_entry(self):
        with self.assertRaises(RuntimeError):
            KafkaConfig.from_env({"NOVA_KAFKA_BOOTSTRAP": "justahost"})
        with self.assertRaises(RuntimeError):
            KafkaConfig.from_env({"NOVA_KAFKA_BOOTSTRAP": "h:notaport"})

    def test_env_parsing(self):
        cfg = KafkaConfig.from_env({
            "NOVA_KAFKA_BOOTSTRAP": "a:9092, b:9093 ",
            "NOVA_KAFKA_CLIENT_ID": "myapp",
            "NOVA_KAFKA_PARTITION": "2",
        })
        self.assertEqual(cfg.bootstrap, [("a", 9092), ("b", 9093)])
        self.assertEqual(cfg.client_id, "myapp")
        self.assertEqual(cfg.partition, 2)

    def test_selection(self):
        old = dict(os.environ)
        try:
            os.environ.pop("NOVA_STREAMS", None)
            os.environ.pop("NOVA_KAFKA_BOOTSTRAP", None)
            self.assertIsInstance(select_stream_provider(),
                                 MemoryStreamProvider)
            os.environ["NOVA_STREAMS"] = "kafka"
            os.environ["NOVA_KAFKA_BOOTSTRAP"] = "localhost:9092"
            self.assertIsInstance(select_stream_provider(),
                                 KafkaStreamProvider)
            os.environ["NOVA_STREAMS"] = "bogus"
            with self.assertRaises(RuntimeError):
                select_stream_provider()
        finally:
            os.environ.clear()
            os.environ.update(old)


if __name__ == "__main__":
    unittest.main(verbosity=2)
