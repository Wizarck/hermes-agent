"""Backend for the ``waba-routing`` dashboard plugin.

A thin proxy from the Hermes dashboard to the **waba-mcp admin REST API**
(``/admin/routing/*``). The dashboard UI calls these localhost routes; this
module adds the bearer token (kept server-side, never sent to the browser) and
forwards to waba-mcp. That keeps waba-mcp authoritative over its own routing
config — the dashboard is only a console.

Mounted at ``/api/plugins/waba-routing/`` by the Hermes dashboard.

Config via env (set in the dashboard process environment):

  WABA_ADMIN_URL        base URL of the waba-mcp server, e.g. http://127.0.0.1:9015
  WABA_ADMIN_API_TOKEN  bearer token; must match waba-mcp's WA_ADMIN_API_TOKEN
"""

from __future__ import annotations

import os
from typing import Any, Optional

try:  # the dashboard process ships httpx; degrade clearly if not
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

# Who the audit trail attributes changes to. The dashboard binds to localhost
# and its plugin routes bypass session auth, so we don't have a per-user
# identity here; "dashboard" is the actor recorded by waba-mcp.
_ACTOR = "dashboard"


def _config() -> tuple[str, str]:
    base = (os.environ.get("WABA_ADMIN_URL") or "").strip().rstrip("/")
    token = (os.environ.get("WABA_ADMIN_API_TOKEN") or "").strip()
    return base, token


async def _read_json(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def _call(
    method: str,
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
) -> JSONResponse:
    base, token = _config()
    if not base or not token:
        return JSONResponse(
            {"error": "waba-routing not configured: set WABA_ADMIN_URL and "
                      "WABA_ADMIN_API_TOKEN in the dashboard environment"},
            status_code=503,
        )
    if httpx is None:  # pragma: no cover
        return JSONResponse({"error": "httpx unavailable in the dashboard environment"}, status_code=500)
    headers = {"Authorization": f"Bearer {token}", "X-Admin-Actor": _ACTOR}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.request(method, f"{base}{path}", params=params, json=json_body, headers=headers)
    except Exception as e:  # waba-mcp down / network
        return JSONResponse({"error": f"waba-mcp unreachable: {e}"}, status_code=502)
    try:
        data = resp.json()
    except Exception:
        data = {"error": resp.text}
    return JSONResponse(data, status_code=resp.status_code)


@router.get("/health")
async def health() -> dict[str, Any]:
    base, token = _config()
    return {"configured": bool(base and token), "url": base or None}


@router.get("/roles")
async def roles() -> JSONResponse:
    return await _call("GET", "/admin/routing/roles")


@router.get("/lines")
async def list_lines() -> JSONResponse:
    return await _call("GET", "/admin/routing/lines")


@router.post("/lines")
async def upsert_line(request: Request) -> JSONResponse:
    return await _call("POST", "/admin/routing/lines", json_body=await _read_json(request))


@router.delete("/lines/{phone_number_id}")
async def delete_line(phone_number_id: str) -> JSONResponse:
    return await _call("DELETE", f"/admin/routing/lines/{phone_number_id}")


@router.get("/grants")
async def list_grants(phone: Optional[str] = None) -> JSONResponse:
    return await _call("GET", "/admin/routing/grants", params={"phone": phone} if phone else None)


@router.post("/grants")
async def upsert_grant(request: Request) -> JSONResponse:
    return await _call("POST", "/admin/routing/grants", json_body=await _read_json(request))


@router.delete("/grants")
async def delete_grant(phone: str, line_id: str = "") -> JSONResponse:
    return await _call("DELETE", "/admin/routing/grants", params={"phone": phone, "line_id": line_id})


@router.get("/audit")
async def audit(limit: int = 50) -> JSONResponse:
    return await _call("GET", "/admin/routing/audit", params={"limit": str(limit)})
