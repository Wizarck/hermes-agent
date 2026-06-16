"""WS2 role-router unit tests for the WhatsApp-via-MCP platform.

Covers the role-resolution logic added in WS2: how the persona/role is read
from the forwarded payload (explicit field, tags, default), the allow-list
derivation, and that a resolved role produces a per-role session key.

Pure-logic tests — no aiohttp server is started.
"""

import asyncio
import json

import pytest

from gateway.config import PlatformConfig
from gateway.session import build_session_key
from gateway.platforms.whatsapp_via_mcp_meta_business_api import (
    WhatsAppViaMcpMetaBusinessApiAdapter,
)


def _adapter(extra=None):
    config = PlatformConfig(enabled=True, extra=extra or {})
    return WhatsAppViaMcpMetaBusinessApiAdapter(config)


ROLE_EXTRA = {
    "channel_prompts": {
        "trading": "You are in trading mode. Be precise.",
        "infra": "You are in infra mode.",
    },
    "channel_skill_bindings": [
        {"id": "trading", "skills": ["market-data"]},
        {"id": "palafito", "skills": ["catalog"]},
    ],
    "default_role": "personal",
    "roles": ["personal", "trading", "infra", "palafito"],
}


# --- allow-list -------------------------------------------------------------

def test_allowed_roles_from_explicit_list():
    assert _adapter(ROLE_EXTRA)._allowed_roles() == {
        "personal",
        "trading",
        "infra",
        "palafito",
    }


def test_allowed_roles_derived_from_prompts_and_bindings():
    a = _adapter(
        {
            "channel_prompts": {"trading": "x"},
            "channel_skill_bindings": [{"id": "infra", "skills": ["s"]}],
        }
    )
    assert a._allowed_roles() == {"trading", "infra"}


def test_allowed_roles_empty_when_unconfigured():
    assert _adapter({})._allowed_roles() == set()


# --- role resolution --------------------------------------------------------

def test_role_from_explicit_field():
    assert _adapter(ROLE_EXTRA)._resolve_role({"role": "trading"}) == "trading"


def test_role_from_tag_prefix():
    assert _adapter(ROLE_EXTRA)._resolve_role({"tags": ["foo", "role:infra"]}) == "infra"


def test_role_from_bare_known_tag():
    assert _adapter(ROLE_EXTRA)._resolve_role({"tags": ["palafito"]}) == "palafito"


def test_role_falls_back_to_default():
    assert _adapter(ROLE_EXTRA)._resolve_role({}) == "personal"


def test_unknown_role_is_rejected():
    assert _adapter(ROLE_EXTRA)._resolve_role({"role": "root"}) is None


def test_no_role_when_routing_unconfigured():
    assert _adapter({})._resolve_role({"role": "trading"}) is None


def test_explicit_field_wins_over_tags():
    a = _adapter(ROLE_EXTRA)
    assert a._resolve_role({"role": "trading", "tags": ["role:infra"]}) == "trading"


# --- tier -------------------------------------------------------------------

def test_tier_read_when_present():
    a = _adapter(ROLE_EXTRA)
    assert a._resolve_tier({"tier": "critical"}) == "critical"
    assert a._resolve_tier({}) is None


# --- per-role session key ---------------------------------------------------

def test_role_produces_per_role_session_key():
    a = _adapter(ROLE_EXTRA)
    common = dict(chat_id="34600", user_id="34600", chat_type="dm")
    key_trading = build_session_key(a.build_source(thread_id="trading", **common))
    key_infra = build_session_key(a.build_source(thread_id="infra", **common))
    key_none = build_session_key(a.build_source(**common))

    assert key_trading.endswith(":dm:34600:trading")
    assert key_infra.endswith(":dm:34600:infra")
    assert key_none.endswith(":dm:34600")
    assert key_trading != key_infra
    assert key_infra != key_none


# --- defense-in-depth dedup -------------------------------------------------

class _FakeReq:
    def __init__(self, body: bytes):
        self._body = body
        self.headers: dict = {}
        self.remote = "test"

    async def read(self) -> bytes:
        return self._body


@pytest.mark.asyncio
async def test_duplicate_webhook_skips_second_agent_run(monkeypatch):
    a = _adapter(ROLE_EXTRA)
    a._secret = ""  # disable secret check for the test

    calls = []

    async def fake_handle(event):
        calls.append(event.message_id)

    monkeypatch.setattr(a, "handle_message", fake_handle)
    body = json.dumps(
        {"message_id": "wamid.dup", "phone": "34600", "type": "text", "content": "hola"}
    ).encode()

    await a._handle_webhook(_FakeReq(body))
    await a._handle_webhook(_FakeReq(body))   # redelivery
    await asyncio.sleep(0.02)                 # let the create_task run

    assert calls == ["wamid.dup"]             # second delivery deduped
