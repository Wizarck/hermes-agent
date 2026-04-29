"""WhatsApp via external MCP (Meta Business Cloud API).

Receives webhook POSTs forwarded by an external WhatsApp MCP server that owns
the Meta Cloud API credentials (access token, phone number ID). When a message
arrives, the platform spawns a Hermes session keyed by sender phone, processes
the message with full agent capabilities (memory, skills, MCPs), and sends the
reply back via Meta Graph API directly.

Why this exists: the upstream ``whatsapp.py`` platform uses the Baileys Node
bridge (reverse-engineered WhatsApp Web), which violates Meta's terms of
service and is fragile on Windows. Users who run a phone number on the official
Meta WhatsApp Business Cloud API have no first-class platform in Hermes today.
This module fills that gap by delegating Meta-side concerns (webhook ingress,
outbound delivery, sender tagging, message persistence) to an external MCP
server, while keeping agent reasoning inside Hermes.

Architecture::

    Meta Cloud
        v webhook
    [external WA-MCP server]
        v POST {message_id, phone, type, content, tags, raw}
    [this platform]                    -> handle_message(event)
        ^                                       v
        | reply via Meta Graph /messages   <-  send()

The forward POST is validated against
``WHATSAPP_VIA_MCP_META_BUSINESS_API_WEBHOOK_SECRET`` via the
``X-Webhook-Secret`` header (constant-time comparison). The platform returns
``200 {"ok": true, "queued": true}`` immediately so the MCP's typical
short forward timeout (Meta itself requires <5s on the original webhook) is
not exceeded by LLM latency; the actual reply is sent asynchronously by
``send()`` once Hermes finishes processing.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from aiohttp import web

    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

try:
    import httpx

    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

logger = logging.getLogger(__name__)

META_GRAPH_BASE_URL = "https://graph.facebook.com/v21.0"
MAX_MESSAGE_LENGTH = 4096  # WhatsApp text body limit per Meta docs


def check_whatsapp_via_mcp_meta_business_api_requirements() -> bool:
    """Return True if the platform's runtime dependencies are available."""
    return AIOHTTP_AVAILABLE and HTTPX_AVAILABLE


def _redact_phone(phone: str) -> str:
    """Mask a phone number for log output (keep first 3 and last 2 digits)."""
    if not phone or len(phone) < 6:
        return "***"
    return f"{phone[:3]}***{phone[-2:]}"


class WhatsAppViaMcpMetaBusinessApiAdapter(BasePlatformAdapter):
    """Receive forwarded WhatsApp webhooks from an external MCP, reply via Meta Cloud API.

    Configuration (via :class:`PlatformConfig`):

    * ``token`` (required): Meta Cloud API access token (System User token
      with ``whatsapp_business_messaging`` permission). Loaded from
      ``WHATSAPP_VIA_MCP_META_BUSINESS_API_TOKEN`` env var by
      ``_apply_env_overrides``.
    * ``extra.phone_number_id`` (required): the phone number id used as
      ``{phone-number-id}`` in the Meta Graph API path. Env:
      ``WHATSAPP_VIA_MCP_META_BUSINESS_API_PHONE_NUMBER_ID``.
    * ``extra.webhook_secret`` (recommended): shared secret with the WA-MCP;
      validated as the ``X-Webhook-Secret`` request header on every forward.
      Env: ``WHATSAPP_VIA_MCP_META_BUSINESS_API_WEBHOOK_SECRET``. If unset,
      forwards are accepted unauthenticated and a warning is emitted at startup.
    * ``extra.host`` (default ``0.0.0.0``): aiohttp bind host.
    * ``extra.port`` (default ``8643``): aiohttp bind port.
    * ``extra.path`` (default ``/wa``): URL path for the forward endpoint.
    * ``extra.meta_base_url`` (default ``https://graph.facebook.com/v21.0``):
      override for testing.
    """

    PLATFORM_NAME = "whatsapp_via_mcp_meta_business_api"

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.WHATSAPP_VIA_MCP_META_BUSINESS_API)
        self._token: str = config.token or ""
        extra = config.extra or {}
        self._phone_number_id: str = extra.get("phone_number_id") or os.getenv(
            "WHATSAPP_VIA_MCP_META_BUSINESS_API_PHONE_NUMBER_ID", ""
        )
        self._secret: str = extra.get("webhook_secret") or os.getenv(
            "WHATSAPP_VIA_MCP_META_BUSINESS_API_WEBHOOK_SECRET", ""
        )
        self._host: str = extra.get("host") or os.getenv(
            "WHATSAPP_VIA_MCP_META_BUSINESS_API_HOST", "0.0.0.0"
        )
        self._port: int = int(
            extra.get("port") or os.getenv("WHATSAPP_VIA_MCP_META_BUSINESS_API_PORT", "8643")
        )
        self._path: str = extra.get("path") or os.getenv(
            "WHATSAPP_VIA_MCP_META_BUSINESS_API_PATH", "/wa"
        )
        self._meta_base_url: str = (
            extra.get("meta_base_url")
            or os.getenv("WHATSAPP_VIA_MCP_META_BUSINESS_API_META_BASE_URL")
            or META_GRAPH_BASE_URL
        )
        self._app: Optional["web.Application"] = None
        self._runner: Optional["web.AppRunner"] = None
        self._site: Optional["web.TCPSite"] = None
        self._http_client: Optional["httpx.AsyncClient"] = None

    # ------------------------------------------------------------------ lifecycle

    async def connect(self) -> bool:
        if not self._token:
            logger.error(
                "WhatsApp via MCP: no token configured (WHATSAPP_VIA_MCP_META_BUSINESS_API_TOKEN)"
            )
            return False
        if not self._phone_number_id:
            logger.error(
                "WhatsApp via MCP: no phone_number_id configured "
                "(WHATSAPP_VIA_MCP_META_BUSINESS_API_PHONE_NUMBER_ID)"
            )
            return False
        if not self._secret:
            logger.warning(
                "WhatsApp via MCP: no webhook secret configured — forwards will be accepted "
                "without authentication. Set WHATSAPP_VIA_MCP_META_BUSINESS_API_WEBHOOK_SECRET."
            )

        self._http_client = httpx.AsyncClient(timeout=30)

        self._app = web.Application()
        self._app.router.add_get("/health", self._handle_health)
        self._app.router.add_post(self._path, self._handle_webhook)
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        logger.info(
            "WhatsApp via MCP listening on %s:%s%s (phone_id=%s)",
            self._host,
            self._port,
            self._path,
            self._phone_number_id,
        )
        return True

    async def disconnect(self) -> None:
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
        logger.info("WhatsApp via MCP disconnected")

    # ------------------------------------------------------------------ inbound

    async def _handle_health(self, _request: "web.Request") -> "web.Response":
        return web.json_response(
            {
                "ok": True,
                "platform": self.PLATFORM_NAME,
                "phone_number_id": self._phone_number_id,
            }
        )

    async def _handle_webhook(self, request: "web.Request") -> "web.Response":
        body = await request.read()
        if self._secret:
            provided = request.headers.get("X-Webhook-Secret", "")
            if not hmac.compare_digest(provided, self._secret):
                logger.warning(
                    "WhatsApp via MCP: invalid webhook secret from %s", request.remote
                )
                return web.json_response({"error": "invalid secret"}, status=403)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid json"}, status=400)

        # The wa-mcp router posts:
        #   {message_id, phone, type, content, tags, raw}
        phone = (payload.get("phone") or payload.get("from") or "").strip()
        content = (payload.get("content") or payload.get("text") or "").strip()
        message_id = payload.get("message_id") or ""
        msg_type = payload.get("type") or "text"

        if not phone:
            return web.json_response({"error": "missing phone"}, status=400)
        if msg_type != "text":
            logger.info(
                "WhatsApp via MCP: skipping non-text message type=%s phone=%s msg=%s",
                msg_type,
                _redact_phone(phone),
                message_id,
            )
            return web.json_response({"ok": True, "skipped": "non_text"})
        if not content:
            return web.json_response({"ok": True, "skipped": "empty"})

        # ELIGIA HITL pre-check: if this looks like an "aprobar X" / "rechazar X"
        # reply from a bound identity, write the resolution and ack — skip the
        # LLM (per .ai-playbook/specs/apply-fix-contract.md §Identity binding).
        hitl_match = self._try_parse_hitl_intent(phone, content)
        if hitl_match is not None:
            written = self._write_hitl_resolution(hitl_match)
            if written:
                ack = (
                    f"✅ Aprobado: {hitl_match['request_id']}"
                    if hitl_match["approved"]
                    else f"❌ Rechazado: {hitl_match['request_id']}"
                )
                asyncio.create_task(self.send(chat_id=phone, content=ack))
                logger.info(
                    "WhatsApp HITL resolved request_id=%s approved=%s phone=%s",
                    hitl_match["request_id"],
                    hitl_match["approved"],
                    _redact_phone(phone),
                )
                return web.json_response({"ok": True, "hitl": True})
            # Write failed — fall through to normal LLM handling so the user
            # sees Hermes' generic response rather than a silent drop.
            logger.error(
                "WhatsApp HITL resolution write failed; falling through to LLM. "
                "phone=%s request_id=%s",
                _redact_phone(phone),
                hitl_match.get("request_id"),
            )

        source = self.build_source(
            chat_id=phone,
            user_id=phone,
            chat_type="dm",
        )
        event = MessageEvent(
            source=source,
            message_type=MessageType.TEXT,
            text=content,
            message_id=message_id,
        )
        # Process asynchronously: the WA-MCP applies a short timeout to the
        # forward POST (Meta requires the original webhook handler to reply
        # in <5 s, and the MCP cascades that constraint). We acknowledge
        # immediately and let send() deliver the reply when Hermes finishes.
        asyncio.create_task(self.handle_message(event))
        logger.info(
            "WhatsApp via MCP queued msg=%s phone=%s len=%d",
            message_id,
            _redact_phone(phone),
            len(content),
        )
        return web.json_response({"ok": True, "queued": True})

    # ------------------------------------------------------------------ outbound

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a text message via Meta Graph API.

        Long messages are split into ``MAX_MESSAGE_LENGTH``-sized chunks and
        sent sequentially.
        """
        if self._http_client is None:
            return SendResult(success=False, error="adapter not connected")

        chunks = (
            [content[i : i + MAX_MESSAGE_LENGTH] for i in range(0, len(content), MAX_MESSAGE_LENGTH)]
            or [""]
        )
        last_id = ""
        for chunk in chunks:
            result = await self._post_meta(
                {
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": chat_id,
                    "type": "text",
                    "text": {"body": chunk},
                }
            )
            if not result.success:
                return result
            last_id = result.message_id or last_id
        return SendResult(success=True, message_id=last_id)

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        # Meta Cloud API does not expose a typing indicator at the message level.
        # Accept the metadata kwarg to stay compatible with BasePlatformAdapter._keep_typing.
        return None

    async def stop_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        return None

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> SendResult:
        """Send an image by public URL.

        Meta accepts either a public URL (``image.link``) or a media id from
        a previous upload to ``/{phone-number-id}/media``. This implementation
        uses ``link`` for simplicity; uploaded media flow can be added later.
        """
        if self._http_client is None:
            return SendResult(success=False, error="adapter not connected")
        payload: Dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": chat_id,
            "type": "image",
            "image": {"link": image_url},
        }
        if caption:
            payload["image"]["caption"] = caption[:1024]
        return await self._post_meta(payload)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        # WhatsApp chats are 1-to-1 keyed by phone; Meta's API does not expose
        # the profile display name without prior message metadata.
        return {"name": chat_id, "type": "dm", "chat_id": chat_id}

    # ------------------------------------------------------------------ internals

    async def _post_meta(self, payload: Dict[str, Any]) -> SendResult:
        assert self._http_client is not None
        url = f"{self._meta_base_url}/{self._phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        try:
            r = await self._http_client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as e:
            logger.error(
                "Meta send failed to %s: %s",
                _redact_phone(payload.get("to", "")),
                e,
            )
            return SendResult(success=False, error=str(e))
        if r.status_code != 200:
            logger.error(
                "Meta send %d to %s: %s",
                r.status_code,
                _redact_phone(payload.get("to", "")),
                r.text[:300],
            )
            return SendResult(
                success=False, error=f"http_{r.status_code}: {r.text[:200]}"
            )
        try:
            mid = r.json().get("messages", [{}])[0].get("id", "") or ""
        except Exception:
            mid = ""
        return SendResult(success=True, message_id=mid)

    # ------------------------------------------------------------------ ELIGIA HITL

    # Approval verbs (case-insensitive). Includes Spanish stems and a couple of
    # common English fallbacks. The captured group is the request_id.
    _HITL_APPROVE_RE = re.compile(
        r"^\s*(?:aprob\w*|aprueb\w*|✅|ok\b|si\b|sí\b|dale\b|yes\b|y\b)\s+(?:el\s+)?(\S+?)\s*$",
        re.IGNORECASE,
    )
    _HITL_REJECT_RE = re.compile(
        r"^\s*(?:rechaz\w*|recha\w*|❌|no\b|nope\b|n\b)\s+(?:el\s+)?(\S+?)\s*$",
        re.IGNORECASE,
    )

    # Bare-token shortcuts (no request_id). When matched, the handler resolves
    # against the MOST RECENT OPEN envelope from approvals-pending.jsonl
    # (within the 24 h staleness cutoff). No-match-on-bare implies no open
    # request → fall through to the LLM so plain "1"/"no" stay conversational.
    _HITL_BARE_APPROVE_RE = re.compile(
        r"^\s*(?:aprob\w*|aprueb\w*|✅|ok|si|sí|dale|yes|y|1|👍)\s*$",
        re.IGNORECASE,
    )
    # Reject side uses "2" (not "0") to match human IVR-list convention:
    # "1) approve / 2) reject" is the cognitive default. The envelope body
    # in channels/whatsapp.py renders the numbered list explicitly.
    _HITL_BARE_REJECT_RE = re.compile(
        r"^\s*(?:rechaz\w*|recha\w*|❌|no|nope|n|2|👎)\s*$",
        re.IGNORECASE,
    )

    # Pending entries older than this are skipped when resolving bare tokens
    # (matches the global 24 h timeout in apply-fix-contract.md §Envelope).
    _HITL_PENDING_STALENESS_HOURS = 24

    def _try_parse_hitl_intent(self, phone: str, content: str) -> Optional[Dict[str, Any]]:
        """Detect HITL approve/reject intent in a WhatsApp message.

        Returns ``None`` if the message is not an HITL response (so the
        caller falls through to the LLM). Otherwise returns a dict with
        ``request_id``, ``approved``, ``signer``.

        Identity binding: only senders whose phone is in
        ``WA_HITL_ARTURO_E164`` (env, comma-separated for future
        multi-signer; v1 is single-signer) get their replies recognised
        as HITL. Other senders are ignored — Hermes' regular LLM picks
        them up. The audit-log of identity rejections is intentionally
        skipped here because non-bound senders are not "trying" to
        approve; they're just chatting normally.
        """
        # Identity binding (HITL-specific list, distinct from
        # WHATSAPP_VIA_MCP_META_BUSINESS_API_ALLOWED_USERS which gates
        # conversational access).
        bound_raw = os.environ.get("WA_HITL_ARTURO_E164", "")
        bound = {p.strip() for p in bound_raw.split(",") if p.strip()}
        if not bound or phone not in bound:
            return None

        approved: Optional[bool] = None
        request_id: Optional[str] = None

        # 1. Try explicit "verb + id" patterns first.
        m = self._HITL_APPROVE_RE.match(content)
        if m:
            approved = True
            request_id = m.group(1)
        else:
            m = self._HITL_REJECT_RE.match(content)
            if m:
                approved = False
                request_id = m.group(1)

        # 2. Bare-token shortcut (no id). Look up the most recent open request.
        #    Only applies when the explicit form did NOT match — guards against
        #    false positives like "ok abc-123" matching both forms.
        if approved is None:
            if self._HITL_BARE_APPROVE_RE.match(content):
                request_id = self._find_most_recent_open_request_id()
                if request_id:
                    approved = True
            elif self._HITL_BARE_REJECT_RE.match(content):
                request_id = self._find_most_recent_open_request_id()
                if request_id:
                    approved = False

        if approved is None or not request_id:
            return None

        return {
            "request_id": request_id,
            "approved": approved,
            "signer": f"whatsapp:{phone}",
        }

    def _find_most_recent_open_request_id(self) -> Optional[str]:
        """Return the latest unresolved request_id within the staleness window.

        Reads ``$ELIGIA_HITL_DIR/approvals-pending.jsonl`` and skips any entry
        whose ``request_id`` already appears in ``approvals-resolved.jsonl``.
        Returns None when:
          - the pending file does not exist,
          - it is empty,
          - all entries are resolved already, or
          - all entries are older than _HITL_PENDING_STALENESS_HOURS.

        Used by ``_try_parse_hitl_intent`` to handle bare-token shortcuts
        like "1", "ok", "no".
        """
        hitl_dir = Path(os.environ.get("ELIGIA_HITL_DIR", "/opt/eligia/data/hitl"))
        pending = hitl_dir / "approvals-pending.jsonl"
        resolved = hitl_dir / "approvals-resolved.jsonl"

        if not pending.is_file():
            return None

        # Build the set of already-resolved ids.
        resolved_ids: set[str] = set()
        if resolved.is_file():
            try:
                with resolved.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        rid = entry.get("request_id")
                        if rid:
                            resolved_ids.add(rid)
            except OSError:
                # If resolved is unreadable, treat as no-resolutions; we may
                # double-resolve a request, but the workflow polling layer
                # already deduplicates by request_id.
                resolved_ids = set()

        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(hours=self._HITL_PENDING_STALENESS_HOURS)
        )

        try:
            with pending.open("r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return None

        # Walk the pending file in reverse to find the most recent open
        # (non-resolved, non-stale) entry.
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = entry.get("request_id")
            if not rid or rid in resolved_ids:
                continue
            ts_str = entry.get("ts", "")
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                # Missing/malformed ts → treat as stale (skip).
                continue
            if ts < cutoff:
                continue
            return rid

        return None

    def _write_hitl_resolution(self, match: Dict[str, Any]) -> bool:
        """Append a resolution line to ``$ELIGIA_HITL_DIR/approvals-resolved.jsonl``.

        Returns True on success, False on filesystem error. Called by
        ``_handle_webhook`` after a successful HITL intent parse + identity
        bind.
        """
        hitl_dir = Path(os.environ.get("ELIGIA_HITL_DIR", "/opt/eligia/data/hitl"))
        try:
            hitl_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error(
                "WhatsApp HITL: mkdir failed at %s: %s", hitl_dir, exc
            )
            return False

        resolution = {
            "request_id": match["request_id"],
            "approved": bool(match["approved"]),
            "signer": match["signer"],
            "channel": "whatsapp",
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        resolved_path = hitl_dir / "approvals-resolved.jsonl"
        try:
            with resolved_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(resolution, ensure_ascii=False) + "\n")
            return True
        except OSError as exc:
            logger.error(
                "WhatsApp HITL: resolution write failed at %s: %s",
                resolved_path,
                exc,
            )
            return False
