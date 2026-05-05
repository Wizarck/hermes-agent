"""Tests for the whatsapp_via_mcp_meta_business_api gateway adapter."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig


def _required_env(monkeypatch, **overrides):
    """Set the minimum env vars the adapter needs."""
    base = {
        "WHATSAPP_VIA_MCP_META_BUSINESS_API_TOKEN": "test-token",
        "WHATSAPP_VIA_MCP_META_BUSINESS_API_PHONE_NUMBER_ID": "1234567890",
        "WHATSAPP_VIA_MCP_META_BUSINESS_API_WEBHOOK_SECRET": "shh",
    }
    base.update(overrides)
    for k, v in base.items():
        monkeypatch.setenv(k, str(v))


def _make_adapter(monkeypatch, **extra):
    _required_env(monkeypatch)
    from gateway.platforms.whatsapp_via_mcp_meta_business_api import (
        WhatsAppViaMcpMetaBusinessApiAdapter,
    )

    cfg = PlatformConfig(
        enabled=True,
        token="test-token",
        extra={
            "phone_number_id": "1234567890",
            "webhook_secret": "shh",
            "host": "127.0.0.1",
            "port": 18643,
            "path": "/wa",
            **extra,
        },
    )
    return WhatsAppViaMcpMetaBusinessApiAdapter(cfg)


# --------------------------------------------------------------------- config


class TestConfigLoading:
    def test_apply_env_overrides_sets_platform(self, monkeypatch):
        _required_env(monkeypatch)
        from gateway.config import GatewayConfig, _apply_env_overrides

        config = GatewayConfig()
        _apply_env_overrides(config)
        assert Platform.WHATSAPP_VIA_MCP_META_BUSINESS_API in config.platforms
        pc = config.platforms[Platform.WHATSAPP_VIA_MCP_META_BUSINESS_API]
        assert pc.enabled is True
        assert pc.token == "test-token"
        assert pc.extra["phone_number_id"] == "1234567890"
        assert pc.extra["webhook_secret"] == "shh"

    def test_defaults_for_optional_extras(self, monkeypatch):
        _required_env(monkeypatch)
        from gateway.config import GatewayConfig, _apply_env_overrides

        config = GatewayConfig()
        _apply_env_overrides(config)
        pc = config.platforms[Platform.WHATSAPP_VIA_MCP_META_BUSINESS_API]
        assert pc.extra["host"] == "0.0.0.0"
        assert pc.extra["port"] == 8643
        assert pc.extra["path"] == "/wa"

    def test_home_channel_set_from_env(self, monkeypatch):
        _required_env(monkeypatch)
        monkeypatch.setenv("WHATSAPP_VIA_MCP_META_BUSINESS_API_HOME_CHANNEL", "+34611111111")
        from gateway.config import GatewayConfig, _apply_env_overrides

        config = GatewayConfig()
        _apply_env_overrides(config)
        hc = config.platforms[Platform.WHATSAPP_VIA_MCP_META_BUSINESS_API].home_channel
        assert hc is not None
        assert hc.chat_id == "+34611111111"

    def test_not_added_without_token(self, monkeypatch):
        monkeypatch.delenv("WHATSAPP_VIA_MCP_META_BUSINESS_API_TOKEN", raising=False)
        monkeypatch.setenv("WHATSAPP_VIA_MCP_META_BUSINESS_API_PHONE_NUMBER_ID", "x")
        from gateway.config import GatewayConfig, _apply_env_overrides

        config = GatewayConfig()
        _apply_env_overrides(config)
        assert Platform.WHATSAPP_VIA_MCP_META_BUSINESS_API not in config.platforms

    def test_not_added_without_phone_id(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_VIA_MCP_META_BUSINESS_API_TOKEN", "t")
        monkeypatch.delenv("WHATSAPP_VIA_MCP_META_BUSINESS_API_PHONE_NUMBER_ID", raising=False)
        from gateway.config import GatewayConfig, _apply_env_overrides

        config = GatewayConfig()
        _apply_env_overrides(config)
        assert Platform.WHATSAPP_VIA_MCP_META_BUSINESS_API not in config.platforms


# --------------------------------------------------------------------- helpers


class TestHelpers:
    def test_check_requirements(self):
        from gateway.platforms.whatsapp_via_mcp_meta_business_api import (
            check_whatsapp_via_mcp_meta_business_api_requirements,
        )

        # aiohttp + httpx are core deps; should pass in test env.
        assert check_whatsapp_via_mcp_meta_business_api_requirements() is True

    def test_redact_phone_short(self):
        from gateway.platforms.whatsapp_via_mcp_meta_business_api import _redact_phone

        assert _redact_phone("123") == "***"

    def test_redact_phone_long(self):
        from gateway.platforms.whatsapp_via_mcp_meta_business_api import _redact_phone

        assert _redact_phone("34612345678") == "346***78"

    def test_redact_phone_empty(self):
        from gateway.platforms.whatsapp_via_mcp_meta_business_api import _redact_phone

        assert _redact_phone("") == "***"


# --------------------------------------------------------------------- adapter


class TestAdapterInit:
    def test_init_reads_extra(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, port=12345, path="/custom")
        assert adapter._token == "test-token"
        assert adapter._phone_number_id == "1234567890"
        assert adapter._secret == "shh"
        assert adapter._port == 12345
        assert adapter._path == "/custom"

    def test_init_falls_back_to_env_for_phone_id(self, monkeypatch):
        _required_env(monkeypatch)
        # Build with extra missing phone_number_id; constructor should use env.
        from gateway.platforms.whatsapp_via_mcp_meta_business_api import (
            WhatsAppViaMcpMetaBusinessApiAdapter,
        )

        cfg = PlatformConfig(enabled=True, token="t2", extra={"webhook_secret": "shh"})
        adapter = WhatsAppViaMcpMetaBusinessApiAdapter(cfg)
        assert adapter._phone_number_id == "1234567890"


# --------------------------------------------------------------------- webhook handler


def _fake_request(body: bytes, secret_header: str = "shh", remote: str = "1.2.3.4"):
    """Minimal stub that mimics aiohttp.web.Request for handler tests."""
    req = MagicMock()
    req.read = AsyncMock(return_value=body)
    req.headers = {"X-Webhook-Secret": secret_header} if secret_header else {}
    req.remote = remote
    return req


class TestWebhookHandler:
    @pytest.mark.asyncio
    async def test_rejects_wrong_secret(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        req = _fake_request(b"{}", secret_header="WRONG")
        resp = await adapter._handle_webhook(req)
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_rejects_invalid_json(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        req = _fake_request(b"not json", secret_header="shh")
        resp = await adapter._handle_webhook(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rejects_missing_phone(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        body = json.dumps({"content": "hi"}).encode()
        req = _fake_request(body, secret_header="shh")
        resp = await adapter._handle_webhook(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_skips_non_text(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        body = json.dumps({"phone": "+34611", "type": "image", "content": "x"}).encode()
        req = _fake_request(body, secret_header="shh")
        resp = await adapter._handle_webhook(req)
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_skips_empty_text(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        body = json.dumps({"phone": "+34611", "type": "text", "content": ""}).encode()
        req = _fake_request(body, secret_header="shh")
        resp = await adapter._handle_webhook(req)
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_queues_text_message(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        # handle_message is what dispatches into the gateway; stub it.
        adapter.handle_message = AsyncMock()
        body = json.dumps(
            {
                "message_id": "wamid.X",
                "phone": "+34611111111",
                "type": "text",
                "content": "hola",
            }
        ).encode()
        req = _fake_request(body, secret_header="shh")
        resp = await adapter._handle_webhook(req)
        assert resp.status == 200
        # The handler creates a background task; give the loop a tick to run it.
        import asyncio

        await asyncio.sleep(0)
        adapter.handle_message.assert_called_once()
        event = adapter.handle_message.call_args.args[0]
        assert event.text == "hola"
        assert event.source.chat_id == "+34611111111"

    @pytest.mark.asyncio
    async def test_no_secret_skips_validation(self, monkeypatch):
        _required_env(monkeypatch, WHATSAPP_VIA_MCP_META_BUSINESS_API_WEBHOOK_SECRET="")
        from gateway.platforms.whatsapp_via_mcp_meta_business_api import (
            WhatsAppViaMcpMetaBusinessApiAdapter,
        )

        cfg = PlatformConfig(
            enabled=True,
            token="test-token",
            extra={"phone_number_id": "1234567890", "webhook_secret": ""},
        )
        adapter = WhatsAppViaMcpMetaBusinessApiAdapter(cfg)
        adapter.handle_message = AsyncMock()
        body = json.dumps(
            {"message_id": "x", "phone": "+34611", "type": "text", "content": "ok"}
        ).encode()
        req = _fake_request(body, secret_header="")
        resp = await adapter._handle_webhook(req)
        assert resp.status == 200


# --------------------------------------------------------------------- send


class _FakeResponse:
    def __init__(self, status: int, json_body: dict, text: str = ""):
        self.status_code = status
        self._json = json_body
        self.text = text or json.dumps(json_body)

    def json(self):
        return self._json


class TestSend:
    @pytest.mark.asyncio
    async def test_send_calls_meta_graph(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter._http_client = MagicMock()
        adapter._http_client.post = AsyncMock(
            return_value=_FakeResponse(
                200, {"messages": [{"id": "wamid.OUT"}]}
            )
        )
        result = await adapter.send("+34611111111", "hola desde Hermes")
        assert result.success is True
        assert result.message_id == "wamid.OUT"
        # Validate the call shape
        call = adapter._http_client.post.await_args
        assert call.args[0].endswith("/1234567890/messages")
        body = call.kwargs["json"]
        assert body["to"] == "+34611111111"
        assert body["type"] == "text"
        assert body["text"]["body"] == "hola desde Hermes"
        assert body["messaging_product"] == "whatsapp"

    @pytest.mark.asyncio
    async def test_send_chunks_long_text(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter._http_client = MagicMock()
        # All chunks succeed
        adapter._http_client.post = AsyncMock(
            return_value=_FakeResponse(200, {"messages": [{"id": "wamid"}]})
        )
        from gateway.platforms.whatsapp_via_mcp_meta_business_api import MAX_MESSAGE_LENGTH

        long_text = "a" * (MAX_MESSAGE_LENGTH * 2 + 5)
        result = await adapter.send("+34611", long_text)
        assert result.success is True
        # 2 full chunks + 1 small remainder = 3 calls
        assert adapter._http_client.post.await_count == 3

    @pytest.mark.asyncio
    async def test_send_returns_error_on_http_failure(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter._http_client = MagicMock()
        adapter._http_client.post = AsyncMock(
            return_value=_FakeResponse(401, {"error": "auth"}, text='{"error":"auth"}')
        )
        result = await adapter.send("+34611", "hi")
        assert result.success is False
        assert "401" in (result.error or "")

    @pytest.mark.asyncio
    async def test_send_image_link(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter._http_client = MagicMock()
        adapter._http_client.post = AsyncMock(
            return_value=_FakeResponse(200, {"messages": [{"id": "wamid.IMG"}]})
        )
        result = await adapter.send_image("+34611", "https://example.com/x.png", caption="cap")
        assert result.success is True
        body = adapter._http_client.post.await_args.kwargs["json"]
        assert body["type"] == "image"
        assert body["image"]["link"] == "https://example.com/x.png"
        assert body["image"]["caption"] == "cap"


# --------------------------------------------------------------------- session source


class TestSessionSource:
    def test_chat_id_is_phone(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        source = adapter.build_source(
            chat_id="+34611111111", user_id="+34611111111", chat_type="dm"
        )
        assert source.chat_id == "+34611111111"
        assert source.platform == Platform.WHATSAPP_VIA_MCP_META_BUSINESS_API
        assert source.chat_type == "dm"


# --------------------------------------------------------------------- auth integration


class TestAuthIntegration:
    def test_platform_in_allowlist_maps(self):
        # Light smoke: the env var name we use must be referenced in run.py's
        # platform_env_map / platform_allow_all_map. We import the module and
        # assert the mappings exist.
        import gateway.run as run_mod

        # Build a dummy GatewayRunner just to access the maps via _is_user_authorized.
        # We grep the source instead to avoid heavy construction:
        src = run_mod.__file__
        with open(src, "r", encoding="utf-8") as f:
            text = f.read()
        assert "WHATSAPP_VIA_MCP_META_BUSINESS_API_ALLOWED_USERS" in text
        assert "WHATSAPP_VIA_MCP_META_BUSINESS_API_ALLOW_ALL_USERS" in text


# --------------------------------------------------------------------- send_message_tool integration


class TestSendMessageTool:
    def test_platform_map_includes_wamba(self):
        import tools.send_message_tool as smt

        with open(smt.__file__, "r", encoding="utf-8") as f:
            text = f.read()
        assert '"whatsapp_via_mcp_meta_business_api": Platform.WHATSAPP_VIA_MCP_META_BUSINESS_API' in text
        assert "_send_whatsapp_via_mcp_meta_business_api" in text
