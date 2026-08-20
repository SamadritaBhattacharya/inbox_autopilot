"""FastAPI application.

This layer adapts HTTP/WebSocket to services and does nothing else. No agent logic, no
LLM calls, no browser control lives here — those go through ports, wired in
`app.config.container`. A route that starts making decisions is a bug in the wrong file.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from inbox_contracts import PROTOCOL_VERSION

from app.config.settings import get_settings

app = FastAPI(
    title="Inbox Autopilot",
    version="0.1.0",
    description="Browser-driven email agent — the brain.",
)

# The cockpit is deployed separately (Vercel) and connects straight to this host: a
# serverless frontend platform cannot hold a long-lived WebSocket, so there is no proxy
# in front of us. That makes an explicit origin policy load-bearing rather than optional.
# TODO(M3): replace the permissive dev default with an allowlist from settings.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, object]:
    """Liveness plus the handshake facts a client needs before connecting.

    `protocolVersion` is here so a cockpit or executor built against a different contract
    version fails loudly at startup instead of mis-parsing frames later.

    Deliberately says nothing about credentials — not even whether they are present.
    """
    settings = get_settings()
    return {
        "status": "ok",
        "protocolVersion": PROTOCOL_VERSION,
        "emailSurface": settings.email_surface,
    }
