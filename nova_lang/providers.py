# providers.py — Nova AI provider interface.
#
# An `ai fn` declares its output schema as its Nova return type. Providers
# turn (model, prompt, schema) into a Nova value. v1 ships a deterministic
# mock; a real HTTP provider hook is included but marked EXPERIMENTAL
# (roadmap: first-class providers with streaming in v2).

import json
import os
import urllib.request


class AIRequest:
    def __init__(self, model, prompt, schema, schema_json, temperature,
                 max_tokens, stream, build_mock, coerce_json):
        self.model = model
        self.prompt = prompt
        self.schema = schema            # Nova Type
        self.schema_json = schema_json  # JSON-schema dict
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.stream = stream
        self.build_mock = build_mock    # (type, prompt) -> Nova value
        self.coerce_json = coerce_json  # (data, type) -> Nova value


class AIProvider:
    name = "base"

    def complete(self, request):
        raise NotImplementedError


class MockProvider(AIProvider):
    """Deterministic mock: builds a value of the schema type. Default."""
    name = "mock"

    def complete(self, request):
        return request.build_mock(request.schema, request.prompt)


class HTTPProvider(AIProvider):
    """EXPERIMENTAL: POSTs to a generic completion endpoint.
    Configure with NOVA_AI_BASE_URL and NOVA_AI_API_KEY.
    Not covered by the v1 test suite (needs network + a key)."""

    name = "http"

    def complete(self, request):
        base = os.environ.get("NOVA_AI_BASE_URL")
        key = os.environ.get("NOVA_AI_API_KEY")
        if not base or not key:
            raise RuntimeError(
                "HTTPProvider needs NOVA_AI_BASE_URL and NOVA_AI_API_KEY")
        payload = {
            "model": request.model,
            "prompt": request.prompt,
            "output_schema": request.schema_json,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": False,  # streaming is v2 (syntax accepted in v1)
        }
        req = urllib.request.Request(
            base.rstrip("/") + "/v1/complete",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + key,
                     "User-Agent": "nova-lang/0.1"})
        with urllib.request.urlopen(req, timeout=60) as r:
            body = json.loads(r.read().decode("utf-8"))
        # Convention: {"output": <JSON matching output_schema>}
        return request.coerce_json(body.get("output"), request.schema)


def select_provider(explicit=None):
    name = explicit or os.environ.get("NOVA_AI_PROVIDER", "mock")
    if name == "mock":
        return MockProvider()
    if name == "http":
        return HTTPProvider()
    raise RuntimeError("unknown NOVA_AI_PROVIDER=%r (want mock|http)" % name)
