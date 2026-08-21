# Running it against your real Gmail

## The error you hit, and why

> **Couldn't sign you in.** This browser or app may not be secure.

Nothing is wrong with your account or your password. Google refuses its sign-in flow in
browsers it does not recognise as genuine Chrome — and the browser in that screenshot was
labelled **"Chrome for Testing"**, which is the build Playwright ships. It has no Google API
keys, so Google rejects it by design. No flag or setting changes that, and the JavaScript
setting you looked at is unrelated.

Two things follow, and both matter:

1. Use your **real, installed Google Chrome**, not Playwright's bundled Chromium.
2. **Never sign in inside an automated browser.** Sign in first, as a human, then let the
   agent attach to that already-authenticated session.

## Setup

### 1. Sign in — once

```powershell
python scripts/chrome.py signin
```

This opens your real Chrome on a dedicated profile (`~/.inbox-agent-profile`) with **no
debugging port and no automation flags**. As far as Google is concerned it is an ordinary
browser window, because it is one. Sign into Gmail, then **close Chrome completely**.

The separate profile is deliberate: Chrome ignores `--remote-debugging-port` when an
instance is already running on that profile, so reusing your everyday profile usually does
nothing at all — and it keeps your normal browsing out of a profile an agent drives.

### 2. Open the same profile for the agent

```powershell
python scripts/chrome.py serve
```

Same Chrome, same profile, now with the debugging port open. You are already signed in, so
nothing authenticates under automation. Leave this window open.

### 3. Point the backend at it

In `backend/.env`:

```ini
EMAIL_SURFACE=playwright
CDP_ENDPOINT=http://127.0.0.1:9222
START_URL=https://mail.google.com
```

### 4. Start the backend and the cockpit

```powershell
python -m app.api.dev     # from backend/
npm run dev               # from frontend/
```

Plain `uvicorn app.api.main:app --reload` works too — the browser falls back to a thread with
its own event loop — but it costs a thread. See `app/api/loop.py` for why.

## What you should see

```
INFO  app.api.main: event loop ProactorEventLoop can start a browser directly
```

And **no** warning about browser brands. If you see this:

```
WARNING  attached to a browser identifying as HeadlessChrome, Chromium, Not)A;Brand.
         Google blocks its sign-in flow on non-Google Chrome builds ...
```

you are attached to Chrome for Testing again — go back to step 1.

The agent opens **its own tab** in your signed-in profile rather than taking over one of
yours, and closes only that tab when a run ends. Your browser stays open.

## What you can ask it

The agent works on **the mailbox that browser is signed into**. "From" is always that
account.

| Ask | What happens |
| --- | --- |
| "summarize my unread emails" | Reads the inbox, reports back. No mutation. |
| "how many unread do I have?" | Reads and answers. |
| "find the thread about the invoice" | Searches, opens, reads. |
| "archive the newsletters" | Archives. Reversible, so no approval gate. |
| "reply to Priya about Friday" | Drafts, shows you the draft, **waits for approval**. |
| "send an email to alice@example.com about the demo" | Composes, fills fields live, **waits for approval** before sending. |
| "add the meeting in this thread to my calendar" | Extracts the event, **approval on the invite**. |

Anything irreversible — send, delete-forever, calendar invites — stops at an approval card in
the left pane. Nothing leaves your mailbox without you clicking approve.

## Without CDP_ENDPOINT

Leave `CDP_ENDPOINT` empty and the backend launches its own Chromium. That is the right mode
for the fixtures in `backend/tests/fixtures/` and for the benchmark, where there is nothing
to sign into. It is not a mode in which you can reach real Gmail.

## Troubleshooting

| What you see | What it means |
| --- | --- |
| "Couldn't sign you in / browser may not be secure" | You are signing in inside Chrome for Testing. Use `python scripts/chrome.py signin`. |
| `could not attach to a browser at http://127.0.0.1:9222` | `scripts/chrome.py serve` is not running, or another Chrome instance owns the profile. Close Chrome fully and re-run it. |
| Agent's tab shows the Gmail login page | That profile is not signed in. Run step 1 again. |
| `this event loop cannot start a browser process` | Very old start command. Use `python -m app.api.dev`. |
| Live view blank, left pane quiet | The run is probably paused on a question or approval card — scroll the left pane. |
