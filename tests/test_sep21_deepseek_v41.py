"""DeepSeek V4.1 Flash migration (2026-09-21): both MTG roles moved to
`deepseek-flash`, and !cost got an exact-priced V4.1 bucket — cache hits at
the cache rate, 2x during DeepSeek's weekday peak hours — while the old
V4-Flash actor and V4-Pro buckets stay frozen at their historical flat rates.

Drives the real adapter + tracker bodies; no Discord, no network. The
tracker writes data/api_costs.json relative to cwd, so those pins run in a
tmp_path cwd.
"""
from datetime import datetime, timezone

import pytest
from openai.types.chat import ChatCompletion

import bot as cb
from rules import llm_adapter as la


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


# --- adapter: cache hits ride along as a SUBSET of input_tokens --------------

@pytest.mark.parametrize("usage, expected", [
    (None, 0),
    ({"prompt_cache_hit_tokens": 40}, 40),                                  # DeepSeek
    ({"prompt_tokens_details": {"cached_tokens": 30}}, 30),                 # OpenAI-standard
    ({"prompt_cache_hit_tokens": 20,
      "prompt_tokens_details": {"cached_tokens": 20}}, 20),                 # both → never summed
    ({"prompt_cache_hit_tokens": 0,
      "prompt_tokens_details": {"cached_tokens": 9}}, 9),
    ({"prompt_tokens_details": {"cached_tokens": None}}, 0),
    ({"prompt_tokens_details": {"cached_tokens": "x"}}, 0),
])
def test_cache_hit_tokens_shapes(usage, expected):
    assert la._cache_hit_tokens(usage) == expected


def test_adapted_response_carries_cache_hits():
    resp = ChatCompletion.model_validate({
        "id": "x", "object": "chat.completion", "created": 0, "model": "deepseek-flash",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "{}"}}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 10, "total_tokens": 1010,
                  "prompt_cache_hit_tokens": 700, "prompt_cache_miss_tokens": 300},
    })
    u = la._AdaptedResponse(resp).usage
    assert (u.input_tokens, u.output_tokens, u.prompt_cache_hit_tokens) == (1000, 10, 700)
    assert la._Usage(5, 1).prompt_cache_hit_tokens == 0


def test_factories_use_deepseek_flash(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake")
    actor = la.create_deepseek_adapter()
    strategist = la.create_deepseek_reasoner_adapter()
    assert actor._model == strategist._model == "deepseek-flash"
    assert actor.messages._thinking_enabled is False                       # fast JSON actor
    assert strategist.messages._reasoning_effort == "medium"                # May 23 tuning kept


# --- exact per-call pricing ---------------------------------------------------

def test_v41_cost_off_peak_and_peak():
    cost, peak = cb._deepseek_v41_cost(1_000_000, 0, 1_000_000, when=utc(2026, 9, 26, 12))  # Sat
    assert not peak and cost == pytest.approx(0.75)
    cost, peak = cb._deepseek_v41_cost(1_000_000, 0, 1_000_000, when=utc(2026, 9, 21, 7))   # Mon
    assert peak and cost == pytest.approx(1.50)


@pytest.mark.parametrize("hour, minute, peak", [
    (0, 59, False), (1, 0, True), (3, 59, True), (4, 0, False),
    (5, 59, False), (6, 0, True), (9, 59, True), (10, 0, False),
])
def test_v41_peak_window_edges_monday(hour, minute, peak):
    assert cb._deepseek_v41_cost(1, 0, 1, when=utc(2026, 9, 21, hour, minute))[1] is peak


def test_v41_cache_hits_bill_at_cache_rate_and_clamp():
    cost, _ = cb._deepseek_v41_cost(1_000_000, 800_000, 0, when=utc(2026, 9, 26, 12))
    assert cost == pytest.approx(0.2 * 0.15 + 0.8 * 0.003)
    cost, _ = cb._deepseek_v41_cost(100, 500, 0, when=utc(2026, 9, 26, 12))  # hits > input
    assert cost == pytest.approx(100 * 0.003 / 1e6)


# --- tracker: routing, persistence, !cost -------------------------------------

_COUNTERS = """total_input_tokens total_output_tokens api_calls opus_input_tokens
opus_output_tokens sonnet_input_tokens sonnet_output_tokens haiku_input_tokens
haiku_output_tokens mtg_game_input_tokens mtg_game_output_tokens
mtg_game_sonnet_input_tokens mtg_game_sonnet_output_tokens mtg_game_calls
mtg_game_deepseek_input_tokens mtg_game_deepseek_output_tokens deepseek_input_tokens
deepseek_output_tokens deepseek_calls deepseek_pro_input_tokens deepseek_pro_output_tokens
deepseek_pro_calls mtg_game_deepseek_pro_input_tokens mtg_game_deepseek_pro_output_tokens
deepseek_v41_input_tokens deepseek_v41_cache_hit_tokens deepseek_v41_output_tokens
deepseek_v41_calls deepseek_v41_peak_calls mtg_game_deepseek_v41_input_tokens
mtg_game_deepseek_v41_output_tokens""".split()


def _fresh_bot():
    b = cb.CompanionBot.__new__(cb.CompanionBot)
    for name in _COUNTERS:
        setattr(b, name, 0)
    b.deepseek_v41_cost = b.mtg_game_deepseek_v41_cost = 0.0
    return b


@pytest.fixture
def tracked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    b = _fresh_bot()
    b.track_mtg_usage(la._Usage(1_000_000, 1_000_000, 800_000), "deepseek-flash")
    b.track_mtg_usage(la._Usage(1000, 10, 0), "deepseek-v4-flash")   # legacy alias = V4.1 now
    b.track_mtg_usage(la._Usage(500, 5, 0), "deepseek-v4-pro")
    return b


def test_routing_v41_vs_frozen_legacy(tracked):
    b = tracked
    assert (b.deepseek_v41_calls, b.deepseek_v41_input_tokens,
            b.deepseek_v41_cache_hit_tokens) == (2, 1_001_000, 800_000)
    assert b.deepseek_calls == 0 and b.deepseek_input_tokens == 0     # legacy actor frozen
    assert b.deepseek_pro_calls == 1 and b.deepseek_pro_input_tokens == 500


def test_tracker_cost_is_exact(tracked):
    off = sum(cb._deepseek_v41_cost(i, h, o, when=utc(2026, 9, 26, 12))[0]
              for i, h, o in ((1_000_000, 800_000, 1_000_000), (1000, 0, 10)))
    # Recorded at the real clock: either both calls off-peak or both at peak.
    assert tracked.deepseek_v41_cost in (pytest.approx(off), pytest.approx(2 * off))
    assert tracked.mtg_game_deepseek_v41_cost == pytest.approx(tracked.deepseek_v41_cost)


def test_persistence_round_trip(tracked):
    b2 = _fresh_bot()
    b2._load_persistent_costs()
    assert (b2.deepseek_v41_calls, b2.deepseek_v41_cache_hit_tokens,
            b2.mtg_game_deepseek_v41_input_tokens) == (2, 800_000, 1_001_000)
    assert b2.deepseek_v41_cost == pytest.approx(tracked.deepseek_v41_cost)


def test_cost_summary(tracked):
    s = tracked.get_cost_summary()
    legacy_pro = 500 * 0.56 / 1e6 + 5 * 1.68 / 1e6
    total = tracked.deepseek_v41_cost + legacy_pro
    assert "V4.1 Flash:" in s and "800,000 cached" in s
    assert f"Total Lifetime Cost: ${total:.4f}" in s
    # V4.1 tokens must NOT fall into the MTG "Opus remainder" (the May 14 bug).
    assert f"Est. cost: ${total:.4f}" in s
