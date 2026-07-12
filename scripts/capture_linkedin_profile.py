#!/usr/bin/env python3
"""Capture a LinkedIn profile into linkedin-current.md via local browser tools.

Order is intentional:
  1. Chrome/Chromium launched with the Chrome DevTools Protocol.
  2. Playwright, only if already installed.
  3. User-facing install/enable guide.

The user performs all login, captcha, and 2FA steps in the visible browser.
"""

from __future__ import annotations

import argparse
import base64
import functools
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Scroll the full profile surface so LinkedIn lazy-loads Experience, Skills, and
# Education, expand every "see more" toggle, then read <main> rather than body.
CAPTURE_JS = r"""
(async () => {
  const root = document.querySelector('main#workspace') || document.querySelector('main') || document.body;
  const candidates = [root, document.scrollingElement, document.documentElement, document.body]
    .filter(Boolean);
  const scroller = candidates.reduce((best, item) =>
    ((item.scrollHeight - item.clientHeight) > (best.scrollHeight - best.clientHeight) ? item : best),
    candidates[0]);
  try {
    let previousHeight = 0;
    let stablePasses = 0;
    for (let pass = 0; pass < 40; pass++) {
      for (const b of Array.from(root.querySelectorAll('button, a[role=button]'))) {
        const t = ((b.innerText || b.getAttribute('aria-label') || "")).toLowerCase();
        if (/(see|show|…|\.\.\.)\s*more\b/.test(t) && !/less/.test(t)) {
          try { b.click(); } catch (e) {}
        }
      }
      const height = scroller.scrollHeight;
      const step = Math.max(scroller.clientHeight * 0.8, 650);
      scroller.scrollTop = Math.min(height, (pass + 1) * step);
      await new Promise(r => setTimeout(r, 350));
      const atBottom = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 80;
      stablePasses = (height === previousHeight && atBottom) ? stablePasses + 1 : 0;
      if (pass > 5 && stablePasses >= 5) break;
      previousHeight = height;
    }
    await new Promise(r => setTimeout(r, 350));
  } catch (e) {}
  let contentRoot = root;
  const detailMatch = location.pathname.match(/\/details\/(experience|skills|education)\/?/i);
  if (detailMatch) {
    const label = detailMatch[1].toLowerCase();
    const exact = Array.from(root.querySelectorAll('h1,h2,h3,p,div'))
      .filter(e => ((e.innerText || "").trim().toLowerCase() === label))
      .sort((a, b) => (a.innerText || "").length - (b.innerText || "").length);
    const section = exact.length ? exact[0].closest('section') : null;
    if (section && (section.innerText || "").trim().length > 100) contentRoot = section;
  }
  const text = ((contentRoot && contentRoot.innerText) || "")
    .replace(/\r/g, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
  return JSON.stringify({
    url: location.href,
    title: document.title || "",
    text
  });
})()
"""


def home_dir() -> Path:
    p = Path(os.environ.get("ROLESCOUT_HOME", Path.home() / ".rolescout")).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


@functools.lru_cache(maxsize=1)
def playwright_chromium() -> str:
    if not has_playwright():
        return ""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            path = str(p.chromium.executable_path or "")
            return path if path and Path(path).exists() else ""
    except Exception:
        return ""


@functools.lru_cache(maxsize=1)
def find_chrome() -> str:
    for env in ("ROLESCOUT_CHROME", "CHROME_BIN", "GOOGLE_CHROME_SHIM"):
        val = os.environ.get(env)
        if val and Path(val).exists():
            return val
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    program_files = Path(os.environ.get("PROGRAMFILES", ""))
    program_files_x86 = Path(os.environ.get("PROGRAMFILES(X86)", ""))
    # Prefer Playwright's Chromium when present: it can use the same persistent
    # profile as the Playwright fallback, so an authenticated session is not
    # split between two otherwise independent browser profiles.
    candidates = [
        playwright_chromium(),
        program_files / "Google/Chrome/Application/chrome.exe",
        program_files_x86 / "Google/Chrome/Application/chrome.exe",
        local / "Google/Chrome/Application/chrome.exe",
        program_files / "Microsoft/Edge/Application/msedge.exe",
        program_files_x86 / "Microsoft/Edge/Application/msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("chrome"),
        shutil.which("msedge"),
    ]
    for c in candidates:
        if c and Path(c).exists():
            return str(c)
    return ""


def browser_session_dir() -> Path:
    override = os.environ.get("ROLESCOUT_LINKEDIN_BROWSER_PROFILE", "").strip()
    if override:
        path = Path(override).expanduser()
    else:
        root = home_dir() / "browser"
        shared = root / "linkedin-session"
        legacy_playwright = root / "linkedin-playwright"
        legacy_cdp = root / "linkedin-chrome-devtools"
        # Preserve an existing authenticated installation while converging all
        # browser backends on one session directory.
        if shared.exists():
            path = shared
        elif legacy_playwright.exists():
            path = legacy_playwright
        elif legacy_cdp.exists():
            path = legacy_cdp
        else:
            path = shared
    path.mkdir(parents=True, exist_ok=True)
    return path


def has_playwright() -> bool:
    try:
        return importlib.util.find_spec("playwright.sync_api") is not None
    except ModuleNotFoundError:
        return False


def choose_backend() -> str:
    if find_chrome():
        return "chrome-devtools"
    if has_playwright():
        return "playwright"
    return "missing"


def missing_backend_message() -> str:
    return (
        "APPROVAL_REQUIRED: LinkedIn browser capture helper unavailable - "
        "install or enable Google Chrome/Chromium for Chrome DevTools Protocol, "
        "or install Playwright with browser binaries, then rerun prep-linkedin."
    )


def login_needed_message(backend: str) -> str:
    return (
        "APPROVAL_REQUIRED: LinkedIn login needed - complete login in the opened "
        f"{backend} browser, make sure the LinkedIn profile page loads, then "
        "rerun prep-linkedin."
    )


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def http_json(port: int, path: str, method: str = "GET") -> dict | list:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_for_devtools(port: int, timeout_s: int = 20) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            http_json(port, "/json/version")
            return True
        except Exception:
            time.sleep(0.25)
    return False


def recv_exact(sock: socket.socket, n: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        chunk = sock.recv(n - len(out))
        if not chunk:
            raise ConnectionError("websocket closed")
        out.extend(chunk)
    return bytes(out)


class CDPClient:
    def __init__(self, ws_url: str):
        self.url = urllib.parse.urlparse(ws_url)
        self.sock: socket.socket | None = None
        self.next_id = 1

    def connect(self) -> None:
        if self.url.scheme != "ws":
            raise ValueError(f"unsupported CDP websocket URL: {self.url.scheme}")
        port = self.url.port or 80
        sock = socket.create_connection((self.url.hostname or "127.0.0.1", port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = self.url.path + (("?" + self.url.query) if self.url.query else "")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {self.url.hostname}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(req.encode("ascii"))
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += sock.recv(4096)
        if b" 101 " not in resp.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"CDP websocket handshake failed: {resp[:120]!r}")
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        )
        if accept not in resp:
            raise ConnectionError("CDP websocket accept key mismatch")
        sock.settimeout(10)
        self.sock = sock

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def send_frame(self, opcode: int, payload: bytes) -> None:
        assert self.sock is not None
        first = 0x80 | opcode
        n = len(payload)
        if n < 126:
            header = bytes([first, 0x80 | n])
        elif n < 65536:
            header = bytes([first, 0x80 | 126]) + struct.pack("!H", n)
        else:
            header = bytes([first, 0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def recv_message(self) -> str:
        assert self.sock is not None
        chunks: list[bytes] = []
        while True:
            b1, b2 = recv_exact(self.sock, 2)
            fin = bool(b1 & 0x80)
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", recv_exact(self.sock, 2))[0]
            elif n == 127:
                n = struct.unpack("!Q", recv_exact(self.sock, 8))[0]
            mask = recv_exact(self.sock, 4) if masked else b""
            payload = recv_exact(self.sock, n)
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 8:
                raise ConnectionError("CDP websocket closed")
            if opcode == 9:
                self.send_frame(10, payload)
                continue
            if opcode in (1, 0):
                chunks.append(payload)
                if fin:
                    return b"".join(chunks).decode("utf-8", errors="replace")

    def call(self, method: str, params: dict | None = None, timeout_s: int = 20) -> dict:
        msg_id = self.next_id
        self.next_id += 1
        self.send_frame(1, json.dumps({
            "id": msg_id, "method": method, "params": params or {}
        }).encode("utf-8"))
        deadline = time.monotonic() + timeout_s
        assert self.sock is not None
        old_timeout = self.sock.gettimeout()
        self.sock.settimeout(2)
        try:
            while time.monotonic() < deadline:
                try:
                    msg = json.loads(self.recv_message())
                except socket.timeout:
                    continue
                if msg.get("id") == msg_id:
                    if "error" in msg:
                        raise RuntimeError(msg["error"])
                    return msg
        finally:
            self.sock.settimeout(old_timeout)
        raise TimeoutError(f"CDP call timed out: {method}")


def evaluate_page(client: CDPClient) -> dict:
    res = client.call("Runtime.evaluate", {
        "expression": CAPTURE_JS,
        "returnByValue": True,
        "awaitPromise": True,
    })
    value = res.get("result", {}).get("result", {}).get("value", "{}")
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {"url": "", "title": "", "text": str(value)}


def useful_profile(payload: dict) -> bool:
    url = (payload.get("url") or "").lower()
    text = (payload.get("text") or "").strip()
    lower = text.lower()
    if "/login" in url or "checkpoint" in url:
        return False
    if "linkedin.com/in/" not in url:
        return False
    if len(text) < 300:
        return False
    bad = ("sign in to linkedin", "join linkedin", "security verification", "captcha")
    if any(marker in lower for marker in bad):
        return False
    # A top-card/footer-only capture is not a usable current LinkedIn source.
    # About is optional, but the three structured recruiter surfaces must load.
    required = ("experience", "skills", "education")
    return all(re.search(rf"(?:^|\n)\s*{marker}(?:\s*\(\d+\))?\s*(?:\n|$)", lower)
               for marker in required)


def authenticated_linkedin(payload: dict) -> bool:
    url = (payload.get("url") or "").lower()
    text = (payload.get("text") or "").strip().lower()
    if "linkedin.com" not in url or "/login" in url or "checkpoint" in url:
        return False
    if len(text) < 200:
        return False
    return not any(marker in text[:1200] for marker in (
        "sign in to linkedin", "join linkedin", "security verification", "captcha"
    ))


def profile_surface_urls(url: str) -> list[tuple[str, str]]:
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path.rstrip("/")
    base = urllib.parse.urlunsplit((parsed.scheme or "https", parsed.netloc, path, "", ""))
    return [
        ("Profile", base + "/"),
        ("Experience", base + "/details/experience/"),
        ("Skills", base + "/details/skills/"),
        ("Education", base + "/details/education/"),
    ]


def surface_ready(payload: dict, label: str) -> bool:
    if not authenticated_linkedin(payload):
        return False
    text = str(payload.get("text", ""))
    if label == "Profile":
        return "linkedin.com/in/" in str(payload.get("url", "")).lower()
    return bool(re.search(
        rf"(?:^|\n)\s*{re.escape(label)}(?:\s*\(\d+\))?\s*(?:\n|$)",
        text,
        re.I,
    ))


def combined_surface_text(surfaces: list[tuple[str, dict]]) -> str:
    return "\n\n".join(
        f"### {label} surface\n\n{str(payload.get('text', '')).strip()}"
        for label, payload in surfaces
    ).strip()


def should_navigate_to_profile(payload: dict) -> bool:
    url = (payload.get("url") or "").lower()
    text = (payload.get("text") or "").lower()
    if "linkedin.com/in/" in url or "/login" in url or "checkpoint" in url:
        return False
    if "linkedin.com" not in url:
        return False
    return "sign in" not in text[:500]


def write_capture(out: Path, method: str, source_url: str, title: str,
                  current_url: str, text: str) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = (
        f"# LinkedIn current profile source\n\n"
        f"- captured at: {stamp}\n"
        f"- source URL: {source_url}\n"
        f"- current URL: {current_url}\n"
        f"- page title: {title}\n"
        f"- capture method: {method}\n\n"
        "## Visible LinkedIn Profile Text\n\n"
        f"{text.strip()}\n"
    )
    out.write_text(body, encoding="utf-8")


def target_from_list(port: int, url: str) -> str:
    targets = http_json(port, "/json/list")
    assert isinstance(targets, list)
    for t in targets:
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
            if url.rstrip("/") in (t.get("url") or "") or "linkedin.com" in (t.get("url") or ""):
                return t["webSocketDebuggerUrl"]
    for t in targets:
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
            return t["webSocketDebuggerUrl"]
    quoted = urllib.parse.quote(url, safe="")
    created = http_json(port, f"/json/new?{quoted}", method="PUT")
    assert isinstance(created, dict)
    return created["webSocketDebuggerUrl"]


def run_chrome_devtools(url: str, out: Path, timeout_s: int) -> int:
    chrome = find_chrome()
    if not chrome:
        return 10
    user_data = browser_session_dir()
    port = free_port()
    cmd = [
        chrome,
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        f"--user-data-dir={user_data}",
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ]
    print(f"Chrome DevTools using shared browser session: {user_data}", flush=True)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    client: CDPClient | None = None
    try:
        if not wait_for_devtools(port):
            print("Chrome DevTools endpoint did not become ready.", flush=True)
            return 10
        ws_url = target_from_list(port, url)
        client = CDPClient(ws_url)
        client.connect()
        try:
            client.call("Runtime.enable")
            print(
                "USER_ACTION_REQUIRED: Chrome DevTools browser opened. "
                "Sign in to LinkedIn there if prompted; this helper will capture "
                "the profile after the target page is visible.",
                flush=True,
            )
            deadline = time.monotonic() + timeout_s
            surfaces: list[tuple[str, dict]] = []
            for index, (label, surface_url) in enumerate(profile_surface_urls(url)):
                client.call("Page.navigate", {"url": surface_url})
                time.sleep(2)
                surface_deadline = deadline if index == 0 else min(deadline, time.monotonic() + 45)
                payload: dict = {}
                while time.monotonic() < surface_deadline:
                    payload = evaluate_page(client)
                    if surface_ready(payload, label):
                        break
                    time.sleep(2)
                if not surface_ready(payload, label):
                    print(f"Chrome DevTools did not load the {label} surface.", flush=True)
                    return 3 if not authenticated_linkedin(payload) else 10
                surfaces.append((label, payload))
            text = combined_surface_text(surfaces)
            combined = {"url": url, "text": text}
            if not useful_profile(combined):
                print("Chrome DevTools capture was incomplete after all profile surfaces.",
                      flush=True)
                return 10
            first = surfaces[0][1]
            write_capture(out, "chrome-devtools", url,
                          first.get("title", ""), first.get("url", ""), text)
            print(f"captured LinkedIn profile source -> {out}", flush=True)
            return 0
        finally:
            try:
                client.call("Browser.close", timeout_s=5)
            except Exception:
                pass
            client.close()
    except Exception as exc:
        print(f"Chrome DevTools capture failed: {type(exc).__name__}: {exc}", flush=True)
        return 10
    finally:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    print(login_needed_message("Chrome DevTools"), flush=True)
    return 3


def run_playwright(url: str, out: Path, timeout_s: int) -> int:
    if not has_playwright():
        return 10
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return 10
    user_data = browser_session_dir()
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(str(user_data), headless=False)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            print(f"Playwright using shared browser session: {user_data}", flush=True)
            print(
                "USER_ACTION_REQUIRED: Playwright browser opened. Sign in to "
                "LinkedIn there if prompted; this helper will capture the profile "
                "after the target page is visible.",
                flush=True,
            )
            deadline = time.monotonic() + timeout_s
            surfaces: list[tuple[str, dict]] = []
            for index, (label, surface_url) in enumerate(profile_surface_urls(url)):
                page.goto(surface_url, wait_until="domcontentloaded", timeout=60000)
                surface_deadline = deadline if index == 0 else min(deadline, time.monotonic() + 45)
                payload: dict = {}
                while time.monotonic() < surface_deadline:
                    try:
                        raw = page.evaluate(CAPTURE_JS)
                        payload = json.loads(raw) if isinstance(raw, str) else raw
                    except Exception:
                        payload = {"url": page.url, "title": page.title(), "text": ""}
                    if surface_ready(payload, label):
                        break
                    time.sleep(2)
                if not surface_ready(payload, label):
                    print(f"Playwright did not load the {label} surface.", flush=True)
                    ctx.close()
                    return 3 if not authenticated_linkedin(payload) else 10
                surfaces.append((label, payload))
            text = combined_surface_text(surfaces)
            if not useful_profile({"url": url, "text": text}):
                print("Playwright capture was incomplete after all profile surfaces.", flush=True)
                ctx.close()
                return 10
            first = surfaces[0][1]
            write_capture(out, "playwright", url, first.get("title", ""),
                          first.get("url", ""), text)
            print(f"captured LinkedIn profile source -> {out}", flush=True)
            ctx.close()
            return 0
            ctx.close()
    except Exception as exc:
        print(f"Playwright capture failed: {exc}", flush=True)
        return 10
    print(login_needed_message("Playwright"), flush=True)
    return 3


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="candidate LinkedIn profile URL")
    ap.add_argument("--out", help="output linkedin-current.md path")
    ap.add_argument("--timeout", type=int, default=300,
                    help="seconds to wait for user login/profile load")
    ap.add_argument("--check-tools", action="store_true",
                    help="print detected backend and exit")
    args = ap.parse_args(argv)

    selected = choose_backend()
    if args.check_tools:
        print(json.dumps({
            "selected": selected,
            "chrome": bool(find_chrome()),
            "chrome_path": find_chrome(),
            "playwright": has_playwright(),
        }, indent=2))
        return 0 if selected != "missing" else 2

    if not args.url or not args.out:
        ap.error("--url and --out are required unless --check-tools is used")

    out = Path(args.out)
    if selected == "missing":
        print(missing_backend_message(), flush=True)
        return 2

    if selected == "chrome-devtools":
        rc = run_chrome_devtools(args.url, out, args.timeout)
        if rc == 0 or rc == 3:
            return rc
        if has_playwright():
            print("Chrome DevTools capture unavailable; falling back to Playwright.", flush=True)
            return run_playwright(args.url, out, args.timeout)
        print(missing_backend_message(), flush=True)
        return 2

    return run_playwright(args.url, out, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
