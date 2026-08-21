"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { env } from "./env";
import {
  type Activity,
  bool,
  num,
  str,
  type AgentEvent,
  type Entry,
  type Choice,
  type PendingApproval,
  type PendingOptions,
  type PendingQuestion,
  type RunStatus,
  type UsageTotals,
} from "./types";

/**
 * The cockpit's socket lifecycle and event log.
 *
 * **The event log is append-only and the timeline is derived from it.** That is not a style
 * choice: the backend replays buffered events verbatim when a cockpit reattaches, so if
 * rendering is a pure function of the log, replay and live are automatically identical. Any
 * state mutated *during* handling would have to be reconstructed separately for replay, and
 * the reattach path is the one nobody tests by hand.
 *
 * Frames are kept OUT of React state entirely — they arrive several times a second and
 * pushing base64 blobs through reconciliation would thrash the whole tree. They go to a
 * subscriber that paints a canvas directly.
 */

type FrameHandler = (jpegBase64: string) => void;

export function useAgentRun(threadId: string, task?: string) {
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [status, setStatus] = useState<RunStatus>("idle");
  const [connected, setConnected] = useState(false);
  const [absent, setAbsent] = useState(false);
  const [location, setLocation] = useState("");
  // Transient by design: the LATEST activity replaces the last one, and it is cleared
  // when the run ends. Keeping a history of them would just be a noisier transcript.
  const [activity, setActivity] = useState<Activity | null>(null);

  const socketRef = useRef<WebSocket | null>(null);
  const frameHandlers = useRef(new Set<FrameHandler>());
  const startedRef = useRef(false);

  const subscribeFrame = useCallback((handler: FrameHandler) => {
    frameHandlers.current.add(handler);
    return () => {
      frameHandlers.current.delete(handler);
    };
  }, []);

  const send = useCallback((payload: Record<string, unknown>) => {
    const socket = socketRef.current;
    if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(payload));
  }, []);

  useEffect(() => {
    const socket = new WebSocket(env.NEXT_PUBLIC_WS_URL);
    socketRef.current = socket;

    socket.onopen = () => {
      setConnected(true);
      // A fresh run when we were given a task; otherwise attach to one already in flight.
      // This is what makes a run's URL shareable and a refresh non-destructive.
      if (task && !startedRef.current) {
        startedRef.current = true;
        socket.send(JSON.stringify({ type: "start", task, threadId }));
      } else {
        socket.send(JSON.stringify({ type: "attach", threadId }));
      }
    };

    socket.onmessage = (message) => {
      const frame = JSON.parse(message.data) as AgentEvent;

      // Off the React path: several a second, and each is a base64 image.
      if (frame.event === "frame") {
        const jpeg = str(frame.data.jpegBase64);
        if (jpeg) frameHandlers.current.forEach((handler) => handler(jpeg));
        return;
      }

      if (frame.event === "activity") {
        setActivity({ phase: str(frame.data.phase), label: str(frame.data.label) });
        return;
      }

      // Where the browser is. Handled before the transcript append and never stored as an
      // event: it changes on every turn and belongs in the viewport chrome, not the log.
      if (frame.event === "location") {
        setLocation(str(frame.data.url));
        return;
      }

      if (frame.event === "run_absent") {
        setAbsent(true);
        setStatus("idle");
        return;
      }

      setEvents((previous) => [...previous, frame]);

      if (["question", "approval_request", "options"].includes(frame.event)) setStatus("awaiting");
      else if (frame.event === "run_complete") {
        setStatus("done");
        setActivity(null);
      }
      else if (frame.event === "finalize") setStatus(bool(frame.data.success) ? "done" : "failed");
      else if (frame.event === "status") {
        const phase = str(frame.data.phase);
        if (phase === "starting") setStatus("starting");
        else if (phase === "running") setStatus("running");
      } else if (status === "awaiting") setStatus("running");
    };

    socket.onclose = () => setConnected(false);

    return () => socket.close();
    // Intentionally once per run: a reconnect is a new mount, not a state change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId]);

  const answer = useCallback(
    (text: string) => {
      send({ type: "answer", answer: text });
      setStatus("running");
    },
    [send],
  );

  const decide = useCallback(
    (verdict: "approve" | "edit" | "reject", edit?: string) => {
      send({ type: "decision", verdict, edit: edit ?? "" });
      setStatus("running");
    },
    [send],
  );

  const choose = useCallback(
    (option: number, text?: string) => {
      send({ type: "choice", option, text: text ?? "" });
      setStatus("running");
    },
    [send],
  );

  const feedback = useCallback((text: string) => send({ type: "feedback", text }), [send]);
  const stop = useCallback(() => send({ type: "stop" }), [send]);

  const timeline = useMemo(() => toTimeline(events, task), [events, task]);
  const question = useMemo(() => latestQuestion(events), [events]);
  const approval = useMemo(() => latestApproval(events), [events]);
  const options = useMemo(() => latestOptions(events), [events]);
  const usage = useMemo(() => totalUsage(events), [events]);

  return {
    timeline,
    question,
    approval,
    activity,
    usage,
    status,
    connected,
    absent,
    location,
    answer,
    decide,
    choose,
    options,
    feedback,
    stop,
    subscribeFrame,
  };
}

/** Events -> rendered rows. Pure, so replay and live cannot diverge. */
function toTimeline(events: AgentEvent[], task?: string): Entry[] {
  const entries: Entry[] = task ? [{ kind: "task", text: task }] : [];

  for (const { event, data } of events) {
    switch (event) {
      case "intent":
        entries.push({
          kind: "intent",
          action: str(data.action),
          slots: (data.slots as Record<string, string>) ?? {},
          confidence: num(data.confidence),
        });
        break;
      case "route":
        entries.push({
          kind: "route",
          route: str(data.route),
          why: str(data.why),
          ruleMatched: bool(data.ruleMatched),
        });
        break;
      case "plan_update":
        entries.push({ kind: "plan", steps: (data.steps as string[]) ?? [] });
        break;
      case "status":
        // The "starting" frame is bookkeeping; the worker line is worth showing.
        if (str(data.phase) === "running") {
          entries.push({ kind: "status", phase: str(data.phase), message: str(data.message) });
        }
        break;
      case "assessment":
        entries.push({ kind: "assessment", text: str(data.text), outcome: str(data.outcome) });
        break;
      case "reasoning":
        entries.push({ kind: "reasoning", text: str(data.text) });
        break;
      case "tool_call":
        entries.push({
          kind: "action",
          name: str(data.name),
          args: (data.args as Record<string, unknown>) ?? {},
        });
        break;
      case "action_result":
        entries.push({
          kind: "result",
          success: bool(data.success),
          reason: str(data.reason),
          errorCode: (data.errorCode as string) ?? null,
        });
        break;
      case "observation":
        entries.push({
          kind: "observation",
          elements: num(data.elements),
          dropped: num(data.droppedCount),
          view: str(data.view),
        });
        break;
      case "feedback_ack":
        entries.push({ kind: "feedback", text: str(data.text) });
        break;
      case "approval_result":
        entries.push({ kind: "decision", verdict: str(data.verdict) });
        break;
      case "diagnosis":
        entries.push({
          kind: "diagnosis",
          plain: str(data.plain),
          evidence: str(data.evidence),
        });
        break;
      case "error":
        entries.push({
          kind: "error",
          message: str(data.message),
          errorCode: (data.errorCode as string) ?? null,
        });
        break;
      case "finalize":
        entries.push({
          kind: "finalize",
          success: bool(data.success),
          reason: str(data.reason),
          errorCode: (data.errorCode as string) ?? null,
        });
        break;
      case "run_complete":
        // Only when a human stopped it. Every other ending already rendered its own
        // `finalize` card, and a second "it ended" row under it reads like a bug.
        if (bool(data.stopped)) entries.push({ kind: "stopped" });
        break;
      default:
        break; // unknown events are ignored, never fatal
    }
  }
  return entries;
}

/** The question still waiting, if any. */
function latestQuestion(events: AgentEvent[]): PendingQuestion | null {
  let pending: PendingQuestion | null = null;
  for (const { event, data } of events) {
    if (event === "question") {
      pending = {
        requestId: str(data.requestId),
        question: str(data.question),
        missing: (data.missing as string[]) ?? [],
      };
    } else if (event === "reasoning" || event === "run_complete" || event === "finalize") {
      // Anything that means the run moved on answers it.
      pending = null;
    }
  }
  return pending;
}

/** The approval still waiting, if any. Cleared by its own result event. */
function latestApproval(events: AgentEvent[]): PendingApproval | null {
  let pending: PendingApproval | null = null;
  for (const { event, data } of events) {
    if (event === "approval_request") {
      pending = {
        requestId: str(data.requestId),
        kind: str(data.kind, "bulk"),
        summary: str(data.summary),
        preview: str(data.preview),
        expiresAt: str(data.expiresAt),
      };
    } else if (event === "approval_result" || event === "run_complete") {
      pending = null;
    }
  }
  return pending;
}

/** The recovery options still open, if any. Needs the diagnosis that preceded them. */
function latestOptions(events: AgentEvent[]): PendingOptions | null {
  let diagnosis = { cause: "", plain: "", evidence: "" };
  let pending: PendingOptions | null = null;

  for (const { event, data } of events) {
    if (event === "diagnosis") {
      diagnosis = {
        cause: str(data.cause),
        plain: str(data.plain),
        evidence: str(data.evidence),
      };
    } else if (event === "options") {
      pending = {
        requestId: str(data.requestId),
        ...diagnosis,
        choices: (data.options as Choice[]) ?? [],
      };
    } else if (event === "reasoning" || event === "run_complete" || event === "finalize") {
      // Anything that means the run moved on has answered them.
      pending = null;
    }
  }
  return pending;
}

function totalUsage(events: AgentEvent[]): UsageTotals {
  return events.reduce<UsageTotals>(
    (totals, { event, data }) =>
      event === "usage"
        ? {
            calls: totals.calls + 1,
            inputTokens: totals.inputTokens + num(data.inputTokens),
            outputTokens: totals.outputTokens + num(data.outputTokens),
            cachedTokens: totals.cachedTokens + num(data.cachedTokens),
          }
        : totals,
    { calls: 0, inputTokens: 0, outputTokens: 0, cachedTokens: 0 },
  );
}
