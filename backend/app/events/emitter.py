"""`EventEmitter` — one typed method per event.

A thin layer over the sink, and worth its existence for two reasons: every emit site names
what it is emitting (so a typo is a missing method rather than a silently unrendered
event), and redaction happens in exactly one place instead of at forty call sites.

The redaction pass here is **defence in depth**, not the primary control. Everything the
graph holds is already tokenized by the funnel; this catches the case where a raw value
reached the graph through some path nobody anticipated. Belt and braces, on the last hop
before the wire.
"""
from __future__ import annotations

from typing import Any

from app.events import protocol
from app.events.protocol import AgentEvent
from app.events.sink import EventSink
from app.security.redaction import scrub
from app.telemetry.records import Usage


def _clean(value: Any) -> Any:
    """Scrub strings recursively on the way out."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


#: Events whose node may re-execute on resume. LangGraph re-runs everything BEFORE an
#: `interrupt()` when a run is resumed, so a card emitted there fires a second time — the
#: cockpit would flash a duplicate approval or options card for a decision already made.
_REPLAYABLE = frozenset({protocol.APPROVAL_REQUEST, protocol.OPTIONS, protocol.QUESTION})


class EventEmitter:
    def __init__(self, sink: EventSink) -> None:
        self._sink = sink
        self._last: tuple[str, str] | None = None

    async def _emit(self, event: str, data: dict[str, Any], *, redact: bool = True) -> None:
        payload = _clean(data) if redact else data

        # Drop a repeat of the same pending decision. Keyed on the REQUEST ID rather than
        # the whole payload: a replay recomputes volatile fields like `expiresAt`, so a
        # payload comparison would never match and every resume would flash a fresh card.
        # This is why the nodes derive their request ids deterministically from state.
        if event in _REPLAYABLE:
            signature = (event, str(payload.get("requestId", "")))
            if signature == self._last:
                return
            self._last = signature

        await self._sink.emit(AgentEvent(event=event, data=payload))

    # ── lifecycle ───────────────────────────────────────────────────────────

    async def status(self, phase: str, message: str = "") -> None:
        await self._emit(protocol.STATUS, {"phase": phase, "message": message})

    async def intent(self, action: str, slots: dict[str, str], confidence: float) -> None:
        await self._emit(
            protocol.INTENT, {"action": action, "slots": slots, "confidence": round(confidence, 2)}
        )

    async def route(self, topology: str, why: str, rule_matched: bool) -> None:
        await self._emit(
            protocol.ROUTE, {"route": topology, "why": why, "ruleMatched": rule_matched}
        )

    async def plan(self, steps: list[str]) -> None:
        await self._emit(protocol.PLAN_UPDATE, {"steps": steps})

    async def provider(self, *, provider: str, status: str, detail: str) -> None:
        """A model provider changed state — rate-limited, benched, or recovered.

        Surfaced rather than logged because the user experiences a 429 as the agent going
        quiet, and "the free tier is exhausted until tomorrow" is something only they can
        act on. Buried in the server log it looks like the agent is broken.
        """
        await self._emit(
            protocol.PROVIDER, {"provider": provider, "status": status, "detail": detail}
        )

    async def draft(self, subject: str, body: str, tone: str) -> None:
        """The composed message, before the browser is touched.

        Shown early on purpose: a wrong tone is cheap to fix here and expensive to fix at
        the approval card, where a compose window is already filled in.
        """
        await self._emit(protocol.DRAFT, {"subject": subject, "body": body, "tone": tone})

    async def location(self, url: str, title: str = "") -> None:
        """Where the browser is, for the human watching.

        **Deliberately not part of `Observation`.** A URL is a raw identifier — it carries
        thread ids and account hints — which is why the contract has `context_id` instead
        and the funnel tokenizes it. The model must never see this. The cockpit is
        authenticated and already shows resolved recipients on approval cards, so showing
        the human where their own browser is breaks nothing; routing it through the
        observation would.
        """
        await self._emit(protocol.LOCATION, {"url": url, "title": title})

    async def finalize(self, success: bool, reason: str, error_code: str | None = None) -> None:
        await self._emit(
            protocol.FINALIZE, {"success": success, "reason": reason, "errorCode": error_code}
        )

    # ── the loop ────────────────────────────────────────────────────────────

    async def reasoning(self, text: str) -> None:
        await self._emit(protocol.REASONING, {"text": text})

    async def assessment(self, text: str, outcome: str) -> None:
        """The agent's verdict on its own last action, plus what actually happened.

        A separate event from `reasoning` so the cockpit can show "it noticed the click did
        nothing" differently from "it is about to click" — and so a user can see the agent
        catching its own mistake, which is most of what makes an agent feel trustworthy.
        """
        await self._emit(protocol.ASSESSMENT, {"text": text, "outcome": outcome})

    async def feedback_ack(self, text: str, accepted: bool = True) -> None:
        """Confirm a human correction reached the loop.

        Feedback that silently fails to land is worse than none: the user watches, sees no
        change, and concludes the agent ignores them.
        """
        await self._emit(protocol.FEEDBACK_ACK, {"text": text, "accepted": accepted})

    async def event_proposed(self, event: dict[str, Any]) -> None:
        """A drafted calendar event.

        Attendees stay as TOKENS here. The cockpit shows the proposal for review, and if an
        invite dispatch is ever built it goes through the approval gate — where the executor
        resolves the tokens for the human, exactly as it does for a draft email.
        """
        await self._emit(protocol.EVENT_PROPOSED, event)

    async def rule_candidate(self, suggestion: str, count: int) -> None:
        await self._emit(protocol.RULE_CANDIDATE, {"suggestion": suggestion, "count": count})

    async def tool_call(self, name: str, args: dict[str, Any]) -> None:
        await self._emit(protocol.TOOL_CALL, {"name": name, "args": args})

    async def action_result(
        self, success: bool, reason: str, error_code: str | None = None
    ) -> None:
        await self._emit(
            protocol.ACTION_RESULT,
            {"success": success, "reason": reason, "errorCode": error_code},
        )

    async def observation(self, *, context_id: str, elements: int, dropped: int, view: str) -> None:
        await self._emit(
            protocol.OBSERVATION,
            {"contextId": context_id, "elements": elements, "droppedCount": dropped, "view": view},
        )

    async def activity(self, phase: str, label: str = "") -> None:
        """What is happening right now. Transient — never a transcript row."""
        await self._emit(protocol.ACTIVITY, {"phase": phase, "label": label})

    async def frame(self, jpeg_base64: str, seq: int) -> None:
        # Not redacted: it is image bytes, and scrubbing a base64 blob would be a pointless
        # scan over megabytes on every frame.
        await self._emit(protocol.FRAME, {"jpegBase64": jpeg_base64, "seq": seq}, redact=False)

    # ── human in the loop ───────────────────────────────────────────────────

    async def question(self, question: str, missing: list[str], request_id: str) -> None:
        await self._emit(
            protocol.QUESTION,
            {"requestId": request_id, "question": question, "missing": missing},
        )

    async def approval_request(
        self, *, request_id: str, kind: str, summary: str, preview: str, expires_at: str
    ) -> None:
        """The ONE place resolved PII deliberately reaches the cockpit.

        A human cannot verify "send to P17" — checking the recipient is the entire point of
        the gate, so the preview carries the real name and address. It goes to the
        authenticated cockpit and nowhere else: never into `messages`, the trajectory, or an
        LLM request. Hence `redact=False`, which is a decision, not an oversight.
        """
        await self._emit(
            protocol.APPROVAL_REQUEST,
            {
                "requestId": request_id,
                "kind": kind,
                "summary": summary,
                "preview": preview,
                "expiresAt": expires_at,
            },
            redact=False,
        )

    async def approval_result(self, request_id: str, verdict: str) -> None:
        # A decision closes the replay window. The dedup above exists to swallow the SAME
        # pending card re-emitted when a node re-executes on resume; once a human has
        # actually decided, any further request is a new question and must reach them —
        # even if it happens to carry the same id. Leaving the key set is how an edited
        # draft was re-proposed and never shown, so the run waited at an interrupt for a
        # card that had been dropped one layer below the UI.
        self._last = None
        await self._emit(protocol.APPROVAL_RESULT, {"requestId": request_id, "verdict": verdict})

    async def diagnosis(self, cause: str, plain: str, evidence: str = "") -> None:
        await self._emit(
            protocol.DIAGNOSIS, {"cause": cause, "plain": plain, "evidence": evidence}
        )

    async def options(self, request_id: str, options: list[dict[str, Any]]) -> None:
        await self._emit(protocol.OPTIONS, {"requestId": request_id, "options": options})

    # ── telemetry ───────────────────────────────────────────────────────────

    async def usage(self, provider: str, role: str, usage: Usage, latency_ms: int) -> None:
        await self._emit(
            protocol.USAGE,
            {
                "provider": provider,
                "role": role,
                "inputTokens": usage.input_tokens,
                "outputTokens": usage.output_tokens,
                "cachedTokens": usage.cached_tokens,
                "latencyMs": latency_ms,
            },
        )

    async def memory(self, key: str, value: str) -> None:
        await self._emit(protocol.MEMORY_UPDATE, {"key": key, "value": value})

    async def error(self, message: str, error_code: str | None = None) -> None:
        await self._emit(protocol.ERROR, {"message": message, "errorCode": error_code})

    async def run_complete(self, **data: Any) -> None:
        """The server's sentinel.

        Always sent, even when the agent produced no `finalize` — some failure paths do not
        reach one, and a cockpit that keys "the run ended" off `finalize` alone would hang
        forever on exactly the runs a user most wants to see the end of.
        """
        await self._emit(protocol.RUN_COMPLETE, data)
