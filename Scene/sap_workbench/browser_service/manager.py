"""Own the live :class:`BrowserNode` instances on the gateway's event loop.

A node is expensive (a whole Chrome), so the manager is the single place that
decides how many exist, which profile directory each owns, and what happens
when a pane reconnects. Everything here runs on the gateway's asyncio loop --
the manager is never touched from the WSGI thread.
"""
from __future__ import annotations

import asyncio
import hashlib
import os

from common.log import logger

from .node import BrowserNode, BrowserNodeError

#: One browser per (tenant, user) is the first slice: it caps a single user at
#: one Chrome without needing multi-user lease arbitration yet. Reconnecting
#: re-attaches the same node; a second simultaneous pane is refused.
NODES_PER_USER = 1

#: How long an unattached node is kept before it is torn down. A dropped
#: socket (a reload, a sleeping laptop, a page switched away) is far more
#: common than a real end of work, and re-attaching is what keeps the SAP
#: login the user just performed; tearing Chrome down on every blip would cost
#: them a re-login each time.
REATTACH_GRACE = float(os.environ.get("SAP_WORKBENCH_REATTACH_GRACE", "120"))

# A manager owns one backend process's nodes. This is independent of tenant
# claims and is not a machine-wide budget shared by Web/desktop processes.
DEFAULT_NODE_MAX_SESSIONS = 4
MAX_NODE_MAX_SESSIONS = 32


def node_max_sessions(value=None) -> int:
    """Read a bounded deployment limit; invalid settings fail before launch."""
    if value is None:
        value = os.environ.get("SAP_WORKBENCH_NODE_MAX_SESSIONS", DEFAULT_NODE_MAX_SESSIONS)
    if isinstance(value, str):
        value = value.strip()
        if not value.isascii() or not value.isdecimal() or len(value) > 2:
            raise ValueError("SAP_WORKBENCH_NODE_MAX_SESSIONS must be an integer from 1 to 32")
        value = int(value)
    if type(value) is not int or not 1 <= value <= MAX_NODE_MAX_SESSIONS:
        raise ValueError("SAP_WORKBENCH_NODE_MAX_SESSIONS must be an integer from 1 to 32")
    return value


def profile_dir(root: str, tenant_id: str, user_id: str) -> str:
    """A stable, per-user profile directory (isolates SAP cookies/logins)."""
    digest = hashlib.sha256(f"{tenant_id}\x00{user_id}".encode("utf-8")).hexdigest()[:16]
    return os.path.join(root, f"node-{digest}")


class _Lease:
    """What the gateway holds: frames/send, and a close that unregisters."""

    def __init__(self, manager: "BrowserNodeManager", key, node: BrowserNode):
        self._manager = manager
        self._key = key
        self._node = node

    @property
    def viewport(self) -> dict:
        return self._node.viewport

    def frames(self):
        return self._node.frames()

    async def send(self, commands) -> None:
        await self._node.send(commands)

    async def resize(self, width: int, height: int) -> None:
        await self._node.resize(width, height)

    async def detach(self) -> None:
        """The pane went away: keep the node briefly in case it comes back."""
        await self._manager.detach(self._key, self._node)

    async def close(self) -> None:
        await self._manager.release(self._key, self._node)


class BrowserNodeManager:
    def __init__(self, *, executable, profile_root, trust_test_certificate=False,
                 headless=True, max_screens=4, factory=BrowserNode,
                 reattach_grace=REATTACH_GRACE, max_nodes=None):
        self._executable = executable
        self._profile_root = profile_root
        self._trust_test_certificate = trust_test_certificate
        self._headless = headless
        self._default_max_screens = max(1, int(max_screens))
        self._max_nodes = node_max_sessions(max_nodes)
        self._factory = factory
        self._reattach_grace = max(0.0, float(reattach_grace))
        self._live: dict[tuple, BrowserNode] = {}
        self._reapers: dict[tuple, asyncio.Task] = {}
        self._pending: set[tuple] = set()
        self._starts: dict[tuple, asyncio.Task] = {}
        self._stopping: set[tuple] = set()
        self._releasing: dict[tuple, asyncio.Task] = {}
        self._closing = False

    @property
    def available(self) -> bool:
        return bool(self._executable)

    def _occupied_keys(self) -> set:
        return self._live.keys() | self._pending | self._releasing.keys()

    def total_count(self) -> int:
        """Count reserved slots across tenants, including startup/teardown."""
        return len(self._occupied_keys())

    def live_count(self, tenant_id: str) -> int:
        return sum(1 for (tenant, _) in self._occupied_keys() if tenant == tenant_id)

    async def acquire(self, claims) -> _Lease:
        tenant_id, user_id = claims.get("tenant_id"), claims.get("user_id")
        url = claims.get("url")
        if not tenant_id or not user_id or not url:
            raise BrowserNodeError("view token is missing its target")
        if not self._executable:
            raise BrowserNodeError("no Chrome executable is available")

        key = (tenant_id, user_id)
        if self._closing or key in self._pending:
            raise BrowserNodeError("browser allocation unavailable")
        if key in self._releasing:
            # A reconnect cannot revive a Chrome already being closed.
            raise BrowserNodeError("browser allocation unavailable")
        existing = self._live.get(key)
        # A node nobody is watching is this user's own session waiting to be
        # re-attached: reusing it preserves where they were (and their SAP
        # login) instead of paying for a fresh Chrome. Never replace a node
        # whose stream still belongs to another pane.
        if existing is not None and existing.attached and existing.usable():
            raise BrowserNodeError("browser already attached")
        if existing is not None and not existing.attached and existing.usable():
            self._cancel_reaper(key)
            existing.attached = True
            logger.info("[SapWorkbench] re-attaching browser node for %s/%s",
                        tenant_id, user_id)
            return _Lease(self, key, existing)
        # The tenant's own configured ceiling travels with the requested view,
        # so one tenant's limit cannot leak into another's.
        limit = max(1, int(claims.get("max_screens") or self._default_max_screens))
        if existing is None and self.live_count(tenant_id) >= limit:
            # Refuse rather than silently evicting someone else's screen: the
            # existing user is mid-task, and guessing which one to kill is worse
            # than telling this caller to retry.
            raise BrowserNodeError("browser capacity reached")
        if existing is None and self.total_count() >= self._max_nodes:
            # Operator-owned process limit applies even when many tenants
            # each have free quota. A reconnect/replacement keeps its own
            # reserved slot; replacement closes the old node before launch.
            raise BrowserNodeError("browser node capacity reached")

        # Reserve before the first await: starting Chrome must count against
        # the capacity limit and may not race another acquisition of this key.
        self._pending.add(key)
        task = asyncio.create_task(self._allocate(key, claims, url, existing))
        self._starts[key] = task
        task.add_done_callback(lambda result: self._start_done(key, result))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            self._cancel_start(key, task)
            # Preserve the capacity reservation until partial Chrome cleanup
            # finishes, even when a disconnected request is cancelled again.
            try:
                lease = await asyncio.shield(task)
                # Success and request cancellation can arrive in one loop
                # turn. A lease never delivered to a pane must not remain
                # attached; keep the normal grace period for its reconnect.
                await lease.detach()
            except (asyncio.CancelledError, Exception):
                pass
            raise

    async def _allocate(self, key, claims, url, existing):
        try:
            await self.release(key, existing)
            return await self._start_node(key, claims, url)
        finally:
            self._pending.discard(key)
            self._starts.pop(key, None)
            self._stopping.discard(key)

    def _cancel_start(self, key, task):
        # A second cancel during node.close would interrupt partial teardown.
        if key not in self._stopping and not task.done():
            self._stopping.add(key)
            task.cancel()

    def _start_done(self, key, task):
        # Cancellation before the coroutine's first step never enters its
        # finally block. Its owner must release that reservation as well.
        if self._starts.get(key) is task:
            self._pending.discard(key)
            self._starts.pop(key, None)
            self._stopping.discard(key)
        if not task.cancelled():
            task.exception()

    async def _start_node(self, key, claims, url):
        tenant_id, user_id = key
        node = self._factory(
            url=url,
            profile_dir=profile_dir(self._profile_root, tenant_id, user_id),
            executable=self._executable,
            headless=self._headless,
            trust_test_certificate=self._trust_test_certificate,
            max_width=int(claims.get("max_width") or 1280),
            max_height=int(claims.get("max_height") or 800),
        )
        try:
            await node.start()
            if self._closing:
                raise BrowserNodeError("browser manager is shutting down")
        except asyncio.CancelledError:
            await node.close()
            raise
        except Exception:  # noqa: BLE001 - reported as an unavailable runtime
            logger.exception("[SapWorkbench] browser node failed to start")
            await node.close()
            raise BrowserNodeError("browser node failed to start") from None
        node.attached = True
        self._live[key] = node
        logger.info("[SapWorkbench] browser node up for %s/%s", tenant_id, user_id)
        return _Lease(self, key, node)

    async def detach(self, key, node) -> None:
        """Release a pane without giving up the browser it was watching."""
        if node is None or self._live.get(key) is not node:
            return
        node.attached = False
        if self._reattach_grace <= 0:
            await self.release(key, node)
            return
        self._cancel_reaper(key)
        self._reapers[key] = asyncio.create_task(self._reap_later(key, node))
        logger.info("[SapWorkbench] browser node idle for %s, keeping it %ss",
                    key[1], self._reattach_grace)

    async def _reap_later(self, key, node) -> None:
        try:
            await asyncio.sleep(self._reattach_grace)
        except asyncio.CancelledError:
            return
        self._reapers.pop(key, None)
        if self._live.get(key) is node and not node.attached:
            logger.info("[SapWorkbench] reclaiming idle browser node for %s", key[1])
            await self.release(key, node)

    def _cancel_reaper(self, key) -> None:
        task = self._reapers.pop(key, None)
        if task is not None and not task.done():
            task.cancel()

    async def release(self, key, node) -> None:
        if node is None:
            return
        if key in self._releasing:
            await asyncio.shield(self._releasing[key])
            return
        if self._live.get(key) is node:
            self._cancel_reaper(key)
        task = asyncio.create_task(self._close_node(key, node))
        self._releasing[key] = task
        # Keep the capacity reservation until Chrome has actually closed,
        # even when the requesting socket/task disappears during cleanup.
        await asyncio.shield(task)

    async def _close_node(self, key, node):
        try:
            await node.close()
        except Exception:  # noqa: BLE001 - teardown must not raise
            logger.exception("[SapWorkbench] browser node teardown failed")
        finally:
            if self._live.get(key) is node:
                self._live.pop(key, None)
            self._releasing.pop(key, None)

    async def shutdown(self) -> None:
        self._closing = True
        for key in list(self._reapers):
            self._cancel_reaper(key)
        starts = list(self._starts.items())
        for key, task in starts:
            self._cancel_start(key, task)
        # The loop must outlive executor launches and their owned teardown.
        # Closing only _live leaves an in-flight allocation outside shutdown.
        await asyncio.gather(*(task for _, task in starts), return_exceptions=True)
        for key, node in list(self._live.items()):
            await self.release(key, node)
