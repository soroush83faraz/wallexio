"""NullAdvisor, AnthropicAdvisor (fake transport only, no network), LoggedAdvisor / shadow mode."""
from __future__ import annotations

import copy
import io
import json
import logging
import pickle
import random
import socket
import urllib.error
from decimal import Decimal

import pytest

from wallexbot.ai import advisor as advisor_mod
from wallexbot.ai.advisor import (ALLOWED_HOSTS, ENDPOINT, SYSTEM_PROMPT, AnthropicAdvisor, EndpointError,
                                  LoggedAdvisor, NullAdvisor, TransportRequest, UrllibTransport, check_endpoint,
                                  make_advisor)
from wallexbot.ai.schema import AdvisorContext, AdvisorVerdict, abstain, shrink_only
from wallexbot.redact import REDACTED, Secret, redact

KEY = "sk-FAKE-ZXCVBNMASDFGHJKLQWERTYUIOP-1234567890abcdef"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def ctx() -> AdvisorContext:
    return AdvisorContext(symbol="BTCUSDT", score=Decimal("0.7"), spread_bps=Decimal("4.5"),
                          cost_margin_bps=Decimal("12"), regime="favourable", rules={"breakout": "1.25"},
                          ret_mean_bps=Decimal("1.2"), ret_std_bps=Decimal("8"), ret_n=20)


def envelope(text: str, usage: dict | None = None) -> bytes:
    body = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
            "content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}
    if usage:
        body["usage"] = usage
    return json.dumps(body).encode()


GOOD = json.dumps({"decision": "allow", "size_multiplier": 0.5, "confidence": 0.7,
                   "rationale": "Spread is a large share of the margin.", "risk_flags": ["cost_margin_thin"]})


class FakeTransport:
    def __init__(self, reply=(200, envelope(GOOD)), clock: Clock | None = None, advance: float = 0.0) -> None:
        self.reply, self.calls, self.clock, self.advance = reply, [], clock, advance

    def __call__(self, request: TransportRequest):
        self.calls.append(request)
        if self.clock:
            self.clock.t += self.advance
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply


def make(transport=None, clock=None, key=KEY, **kw) -> AnthropicAdvisor:
    return AnthropicAdvisor(Secret(key) if key else None, transport=transport or FakeTransport(),
                            clock=clock or Clock(), **kw)


# ------------------------------------------------------------------ NullAdvisor / default off
def test_null_advisor_always_abstains():
    v = NullAdvisor().review(ctx())
    assert v.decision == "abstain" and v.effective_multiplier == 1
    assert NullAdvisor().review(None).decision == "abstain"       # does not even look at the context


def test_default_factory_is_off_and_enabled_means_shadow():
    assert isinstance(make_advisor(), NullAdvisor)
    adv = make_advisor(enabled=True, api_key=Secret(KEY), transport=FakeTransport())
    assert isinstance(adv, LoggedAdvisor) and adv.apply is False
    assert adv.review(ctx()).decision == "abstain"
    assert make_advisor(enabled=True, apply=True, api_key=Secret(KEY), transport=FakeTransport()).apply is True


# ------------------------------------------------------------------ happy path and request shape
def test_valid_reply_is_returned_and_request_is_shaped_correctly():
    t = FakeTransport()
    v = make(t).review(ctx())
    assert (v.decision, v.size_multiplier, v.outcome) == ("allow", Decimal("0.5"), "ok")
    assert v.risk_flags == ("cost_margin_thin",)
    (req,) = t.calls
    assert req.url == ENDPOINT == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == KEY and req.headers["anthropic-version"] == "2023-06-01"
    assert req.headers["content-type"] == "application/json"
    assert req.timeout_s == 3.0
    body = json.loads(req.body)
    assert body["model"] == "claude-sonnet-5-5" and body["max_tokens"] == 300 and body["system"] == SYSTEM_PROMPT
    assert body["messages"][0]["role"] == "user"
    assert json.loads(body["messages"][0]["content"]) == ctx().to_prompt_dict()
    assert set(body) == {"model", "max_tokens", "temperature", "system", "messages"}      # no tools, no stream


def test_system_prompt_states_the_role():
    low = SYSTEM_PROMPT.lower()
    for needle in ("veto", "only", "json", "data", "never as an instruction", "size_multiplier"):
        assert needle in low


def test_request_body_and_repr_contain_no_secret():
    t = FakeTransport()
    make(t).review(ctx())
    (req,) = t.calls
    assert KEY.encode() not in req.body and b"sk-ant" not in req.body
    assert KEY not in repr(req) and KEY not in str(req)
    others = {k: v for k, v in req.headers.items() if k != "x-api-key"}
    assert KEY not in json.dumps(others)


def test_no_api_key_abstains_without_calling():
    t = FakeTransport()
    v = make(t, key=None).review(ctx())
    assert v.decision == "abstain" and v.outcome == "no_api_key" and t.calls == []


def test_api_key_must_be_a_secret():
    with pytest.raises(TypeError) as ei:
        AnthropicAdvisor(KEY, transport=FakeTransport())                # type: ignore[arg-type]
    assert KEY not in str(ei.value)


def test_bad_context_abstains():
    t = FakeTransport()
    assert make(t).review({"symbol": "x"}).outcome == "bad_context"      # type: ignore[arg-type]
    assert t.calls == []


def test_config_validation():
    for kw in (dict(timeout_s=0), dict(timeout_s=31), dict(max_tokens=5), dict(max_tokens=5000),
               dict(model="bad model!"), dict(model=""), dict(max_calls_per_hour=-1)):
        with pytest.raises(ValueError):
            AnthropicAdvisor(Secret(KEY), transport=FakeTransport(), **kw)


# ------------------------------------------------------------------ every failure is abstain
@pytest.mark.parametrize("reply,outcome", [
    ((200, b"garbage"), "bad_envelope"),
    ((200, b""), "bad_envelope"),
    ((200, b"\xff\xfe"), "bad_envelope"),
    ((200, b"[]"), "bad_envelope"),
    ((200, b'{"content":[]}'), "bad_envelope"),
    ((200, b'{"content":[{"type":"tool_use","name":"x"}]}'), "bad_envelope"),
    ((200, b'{"content":"text"}'), "bad_envelope"),
    ((200, envelope("not json at all")), "rejected:bad_json"),
    ((200, envelope('{"decision":"allow","confidence":1,"price":1}')), "rejected:forbidden_key"),
    ((200, envelope('{"decision":"allow","confidence":1,"size_multiplier":1.5}')), "rejected:multiplier_out_of_range"),
    ((200, envelope('{"decision":"allow","confidence":1,"size_multiplier":NaN}')), "rejected:non_finite_number"),
    ((200, envelope("```json\n" + GOOD + "\n```")), "rejected:bad_json"),
    ((200, envelope("Sure! " + GOOD)), "rejected:bad_json"),
    ((200, envelope("x" * 5000)), "rejected:payload_too_large"),
    ((200, b"x" * 70_000), "response_too_large"),
    ((500, b"{}"), "http_500"),
    ((503, b""), "http_503"),
    ((429, envelope(GOOD)), "http_429"),
    ((401, envelope(GOOD)), "http_401"),
    ((301, b""), "http_301"),
    ((204, b""), "http_204"),
    (("200", b"{}"), "bad_transport_result"),
    ((200, "str"), "bad_transport_result"),
])
def test_bad_replies_abstain(reply, outcome):
    t = FakeTransport(reply)
    v = make(t).review(ctx())
    assert v.decision == "abstain" and v.effective_multiplier == 1 and v.outcome == outcome
    assert len(t.calls) == 1                                           # never retries


@pytest.mark.parametrize("exc,outcome", [
    (TimeoutError("took too long"), "timeout"),
    (socket.timeout("x"), "timeout"),
    (urllib.error.URLError(TimeoutError("t")), "timeout"),
    (urllib.error.URLError("dns failure"), "transport:URLError"),
    (ConnectionResetError("reset"), "transport:ConnectionResetError"),
    (RuntimeError("boom"), "transport:RuntimeError"),
    (ValueError("v"), "transport:ValueError"),
])
def test_transport_exceptions_abstain_without_retry(exc, outcome):
    t = FakeTransport(exc)
    v = make(t).review(ctx())
    assert v.decision == "abstain" and v.outcome == outcome and len(t.calls) == 1


def test_slow_reply_over_timeout_is_abstain_even_if_valid():
    clock = Clock()
    t = FakeTransport(clock=clock, advance=3.5)
    v = make(t, clock=clock).review(ctx())
    assert v.decision == "abstain" and v.outcome == "timeout"
    clock2 = Clock()
    ok = make(FakeTransport(clock=clock2, advance=2.9), clock=clock2).review(ctx())
    assert ok.decision == "allow"


def test_keyboard_interrupt_is_not_swallowed():
    with pytest.raises(KeyboardInterrupt):
        make(FakeTransport(KeyboardInterrupt())).review(ctx())


def test_unexpected_internal_error_is_abstain(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError(KEY)
    monkeypatch.setattr(advisor_mod, "parse_verdict", boom)
    v = make().review(ctx())
    assert v.decision == "abstain" and v.outcome == "error:RuntimeError" and KEY not in repr(v)


# ------------------------------------------------------------------ budget
def test_call_budget_per_hour():
    clock, t = Clock(), FakeTransport()
    adv = make(t, clock=clock, max_calls_per_hour=3)
    assert [adv.review(ctx()).decision for _ in range(3)] == ["allow"] * 3
    v = adv.review(ctx())
    assert v.decision == "abstain" and v.outcome == "budget_exceeded" and len(t.calls) == 3
    clock.t += 3599
    assert adv.review(ctx()).outcome == "budget_exceeded"
    clock.t += 2
    assert adv.review(ctx()).decision == "allow"                       # window slid


def test_failed_calls_count_against_the_budget():
    t = FakeTransport((500, b""))
    adv = make(t, max_calls_per_hour=2)
    assert [adv.review(ctx()).outcome for _ in range(3)] == ["http_500", "http_500", "budget_exceeded"]


def test_zero_calls_budget_never_calls():
    t = FakeTransport()
    assert make(t, max_calls_per_hour=0).review(ctx()).outcome == "budget_exceeded" and t.calls == []


def test_token_budget_uses_reservation_then_actual_usage():
    t = FakeTransport()
    adv = make(t, max_total_tokens=1)                                  # less than one call's reservation
    assert adv.review(ctx()).outcome == "budget_exceeded" and t.calls == []
    t2 = FakeTransport((200, envelope(GOOD, {"input_tokens": 100, "output_tokens": 20})))
    adv2 = make(t2, max_total_tokens=1500)             # one reservation is ~600+; actual usage is 120
    outcomes = [adv2.review(ctx()).decision for _ in range(12)]
    n = outcomes.count("allow")
    assert 3 <= n < 12 and outcomes == ["allow"] * n + ["abstain"] * (12 - n) and len(t2.calls) == n
    t3 = FakeTransport((200, envelope(GOOD, {"input_tokens": True, "output_tokens": -5})))
    assert make(t3, max_total_tokens=100_000).review(ctx()).decision == "allow"      # junk usage ignored


# ------------------------------------------------------------------ host pinning
@pytest.mark.parametrize("url", [
    "http://api.anthropic.com/v1/messages", "https://evil.example/v1/messages",
    "https://api.anthropic.com.evil.example/v1/messages", "https://evil.example/api.anthropic.com",
    "https://api.anthropic.com@evil.example/v1/messages", "https://user:pw@api.anthropic.com/v1/messages",
    "https://api.anthropic.com:8443/v1/messages", "https://API.ANTHROPIC.COM.evil.example/", "ftp://api.anthropic.com/",
    "", "api.anthropic.com/v1/messages", "https://127.0.0.1/v1/messages", "https://api.wallex.ir/v1/messages",
    None, 5,
])
def test_host_pinning_rejects(url):
    with pytest.raises(EndpointError):
        check_endpoint(url)
    with pytest.raises(EndpointError):
        AnthropicAdvisor(Secret(KEY), endpoint=url, transport=FakeTransport())     # type: ignore[arg-type]


def test_host_pinning_accepts_only_the_api_host():
    assert ALLOWED_HOSTS == frozenset({"api.anthropic.com"})
    assert check_endpoint(ENDPOINT) == ENDPOINT
    assert check_endpoint("https://api.anthropic.com:443/v1/messages")
    assert check_endpoint("https://API.anthropic.com/v1/messages")


def test_default_transport_rechecks_host_and_never_touches_network(monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("network used")
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", no_network)
    req = TransportRequest("https://evil.example/x", b"{}", 1.0, {"x-api-key": KEY})
    with pytest.raises(EndpointError):
        UrllibTransport()(req)


def test_default_transport_behaviour_with_stubbed_opener(monkeypatch):
    """Redirects are refused, HTTP errors become a status, the deadline is enforced while reading."""
    class Resp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    seen = {}

    def fake_open(self, req, timeout=None):
        seen.update(url=req.full_url, method=req.get_method(), timeout=timeout, headers=dict(req.header_items()))
        return Resp(b'{"ok":1}')

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", fake_open)
    req = TransportRequest(ENDPOINT, b"{}", 2.0, {"x-api-key": KEY, "anthropic-version": "2023-06-01"})
    assert UrllibTransport()(req) == (200, b'{"ok":1}')
    assert seen["method"] == "POST" and seen["timeout"] == 2.0 and seen["url"] == ENDPOINT
    assert seen["headers"]["X-api-key"] == KEY

    clock = Clock()

    class Slow(Resp):
        def read(self, n=-1):
            clock.t += 5
            return b"x"
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda self, r, timeout=None: Slow(b""))
    with pytest.raises(TimeoutError):
        UrllibTransport(clock)(req)

    class Huge(Resp):
        def read(self, n=-1):
            return b"x" * 4096
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda self, r, timeout=None: Huge(b""))
    with pytest.raises(ValueError):
        UrllibTransport()(req)

    def http_error(self, r, timeout=None):
        raise urllib.error.HTTPError(ENDPOINT, 429, "slow down", {}, io.BytesIO(b"x"))
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", http_error)
    assert UrllibTransport()(req) == (429, b"")


def test_redirects_are_not_followed():
    handler = advisor_mod._NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/") is None


# ------------------------------------------------------------------ the key never leaks
def test_key_never_in_exception_log_repr_or_record(caplog):
    caplog.set_level(logging.DEBUG)
    leaky = FakeTransport(RuntimeError(f"connect failed, headers: x-api-key: {KEY}"))
    adv = LoggedAdvisor(make(leaky), apply=True)
    v = adv.review(ctx())
    assert v.outcome == "transport:RuntimeError"
    records = adv.drain()
    blobs = [repr(v), json.dumps(records), caplog.text, repr(adv), repr(make()), repr(Secret(KEY))]
    for blob in blobs:
        assert KEY not in blob and "ZXCVBNMASDFGHJKL" not in blob
    with pytest.raises(TypeError):
        pickle.dumps(Secret(KEY))
    a = make()
    assert KEY not in repr(vars(a)) and KEY not in str(a.__dict__.values())
    assert copy.copy(a)._api_key is a._api_key and KEY not in repr(copy.copy(a))
    assert KEY not in repr(redact({"headers": {"x-api-key": KEY}, "msg": f"key={KEY}"}))
    assert REDACTED in repr(redact(f"x-api-key: {KEY}"))


# ------------------------------------------------------------------ LoggedAdvisor / shadow mode
def test_shadow_mode_returns_abstain_but_logs_the_real_verdict():
    adv = LoggedAdvisor(make(), apply=False, wall_clock=lambda: 1234.5)
    returned = adv.review(ctx())
    assert returned.decision == "abstain" and returned.effective_multiplier == 1 and returned.outcome == "shadow"
    (rec,) = adv.drain()
    assert rec["mode"] == "shadow" and rec["verdict"]["decision"] == "allow" and rec["verdict"]["outcome"] == "ok"
    assert rec["returned_decision"] == "abstain" and rec["ok"] is True and rec["ts"] == 1234.5
    assert rec["symbol"] == "BTCUSDT" and rec["context"]["regime"] == "favourable"
    assert adv.drain() == []
    json.dumps(rec)                                                    # persistable as-is


def test_default_is_shadow():
    assert LoggedAdvisor(make()).apply is False
    assert LoggedAdvisor(make()).review(ctx()).decision == "abstain"


def test_apply_mode_returns_the_verdict():
    adv = LoggedAdvisor(make(), apply=True)
    v = adv.review(ctx())
    assert v.decision == "allow" and v.size_multiplier == Decimal("0.5")
    assert adv.drain()[0]["mode"] == "apply" and adv.drain() == []


def test_failures_are_logged_with_latency_and_outcome():
    clock = Clock()
    t = FakeTransport((503, b""), clock=clock, advance=0.25)
    adv = LoggedAdvisor(make(t, clock=clock), apply=True, clock=clock)
    v = adv.review(ctx())
    (rec,) = adv.drain()
    assert v.outcome == "http_503" and rec["ok"] is False and rec["verdict"]["outcome"] == "http_503"
    assert rec["latency_ms"] == 250 and rec["verdict"]["decision"] == "abstain"


def test_inner_exception_and_bad_return_types_are_abstain():
    class Boom:
        def review(self, c):
            raise RuntimeError(KEY)

    class Wrong:
        def review(self, c):
            return "veto"

    class Sub(AdvisorVerdict):
        @property
        def effective_multiplier(self):
            return Decimal(5)

    class Sneaky:
        def review(self, c):
            return Sub("allow", Decimal(1), Decimal(1), "", (), "ok")

    for inner, outcome in ((Boom(), "advisor_error:RuntimeError"), (Wrong(), "bad_verdict_type"),
                           (Sneaky(), "bad_verdict_type")):
        adv = LoggedAdvisor(inner, apply=True)
        v = adv.review(ctx())
        assert v.decision == "abstain" and v.outcome == outcome
        assert KEY not in json.dumps(adv.drain())


def test_sink_receives_records_and_its_failure_is_swallowed():
    got = []
    adv = LoggedAdvisor(make(), apply=True, sink=got.append)
    adv.review(ctx())
    assert len(got) == 1 and got[0]["verdict"]["decision"] == "allow"

    def bad_sink(rec):
        raise OSError("disk full")
    assert LoggedAdvisor(make(), apply=True, sink=bad_sink).review(ctx()).decision == "allow"


def test_buffer_is_bounded():
    adv = LoggedAdvisor(NullAdvisor(), max_buffer=5)
    for _ in range(20):
        adv.review(ctx())
    assert len(adv.drain()) == 5


def test_invalid_context_object_still_logged_and_safe():
    adv = LoggedAdvisor(make(), apply=True)
    assert adv.review("not a context").decision == "abstain"            # type: ignore[arg-type]
    (rec,) = adv.drain()
    assert rec["symbol"] is None and rec["context"] is None and rec["ok"] is False


def test_logged_advisor_satisfies_protocol():
    assert isinstance(NullAdvisor(), advisor_mod.Advisor)
    assert isinstance(LoggedAdvisor(NullAdvisor()), advisor_mod.Advisor)
    assert isinstance(make(), advisor_mod.Advisor)


# ------------------------------------------------------------------ never enlarge, end to end
def test_random_model_replies_never_enlarge_a_size():
    rnd = random.Random(99)
    quantities = [Decimal("0.001"), Decimal("1"), Decimal("12345.678")]
    for _ in range(500):
        reply = json.dumps({"decision": rnd.choice(["allow", "veto", "abstain", "buy"]),
                            "size_multiplier": rnd.choice([rnd.uniform(-2, 3), 1, 0, 2, 0.5]),
                            "confidence": rnd.uniform(-1, 2)})
        adv = LoggedAdvisor(make(FakeTransport((200, envelope(reply)))), apply=True)
        v = adv.review(ctx())
        assert 0 <= v.effective_multiplier <= 1
        for q in quantities:
            assert shrink_only(q, v) <= q


def test_abstain_helper_is_what_failures_return():
    assert abstain("x") == abstain("x") and abstain("x").decision == "abstain"
