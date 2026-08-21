# Deploying it

The shape that actually works for more than one person: a backend anywhere, a cockpit on any
static host, and each user's own Chrome doing the browsing.

```
  cockpit (Vercel)            backend (Render / Fly / HF Space)
        │ sign in with Google        │
        │───────── /auth/login ─────►│
        │◄──── #token= redirect ─────│
        │                            │
        │──── /ws/run?token=… ──────►│   the graph, the model keys
                                     │
                                     │◄── /ws/bridge ── extension (user's Chrome)
                                                            the DOM, the vault, Gmail
```

The backend never launches a browser and never holds a mailbox cookie. That is what makes it
deployable at all.

## 1. A Google OAuth client

[console.cloud.google.com](https://console.cloud.google.com) → APIs & Services → Credentials
→ **Create OAuth client ID** → Web application.

Authorised redirect URI — exactly, including the path:

```
https://your-backend.example.com/auth/callback
```

Scopes are **`openid email profile`** and nothing else. Those are *not* restricted, so this
needs **no Google verification and no CASA assessment**. The consent screen can stay in
Testing or go to Production; either works, because non-restricted scopes are not gated.

> Adding any `gmail.*` scope here would move the whole project into restricted-scope review —
> weeks, plus a paid security assessment — for a login button. The agent never touches Gmail
> through Google; it drives the user's own browser.

## 2. Backend environment

```ini
EMAIL_SURFACE=extension

AUTH_MODE=google
GOOGLE_CLIENT_ID=…apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=…
GOOGLE_REDIRECT_URI=https://your-backend.example.com/auth/callback
COCKPIT_URL=https://your-cockpit.example.com
ALLOWED_ORIGINS=https://your-cockpit.example.com

# Signs sessions, bridge tokens, and the OAuth state. 32+ random bytes.
AUTH_SECRET=…

GROQ_API_KEY=…
OPENROUTER_API_KEY=…
GEMINI_API_KEY=…
GEMINI_MODEL_EXECUTOR=gemini-3.6-flash
GEMINI_MODEL_CLASSIFIER=gemini-3.5-flash-lite
GEMINI_MODEL_VALIDATOR=gemini-3.5-flash-lite
```

`AUTH_SECRET` is the revocation lever: rotating it invalidates **every** session and **every**
paired browser at once. Generate it with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

Two settings are load-bearing and easy to get wrong:

- **`ALLOWED_ORIGINS` is not `*`.** With a bearer token in play, `*` is the difference between
  a private API and a public one.
- **`AUTH_MODE=off` refuses nobody.** The server logs a loud warning at startup when it is
  off. That is correct on a laptop and a breach on a public URL.

## 3. Cockpit environment

```ini
NEXT_PUBLIC_WS_URL=wss://your-backend.example.com/ws/run
```

One variable. The HTTP origin is *derived* from it, because they are the same host by
definition and a second variable is a second thing to get out of step.

Use `wss://`, not `ws://` — the derivation keeps the scheme, so a plaintext socket URL would
send session tokens to an `http://` origin.

## 4. Ship the extension

```bash
pnpm run gen-contracts
pnpm -C bridge-extension build     # -> bridge-extension/dist
```

Then either publish `dist/` to the Chrome Web Store (review takes days to weeks), or have
users load it unpacked via `chrome://extensions` → Developer mode → Load unpacked.

**Point it at your backend.** The extension's default is `ws://localhost:8000/ws/bridge`;
users change it in the popup, or you edit the default in `src/background.ts` before building.

## What a user does

1. Opens the cockpit → **Continue with Google**
2. Clicks **Get a pairing code**
3. Installs the extension, clicks its icon, pastes the code, **Save and connect**
4. Opens Gmail in that browser — signed in normally, as they always are
5. Types a task

Step 3 is once, ever. The extension trades the code for a durable token, so it survives
restarts on both ends.

## What this does and does not give you

**Does:**
- Multiple users at once, each driving their own mailbox
- The backend never sees a real address, name, coordinate, or URL — only `P1`, `[54]`
- Nothing irreversible sends without an approval card
- Backend deployable anywhere; no browser, no display, no xvfb

**Does not:**
- **Zero install.** Users must install a Chrome extension. Inherent to this architecture.
- **Any browser.** `chrome.debugger` is Chrome and Edge only.
- **Mobile.** At all.
- **A quiet browser.** Chrome shows "started debugging this browser" during a run, and that
  banner cannot be suppressed — nor should it be.
- **The live browser pane.** Not yet wired for this surface; the run works, the right-hand
  view stays blank. Users can watch their real Gmail tab instead.

If zero-install on any device is a hard requirement, the extension is the wrong architecture
and the Gmail API is the right one — at the cost of Google verification and the browser view.

## Operational notes

- **Free-tier quota is per key, not per user.** Every user spends *your* Groq/OpenRouter/
  Gemini allowance. Groq's daily token cap is reachable by one enthusiastic person in an
  afternoon. Rate-limit before you invite anyone.
- **Pairing codes live in memory** with a ten-minute TTL. A restart mid-pairing means the
  user clicks the button again. Bridge tokens are signed and stateless, so already-paired
  browsers are unaffected.
- **`/health` deliberately says nothing about credentials** — not even whether they exist.
