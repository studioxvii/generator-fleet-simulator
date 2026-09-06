import json
import urllib.error

from tools.launch_soak import run_soak


class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_soak_records_connection_failure_and_writes_json(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("tools.launch_soak.urllib.request.urlopen", boom)
    out = tmp_path / "soak.json"
    assert run_soak(base_url="http://127.0.0.1:5999", seconds=5, interval=0, json_out=str(out)) == 1
    payload = json.loads(out.read_text())
    assert payload["failed"] == 1
    assert payload["samples"][0]["ready_status"] == 0
    assert payload["samples"][0]["ready"] is not True
    assert "connection refused" in str(payload["samples"][0]["error"])


def test_soak_counts_http_not_ready_as_failure(tmp_path, monkeypatch):
    def fake_urlopen(request, timeout):
        url = request.full_url
        if url.endswith("/api/ready"):
            return _FakeResponse(503, json.dumps({"ready": False}))
        return _FakeResponse(200, json.dumps({"summary": {"generator_count": 50}, "metrics": {"avg_tick_ms": 1.2}}))

    monkeypatch.setattr("tools.launch_soak.urllib.request.urlopen", fake_urlopen)
    out = tmp_path / "soak.json"
    assert run_soak(base_url="http://example.test", seconds=1, interval=0, json_out=str(out)) == 1
    payload = json.loads(out.read_text())
    assert payload["failed"] == 1
    assert payload["samples"][0]["ready_status"] == 503
