#!/usr/bin/env python3
"""
Upskill Coach — local screen observer agent.

Runs on YOUR laptop (not the server). Declares a study session to
the server, then captures the screen every INTERVAL seconds and
uploads it. The server summarizes each screenshot to text (images
are not retained) and feeds recent observations to the tutor
conversation as live context.

Usage:
    export UPSKILL_SECRET="<same value as CRON_SECRET on the server>"
    python3 observer.py                     # default 60s interval
    python3 observer.py --interval 30
    python3 observer.py --server https://other-host.example.com

Stop with Ctrl-C — the session is closed cleanly on exit.

Requirements: macOS only (uses `screencapture` + `sips`, both ship
with the OS). Python 3.9+ stdlib only — no pip install needed, no
venv needed. First run will trigger the macOS Screen Recording
permission prompt for your terminal app; grant it in System
Settings → Privacy & Security → Screen Recording, then re-run.

Privacy: macOS shows the system screen-recording indicator while
this runs. That's intentional and good — the tutor watching should
always be visible. Ctrl-C or closing the terminal stops everything.
"""

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

DEFAULT_SERVER = "https://upskill-coach-dmmu.onrender.com"
DEFAULT_INTERVAL = 60          # seconds between captures
MAX_LONG_SIDE = 1568           # vision-model sweet spot; also keeps upload small


def _post(url, data=None, content_type=None, timeout=60):
    """Tiny urllib wrapper. Returns parsed JSON dict."""
    req = urllib.request.Request(url, data=data, method="POST")
    if content_type:
        req.add_header("Content-Type", content_type)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def capture_screenshot(path):
    """Capture main display silently → downscale + JPEG in place."""
    subprocess.run(["screencapture", "-x", "-t", "png", path], check=True)
    # Resize longest side + convert to JPEG. sips ships with macOS.
    subprocess.run(
        ["sips", "-Z", str(MAX_LONG_SIDE), "-s", "format", "jpeg",
         path, "--out", path],
        check=True, capture_output=True,
    )


def main():
    parser = argparse.ArgumentParser(description="Upskill Coach screen observer")
    parser.add_argument("--server", default=os.environ.get("UPSKILL_SERVER", DEFAULT_SERVER))
    parser.add_argument("--interval", type=int,
                        default=int(os.environ.get("UPSKILL_INTERVAL", DEFAULT_INTERVAL)))
    args = parser.parse_args()

    secret = os.environ.get("UPSKILL_SECRET", "").strip()
    if not secret:
        print("❌ Set UPSKILL_SECRET first (same value as the server's CRON_SECRET).")
        sys.exit(1)

    if sys.platform != "darwin":
        print("❌ macOS only for now (uses `screencapture`).")
        sys.exit(1)

    base = args.server.rstrip("/")

    # Declare session
    try:
        resp = _post(f"{base}/observe/start?secret={secret}")
    except urllib.error.HTTPError as e:
        print(f"❌ Session start failed: HTTP {e.code} — {e.read().decode()[:200]}")
        sys.exit(1)
    session_id = resp["session_id"]
    print(f"👁  Observe session {session_id} started — capturing every {args.interval}s")
    print("    (Ctrl-C to stop; macOS will show the screen-recording indicator)")

    # Clean shutdown on Ctrl-C / SIGTERM
    def _shutdown(signum, frame):
        print("\n⏹  Ending session...")
        try:
            _post(f"{base}/observe/end?secret={secret}&session_id={session_id}",
                  timeout=15)
            print(f"✅ Session {session_id} closed.")
        except Exception as e:
            print(f"⚠️  Could not close session cleanly: {e}")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    last_hash = None
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()

    while True:
        try:
            capture_screenshot(tmp.name)
            with open(tmp.name, "rb") as f:
                img = f.read()

            # Skip upload if the screen hasn't visibly changed —
            # identical hash means identical pixels after downscale.
            # Saves vision-call cost during long static periods
            # (reading a page, thinking, away from keyboard).
            h = hashlib.sha256(img).hexdigest()
            if h == last_hash:
                print("…  (screen unchanged, skipping)")
            else:
                last_hash = h
                r = _post(
                    f"{base}/observe/capture?secret={secret}&session_id={session_id}",
                    data=img, content_type="image/jpeg",
                )
                print(f"📸 {time.strftime('%H:%M:%S')}  {r.get('summary', '')[:120]}")
        except subprocess.CalledProcessError:
            print("⚠️  screencapture failed — Screen Recording permission granted?")
        except urllib.error.HTTPError as e:
            print(f"⚠️  Upload failed: HTTP {e.code} — {e.read().decode()[:120]}")
        except Exception as e:
            print(f"⚠️  {type(e).__name__}: {e}")

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
