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


def _get(url, timeout=30):
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def capture_screenshot(path, png=False):
    """Capture main display silently → downscale in place.

    Ambient captures convert to JPEG (small upload, gist is enough).
    On-demand captures stay PNG: JPEG artifacts smear small text and
    the deep vision tier is trying to transcribe code verbatim.
    """
    subprocess.run(["screencapture", "-x", "-t", "png", path], check=True)
    args = ["sips", "-Z", str(MAX_LONG_SIDE)]
    if not png:
        args += ["-s", "format", "jpeg"]
    subprocess.run(args + [path, "--out", path], check=True, capture_output=True)


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
    last_capture_at = 0.0
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()

    def do_capture(forced=False):
        """Capture + upload once. `forced` (on-demand from the tutor
        conversation) bypasses the unchanged-screen skip so a fresh
        observation row always lands for the waiting reply, uploads
        PNG (JPEG artifacts smear small code text), and tells the
        server to use the deep transcription tier."""
        nonlocal last_hash, last_capture_at
        capture_screenshot(tmp.name, png=forced)
        with open(tmp.name, "rb") as f:
            img = f.read()
        h = hashlib.sha256(img).hexdigest()
        if not forced and h == last_hash:
            print("…  (screen unchanged, skipping)")
            last_capture_at = time.time()
            return
        last_hash = h
        url = f"{base}/observe/capture?secret={secret}&session_id={session_id}"
        if forced:
            url += "&forced=1"
        r = _post(
            url, data=img,
            content_type="image/png" if forced else "image/jpeg",
        )
        last_capture_at = time.time()
        tag = "⚡" if forced else "📸"
        print(f"{tag} {time.strftime('%H:%M:%S')}  {r.get('summary', '')[:120]}")

    while True:
        try:
            due = time.time() - last_capture_at >= args.interval
            if due:
                do_capture()
                continue

            # Between timer captures we long-poll the server instead
            # of sleeping: if the user texts the tutor mid-session,
            # the server flips this poll to {"capture": true} and we
            # capture within a second instead of up to interval-s late.
            r = _get(f"{base}/observe/poll?secret={secret}")
            if r.get("capture"):
                do_capture(forced=True)
        except subprocess.CalledProcessError:
            print("⚠️  screencapture failed — Screen Recording permission granted?")
            time.sleep(5)
        except urllib.error.HTTPError as e:
            print(f"⚠️  Request failed: HTTP {e.code} — {e.read().decode()[:120]}")
            time.sleep(5)
        except Exception as e:
            print(f"⚠️  {type(e).__name__}: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
