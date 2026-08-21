"""Finding, starting, and waiting for the Chrome the agent attaches to.

**Why this is not just `subprocess.Popen`.** Attaching to a signed-in browser is the whole
authentication story (see `connect_surface`), and every way it fails looks identical from
the outside: `ECONNREFUSED` on the debugging port. It could be that nothing was started, or
that Chrome started and silently dropped the flag because another instance already owned
the profile, or that the profile has never been signed in. Those need different answers from
the human, so they are told apart here rather than collapsed into one unhelpful error.

**Stdlib only, and no imports from `app`.** `scripts/chrome.py` is meant to run before the
backend's dependencies are installed, and it imports this module. Keep it that way.

**Why a dedicated profile directory is the default.** Chrome ignores
`--remote-debugging-port` while another instance already owns the user-data-dir — which is
why reusing the everyday profile forces the "close every window, check the system tray"
dance. A separate directory has no such contention, so the agent's browser and the user's
own can run side by side.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

#: The agent's own profile. Signed in once, by hand, and thereafter reused.
ISOLATED_PROFILE = Path.home() / ".inbox-agent-profile"
DEFAULT_PORT = 9222

WINDOWS_CHROME = (
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
    / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("LOCALAPPDATA", ".")) / "Google/Chrome/Application/chrome.exe",
)
MAC_CHROME = (Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),)
LINUX_CHROME = (Path("/usr/bin/google-chrome"), Path("/usr/bin/google-chrome-stable"))


class ChromeNotFound(RuntimeError):
    """No installed Google Chrome. Deliberately not a fallback to Chromium: Google refuses
    its sign-in flow on non-Google builds, so a fallback would only move the failure later.
    """


def find_chrome() -> Path:
    """The user's installed Google Chrome — never Playwright's Chrome for Testing."""
    if sys.platform == "win32":
        candidates = WINDOWS_CHROME
    elif sys.platform == "darwin":
        candidates = MAC_CHROME
    else:
        candidates = LINUX_CHROME
    for path in candidates:
        if path.exists():
            return path
    raise ChromeNotFound(
        "could not find Google Chrome in the usual places. Install it, or set "
        "CHROME_PATH to the executable."
    )


def chrome_path() -> Path:
    override = os.environ.get("CHROME_PATH")
    return Path(override) if override else find_chrome()


def endpoint_host_port(endpoint: str) -> tuple[str, int]:
    parsed = urlparse(endpoint if "//" in endpoint else f"http://{endpoint}")
    return parsed.hostname or "127.0.0.1", parsed.port or DEFAULT_PORT


#: Chrome moved the cookie store under `Network/` around M96 and kept the old path working
#: on profiles that predate the move. Check both: guessing one leaves a signed-in profile
#: looking brand new forever, which traps the user in the first-run branch.
COOKIE_PATHS = (
    Path("Network") / "Cookies",
    Path("Cookies"),
)


def _cookie_store(profile: Path) -> Path | None:
    for rel in COOKIE_PATHS:
        candidate = profile / rel
        if candidate.exists():
            return candidate
    return None


def signed_in_profile(data_dir: Path = ISOLATED_PROFILE) -> str | None:
    """Which profile *inside* this directory has actually browsed — `"Profile 5"`, say.

    A user-data-dir is not a profile; it is a container of them, and the one Chrome opens by
    default is `Default`. Signing in through the account picker can land the session in a
    numbered profile instead, leaving `Default` pristine. Attaching to that empty `Default`
    puts the agent on a login wall while the human is looking at their inbox in the very
    same browser — so the profile has to be found, not assumed.

    Preference order is `Local State`'s `last_used` (what Chrome itself would reopen), then
    the largest cookie store as a fallback. A cookie store only exists once a profile has
    loaded a page, so its absence is a reliable "never signed in".
    """
    if not data_dir.is_dir():
        return None

    candidates = {
        child.name: store
        for child in data_dir.iterdir()
        if child.is_dir() and (store := _cookie_store(child)) is not None
    }
    if not candidates:
        return None

    last_used = _last_used_profile(data_dir)
    if last_used in candidates:
        return last_used

    return max(candidates, key=lambda name: candidates[name].stat().st_size)


def _last_used_profile(data_dir: Path) -> str | None:
    """The profile Chrome would reopen on its own. Best-effort: a missing or malformed
    `Local State` is normal on a young profile, not an error worth raising."""
    try:
        state = json.loads((data_dir / "Local State").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = state.get("profile", {}).get("last_used")
    return value if isinstance(value, str) else None


def profile_is_signed_in(data_dir: Path = ISOLATED_PROFILE) -> bool:
    """Has any profile here ever loaded a page?

    Not proof of a live Gmail session — that question belongs to the surface, which asks
    Gmail itself and raises `NOT_SIGNED_IN`. This only separates first run from every later
    run.
    """
    return signed_in_profile(data_dir) is not None


def spawn(
    *,
    port: int | None,
    data_dir: Path = ISOLATED_PROFILE,
    profile_directory: str | None = None,
    start_url: str = "https://mail.google.com",
) -> subprocess.Popen[bytes]:
    """Start Chrome, detached, so it outlives the request that needed it.

    `port=None` opens an ordinary window with no debugging port — the only configuration in
    which Google will accept a sign-in.

    `profile_directory` picks which profile inside `data_dir` to open. Omitting it means
    `Default`, which is emphatically not always where the session lives; see
    `signed_in_profile`.
    """
    argv = [str(chrome_path()), f"--user-data-dir={data_dir}", "--no-first-run"]
    if profile_directory:
        argv.append(f"--profile-directory={profile_directory}")
    if port is not None:
        argv.append(f"--remote-debugging-port={port}")
    argv.append(start_url)

    kwargs: dict[str, object] = {}
    if sys.platform == "win32":
        # Without this the browser dies with the server, and a run that ends should not
        # take the user's window with it.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(argv, **kwargs)  # type: ignore[arg-type]


async def port_is_open(host: str, port: int, *, timeout: float = 0.5) -> bool:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


async def wait_for_port(host: str, port: int, *, timeout: float = 20.0) -> bool:
    """Poll until the debugging port accepts connections, or give up.

    Chrome takes a few seconds on a cold profile, so a single check after spawning would
    report failure almost every time.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await port_is_open(host, port):
            return True
        await asyncio.sleep(0.25)
    return False
