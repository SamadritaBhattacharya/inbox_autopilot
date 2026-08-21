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

## Setup — once, ever

The agent uses **an account you are already signed into**. You never sign in through it,
because Google refuses its sign-in flow in any browser running a debugging port.

So the flow splits in two: sign in once in an ordinary window, and let every run after that
attach to the profile those cookies live in. The backend handles the second half itself.

### 1. Sign in, in a window with no debugging port

```powershell
python scripts/chrome.py signin
```

Chrome opens on the agent's own profile (`~/.inbox-agent-profile`) with **no** debugging
port — the one configuration Google accepts. Sign into Gmail, then **close that window**.

You only ever do this again if you sign out or the session expires.

### 2. Point the backend at it

In `backend/.env`:

```ini
EMAIL_SURFACE=playwright
CDP_ENDPOINT=http://127.0.0.1:9222
```

### 3. Start the backend and the cockpit

```powershell
python -m app.api.dev     # from backend/
npm run dev               # from frontend/
```

**That is all.** When a run starts and nothing is listening on 9222, the backend launches
Chrome on that signed-in profile itself. No second terminal, and no `serve` command to
remember.

### Why its own profile, and not yours

Chrome **silently ignores** `--remote-debugging-port` while another instance already owns
the user-data-dir — it just opens a tab in the running browser and no port ever listens,
which looks exactly like the flag not working. That is the entire reason the old
instructions made you close every Chrome window and check the system tray.

A separate profile directory has no such contention, so **the agent's browser and your
everyday Chrome run side by side**. It also keeps your main profile's cookies away from a
port that anything local can connect to.

To use your everyday profile anyway, run `python scripts/chrome.py list`, then
`serve --profile-directory "Profile 5"` — with every Chrome window closed first — and the
backend will attach to that instead of launching its own.

### Turning the auto-launch off

```ini
CDP_AUTO_LAUNCH=false      # you start the browser; the backend only attaches
CHROME_PROFILE_DIR=        # empty = ~/.inbox-agent-profile
```

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
| "Couldn't sign you in / browser may not be secure" | You are trying to **sign in** in a browser Google rejects — either Chrome for Testing, or any Chrome started with a debugging port. Do not sign in there. Use a profile that is already signed in: `python scripts/chrome.py list`, then `serve --profile-directory "..."`. |
| `NOT_SIGNED_IN` and the run stops immediately | Correct behaviour: that browser is on a login page. Sign in, then start the run again. |
| `serve` says Chrome is already running | Close every window, including the system tray icon. Chrome ignores the debugging port otherwise. |
| "this is the first run, so I opened a normal Chrome window" | Exactly what it says: sign into Gmail in that window, close it, and send your message again. |
| "started Chrome but nothing ever listened on port 9222" | Another Chrome instance already owns that profile directory, so the debugging port was dropped. Close any window using `~/.inbox-agent-profile`. |
| `could not attach to a browser at http://127.0.0.1:9222` | Something is on the port that is not a Chrome debugging endpoint. Free the port, or change `CDP_ENDPOINT`. |
| Agent's tab shows the Gmail login page | That profile is not signed in. Run step 1 again. |
| `this event loop cannot start a browser process` | Very old start command. Use `python -m app.api.dev`. |
| Live view blank, left pane quiet | The run is probably paused on a question or approval card — scroll the left pane. |
