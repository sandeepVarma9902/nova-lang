# URL Shortener with Analytics — the platform story

Shorten URLs over HTTP; every redirect publishes a visit event to a
stream; a subscriber counts clicks per code. Nova Boot + streams, one
language, one process. It really runs.

## What it shows

| Nova feature | Where |
|---|---|
| Boot routing + `redirect()` (302 + Location) | `server.nova:96-113` |
| Streams: `stream visits: String` | `server.nova:21` |
| Publishing from an HTTP handler | `server.nova:106` |
| Subscriber: poll → handle → commit → drain | `server.nova:42-64` |
| Consumer groups (offsets per group) | `server.nova:125` |
| JSON API (`/shorten`, `/stats`) | `server.nova:83`, `server.nova:116` |

## How to run

From the `nova-lang` directory:

```bash
NOVA_PORT=8083 ./nova run showcase/shortener/server.nova
```

```bash
# shorten
curl -X POST localhost:8083/shorten -d '{"url":"https://example.com"}'
# -> "c1"

# visit (302 redirect; publishes a visit event)
curl -i localhost:8083/c1
# HTTP/1.0 302 Found
# Location: https://example.com

# analytics (subscriber counted the clicks)
curl localhost:8083/stats
# [{"code":"c1","n":1}]
```

## Key design points (for the React dev)

**Events are typed.** `stream visits: String` declares a topic carrying
`String`s. `publish`, `subscribe`, `poll`, and `commit` are checked —
publishing the wrong type is a compile-time error, like everything else
in Nova.

**The subscriber is just a function.** `drainClicks` (server.nova:58)
polls the subscription, `handleVisit` records the click and commits the
offset, then recurses. No framework, no annotations — the stream is a
value and the consumer is a loop. Compare: a Kafka consumer group in
Java is ~100 lines of configuration; here it is 15 lines of Nova.

**Consumer groups are real.** `visits.subscribe(group: "analytics")`
tracks its own offset. Add a second group and it replays from the start
— the same semantics as Kafka, on the in-memory provider. Set
`NOVA_STREAMS=kafka` for the real wire-protocol provider (v2, partial —
see `docs/v2-kafka.md`).

**Honest single-process note.** The server is single-threaded, so the
redirect handler drains the subscriber inline after publishing
(server.nova:108). In a multi-process deployment the subscriber would be
its own consumer; the code — publish here, poll/commit there — does not
change. This README says so instead of pretending the demo is
distributed.

## Files

- `server.nova` — the whole app: routes, stream, subscriber, boot.
  (Named `server.nova`, not `app.nova` — `app.nova` is the manifest
  convention and would be misread by `nova run`.)
