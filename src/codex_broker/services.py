from __future__ import annotations

import shutil
import signal
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from typing import Any

from .app_server import AppServerPool
from .auth import AuthManager
from .bundles import BundleRegistry
from .config import BrokerConfig
from .scheduler import TurnScheduler
from .state import StateStore
from .util import ensure_dir


@dataclass
class BrokerServices:
    config: BrokerConfig
    state: StateStore
    auth: AuthManager
    bundles: BundleRegistry
    pool: AppServerPool
    scheduler: TurnScheduler

    @classmethod
    def build(cls, config: BrokerConfig) -> "BrokerServices":
        for path in (config.data_dir, config.auth_root, config.inline_bundle_root, config.overlay_root):
            ensure_dir(path)
        state = StateStore(config.state_db_path)
        recovered_turns = state.recover_incomplete_turns("Broker restarted before the turn completed.")
        state.recover_pending_interactions()
        for child in config.overlay_root.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)
        pruned_raw_events = 0
        if config.raw_event_retention_seconds > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=config.raw_event_retention_seconds)
            pruned_raw_events = state.prune_raw_events_before(cutoff.isoformat().replace("+00:00", "Z"))
        if config.history_retention_seconds > 0:
            history_cutoff = datetime.now(timezone.utc) - timedelta(seconds=config.history_retention_seconds)
            state.prune_history_before(history_cutoff.isoformat().replace("+00:00", "Z"))
        state.prune_excess_events(config.max_events_per_turn)
        auth = AuthManager(config, state)
        bundles = BundleRegistry(config, state)
        pool = AppServerPool(config, state)
        scheduler = TurnScheduler(config=config, state=state, auth=auth, bundles=bundles, pool=pool)
        scheduler.note_recovered_turns(recovered_turns)
        scheduler.note_pruned_raw_events(pruned_raw_events)
        return cls(config=config, state=state, auth=auth, bundles=bundles, pool=pool, scheduler=scheduler)


class BrokerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


def serve(config: BrokerConfig) -> None:
    from .http_api import BrokerHandler

    services = BrokerServices.build(config)

    class Handler(BrokerHandler):
        broker = services

    server = BrokerHTTPServer((config.host, config.port), Handler)
    shutdown_started = threading.Event()

    def request_shutdown(signum: int, frame: Any) -> None:
        if shutdown_started.is_set():
            return
        shutdown_started.set()
        threading.Thread(target=server.shutdown, name="broker-http-shutdown", daemon=True).start()

    previous_handlers: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_shutdown)
    try:
        server.serve_forever()
    finally:
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)
        server.server_close()
        services.scheduler.shutdown(config.shutdown_mode, config.shutdown_drain_timeout_seconds)
        services.pool.close_all()
        services.state.close()
