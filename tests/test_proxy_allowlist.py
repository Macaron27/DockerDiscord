"""The socket-proxy allowlist in docker-compose.yml must admit exactly what docker-py sends for our calls."""

import re
from pathlib import Path
from urllib.parse import urlsplit

import docker
import requests
from requests.adapters import BaseAdapter

COMPOSE = Path(__file__).parent.parent / "docker-compose.yml"


def allowlist() -> dict[str, re.Pattern[str]]:
    rules = dict(re.findall(r"-allow(GET|POST)=([^'\"]+)", COMPOSE.read_text()))
    return {method: re.compile(f"^{pattern}$") for method, pattern in rules.items()}  # proxy auto-anchors


class Recorder(BaseAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.seen: list[tuple[str, str]] = []

    def send(self, request: requests.PreparedRequest, **_: object) -> requests.Response:
        path = urlsplit(request.url or "").path
        self.seen.append((request.method or "", path))
        response = requests.Response()
        response.status_code = 200
        response._content = b"{}" if path.endswith(("/stats", "/version")) else b""
        if path.endswith("/json"):  # list, or inspect (logs() inspects first to check for a TTY)
            response._content = b"[]" if path.endswith("/containers/json") else b'{"Config": {"Tty": false}}'
        response.headers["Content-Type"] = "application/json"
        response.request = request
        return response

    def close(self) -> None:
        pass


def calls_made_by_the_bot() -> list[tuple[str, str]]:
    api = docker.APIClient(base_url="tcp://127.0.0.1:2375", version="1.45")
    recorder = Recorder()
    api.mount("http://", recorder)
    api.version(api_version=False)  # what version="auto" does at startup
    api.containers(all=True)
    api.inspect_container("my-app.1")
    api.logs("my-app.1", stdout=True, stderr=True, tail=30)
    api.stats("my-app.1", stream=False, one_shot=True)
    api.restart("my-app.1", timeout=10)
    api.stop("my-app.1", timeout=10)
    api.events(decode=True, filters={"type": ["container"]})
    return recorder.seen


def allowed(method: str, path: str) -> bool:
    rule = allowlist().get(method)
    return bool(rule and rule.match(path))


def test_every_bot_call_passes_the_proxy() -> None:
    seen = calls_made_by_the_bot()
    assert len(seen) == 9  # logs() adds an inspect
    assert [(m, p) for m, p in seen if not allowed(m, p)] == []


def test_dangerous_endpoints_are_refused() -> None:
    for method, path in [
        ("POST", "/v1.45/containers/create"),
        ("POST", "/v1.45/containers/x/start"),
        ("POST", "/v1.45/containers/x/exec"),
        ("POST", "/v1.45/containers/x/kill"),
        ("POST", "/v1.45/exec/abc/start"),
        ("GET", "/v1.45/containers/x/archive"),
        ("GET", "/v1.45/containers/x/export"),
        ("GET", "/v1.45/images/json"),
        ("GET", "/v1.45/info"),
        ("GET", "/v1.45/secrets"),
        ("DELETE", "/v1.45/containers/x"),
        ("POST", "/v1.45/containers/../restart"),
    ]:
        assert not allowed(method, path), (method, path)
