# Nova streams on real Kafka (v2 provider)

`NOVA_STREAMS=kafka` swaps the in-memory stream provider for
`nova_lang/kafka_provider.py`: a real Kafka wire-protocol client written
with **only the Python standard library** (`socket` + `struct`). Same Nova
code — `stream`, `publish`, `subscribe`, `poll`, `commit` — now talks to a
real broker.

## What works

- **Produce** (`stream.publish(...)`) — Kafka Produce API **v2**, `acks=1`,
  one v2 record batch (magic=2) per publish, CRC32C checksummed. Returns the
  broker-assigned base offset.
- **Fetch** (`subscription.poll()`) — Kafka Fetch API **v4**,
  `READ_UNCOMMITTED`, from the group's committed offset + 1. Fetched records
  are buffered client-side so consecutive `poll()` calls each return one
  message, matching the in-memory provider's contract.
- **Bootstrap & leader discovery** — tries each `NOVA_KAFKA_BOOTSTRAP`
  host in order, runs **ApiVersions v0** to negotiate, then **Metadata v1**
  to find the partition leader. Metadata is refreshed automatically on
  `NOT_LEADER_FOR_PARTITION` / `UNKNOWN_TOPIC_OR_PARTITION` and the request
  is retried once.
- **Version safety** — the provider requires the broker to support Produce
  v2, Fetch v4 and Metadata v1 (any modern Kafka ≥ 2.x and Redpanda do) and
  fails fast with a clear error otherwise.
- **Serialization** — Nova `String` keys/values are UTF-8 on the wire;
  `None` keys become null. Non-UTF-8 payloads from other producers are
  returned as raw bytes rather than crashing.
- **Threading** — one lock guards connections, correlation ids and group
  state, like the memory provider.

## What it needs

| Env var | Required | Default | Meaning |
|---|---|---|---|
| `NOVA_STREAMS` | to select | `memory` | set to `kafka` for this provider |
| `NOVA_KAFKA_BOOTSTRAP` | **yes** | — | `host:port[,host:port...]`; clear error if unset |
| `NOVA_KAFKA_CLIENT_ID` | no | `nova` | Kafka `client.id` |
| `NOVA_KAFKA_CONNECT_TIMEOUT_MS` | no | `10000` | TCP connect timeout |
| `NOVA_KAFKA_REQUEST_TIMEOUT_MS` | no | `30000` | per-request socket timeout |
| `NOVA_KAFKA_FETCH_MAX_WAIT_MS` | no | `1000` | fetch long-poll wait |
| `NOVA_KAFKA_FETCH_MAX_BYTES` | no | `1048576` | fetch size cap |
| `NOVA_KAFKA_PARTITION` | no | `0` | the single partition used per topic |

- **The topic must exist**, or the broker must have topic auto-creation
  enabled (`auto.create.topics.enable=true`, the default in Apache Kafka).
  The provider does not create topics (no CreateTopics admin API yet).
- **PLAINTEXT only.** No SASL, no TLS — the provider speaks the raw binary
  protocol over plain TCP.
- **One partition per topic** (configurable via `NOVA_KAFKA_PARTITION`).
  Nova's ordered-log contract maps onto a single partition's log; the
  provider does not fan out across partitions.

## What is NOT implemented (honest list)

- **Consumer groups / OffsetCommit** — `commit()` is tracked **client-side,
  in memory**, exactly like the memory provider's semantics. Restart the
  process and group cursors reset to the beginning. Real consumer-group
  rebalancing is roadmap work.
- **Transactions, idempotent producer** — `acks=1`, no `producer.id`.
- **SASL/TLS, compression, CreateTopics/DeleteTopics admin, fetch sessions,
  multi-partition produce/fetch.**

## Verification status

There was **no live broker available when this was written**, so the
implementation is correct-by-construction against the Kafka protocol guide
(ApiVersions v0, Metadata v1, Produce v2, Fetch v4, v2 record batches —
all field layouts verified against https://kafka.apache.org/protocol.html)
plus **28 unit tests** (`tests/test_kafka_provider.py`) that drive the
provider with a fake socket:

- exact request bytes asserted field-by-field (length prefixes, api keys,
  `acks=1`, `isolation_level=0`, fetch offsets, record batch layout);
- canned broker responses parsed back (version map, leader endpoints,
  produce offsets, fetch records with absolute offsets);
- end-to-end `publish → poll → poll → commit → poll` against the fake
  transport, including independent consumer groups and correlation-id
  mismatch detection;
- CRC32C verified against the standard check vector
  (`crc32c(b"123456789") == 0xE3069283`) and corrupt batches rejected.

**Before trusting this in production, run the walkthrough below against a
real broker** — that live round-trip is the one verification the fake
transport cannot give.

## Walkthrough: Nova + local Kafka

```bash
cd ~/workspace/nova-lang

# 1. Start a single-node KRaft broker (no ZooKeeper needed)
docker compose -f docker-compose.kafka.yml up -d

# 2. Wait for it to be ready, then create the topic Nova will use
#    (skip this if you rely on auto.create.topics.enable=true,
#    which the compose file already sets)
docker exec nova-kafka /opt/kafka/bin/kafka-topics.sh \
  --create --topic events --bootstrap-server localhost:9092

# 3. Run the streams example against the broker
NOVA_STREAMS=kafka NOVA_KAFKA_BOOTSTRAP=localhost:9092 \
  ./nova run examples/streams.nova
# EXPECT:
# hello
# (no key)
# world
# key=k1
# 2

# 4. Confirm the records really landed on the broker
docker exec nova-kafka /opt/kafka/bin/kafka-console-consumer.sh \
  --bootstrap-server localhost:9092 --topic events --from-beginning

# 5. Shut it down
docker compose -f docker-compose.kafka.yml down
```

Notes:

- `stream events: String` maps 1:1 to the Kafka topic `events`
  (the declared `topic` name, or the stream name).
- If `NOVA_KAFKA_BOOTSTRAP` is unset you get:
  `NOVA_STREAMS=kafka requires NOVA_KAFKA_BOOTSTRAP to be set, e.g.
  NOVA_KAFKA_BOOTSTRAP=localhost:9092 ...`
- If the topic doesn't exist and auto-creation is off, `publish` raises
  `KafkaError: metadata for topic 'events' failed:
  UNKNOWN_TOPIC_OR_PARTITION (create the topic first, or enable the
  broker's auto.create.topics.enable)`.
- Redpanda speaks the same wire protocol and should work wherever it
  supports these API versions; it has not been tested here.

## Files

- `nova_lang/kafka_provider.py` — the provider (stdlib only)
- `nova_lang/streams.py` — `select_stream_provider()` (`NOVA_STREAMS`)
- `tests/test_kafka_provider.py` — 28 tests, fake-socket transport
- `docker-compose.kafka.yml` — local single-node broker
