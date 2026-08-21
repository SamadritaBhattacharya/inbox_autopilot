# Tech Stack — Inbox Autopilot

- **Cost target: $0.** Every choice below is either open source or has a free tier sufficient for v1.
- **Constraint that drives everything:** the free-tier limit is **rate and quota, not dollars**.

---

## 1. At a glance

| Layer | Choice | Version | Why this one |
| --- | --- | --- | --- |
| Backend runtime | Python | 3.12+ | Modern typing (`X \| None`, `Protocol`), the LangGraph ecosystem lives here |
| Web framework | FastAPI | latest | Native async, native WebSocket, Pydantic-first, tiny surface |
| Orchestration | **LangGraph** | latest | Explicit state machine, checkpointer, `interrupt()` — the three things this project needs |
| LLM plumbing | LangChain core | latest | Tool binding + streaming, **behind the `LLMClient` port only** |
| Validation / contracts | Pydantic v2 | latest | One source of truth for the wire; emits JSON Schema |
| Package manager (py) | uv | latest | Fast, lockfile-based, first-class workspace path deps |
| Browser control | Playwright (Chromium) + raw CDP | latest | Playwright for launch/targets/nav/screenshot; raw CDP for `DOMSnapshot`, `Accessibility`, `Input.*` |
| Extension | TypeScript + `chrome.debugger` (MV3) | — | The only way to get trusted input in the user's own Chrome |
| **Frontend** | **Next.js (App Router) + React 18** | 15.x | See §3 |
| Styling | Tailwind CSS | v4 | Utility-first; suits a dense cockpit where every rule is layout |
| UI primitives | `shadcn/ui` (copy-in Radix) | latest | Accessible dialog/card/popover for the approval, question, and options cards — copied in, so they restyle to our theme rather than fighting a dependency |
| Client state / data | TanStack Query + Zustand | latest | Query for run history REST; Zustand for the live WS event store |
| Client validation | Zod (**generated**) | latest | Generated from the Pydantic schema; never hand-written |
| Real-time | WebSocket | — | Bidirectional; needed for `answer` / `decision` frames going *up* |
| Persistence | LangGraph checkpointer — SQLite (dev) / Postgres (prod) | — | Buys resume + all three interrupts for free |
| LLM providers | **Groq → OpenRouter → Gemini** | — | See §4 |
| Task runner | `just` | — | One entry point per operation across two package managers |
| JS workspace | pnpm workspaces | — | Frontend + extension + contracts in one lockfile |
| Container | Docker (HF Space, port 7860) | — | xvfb inside, so headful Chromium runs on a headless host |

## 2. Repository layout

```
inbox_autopilot/                     # monorepo
├─ backend/
│  ├─ app/
│  │  ├─ agent/        # graph build, nodes, routing, state, prompts, compaction
│  │  ├─ manager/      # supervisor, intake, context_gate, router, planner
│  │  ├─ workers/      # Triage · Compose · Calendar · Rules (each a subgraph)
│  │  ├─ observation/  # Observation model + server-side funnel
│  │  ├─ actions/      # ActionCall types + dispatcher + one handler per verb
│  │  ├─ recovery/     # diagnose, RemediationStrategy impls, SkillRegistry
│  │  ├─ rules/        # RulesStore + deterministic matcher + soft-guidance renderer
│  │  ├─ surface/      # EmailSurface port; PlaywrightEmailSurface, ExtensionEmailSurface
│  │  ├─ security/     # PiiVault + PiiTokenizer + redaction filters
│  │  ├─ llm/          # LLMClient port + FallbackLLMClient + Groq/OpenRouter/Gemini + metering
│  │  ├─ telemetry/    # StepRecord, ErrorCode, TrajectoryStore
│  │  ├─ events/       # EventSink port + emitter + protocol
│  │  ├─ api/          # FastAPI routes + WS hub + run manager (transport → services ONLY)
│  │  └─ config/       # settings + composition root (the ONLY place wiring concretes)
│  ├─ bench/           # fixture tasks, judge, recorder, run_bench
│  ├─ tests/           # fakes, unit, graph-path, integration
│  └─ pyproject.toml
├─ bridge-extension/   # MV3 TS: funnel + PII tokenizer + chrome.debugger dispatch + WS client
├─ frontend/           # Next.js cockpit (see §3)
├─ packages/contracts/ # Pydantic (truth) → JSON Schema → Zod/TS (generated, committed)
├─ docs/               # this documentation set
├─ justfile · docker-entrypoint.sh · Dockerfile · pnpm-workspace.yaml
└─ CLAUDE.md           # the build contract
```

## 3. Frontend — Next.js, and how a live WS cockpit fits it

**Decision: Next.js App Router**, not Vite. The consequences are real and worth stating plainly,
because a live WebSocket cockpit is not the shape Next.js optimizes for by default.

### 3.1 Layout

```
frontend/
├─ app/
│  ├─ layout.tsx                # shell, fonts, Tailwind, theme  (Server Component)
│  ├─ page.tsx                  # hero + composer                (Server Component)
│  ├─ run/[threadId]/page.tsx   # the two-pane cockpit           (Server shell)
│  └─ history/page.tsx          # past runs                      (Server Component + fetch)
├─ components/cockpit/
│  ├─ CockpitClient.tsx         # "use client" — owns the socket, the ONLY stateful root
│  ├─ Transcript.tsx            # LHS: reasoning · tool calls · results
│  ├─ QuestionCard.tsx          # AskUser interrupt
│  ├─ ApprovalCard.tsx          # Send/invite/delete approval — resolved draft preview
│  ├─ OptionsCard.tsx           # 4 ranked self-heal options, [4] free-form
│  ├─ Viewport.tsx              # RHS: live browser frames + current-action label
│  └─ Composer.tsx              # task input + stop
├─ lib/
│  ├─ useAgentRun.ts            # WS lifecycle: start · attach · answer · decide · stop
│  ├─ eventStore.ts             # Zustand: append-only event log → derived timeline
│  └─ env.ts                    # NEXT_PUBLIC_WS_URL, validated with Zod at boot
└─ next.config.ts
```

### 3.2 Visual language

Monochrome and minimal: black, white, and grey only, one theme, applied consistently. Rounded
corners, sleek modern surfaces, smooth transitions between states. The cockpit is a place to *watch*
something, so the chrome recedes and the agent's output carries the page.

**Colour is reserved for meaning, never decoration** — a pending approval, a failure, the
recommended option. In a monochrome interface those few coloured moments become unmissable, which is
exactly the property an approval gate needs. Anything that is merely "nice to look at" is grey.

### 3.3 The rules that keep it sane

| Rule | Why |
| --- | --- |
| **Exactly one `"use client"` root** — `CockpitClient`. Everything live hangs beneath it. | Prevents the "everything is a client component" drift that makes App Router pointless. |
| **The WebSocket connects directly to the backend**, not through a Next.js route handler. | Vercel's serverless functions do not hold long-lived WS connections. The cockpit talks to the FastAPI host directly via `NEXT_PUBLIC_WS_URL`. |
| **No LLM key, no provider config, nothing secret in `frontend/`** — not even in a server-only env var. | The backend owns every key. The frontend needs exactly one env var: the WS URL. |
| **Server Components for anything static**: shell, hero, history list, docs pages. | This is what Next.js buys us over Vite — a fast, cached, SEO-able shell around a live island. |
| **Frames are rendered to a `<canvas>`**, decoded off the React render path. | 2–10 fps of base64 JPEG through React state would thrash reconciliation. |
| **The event log is append-only**; the timeline is a derived selector. | Replay on reattach is then a pure function of the buffer, identical to the live path. |

### 3.3 What we give up, and the mitigation

| Cost of Next.js here | Mitigation |
| --- | --- |
| Heavier dev server and build than Vite | Acceptable; the cockpit is small and the build runs in CI |
| WS cannot be proxied through Vercel | Direct connection to the backend host; CORS + WS origin allowlist configured server-side |
| Hydration mismatches on time-formatted event rows | Format timestamps client-side only, after mount |
| SSR of a socket-driven page is meaningless | The run page server-renders a **skeleton**; the client component owns everything live |

**What we gain:** a real router with per-run URLs (`/run/[threadId]` is shareable and reattachable),
Server Components for history, image/font optimization, and a deployment story on Vercel that is one
`git push`.

## 4. LLM providers — the chain and the math

```python
FallbackLLMClient([
    GroqClient(...),        # primary
    OpenRouterClient(...),  # fallback 1
    GeminiClient(...),      # fallback 2
])
```

| Provider | Role in the chain | Free-tier character | Notes |
| --- | --- | --- | --- |
| **Groq** | primary | Highest requests/min of the three; very low latency | Best fit for a per-step agent loop where latency compounds |
| **OpenRouter** | fallback 1 | `:free` model roster; low daily cap; no card required | OpenAI-compatible — a `base_url` swap, no new client |
| **Gemini** | fallback 2 | Generous daily free tier | Also the natural home for the `validator` rubric role |

**Rules (non-negotiable):**
- Keys are read from a gitignored `.env`, **server-side only**.
- Model slugs come from settings. **Never hardcode a `:free` model ID** — that roster rotates and a
  hardcoded slug is a time bomb.
- Fallback happens **between** attempts, never inside a retry. A retry uses the same model; a
  fallback is a new attempt on the next provider.
- Every call is metered into a `StepRecord`.

### 4.1 Why quota, not cost, is the design constraint

A naive triage over 40 emails at one LLM call per step is ~40+ calls. On a low daily cap that is one
run per day. The architecture answers this in four places:

| Lever | Saving |
| --- | --- |
| Linear route (`RulesWorker`) | **100%** — zero LLM calls for rule-matched work |
| Batched classification | one call scores N subjects instead of N calls |
| `classifier` role on a small model | ~5–10× cheaper per call than the executor model |
| Prompt caching on a stable prefix | input cost stays roughly flat as history grows |

Ordering matters: the cheapest correct path is tried first, always.

## 5. Environment variables

```bash
# ── LLM (server-side ONLY — never in frontend/ or bridge-extension/) ──
GROQ_API_KEY=
OPENROUTER_API_KEY=
GEMINI_API_KEY=
LLM_MODEL_CLASSIFIER=            # slug from config; never hardcoded in code
LLM_MODEL_EXECUTOR=
LLM_MODEL_VALIDATOR=

# ── Agent ──
MAX_STEPS=40
LLM_TEMPERATURE=0.2
LLM_MAX_RETRIES=3
LLM_REQUEST_TIMEOUT=45
LLM_MAX_OUTPUT_TOKENS=2000
CONTEXT_CONFIDENCE_THRESHOLD=0.85

# ── Surface ──
EMAIL_SURFACE=playwright         # playwright | extension | fake
CDP_HEADLESS=false               # headful is the anti-detection lever; xvfb in Docker
STEALTH=true
START_URL=https://mail.google.com
BROWSER_LOCALE=en-IN
BROWSER_TIMEZONE=Asia/Kolkata

# ── Security ──
PII_TOKENIZE_NAMES=true          # addresses+phones are always on and non-optional
APPROVAL_TIMEOUT_SECONDS=600

# ── Persistence ──
CHECKPOINT_DSN=sqlite:///runs/checkpoints.db
RUNS_DIR=runs

# ── Frontend (the ONLY frontend var) ──
NEXT_PUBLIC_WS_URL=ws://localhost:8000/ws/run
```

## 6. Commands

```bash
just setup          # uv sync + playwright install chromium + pnpm install + gen-contracts
just gen-contracts  # Pydantic → JSON Schema → Zod → build @inbox/contracts
just check          # regenerate contracts and fail on drift
just test           # backend pytest + contracts pytest + pnpm -r test
just dev-backend    # python -m app.api.dev  (uvicorn --reload + a browser-capable loop)
just dev-frontend   # pnpm -C frontend dev  (Next.js)
just bench          # run the fixture benchmark suite
```

## 7. Deployment

| Unit | Host | Free tier | Notes |
| --- | --- | --- | --- |
| Backend + browser | Hugging Face Space (Docker, port 7860) or Render / Fly | yes | Needs xvfb for headful Chromium; the Space sleeps — the cockpit shows a "waking" state |
| Frontend | Vercel | yes | One env var: `NEXT_PUBLIC_WS_URL` |
| Postgres (prod checkpointer) | Neon or Supabase | yes | SQLite is fine until multi-replica |
| Extension | unpacked / Chrome Web Store | yes | Ships no keys; authenticates to the backend over the relay |

**Total recurring cost: $0.**

## 8. Rejected alternatives

| Rejected | In favour of | Reason |
| --- | --- | --- |
| Gmail REST API | Browser UI via CDP | The API cannot do everything the UI can, requires OAuth scopes users resist, and would discard the funnel engine entirely. The `EmailSurface` port keeps an API adapter possible later. |
| Code execution (CodeAct) for actions | Structured tool calls | Email bodies are attacker-controlled text entering the model context. Prompt injection on CodeAct escalates to backend RCE. Tool calls are schema-validated and observable. See [ADR-004](ADR.md#adr-004). |
| Vite + React SPA | Next.js | User requirement; also buys per-run URLs and Server Components for the static shell. |
| Server-Sent Events | WebSocket | The cockpit must send `answer`, `decision`, and `stop` *upward*. SSE is one-way. |
| Celery / Redis job queue | asyncio tasks + a process-level run registry | One browser per session is the real bottleneck, not task dispatch. Adding a broker adds a service without removing a constraint. |
| Redis for run state | LangGraph checkpointer | The checkpointer already gives durable pause/resume and interrupts. A second state store would be a consistency bug waiting to happen. |
| JS `element.click()` | CDP `Input.*` trusted input | `isTrusted: false` skips hover handlers and trips automation detection. |
| Electron / desktop host | Web app | Explicit guardrail — this is a web product. |
