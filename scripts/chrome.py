"""Open the Chrome the agent attaches to - using an account you are ALREADY signed into.

    python scripts/chrome.py list      # your Chrome profiles and their accounts
    python scripts/chrome.py serve     # open one with a debugging port, for the agent

**Why this never asks you to sign in.** Google refuses its sign-in flow in two situations
that both apply here: browsers without Google's API keys (Playwright's bundled "Chrome for
Testing"), and browsers started with remote debugging enabled. The message is the same
either way - *"Couldn't sign you in / This browser or app may not be secure."* Signing in
under a debugging port does not work, and no flag makes it work.

So we do not sign in. We open the profile where you signed in **already**, through ordinary
Chrome, days or months ago. Those cookies are sitting in the profile; the agent attaches to
a session that was authenticated long before automation was involved, and Google never sees
a login attempt at all.

**One caveat worth knowing.** While `serve` is running, anything else on your machine can
connect to that debugging port and drive the browser, including reading the cookies of
whatever is signed in. The port is local-only and open only while you leave the window up.
If you would rather not expose your everyday profile, `--isolated` uses a separate one; you
sign into that once, in an ordinary window, with `signin`.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

#: Real Chrome. Playwright's Chromium is deliberately not a candidate - it is a "Chrome for
#: Testing" build, which is exactly what Google rejects.
WINDOWS_CHROME = (
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
    / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("LOCALAPPDATA", ".")) / "Google/Chrome/Application/chrome.exe",
)
MAC_CHROME = (Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),)
LINUX_CHROME = (Path("/usr/bin/google-chrome"), Path("/usr/bin/google-chrome-stable"))

ISOLATED_PROFILE = Path.home() / ".inbox-agent-profile"
DEFAULT_PORT = 9222


def find_chrome() -> Path:
    if sys.platform == "win32":
        candidates = WINDOWS_CHROME
    elif sys.platform == "darwin":
        candidates = MAC_CHROME
    else:
        candidates = LINUX_CHROME
    for path in candidates:
        if path.exists():
            return path
    raise SystemExit(
        "Could not find Google Chrome. Install it from https://google.com/chrome - "
        "Playwright's bundled Chromium will not work, because Google blocks sign-in on "
        "Chrome for Testing builds."
    )


def user_data_dir() -> Path:
    """Where your everyday Chrome keeps its profiles."""
    if sys.platform == "win32":
        return Path(os.environ["LOCALAPPDATA"]) / "Google/Chrome/User Data"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Google/Chrome"
    return Path.home() / ".config/google-chrome"


def profiles() -> list[tuple[str, str, str]]:
    """`(directory, display name, account email)` per profile, signed-in ones first.

    Read from Chrome's own `Local State`, which is the only thing that knows the mapping
    from a directory called "Profile 5" to the account living inside it.
    """
    state = user_data_dir() / "Local State"
    if not state.exists():
        return []
    try:
        cache = json.loads(state.read_text(encoding="utf-8", errors="replace"))
        cache = cache.get("profile", {}).get("info_cache", {})
    except (json.JSONDecodeError, OSError):
        return []

    found = [
        (directory, info.get("name") or directory, info.get("user_name") or "")
        for directory, info in cache.items()
    ]
    # Signed-in profiles first: they are the only ones that can reach a mailbox.
    return sorted(found, key=lambda row: (not row[2], row[0]))


def chrome_is_running() -> bool:
    if sys.platform != "win32":
        return False
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
        capture_output=True,
        text=True,
        check=False,
    )
    return "chrome.exe" in result.stdout


def cmd_list() -> None:
    rows = profiles()
    if not rows:
        print("No Chrome profiles found at", user_data_dir())
        return
    print("Chrome profiles in", user_data_dir(), "\n")
    for directory, name, email in rows:
        who = email or "(not signed into Google)"
        print(f"  {directory:12}  {name:28}  {who}")
    print('\nUse one with:  python scripts/chrome.py serve --profile-directory "Profile 5"')


def pick_profile() -> str:
    """Choose a signed-in profile, or say which ones exist."""
    signed_in = [row for row in profiles() if row[2]]
    if len(signed_in) == 1:
        directory, name, email = signed_in[0]
        print(f"Using your only signed-in profile: {name} <{email}>")
        return directory
    if not signed_in:
        raise SystemExit(
            "No Chrome profile is signed into Google. Sign into Gmail in ordinary Chrome "
            "first, then run this again."
        )
    print("Several profiles are signed in:\n")
    for directory, name, email in signed_in:
        print(f"  {directory:12}  {name:28}  {email}")
    raise SystemExit(
        '\nPick one:  python scripts/chrome.py serve --profile-directory "Profile 5"'
    )


def cmd_serve(args: argparse.Namespace) -> None:
    chrome = find_chrome()

    if args.isolated:
        data_dir: Path = ISOLATED_PROFILE
        profile_dir: str | None = None
    else:
        data_dir = user_data_dir()
        profile_dir = args.profile_directory or pick_profile()

        # Chrome silently ignores --remote-debugging-port when an instance already owns the
        # profile: it opens a tab in the running browser and no port ever listens. That looks
        # exactly like the flag not working, so catch it here rather than let the agent fail
        # to connect later with nothing to point at.
        if chrome_is_running():
            raise SystemExit(
                "Chrome is already running, and it will ignore the debugging port while it "
                "is.\nClose every Chrome window (check the system tray too), then run this "
                "again."
            )

    argv = [
        str(chrome),
        f"--user-data-dir={data_dir}",
        f"--remote-debugging-port={args.port}",
    ]
    if profile_dir:
        argv.append(f"--profile-directory={profile_dir}")
    argv += ["--no-first-run", "https://mail.google.com"]

    print(f"Chrome:  {chrome}")
    print(f"Profile: {data_dir}" + (f" / {profile_dir}" if profile_dir else ""))
    print(f"\nPut this in backend/.env:\n    CDP_ENDPOINT=http://127.0.0.1:{args.port}")
    print("\nLeave this window open while you use the agent.")
    subprocess.run(argv, check=False)


def cmd_signin() -> None:
    """Only for --isolated: an ordinary window, no port, so Google accepts the login."""
    chrome = find_chrome()
    argv = [
        str(chrome),
        f"--user-data-dir={ISOLATED_PROFILE}",
        "--no-first-run",
        "https://mail.google.com",
    ]
    print(f"Profile: {ISOLATED_PROFILE}")
    print("\nNo debugging port is open, so Google will accept the sign-in.")
    print("Sign in, close Chrome completely, then run:")
    print("    python scripts/chrome.py serve --isolated")
    subprocess.run(argv, check=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Open Chrome for the inbox agent.")
    sub = parser.add_subparsers(dest="mode", required=True)

    sub.add_parser("list", help="show your Chrome profiles and their accounts")

    serve = sub.add_parser("serve", help="open a signed-in profile with a debugging port")
    serve.add_argument("--profile-directory", help='e.g. "Profile 5" (see `list`)')
    serve.add_argument(
        "--isolated",
        action="store_true",
        help="use a separate profile instead of your everyday one",
    )
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)

    sub.add_parser("signin", help="sign into the --isolated profile (ordinary window)")

    args = parser.parse_args()
    if args.mode == "list":
        cmd_list()
    elif args.mode == "serve":
        cmd_serve(args)
    else:
        cmd_signin()


if __name__ == "__main__":
    main()
