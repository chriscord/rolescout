from __future__ import annotations

import http.client
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from rolescout.paths import RoleScoutError
from rolescout.web import server as web_server
from rolescout.web.server import MAX_JSON_BODY, serve


def _request(url: str, *, token: str = "", host: str = "", origin: str = "",
             data: bytes | None = None):
    headers = {}
    if token:
        headers["X-RoleScout-Token"] = token
    if host:
        headers["Host"] = host
    if origin:
        headers["Origin"] = origin
    if data is not None:
        headers["Content-Type"] = "application/json"
    return urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=3)


def test_web_bootstraps_root_but_requires_api_token_and_rejects_host_origin_and_oversize():
    server = serve(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    try:
        # Direct navigation and old bookmarks must bootstrap a fresh session;
        # the generated HTML carries the token used by secureFetch.
        with _request(f"{base}/") as response:
            html = response.read().decode()
            assert server.session_token in html
            assert response.headers["X-Frame-Options"] == "DENY"
        with _request(f"{base}/?token={server.session_token}") as response:
            html = response.read().decode()
            assert server.session_token in html
            assert response.headers["X-Frame-Options"] == "DENY"
        try:
            _request(f"{base}/?token=wrong-token")
            assert False, "an explicitly wrong bootstrap token should be forbidden"
        except urllib.error.HTTPError as error:
            assert error.code == 403
        for path, kwargs in [
            ("/api/state", {}),
            ("/api/state", {"token": server.session_token, "host": "evil.example"}),
            ("/api/state", {"token": server.session_token,
                            "origin": "https://evil.example"}),
        ]:
            try:
                _request(base + path, **kwargs)
                assert False, "request should be forbidden"
            except urllib.error.HTTPError as error:
                assert error.code == 403
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        connection.putrequest("POST", "/api/run")
        connection.putheader("X-RoleScout-Token", server.session_token)
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(MAX_JSON_BODY + 1))
        connection.endheaders()
        assert connection.getresponse().status == 413
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_web_server_rejects_a_second_listener_on_the_same_port():
    first = serve(0)
    port = first.server_address[1]
    try:
        with pytest.raises(RoleScoutError, match="already in use"):
            serve(port)
    finally:
        first.server_close()


def test_web_search_run_does_not_queue_score(monkeypatch, tmp_path: Path):
    calls: list[str] = []

    def run_workflow(workflow: str, **kwargs):
        calls.append(workflow)
        return {"status": "ok", "summary": f"{workflow} complete"}

    monkeypatch.setattr(web_server.workflows, "run_workflow", run_workflow)
    manager = web_server.RunManager()
    entry = {
        "rid": "test", "workflow": "search", "task": "", "project": "",
        "mode": "live", "status": "running", "events": [], "summary": "",
        "_cancel": threading.Event(),
    }
    manager._execute(entry, False, tmp_path)
    assert calls == ["search"]
    assert entry["status"] == "done"
    assert entry["summary"] == "search complete"
