"""WS3: per-role Hindsight bank resolution.

The memory bank is re-resolved per operation from the task-local role
contextvar when the configured ``bank_id_template`` contains ``{role}``;
otherwise the init-resolved bank is returned unchanged (no behavioural change
for existing single-bank configs).
"""

import pytest

from gateway.session_context import (
    set_session_vars,
    clear_session_vars,
    get_session_env,
    _VAR_MAP,
    _UNSET,
)
from plugins.memory.hindsight import (
    HindsightMemoryProvider,
    _resolve_bank_id_template,
)


@pytest.fixture(autouse=True)
def _reset_contextvars():
    yield
    for var in _VAR_MAP.values():
        var.set(_UNSET)


def _provider(template="", bank="hermes"):
    p = HindsightMemoryProvider()
    p._bank_id_template = template
    p._bank_id = bank
    p._agent_identity = ""
    p._agent_workspace = ""
    p._platform = "whatsapp_via_mcp_meta_business_api"
    p._user_id = ""
    p._session_id = ""
    return p


# --- template resolver ------------------------------------------------------

def test_template_resolves_role():
    assert (
        _resolve_bank_id_template("hermes-{role}", fallback="hermes", role="trading")
        == "hermes-trading"
    )


def test_template_role_empty_collapses_to_fallback_shape():
    assert (
        _resolve_bank_id_template("hermes-{role}", fallback="hermes", role="")
        == "hermes"
    )


def test_template_sanitizes_role_segment():
    assert (
        _resolve_bank_id_template("hermes-{role}", fallback="hermes", role="a/b c")
        == "hermes-a-b-c"
    )


# --- _effective_bank_id -----------------------------------------------------

def test_fast_path_no_template():
    p = _provider(template="", bank="hermes")
    set_session_vars(role="trading")
    assert p._effective_bank_id() == "hermes"  # no template → init value unchanged


def test_fast_path_template_without_role_placeholder():
    p = _provider(template="hermes-{user}", bank="hermes-bob")
    set_session_vars(role="trading")
    assert p._effective_bank_id() == "hermes-bob"  # {role} absent → init value


def test_role_aware_resolves_per_role():
    p = _provider(template="hermes-{role}", bank="hermes")
    set_session_vars(role="trading")
    assert p._effective_bank_id() == "hermes-trading"
    set_session_vars(role="infra")
    assert p._effective_bank_id() == "hermes-infra"


def test_role_aware_no_role_uses_fallback_shape():
    p = _provider(template="hermes-{role}", bank="hermes")
    set_session_vars(role="")
    assert p._effective_bank_id() == "hermes"


def test_role_aware_unset_contextvar_falls_back(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_ROLE", raising=False)
    p = _provider(template="hermes-{role}", bank="hermes")
    # No set_session_vars call → contextvar _UNSET, no os env → role "".
    assert p._effective_bank_id() == "hermes"


# --- session_context role plumbing -----------------------------------------

def test_flush_on_switch_captures_role_bank_synchronously():
    """on_session_switch must flush the OLD session's turns to the role bank.

    The flush runs in the writer thread (no contextvar), so the bank has to be
    captured synchronously while the role contextvar is still live — otherwise
    it resolves the no-role fallback and the old session's memory lands in the
    wrong bank. Regression for the WS3 gap found in audit.
    """
    import threading

    p = _provider(template="hermes-{role}", bank="hermes")
    p._session_turns = ["turn-json"]
    p._session_id = "old-sess"
    p._parent_session_id = ""
    p._turn_index = 0
    p._document_id = "old-doc"
    p._turn_counter = 1
    p._retain_context = None
    p._retain_async = False
    p._prefetch_thread = None
    p._prefetch_lock = threading.Lock()
    p._prefetch_result = ""
    p._shutting_down = threading.Event()
    p._build_metadata = lambda **k: {}
    p._resolve_retain_target = lambda doc: (doc, None)
    p._build_retain_kwargs = lambda *a, **k: {"bank_id": "ignored", "retain_async": False}
    p._ensure_writer = lambda: None
    p._register_atexit = lambda: None

    class _ImmediateQueue:
        def put(self, fn):
            fn()   # run the flush closure now, in-thread

    captured = {}

    class _Recorder:
        def aretain_batch(self, **kw):
            captured["bank_id"] = kw.get("bank_id")
            return None

    p._retain_queue = _ImmediateQueue()
    p._run_hindsight_operation = lambda op: op(_Recorder())

    set_session_vars(role="trading")
    p.on_session_switch(new_session_id="new-sess")

    assert captured["bank_id"] == "hermes-trading"   # role bank, NOT the "hermes" fallback


def test_set_session_env_propagates_role():
    from gateway.config import Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionContext, SessionSource

    runner = object.__new__(GatewayRunner)
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="34600",
        chat_type="dm",
        user_id="34600",
        thread_id="trading",
        role="trading",
    )
    context = SessionContext(source=source, connected_platforms=[], home_channels={})
    tokens = runner._set_session_env(context)
    try:
        assert get_session_env("HERMES_SESSION_ROLE", "") == "trading"
    finally:
        clear_session_vars(tokens)
