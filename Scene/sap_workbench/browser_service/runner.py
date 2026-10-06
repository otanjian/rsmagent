"""Host the screen gateway beside the WSGI app.

The Web channel is WSGI and cannot upgrade a WebSocket, so -- exactly like
``integrations/desktop/local_gateway.py`` does for the device gateway -- the
browser gateway runs in a daemon thread with its own asyncio loop, bound to an
ephemeral ``127.0.0.1`` port. The pane learns that port from the scene endpoint
that also hands it the view token.
"""
from __future__ import annotations

import asyncio
import atexit
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import suppress
import os
import threading
from threading import current_thread
import time
import weakref

from common.log import logger

from .gateway import build_app
from .manager import BrowserNodeManager, node_max_sessions
from .node import BrowserNode
from .state import view_tokens

SHUTDOWN_GRACE_SECONDS = 46
SHUTDOWN_LOOP_DRAIN_SECONDS = 2
SHUTDOWN_WAIT_SECONDS = 55


def _chrome_executable() -> str:
    from ..backend.environment import chrome_executable
    return chrome_executable()


def _profile_root() -> str:
    from config import get_data_root

    return os.path.join(get_data_root(), "scenes", "sap_workbench_browser")


def _env_flag(name, default=False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


class BrowserGatewayRunner:
    """Lazily started, idempotent host for the screen WebSocket."""

    def __init__(self, *, executable=None, profile_root=None, allowed_origins=None):
        self.port: int | None = None
        self.proxy_port: int | None = None
        self._executable = executable
        self._profile_root = profile_root
        self._allowed_origins = allowed_origins
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopped: asyncio.Event | None = None
        self._ready: threading.Event | None = None
        self._failure: list = []
        self._lock = threading.Lock()
        self.manager = None
        self.runtimes = {}
        self.session_locks = weakref.WeakValueDictionary()
        self.session_requests = {}
        self._nodes = weakref.WeakSet()
        self._children_lock = threading.Lock()
        self._owned_child_handles = []
        self._closing_runtimes = []
        self._stop_requested = threading.Event()
        self._exit_registered = False
        self._exit_pid = None
        self._exit_closing = False

    def start(self) -> int:
        """Return the loopback port, starting the gateway on first use."""
        with self._lock:
            if self._exit_closing or (self._thread and self._thread.is_alive() and self._stop_requested.is_set()):
                raise RuntimeError('screen gateway is stopping')
            if self._thread and self._thread.is_alive():
                if self.port:
                    return self.port
                # Another WSGI request may have entered while the first thread
                # is still binding its socket. Converge on that startup rather
                # than replacing the shared loop/stop state with a second one.
                ready, failure = self._ready, self._failure
            else:
                ready, failure = self._start_thread()
        if not ready.wait(10):
            self.stop()
            raise RuntimeError("screen gateway startup timed out")
        if failure or self.port is None:
            raise RuntimeError("screen gateway startup failed") from (failure[0] if failure else None)
        with self._lock:
            if self._stop_requested.is_set():
                raise RuntimeError('screen gateway is stopping')
            if not self._exit_registered:
                # Import and invalid deployment settings register nothing.
                # The app's existing sys.exit signal path runs normal atexit.
                atexit.register(self._exit_stop)
                self._exit_registered = True
                self._exit_pid = os.getpid()
        return self.port

    def _start_thread(self):
        # Validate the scene deployment setting before starting the gateway
        # thread or resolving a profile root. It never comes from view claims.
        max_nodes = node_max_sessions()
        # Remote display is a deployment setting too: an unusable public base or
        # port must fail here, before a listener exists, rather than serve a
        # half-open entry. Unset means loopback only, which is the default.
        from ..backend import environment
        public_base_value = environment.public_base()
        proxy_port_value = environment.proxy_port() if public_base_value else None
        # A completed explicit stop may be followed by a normal lazy restart.
        # Exact living handles have their own registry; do not retain closed
        # runtime/controller/config objects across successive gateway loops.
        self._closing_runtimes = []
        self.session_locks = weakref.WeakValueDictionary()
        self.session_requests = {}
        self._stop_requested.clear()
        self._loop = None
        self._stopped = None
        ready = threading.Event()
        failure: list = []
        self._ready, self._failure = ready, failure

        async def serve():
            from aiohttp import web

            self._loop = asyncio.get_running_loop()
            self._stopped = asyncio.Event()
            manager = BrowserNodeManager(
                executable=self._executable or _chrome_executable(),
                profile_root=self._profile_root or _profile_root(),
                headless=_env_flag("SAP_WORKBENCH_HEADLESS", True),
                max_nodes=max_nodes,
                factory=self._new_node,
            )
            self.manager = manager
            app = build_app(tokens=view_tokens, node_factory=manager.acquire,
                            allowed_origins=self._allowed_origins)
            runner = web.AppRunner(app, shutdown_timeout=1)
            proxy = None
            try:
                await runner.setup()
                # Port 0: the OS picks a free loopback port, so two
                # workspaces never collide on a hard-coded one.
                await web.TCPSite(runner, "127.0.0.1", 0).start()
                self.port = runner.addresses[0][1]
                logger.info("[SapWorkbench] screen gateway on 127.0.0.1:%d", self.port)
                if public_base_value:
                    # The public entry forwards here. The port is fixed because
                    # the entry's configuration is static, and it shares this
                    # loop so a session's port is read from the live registry --
                    # never from a directory a stale entry could survive in.
                    from ..backend import dispatcher
                    proxy = web.AppRunner(dispatcher.build_app(port_of=dispatcher.port_lookup(self.runtimes)),
                                          shutdown_timeout=1, access_log=None)
                    await proxy.setup()
                    await web.TCPSite(proxy, "127.0.0.1", proxy_port_value).start()
                    self.proxy_port = proxy_port_value
                    logger.info("[SapWorkbench] public entry proxy on 127.0.0.1:%d", proxy_port_value)
                ready.set()
                if self._stop_requested.is_set():
                    self._stopped.set()
                await self._stopped.wait()
            finally:
                self.port = None
                self.proxy_port = None
                await self._shutdown(manager, runner, proxy)

        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(serve())
            except Exception as error:
                failure.append(error)
                logger.warning('[SapWorkbench] shutdown component=gateway code=runner_failed')
            finally:
                # asyncio.run waits indefinitely for cancellation-resistant
                # tasks. This private loop has an explicit final drain budget.
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    with suppress(Exception):
                        loop.run_until_complete(asyncio.wait(pending, timeout=SHUTDOWN_LOOP_DRAIN_SECONDS))
                self._force_owned()
                loop.close()
                ready.set()

        self._thread = threading.Thread(target=run, name="sap-screen-gateway",
                                        daemon=True)
        self._thread.start()
        return ready, failure

    def _new_node(self, **kwargs):
        node = BrowserNode(**kwargs)
        node._child_owner = self._remember_child
        self._nodes.add(node)
        if self._stop_requested.is_set():
            node.request_stop()
        return node

    @staticmethod
    def _raw_child(child):
        transport = getattr(child, '_transport', None)
        raw = transport.get_extra_info('subprocess') if transport is not None else None
        return raw if raw is not None else child

    def _remember_child(self, child):
        # Retain living exact handles even after normal manager/registry
        # removal, and prune reaped children on each new assignment. A fast
        # close failure cannot hide a child behind weak node ownership.
        child = self._raw_child(child)
        with self._children_lock:
            retained = []
            for owned in self._owned_child_handles:
                try:
                    code = owned.poll() if callable(getattr(owned, 'poll', None)) else owned.returncode
                except Exception:
                    code = None
                if code is None:
                    retained.append(owned)
            if not any(child is owned for owned in retained):
                retained.append(child)
            self._owned_child_handles = retained

    def _runtime_owners(self):
        owners = []
        for value in list(self.runtimes.values()) + self._closing_runtimes:
            owner = getattr(value, 'sap_runtime', None) if isinstance(value, asyncio.Task) else value
            if owner is not None and not any(owner is old for old in owners):
                owners.append(owner)
        return owners

    def _children(self):
        with self._children_lock:
            children = list(self._owned_child_handles)
        for node in list(self._nodes):
            node.request_stop()
            children.extend(node.owned_children())
        for runtime in self._runtime_owners():
            if hasattr(runtime, 'owned_children'):
                children.extend(runtime.owned_children())
            else:
                children.extend(p for p in (getattr(runtime, 'process', None),
                                           getattr(getattr(runtime, 'mcp', None), 'process', None)) if p is not None)
        unique = []
        for child in children:
            # asyncio.Process exposes its exact stdlib Popen through transport
            # metadata. Keep that handle for the main-thread emergency reap.
            child = self._raw_child(child)
            if not any(child is old for old in unique):
                unique.append(child)
        return unique

    def _force_owned(self):
        for child in self._children():
            try:
                code = child.poll() if callable(getattr(child, 'poll', None)) else child.returncode
                if code is None:
                    child.kill()
            except Exception:
                logger.warning('[SapWorkbench] shutdown component=child code=kill_failed')

    async def _shutdown(self, manager, http, proxy=None):
        self._stop_requested.set()
        manager._closing = True
        values = list(self.runtimes.values()) + list(getattr(self, 'session_requests', {}).values())
        self._closing_runtimes = self._runtime_owners()
        for node in list(self._nodes):
            node.request_stop()
        for value in values:
            if isinstance(value, asyncio.Task):
                value.cancel()

        async def clean(component, callback):
            try:
                await callback()
            except BaseException:
                logger.warning('[SapWorkbench] shutdown component=%s code=cleanup_failed', component)

        callbacks = [('runtime', owner.close) for owner in self._closing_runtimes]
        # Start every node teardown together; manager's normal release loop
        # then joins each node's same idempotent close task without multiplying
        # the native wait by the configured global node count.
        callbacks.extend([('browser', node.close) for node in list(self._nodes)])
        callbacks.extend([('manager', manager.shutdown)])
        if proxy is not None:
            callbacks.append(('proxy', proxy.cleanup))
        callbacks.append(('http', http.cleanup))
        tasks = [asyncio.create_task(clean(name, callback)) for name, callback in callbacks]
        tasks += [asyncio.create_task(clean('allocation', lambda value=value: asyncio.gather(value, return_exceptions=True)))
                  for value in values if isinstance(value, asyncio.Task)]
        _, unfinished = await asyncio.wait(tasks, timeout=SHUTDOWN_GRACE_SECONDS)
        if unfinished:
            logger.warning('[SapWorkbench] shutdown component=gateway code=cleanup_timeout')
            self._force_owned()
            for task in unfinished:
                task.cancel()
        # A failed callback cannot hide its exact child handles from fallback.
        self._force_owned()
        self.runtimes.clear()
        self.session_requests.clear()

    def _exit_stop(self):
        if os.getpid() == self._exit_pid:
            self._exit_closing = True
            self.stop()

    def submit(self, coroutine, timeout=90):
        try:
            self.start()
            pending = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        except BaseException:
            coroutine.close()
            raise
        try:
            return pending.result(timeout)
        except FutureTimeoutError:
            # Cancel the request; any shielded allocation/cleanup remains
            # owned by the runtime registry and can be reconciled on retry.
            pending.cancel()
            raise

    def stop(self) -> None:
        started = time.monotonic()
        self._stop_requested.set()
        if not self._thread or not self._thread.is_alive():
            self._force_owned()
            return
        if self._loop and self._stopped:
            with suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._stopped.set)
        if current_thread() is self._thread:
            return
        self._thread.join(timeout=SHUTDOWN_WAIT_SECONDS)
        self._force_owned()
        # Reap only this runner's actual Popen handles, sharing one total
        # budget rather than multiplying it by tenant/runtime/node count.
        deadline = started + SHUTDOWN_WAIT_SECONDS
        for child in self._children():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if callable(getattr(child, 'poll', None)):
                with suppress(Exception):
                    child.wait(timeout=remaining)
        if self._thread.is_alive():
            logger.warning('[SapWorkbench] shutdown component=gateway code=stop_timeout')


browser_gateway = BrowserGatewayRunner()
