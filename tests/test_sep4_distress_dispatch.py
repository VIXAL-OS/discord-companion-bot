"""The distress-dispatch chain (immediate response, stand-down, per-channel
lock, follow-up window, billing alerts, birthdate-derived age).

FORK NOTE: ported from the upstream bot's audit of one evening's transcript,
generalized per monitored user. Four mechanical defects motivated it, none in
the support content itself:

1. A DOUBLE reply: a Discord reply that pinged the bot was answered by the
   normal path at keyword score 0.00 while the Haiku classifier — launched
   regardless — came back at 0.4 and re-dispatched BEFORE the (much slower)
   Opus reply had been sent. A stand-down that reads only already-answered
   ids loses that race every time. Fix: skip the classifier for any message
   the handler will answer anyway, plus an in-flight marker recorded
   synchronously the moment should_respond is decided.
2. Two rapid-fire messages generated in parallel, the second reply never
   seeing the first and repeating it. Fix: one asyncio.Lock per channel.
3. A calm-scoring follow-up one minute after a spiral left unanswered for
   22 minutes. Fix: inside an open distress window, answer regardless.
4. The Anthropic credit balance ran out mid-conversation and the classifier
   swallowed the 400. Fix: classify billing errors by TEXT and DM the
   maintainer once per provider per cooldown.

Behavioral pins drive the REAL production method bodies with duck-typed
collaborators. Source-level pins are used only for the on_message wiring,
which cannot be driven without a Discord client — each is labelled.
"""
import asyncio
import json
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import bot as bot_module
from bot import CONFIG, CompanionBot, _age_from_birthdate, is_billing_error

SRC = (Path(__file__).resolve().parent.parent / "bot.py").read_text(encoding="utf-8")

LIVE_ANTHROPIC = (
    "Error code: 400 - {'type': 'error', 'error': {'type': "
    "'invalid_request_error', 'message': 'Your credit balance is too low to "
    "access the Anthropic API. Please go to Plans & Billing to upgrade or "
    "purchase credits.'}, 'request_id': 'req_x'}")
LIVE_DEEPSEEK = (
    "Error code: 402 - {'error': {'message': 'Insufficient Balance', "
    "'type': 'unknown_error', 'param': None, 'code': 'invalid_request_error'}}")

UID = 4242


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeMsg:
    _next = iter(range(500_000, 999_999))

    def __init__(self):
        self.id = next(self._next)


def _make_duck(score=0.5, spiral=False, *, raise_exc=None):
    """Duck carrying exactly what _classify_distress touches."""

    class _Usage:
        input_tokens = 10
        output_tokens = 5

    class _Block:
        def __init__(self, text):
            self.text = text

    class _Response:
        usage = _Usage()

        def __init__(self, text):
            self.content = [_Block(text)]

    class _Messages:
        def create(self, **kwargs):
            if raise_exc is not None:
                raise raise_exc
            body = json.dumps({"score": score, "spiral": spiral,
                               "reason": "test"})[1:]
            return _Response(body)

    class _Claude:
        messages = _Messages()

    class _Duck:
        def __init__(self):
            self.semantic_pending = defaultdict(bool)
            self.semantic_triggered = {}
            self.answered_message_ids = deque(maxlen=50)
            self._responding_message_ids = deque(maxlen=50)
            self._redispatch_ids = set()
            self.message_buffers = {UID: [(datetime.now(), "User", "test message")]}
            self.user_name_map = {}
            self.haiku_input_tokens = 0
            self.haiku_output_tokens = 0
            self.total_input_tokens = 0
            self.total_output_tokens = 0
            self.api_calls = 0
            self.claude = _Claude()
            self.on_message_calls = []
            self.alerts = []

        def _get_distress_context(self, user_id):
            return ""

        async def on_message(self, message):
            self.redispatch_flag_seen = (
                getattr(message, 'id', None) in self._redispatch_ids)
            self.on_message_calls.append(message)

        async def _maybe_billing_alert(self, provider, exc):
            self.alerts.append((provider, exc))
            return True

    return _Duck()


# ---------------------------------------------------------------------------
# 1. Immediate response + stand-down
# ---------------------------------------------------------------------------

class TestImmediateResponse:
    def test_flagged_score_responds_immediately(self):
        duck = _make_duck(score=0.5)
        msg = _FakeMsg()
        _run(CompanionBot._classify_distress(duck, UID, msg))
        assert duck.semantic_triggered.get(UID) is not None, "flag still armed as fallback"
        assert duck.on_message_calls == [msg]
        assert duck.redispatch_flag_seen is True
        assert msg.id not in duck._redispatch_ids, "must be discarded after"

    def test_sub_threshold_score_does_not_respond(self):
        duck = _make_duck(score=0.2)
        _run(CompanionBot._classify_distress(duck, UID, _FakeMsg()))
        assert duck.semantic_triggered.get(UID) is None
        assert duck.on_message_calls == []

    def test_answered_message_stands_down(self):
        duck = _make_duck(score=0.5)
        msg = _FakeMsg()
        duck.answered_message_ids.append(msg.id)
        _run(CompanionBot._classify_distress(duck, UID, msg))
        assert duck.on_message_calls == []
        assert duck.semantic_triggered.get(UID) is None, "no armed flag either"
        assert duck.semantic_pending[UID] is False

    def test_in_flight_message_stands_down(self):
        duck = _make_duck(score=0.5)
        msg = _FakeMsg()
        duck._responding_message_ids.append(msg.id)
        _run(CompanionBot._classify_distress(duck, UID, msg))
        assert duck.on_message_calls == []
        assert duck.semantic_triggered.get(UID) is None

    def test_a_different_id_does_not_block(self):
        duck = _make_duck(score=0.5)
        duck.answered_message_ids.append(1)
        duck._responding_message_ids.append(2)
        msg = _FakeMsg()
        _run(CompanionBot._classify_distress(duck, UID, msg))
        assert duck.on_message_calls == [msg]

    def test_redispatch_failure_keeps_the_fallback_flag(self):
        duck = _make_duck(score=0.5)

        async def boom(message):
            raise RuntimeError("send failed")
        duck.on_message = boom
        _run(CompanionBot._classify_distress(duck, UID, _FakeMsg()))
        assert duck.semantic_triggered.get(UID) is not None
        assert duck.semantic_pending[UID] is False

    def test_no_trigger_message_is_backward_compatible(self):
        duck = _make_duck(score=0.5)
        _run(CompanionBot._classify_distress(duck, UID))
        assert duck.semantic_triggered.get(UID) is not None
        assert duck.on_message_calls == []

    def test_pending_guard_blocks_reentry(self):
        duck = _make_duck(score=0.5)
        duck.semantic_pending[UID] = True
        _run(CompanionBot._classify_distress(duck, UID, _FakeMsg()))
        assert duck.on_message_calls == []


class TestClassifierGate:
    """Source-level (acknowledged weak; on_message needs a Discord client)."""

    def test_mention_and_thread_are_computed_before_the_launch(self):
        launch = "asyncio.create_task(self._classify_distress(monitored_uid, message))"
        assert SRC.count("is_mentioned = self.user.mentioned_in(message)") == 1
        assert SRC.index("is_mentioned = self.user.mentioned_in(message)") < SRC.index(launch)

    def test_launch_is_sub_threshold_and_gated_on_will_respond_anyway(self):
        gate = SRC[SRC.index("if (distress_score < CONFIG.distress_threshold"):]
        gate = gate[:gate.index("asyncio.create_task(self._classify_distress(")]
        assert "and not will_respond_anyway" in gate
        assert "if (distress_score == 0\n" not in SRC, "the zero-only gate is back"

    def test_will_respond_anyway_names_every_answering_path(self):
        block = SRC[SRC.index("will_respond_anyway = bool("):]
        block = block[:block.index(")")]
        for name in ("is_mtg_channel", "is_mentioned", "is_bot_thread",
                     "monitored_followup", "monitored_semantic"):
            assert name in block, f"{name} missing"

    def test_in_flight_marker_is_recorded_before_the_first_await(self):
        i_decide = SRC.index("if not should_respond:")
        i_mark = SRC.index("self._responding_message_ids.append(message.id)")
        i_await = SRC.index("content_parts = await self._process_message_content(")
        assert i_decide < i_mark < i_await
        code_lines = [l for l in SRC[i_decide:i_mark].split("\n")
                      if not l.strip().startswith("#")]
        assert not any("await " in l for l in code_lines), code_lines

    def test_answered_id_producer_follows_the_send_chokepoint(self):
        anchor = "await self._send_response(message.channel, reply, files)"
        assert SRC.count(anchor) == 1
        tail = SRC.split(anchor, 1)[1][:220]
        assert "self.answered_message_ids.append(message.id)" in tail

    def test_buffer_append_skips_redispatched_messages(self):
        i_skip = SRC.index("not in self._redispatch_ids")
        i_append = SRC.index("self.message_buffers[monitored_uid].append((now, buf_name, text_content))")
        assert i_skip < i_append and i_append - i_skip < 200


# ---------------------------------------------------------------------------
# 2. Per-channel lock
# ---------------------------------------------------------------------------

class _LockDuck:
    def __init__(self):
        self._chat_locks = {}


class TestChatLock:
    def test_one_lock_per_channel(self):
        d = _LockDuck()
        a1 = CompanionBot._chat_lock_for(d, 1)
        assert a1 is CompanionBot._chat_lock_for(d, 1)
        assert a1 is not CompanionBot._chat_lock_for(d, 2)

    def test_lock_serializes_two_handlers_in_one_channel(self):
        d = _LockDuck()
        order = []

        async def handler(name):
            async with CompanionBot._chat_lock_for(d, 7):
                order.append(f"{name}:start")
                await asyncio.sleep(0.01)
                order.append(f"{name}:end")

        async def main():
            await asyncio.gather(handler("first"), handler("second"))
        _run(main())
        assert order == ["first:start", "first:end", "second:start", "second:end"]

    def test_generation_and_send_sit_under_the_lock(self):
        """Source-level: the append + generate + send span is nested under the
        lock, with the user-turn append INSIDE it."""
        i_lock = SRC.index("async with self._chat_lock_for(thread_id):")
        i_append = SRC.index("        # Build conversation context")
        i_typing = SRC.index("async with message.channel.typing():")
        assert i_lock < i_append < i_typing
        lock_line = SRC[SRC.rfind("\n", 0, i_lock) + 1:i_lock]
        typing_line = SRC[SRC.rfind("\n", 0, i_typing) + 1:i_typing]
        assert len(typing_line) == len(lock_line) + 4
        assert "\n        async with message.channel.typing():\n" not in SRC


# ---------------------------------------------------------------------------
# 3. The follow-up window
# ---------------------------------------------------------------------------

def _window_duck():
    class _D:
        pass
    d = _D()
    d.distress_history = defaultdict(list)
    d.calm_message_count = defaultdict(int)
    return d


class TestFollowupWindow:
    def test_no_history_is_not_a_window(self):
        assert CompanionBot._followup_active(_window_duck(), 42) is False

    def test_recent_spiral_opens_the_window_regardless_of_calm_count(self):
        d = _window_duck()
        d.distress_history[42].append((datetime.now() - timedelta(minutes=5), 0.7, True))
        d.calm_message_count[42] = CONFIG.calm_messages_to_stepdown + 2
        assert CompanionBot._followup_active(d, 42) is True

    def test_spiral_window_expires(self):
        d = _window_duck()
        too_old = datetime.now() - timedelta(minutes=CONFIG.spiral_cooldown_minutes + 1)
        d.distress_history[42].append((too_old, 0.7, True))
        assert CompanionBot._followup_active(d, 42) is False

    def test_stress_window_respects_the_calm_counter(self):
        d = _window_duck()
        d.distress_history[42].append((datetime.now() - timedelta(minutes=2), 0.5, False))
        assert CompanionBot._followup_active(d, 42) is True
        d.calm_message_count[42] = CONFIG.calm_messages_to_stepdown
        assert CompanionBot._followup_active(d, 42) is False

    def test_window_is_per_channel_and_side_effect_free(self):
        d = _window_duck()
        d.distress_history[42].append((datetime.now(), 0.7, True))
        assert CompanionBot._followup_active(d, 43) is False
        CompanionBot._followup_active(d, 42)
        assert len(d.distress_history[42]) == 1 and d.calm_message_count[42] == 0

    def test_config_switch_disables_it(self, monkeypatch):
        d = _window_duck()
        d.distress_history[42].append((datetime.now(), 0.7, True))
        monkeypatch.setattr(CONFIG, "followup_after_distress", False)
        assert CompanionBot._followup_active(d, 42) is False

    def test_followup_feeds_needs_support(self):
        needs = SRC[SRC.index("monitored_needs_support = is_monitored and ("):]
        needs = needs[:needs.index(")")]
        assert "monitored_followup" in needs


# ---------------------------------------------------------------------------
# 4. Billing errors and the maintainer DM
# ---------------------------------------------------------------------------

class TestBillingClassifier:
    def test_live_anthropic_400_is_billing(self):
        assert is_billing_error(RuntimeError(LIVE_ANTHROPIC))

    def test_live_deepseek_402_is_billing(self):
        assert is_billing_error(RuntimeError(LIVE_DEEPSEEK))

    def test_402_status_code_is_billing(self):
        class _E(Exception):
            status_code = 402
        assert is_billing_error(_E("Payment Required"))

    @pytest.mark.parametrize("text", [
        "Error code: 429 - {'error': {'message': 'Rate limit reached'}}",
        "Request timed out.",
        "Connection reset by peer",
    ])
    def test_transients_are_not_billing(self, text):
        assert not is_billing_error(RuntimeError(text))


class _Owner:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


def _alert_duck(*, maintainer_id=None, fetched=None, owner=None):
    class _D:
        def __init__(self):
            self._billing_alert_last = {}
            self.maintainer_user_id = maintainer_id
            self.owner = owner if owner is not None else _Owner()

        def get_user(self, uid):
            return None

        async def fetch_user(self, uid):
            return fetched

        async def application_info(self):
            class _Info:
                pass
            info = _Info()
            info.owner = self.owner
            return info

    return _D()


class TestMaintainerAlert:
    def test_dms_the_app_owner_when_no_maintainer_configured(self):
        d = _alert_duck()
        assert _run(CompanionBot._maybe_billing_alert(d, "anthropic", RuntimeError(LIVE_ANTHROPIC))) is True
        assert len(d.owner.sent) == 1 and "Anthropic" in d.owner.sent[0]

    def test_configured_maintainer_wins_over_owner(self):
        target = _Owner()
        d = _alert_duck(maintainer_id=1234, fetched=target)
        _run(CompanionBot._maybe_billing_alert(d, "anthropic", RuntimeError(LIVE_ANTHROPIC)))
        assert len(target.sent) == 1 and d.owner.sent == []

    def test_one_dm_per_provider_per_cooldown(self):
        d = _alert_duck()
        first = _run(CompanionBot._maybe_billing_alert(d, "anthropic", RuntimeError("credit balance")))
        second = _run(CompanionBot._maybe_billing_alert(d, "claude-haiku-4-5", RuntimeError("credit balance")))
        assert (first, second) == (True, False)
        d._billing_alert_last["anthropic"] -= timedelta(hours=CONFIG.billing_alert_cooldown_hours + 1)
        assert _run(CompanionBot._maybe_billing_alert(d, "anthropic", RuntimeError("credit balance")))
        assert len(d.owner.sent) == 2

    def test_dm_failure_never_raises(self):
        class _Broken:
            async def send(self, text):
                raise AttributeError("no DM channel")
        d = _alert_duck(owner=_Broken())
        assert _run(CompanionBot._maybe_billing_alert(d, "anthropic", RuntimeError("credit balance"))) is False


class TestClassifierBillingPath:
    def test_live_anthropic_error_in_the_classifier_alerts(self):
        err = RuntimeError(LIVE_ANTHROPIC)
        duck = _make_duck(raise_exc=err)
        _run(CompanionBot._classify_distress(duck, UID, _FakeMsg()))
        assert duck.alerts == [("anthropic", err)]
        assert duck.semantic_pending[UID] is False
        assert duck.on_message_calls == []

    def test_transient_classifier_error_does_not_alert(self):
        duck = _make_duck(raise_exc=RuntimeError("Request timed out."))
        _run(CompanionBot._classify_distress(duck, UID, _FakeMsg()))
        assert duck.alerts == []


# ---------------------------------------------------------------------------
# 5. Age from birthdate
# ---------------------------------------------------------------------------

class TestAgeFromBirthdate:
    def test_whole_years(self):
        assert _age_from_birthdate("2001-02-27", today=date(2026, 9, 4)) == 25
        assert _age_from_birthdate("2001-02-27", today=date(2026, 2, 26)) == 24
        assert _age_from_birthdate("2001-02-27", today=date(2026, 2, 27)) == 25

    def test_garbage_is_none(self):
        assert _age_from_birthdate("mardi gras") is None
        assert _age_from_birthdate(None) is None


class _User:
    def __init__(self, uid=1, display_name="Someone"):
        self.id = uid
        self.display_name = display_name


class _CtxDuck:
    user_name_map = {}


class TestUserContextRendering:
    def test_birthdate_renders_a_computed_age(self, monkeypatch):
        monkeypatch.setattr(bot_module, "load_personal_memories", lambda uid: {
            "name": "Someone", "birthdate": "2001-02-27", "birthday_note": "born on Mardi Gras"})
        ctx = CompanionBot.get_user_context(_CtxDuck(), _User())
        expected = _age_from_birthdate("2001-02-27")
        assert f"Age: {expected} (born 2001-02-27; born on Mardi Gras)" in ctx

    def test_legacy_static_age_is_labelled_stale(self, monkeypatch):
        monkeypatch.setattr(bot_module, "load_personal_memories",
                            lambda uid: {"name": "X", "age": 24})
        assert "Age: 24 (static field" in CompanionBot.get_user_context(_CtxDuck(), _User())

    def test_current_situation_is_injected_with_its_date(self, monkeypatch):
        monkeypatch.setattr(bot_module, "load_personal_memories", lambda uid: {
            "name": "X",
            "current_situation": {"as_of": "2026-09-04", "notes": ["moving in November"]}})
        ctx = CompanionBot.get_user_context(_CtxDuck(), _User())
        assert "Current situation (as of 2026-09-04" in ctx
        assert "  - moving in November" in ctx
