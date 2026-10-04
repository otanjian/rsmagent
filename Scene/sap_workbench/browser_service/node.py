"""One real Chrome, driven over Chrome DevTools Protocol.

Reuse of ``agent/tools/browser/chrome_launcher.ChromeLauncher`` is deliberate:
it already spawns the system Chrome with an isolated ``--user-data-dir`` and a
private ``--remote-debugging-port``, which is what keeps one scene session's
SAP login from touching another's or the user's day-to-day browser.

The CDP socket is the pane's whole engine: ``Page.startScreencast`` yields JPEG
frames down to the pane, and ``Input.dispatch*`` (built by :mod:`mapping`)
carries the user's clicks and keys back into the very same target the frames
come from, so "what the user sees" and "what is operated" cannot diverge.
"""
from __future__ import annotations

import asyncio
import base64
from concurrent.futures import Future
import json
import queue
import threading
import urllib.request

from common.log import logger

#: Fallback viewport until the first screencast frame reports the real one.
_DEFAULT_VIEWPORT = {"width": 1280, "height": 800}

_WS_MAX = 8 * 1024 * 1024


class BrowserNodeError(RuntimeError):
    """A CDP call failed, or the socket went away."""


class _CDP:
    """Minimal CDP client multiplexing one aiohttp WebSocket."""

    def __init__(self, ws):
        self._ws = ws
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._events: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._closed = False
        self._reader: asyncio.Task | None = None

    def start(self) -> None:
        self._reader = asyncio.create_task(self._read_loop())

    @property
    def closed(self) -> bool:
        return self._closed

    async def _read_loop(self) -> None:
        import aiohttp

        try:
            async for message in self._ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    if message.type in (aiohttp.WSMsgType.CLOSED,
                                        aiohttp.WSMsgType.CLOSE,
                                        aiohttp.WSMsgType.ERROR):
                        break
                    continue
                try:
                    data = json.loads(message.data)
                except ValueError:
                    continue
                if "id" in data:
                    future = self._pending.pop(data["id"], None)
                    if future is not None and not future.done():
                        future.set_result(data)
                elif "method" in data:
                    # Bounded: a slow pane must not grow this without limit.
                    if self._events.full():
                        try:
                            self._events.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    await self._events.put(data)
        finally:
            self._closed = True
            # Wake a frame consumer waiting on an otherwise empty queue when
            # Chrome exits. Without this it leaves a frozen pane forever.
            if self._events.full():
                self._events.get_nowait()
            self._events.put_nowait(None)
            for future in self._pending.values():
                if not future.done():
                    future.cancel()

    async def call(self, method: str, params=None, timeout: float = 10.0):
        if self._closed:
            raise BrowserNodeError("cdp connection is closed")
        self._next_id += 1
        message_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        try:
            await self._ws.send_json({"id": message_id, "method": method,
                                      "params": params or {}})
            result = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise BrowserNodeError(f"{method}: timed out") from None
        finally:
            self._pending.pop(message_id, None)
        if "error" in result:
            raise BrowserNodeError(f"{method}: {result['error'].get('message')}")
        return result.get("result")

    async def events(self):
        while not self._closed:
            event = await self._events.get()
            if event is None:
                return
            yield event


def _page_ws_url(endpoint: str) -> str:
    """Page target's debugger URL, creating a tab if Chrome has none."""
    with urllib.request.urlopen(f"{endpoint}/json", timeout=5) as response:
        targets = json.load(response)
    for target in targets:
        if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
            return target["webSocketDebuggerUrl"]
    request = urllib.request.Request(f"{endpoint}/json/new?about:blank", method="PUT")
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)["webSocketDebuggerUrl"]


class BrowserNode:
    """A single headed Chrome streaming the configured SAP Web GUI.

    Browser certificate validation remains enabled. The backend MCP exception
    does not grant permission to bypass a browser security warning.
    """

    def __init__(self, *, url, profile_dir, executable, headless=False,
                 trust_test_certificate=False, max_width=4096, max_height=4096):
        self._url = url
        self._profile_dir = profile_dir
        self._executable = executable
        self._headless = headless
        self._trust_test_certificate = trust_test_certificate
        self._max = (max_width, max_height)
        self._launcher = None
        self._launch_future = None
        self._close_task = None
        self._closing = threading.Event()
        self._launch_closed = threading.Event()
        self._children_lock = threading.Lock()
        self._owned_children = []
        self._native_lock = threading.Lock()
        self._native_queue = None
        self._native_thread = None
        self._native_close = Future()
        self._native_close_queued = False
        self._session = None
        self._ws = None
        self._cdp: _CDP | None = None
        self._viewport = dict(_DEFAULT_VIEWPORT)
        self._attached = False

    @property
    def viewport(self) -> dict:
        return dict(self._viewport)

    @property
    def attached(self) -> bool:
        """Whether a pane socket currently holds this node's frame stream."""
        return self._attached

    @attached.setter
    def attached(self, value: bool) -> None:
        self._attached = bool(value)

    def usable(self) -> bool:
        """A node whose CDP socket is still open can be re-attached as-is."""
        return self._cdp is not None and not self._cdp.closed

    async def start(self) -> None:
        import aiohttp

        from agent.tools.browser.chrome_launcher import ChromeLauncher

        if self._close_task is not None:
            raise BrowserNodeError("browser node is closed")
        node = self
        class OwnedLauncher(ChromeLauncher):
            def __setattr__(self, name, value):
                # ChromeLauncher clears _proc before waiting during close.
                # Retain the exact handle from its first assignment, including
                # a spawn whose readiness check has not returned yet.
                if name == '_proc' and value is not None:
                    node._remember_child(value)
                super().__setattr__(name, value)
        self._launcher = OwnedLauncher(self._executable, self._profile_dir,
                                       headless=self._headless)
        try:
            # A default ThreadPoolExecutor is joined before normal atexit
            # callbacks. Scene-owned daemon threads let the runner's exit hook
            # set the close latch before waiting for any delayed native spawn.
            self._launch_future = self._offload(self._launch_owned)
            endpoint = await asyncio.shield(self._launch_future)
            if self._close_task is not None:
                raise BrowserNodeError("browser node is closed")
            ws_url = await asyncio.shield(self._offload(_page_ws_url, endpoint))
            self._session = aiohttp.ClientSession()
            self._ws = await self._session.ws_connect(ws_url, max_msg_size=_WS_MAX,
                                                      heartbeat=20)
            self._cdp = _CDP(self._ws)
            self._cdp.start()
            await self._cdp.call("Page.enable")
            # Start the stream before navigating: Chrome reports "not attached
            # to an active page" if a screencast is requested while the target
            # is mid-navigation, and a stream on about:blank survives the
            # navigation anyway, so this both avoids the race and shows frames
            # a moment sooner.
            await self._start_screencast()
            await self._navigate()
        except BaseException:
            await self.close()
            raise

    async def _navigate(self) -> None:
        try:
            await self._cdp.call("Page.navigate", {"url": self._url}, timeout=30)
        except BrowserNodeError as error:
            if str(error) != "Page.navigate: timed out":
                raise
            # A slow SAP/TLS response does not mean Chrome died. Keep the
            # target and stream so the user sees its loading/error page;
            # restarting Chrome here interrupts every navigation attempt.
            logger.warning("[SapWorkbench] SAP navigation still pending; keeping browser visible")

    async def _start_screencast(self) -> None:
        """Ask Chrome to stream, retrying while the target settles."""
        params = {"format": "jpeg", "quality": 80,
                  "maxWidth": self._max[0], "maxHeight": self._max[1],
                  "everyNthFrame": 1}
        last: BrowserNodeError | None = None
        for _ in range(5):
            try:
                await self._cdp.call("Page.startScreencast", params, timeout=5)
                return
            except BrowserNodeError as error:
                last = error
                await asyncio.sleep(0.3)
        raise last

    async def frames(self):
        """Yield ``{"viewport", "data"}`` for each screencast frame, ACK-ing it."""
        # A detached consumer may have been cancelled before ACK. Restarting
        # and sending a fresh image prevents a black reconnect.
        await self._cdp.call('Page.stopScreencast')
        await self._start_screencast()
        initial = await self._cdp.call('Page.captureScreenshot', {'format': 'jpeg', 'quality': 80})
        if initial.get('data'):
            yield {'viewport': dict(self._viewport), 'data': base64.b64decode(initial['data'])}
        async for event in self._cdp.events():
            if event.get("method") != "Page.screencastFrame":
                continue
            params = event.get("params") or {}
            metadata = params.get("metadata") or {}
            self._viewport = {
                "width": int(metadata.get("deviceWidth") or self._viewport["width"]),
                "height": int(metadata.get("deviceHeight") or self._viewport["height"]),
            }
            try:
                data = base64.b64decode(params.get("data") or "")
            except (ValueError, TypeError):
                continue
            yield {"viewport": dict(self._viewport), "data": data}
            try:
                # Chrome only sends the next frame once this one is acknowledged;
                # the pane's back-pressure is therefore the frame rate.
                await self._cdp.call("Page.screencastFrameAck",
                                     {"sessionId": params.get("sessionId")}, timeout=5)
            except BrowserNodeError:
                return

    async def resize(self, width: int, height: int) -> None:
        from .mapping import resize_viewport

        size = resize_viewport({'t': 'resize', 'width': width, 'height': height})
        if size is None:
            raise BrowserNodeError('invalid viewport dimensions')
        if size == self._viewport:
            return
        # Resize the actual SAP layout, rather than stretching its JPEG. The
        # next screencast metadata remains the authoritative input viewport.
        await self._cdp.call('Emulation.setDeviceMetricsOverride', {
            **size, 'deviceScaleFactor': 1, 'mobile': False,
        })

    async def send(self, commands) -> None:
        for command in commands:
            await self._cdp.call(command["method"], command.get("params"))

    async def close(self) -> None:
        self.request_stop()
        if self._close_task is None:
            self._attached = False
            self._close_task = asyncio.create_task(self._cleanup())
        # Teardown owns the launcher even if its caller is cancelled again.
        await asyncio.shield(self._close_task)

    def request_stop(self):
        # The launch thread checks this flag without relying on the asyncio
        # loop still being alive when a late native spawn returns.
        self._closing.set()
        with self._native_lock:
            if self._native_queue is not None and not self._native_close_queued:
                # Enqueue on the already-started daemon worker. Starting a new
                # thread during Python 3.12+ atexit is prohibited.
                self._native_close_queued = True
                self._native_queue.put((self._close_owned, (), None))

    def owned_children(self):
        process = getattr(self._launcher, '_proc', None)
        if process is not None:
            self._remember_child(process)
        with self._children_lock:
            return list(self._owned_children)

    def _remember_child(self, process):
        with self._children_lock:
            if not any(process is child for child in self._owned_children):
                self._owned_children.append(process)
        owner = getattr(self, '_child_owner', None)
        if owner is not None:
            owner(process)
        # This runs in the native launch thread as well: no callback on an
        # already-closed loop is needed to own a child delivered after stop.
        if self._closing.is_set():
            self._kill_child(process)

    @staticmethod
    def _kill_child(process):
        try:
            if process.poll() is None:
                process.kill()
        except Exception:
            logger.warning('[SapWorkbench] shutdown component=browser code=kill_failed')

    def _offload(self, callback, *args):
        """One daemon worker per node, never a global executor queue."""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        # If its async waiter is gone, the native ownership wrapper still
        # finishes; retrieving a failure prevents an unhandled Future warning.
        future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        def deliver(result, error):
            if not future.done():
                if error is None:
                    future.set_result(result)
                else:
                    future.set_exception(error)
        def done(result, error):
            try:
                loop.call_soon_threadsafe(deliver, result, error)
            except RuntimeError:
                # Native ownership cleanup does not depend on this callback.
                pass
        with self._native_lock:
            if self._launch_closed.is_set():
                raise BrowserNodeError('browser node is closed')
            if self._native_queue is None:
                self._native_queue = queue.Queue()
                self._native_thread = threading.Thread(target=self._native_work,
                    name='sap-browser-native', daemon=True)
                self._native_thread.start()
            self._native_queue.put((callback, args, done))
        return future

    def _native_work(self):
        while True:
            callback, args, done = self._native_queue.get()
            try:
                result, error = callback(*args), None
            except BaseException as failure:
                result, error = None, failure
            if done is not None:
                done(result, error)
            if self._launch_closed.is_set():
                # Never leave a waiter behind if close was requested between
                # Future delivery and the loop enqueueing its next operation.
                while True:
                    try:
                        _, _, waiting = self._native_queue.get_nowait()
                    except queue.Empty:
                        return
                    if waiting is not None:
                        waiting(None, BrowserNodeError('browser node is closed'))

    def _launch_owned(self):
        if self._closing.is_set():
            raise BrowserNodeError('browser node is closed')
        try:
            return self._launcher.launch()
        finally:
            if self._closing.is_set():
                self._close_owned()

    def _close_owned(self):
        if self._launch_closed.is_set():
            return
        try:
            if self._launcher is not None:
                self._launcher.close()
        except Exception:
            logger.warning('[SapWorkbench] shutdown component=browser code=cleanup_failed')
        finally:
            for child in self.owned_children():
                self._kill_child(child)
                try:
                    child.wait(timeout=2)
                except Exception:
                    logger.warning('[SapWorkbench] shutdown component=browser code=reap_failed')
            self._launch_closed.set()
            if not self._native_close.done():
                self._native_close.set_result(None)

    async def _cleanup(self) -> None:
        logger.info("[SapWorkbench] closing browser node (viewport=%s)", self._viewport)
        if self._launch_future is not None:
            try:
                await asyncio.shield(self._launch_future)
            except Exception:  # noqa: BLE001 - a failed launch still needs close
                pass
        if self._cdp is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001 - teardown must not raise
                pass
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:  # noqa: BLE001
                pass
        if not self._launch_closed.is_set():
            try:
                if self._native_queue is not None:
                    await asyncio.shield(asyncio.wrap_future(self._native_close))
                elif self._launcher is not None:
                    await asyncio.shield(self._offload(self._close_owned))
            except Exception:  # noqa: BLE001
                pass
