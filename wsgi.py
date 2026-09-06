"""Production WSGI entry point for Gunicorn.

The simulator keeps runtime state in-process, so production deployments must
run a single worker process with a threaded worker class.
"""

from __future__ import annotations

import atexit
import logging
import os

from main import Settings, create_app


log = logging.getLogger(__name__)


def _reject_multi_worker() -> None:
    raw = os.environ.get("WEB_CONCURRENCY") or os.environ.get("GUNICORN_WORKERS")
    if raw is None or raw.strip() == "":
        return
    try:
        workers = int(raw)
    except ValueError as exc:
        raise SystemExit("WEB_CONCURRENCY / GUNICORN_WORKERS must be a whole number.") from exc
    if workers > 1:
        raise SystemExit(
            "Generator Fleet Simulator keeps state in one process. "
            "Run exactly one Gunicorn worker (unset WEB_CONCURRENCY / GUNICORN_WORKERS "
            "or set it to 1). Scale with separate instances, not extra workers."
        )


_reject_multi_worker()

settings = Settings()
app, socketio, controller = create_app(settings=settings)
application = app


def _save_state_on_exit() -> None:
    log.info("Saving generator state on WSGI worker exit")
    controller.save_state()


atexit.register(_save_state_on_exit)
