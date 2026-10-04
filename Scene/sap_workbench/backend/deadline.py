"""Same-task deadlines for both the platform and Python 3.10 MCP worker."""
import asyncio
from contextlib import asynccontextmanager


@asynccontextmanager
async def task_timeout(seconds):
    native = getattr(asyncio, 'timeout', None)
    if native is not None:
        async with native(seconds):
            yield
        return
    # The worker's Python 3.10 cannot use asyncio.timeout. wait_for creates
    # another task, which cannot exit the AnyIO scopes owned by this one.
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError('deadline requires an asyncio task')
    marker = object()
    timer = asyncio.get_running_loop().call_later(max(0, seconds), task.cancel, marker)
    try:
        yield
    except asyncio.CancelledError as error:
        if error.args and error.args[0] is marker:
            raise asyncio.TimeoutError() from None
        raise
    finally:
        timer.cancel()
