import importlib
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_wsgi_rejects_multiple_gunicorn_workers(monkeypatch, tmp_path):
    monkeypatch.setenv("ACCEPT_LICENSE", "yes")
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    monkeypatch.setenv("GENSIM_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("GENSIM_RUNBOOKS_FILE", str(tmp_path / "runbooks.json"))
    sys.modules.pop("wsgi", None)

    try:
        importlib.import_module("wsgi")
    except SystemExit as exc:
        assert "exactly one Gunicorn worker" in str(exc)
    else:
        raise AssertionError("expected SystemExit for WEB_CONCURRENCY=2")


def test_wsgi_entrypoint_builds_socketio_app_without_starting_runtime(monkeypatch, tmp_path):
    monkeypatch.delenv("ACCEPT_LICENSE", raising=False)
    monkeypatch.setenv("GENSIM_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("GENSIM_RUNBOOKS_FILE", str(tmp_path / "runbooks.json"))
    monkeypatch.setenv("GENSIM_WEB_PORT", "5510")
    monkeypatch.setenv("GENSIM_MODBUS_PORT", "5511")
    sys.modules.pop("wsgi", None)

    module = importlib.import_module("wsgi")

    assert module.application is module.app
    assert module.app.config["SIMULATOR_CONTROLLER"] is module.controller
    assert module.controller.is_started is False
    assert module.settings.state_file == tmp_path / "state.json"
    assert module.settings.runbooks_file == tmp_path / "runbooks.json"


def test_dockerfile_uses_gunicorn_wsgi_entrypoint():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "gunicorn --worker-class gthread --workers 1" in dockerfile
    assert "--threads ${GENSIM_GUNICORN_THREADS:-100}" in dockerfile
    assert "--bind ${GENSIM_WEB_HOST:-0.0.0.0}:${GENSIM_WEB_PORT:-5000}" in dockerfile
    assert "wsgi:app" in dockerfile
    assert 'CMD ["python", "main.pyc"]' not in dockerfile


def test_production_server_dependency_is_packaged():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    dockerignore = set((ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines())

    assert "gunicorn>=22,<24" in requirements
    assert '"gunicorn>=22,<24"' in pyproject
    assert "wsgi.py" not in dockerignore


def test_docker_publish_smokes_image_before_push():
    workflow = (ROOT / ".github" / "workflows" / "docker-publish.yml").read_text(encoding="utf-8")

    smoke_step = workflow.index("Run Docker release smoke")
    login_step = workflow.index("Log in to Docker Hub")
    push_step = workflow.index("Build and push")

    assert "docker build -t generator-fleet-simulator:publish-smoke ." in workflow
    assert "python tools/release_smoke.py" in workflow
    assert "--startup-modbus-port 5020" in workflow
    assert smoke_step < login_step < push_step


def test_launch_checklist_uses_container_modbus_port_for_docker_smoke():
    checklist = (ROOT / "LAUNCH_CHECKLIST.md").read_text(encoding="utf-8")

    assert "--base-url http://localhost:5001" in checklist
    assert "--modbus-port 5021" in checklist
    assert "--startup-modbus-port 5020" in checklist


def test_operations_guide_documents_compose_smoke_port_split():
    operations = (ROOT / "OPERATIONS.md").read_text(encoding="utf-8")

    assert "published host Modbus port is `5021`" in operations
    assert "app still binds `5020` inside the container" in operations
    assert "--base-url http://localhost:5001" in operations
    assert "--modbus-port 5021" in operations
    assert "--startup-modbus-port 5020" in operations
    assert "`127.0.0.1` local Python; `0.0.0.0` in Docker" in operations
    assert "`generator_state.json` local Python; `/data/generator_state.json` in Docker" in operations


def test_compose_sets_public_connection_hints_for_dashboard_help():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    demo_compose = (ROOT / "demo" / "docker-compose.yml").read_text(encoding="utf-8")

    for text in (compose, demo_compose):
        assert "GENSIM_PUBLIC_MODBUS_PORT" in text
        assert "5021" in text or "5030" in text
        assert "GENSIM_PUBLIC_WEB_HOST" not in text
        assert "GENSIM_PUBLIC_MODBUS_HOST" not in text
