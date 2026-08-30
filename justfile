set shell := ["bash", "-uc"]
set windows-shell := ["powershell.exe", "-NoLogo", "-NoProfile", "-Command"]

# `just` is optional — every recipe below is mirrored as an npm script in package.json,
# so `pnpm run <name>` works identically without installing just.

default:
    @just --list

# Install everything: backend venv (uv fetches Python 3.12), JS workspace, contracts build.
setup:
    uv sync --project backend
    pnpm install
    just gen-contracts

# Chromium for the Playwright surface (M1) — separate because it's a large download.
setup-browser:
    uv run --project backend playwright install chromium

# Regenerate contracts: Pydantic -> JSON Schema -> Zod -> build @inbox/contracts
gen-contracts:
    uv run --project backend python packages/contracts/scripts/gen.py
    node packages/contracts/scripts/gen-zod.mjs
    pnpm -C packages/contracts build

# Drift guard: regenerate and fail if committed artifacts changed.
check: gen-contracts
    git diff --exit-code -- packages/contracts/schema packages/contracts/src/generated

# Security guards: no committed keys, .env untracked, one public env var in the cockpit.
guards:
    uv run --project backend python scripts/guards.py

# The full local gate. Run this before every commit — it is what CI would run.
verify: check guards lint test
    pnpm -C frontend typecheck

# All tests: backend + contracts (pytest) then the JS workspace (vitest).
test:
    uv run --project backend pytest -q backend/tests packages/contracts/tests
    pnpm -r test

test-py:
    uv run --project backend pytest -q backend/tests packages/contracts/tests

test-js:
    pnpm -r test

# The browser-marked suite: the real surface methods, against real Chrome, over synthetic
# DOM that reproduces Gmail's structure. Slow (minutes) and excluded from `test` for that
# reason -- but it is the only place selector logic is proved rather than re-asserted.
test-browser:
    uv run --project backend pytest -q backend/tests -m browser

# Golden-task table: success rate, steps, tokens, typed termination, gate bypasses.
# Exits 1 on any failure or any bypassed approval, so it works as a gate in a script.
eval:
    uv run --project backend python -m tests.bench.run

lint:
    uv run --project backend ruff check backend packages/contracts scripts

# Dev servers
# Not plain `uvicorn --reload`: on Windows that selects an event loop which cannot spawn
# the browser process. See app/api/loop.py.
dev-backend:
    uv run --project backend python -m app.api.dev

dev-frontend:
    pnpm -C frontend dev

# Benchmark harness: reliability numbers on a deterministic suite (no provider needed).
bench:
    uv run --project backend python -m bench.run_bench
