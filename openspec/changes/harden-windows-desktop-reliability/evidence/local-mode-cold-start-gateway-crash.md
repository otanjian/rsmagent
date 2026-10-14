# 发现：本地模式冷启动时网关 10 秒预算不足，且清理路径把原因掩盖成 `AttributeError`

日期：2026-10-14。发现场景：把打包版从远程模式切回 **local** 模式并冷启动（`desktop-remote.json` 已写入 `mode=local`、`activeProfileId=null`，profiles 保留）。

## 1. 现象

- 应用窗口显示：**`初始化失败` / `后端进程异常退出` / `The app exited during startup (code 1): AttributeError: 'LocalGateway' object has no attribute '_loop'`**，并给出 **重试** 与 **打开日志目录** 两个动作（`getBackendStatus()` = `"error"`，`getBackendError()` = `{"code":"backend_crashed", …}`）。
- 点一次 **重试** 后：后端 pid 32156 正常启动、`getBackendStatus()` = `"ready"`、监听 **127.0.0.1:9876** ✓。

## 2. 时间线（`~/.cow/app-lifecycle.log` / `~/.cow/run.log`）

| 时刻 | 事实 |
|---|---|
| 14:35:57 | 后端 pid 15584 启动 → `run.log` 尾部：`app.py:329 _run_channel` → `runtime.py:2900 startup` → `local_gateway.py:49 start` → `local_gateway.py:56 stop` → **`AttributeError: 'LocalGateway' object has no attribute '_loop'`** |
| 14:38:28 | 后端 pid 17400 启动 |
| 14:38:43 | **`exiting reason=desktop channel 'web' failed to start uptime=14s`** ← **14 秒**后失败退出 |
| 14:40:12 | 我点界面「重试」→ 后端 pid 32156 启动 → `Local access: http://localhost:9876`、`Listening on 127.0.0.1 only` ✓ |

## 3. 代码定位（`integrations/desktop/local_gateway.py`，**本 change 未改动该文件**）

```python
def start(self, identity_service):
    if self._thread and self._thread.is_alive(): return
    self._ready = threading.Event(); self._error = None
    async def serve():
        from aiohttp import web
        from integrations.desktop.gateway import build_app
        self._loop = asyncio.get_running_loop()      # ← 在子线程协程内才赋值
        self._stopped = asyncio.Event()
        ...
    def run():
        try: asyncio.run(serve())
        except Exception as exc: self._error = exc
        finally: self._ready.set()
    self._thread.start()
    if not self._ready.wait(10):                     # ← 10 秒就绪预算
        self.stop()                                  # ← 超时分支
        raise RuntimeError("desktop gateway startup timed out")
    if self._error:
        raise RuntimeError("desktop gateway startup failed") from self._error

def stop(self):
    if self._thread and self._thread.is_alive():
        self._loop.call_soon_threadsafe(self._stopped.set)   # ← _loop 可能尚未赋值 → AttributeError
        self._thread.join(timeout=5)
```

**两处缺陷**：

1. **预算过短**：冻结包冷启动时 `from aiohttp import web` + `build_app` 的首次 import 实测 ~**14 s**（`uptime=14s`），超过硬编码的 **10 s**。预热后（重试）远小于 10 s，故表现为**间歇性**——这也解释了为何本机 13:52 那次本地模式启动是成功的（7.1 冒烟未被触发到）。
2. **清理路径不安全且掩盖原因**：超时后调用 `stop()`，而 `self._loop` 只在协程里赋值；导入慢时它尚未赋值 → 抛 `AttributeError`，把真实原因（"启动超时"）替换成误导性报错，且后端以 code 1 退出、`desktop channel 'web' failed to start`。

## 4. 归属与影响

- **不属于本 change 的改动**：`git diff --name-only origin/rdai` 中**没有** `integrations/desktop/local_gateway.py`（也没有 `integrations/desktop/gateway.py`）→ 非本次加固引入，属既有实现的健壮性缺口。
- 也**不是**"切模式"造成的：切模式只让应用改为**在本地模式下启动**，从而走到了这段代码。
- 正面记录：应用的错误面**准确报出**了 `backend_crashed` 与具体异常文本，并提供 **重试**——本 change 的 2.2/2.3（缺项/失败提示与重试）在真实崩溃上表现正确。
- 影响面：**打包版本地模式的冷启动可能一次失败**（需用户点一次重试）；对"Windows 普通用户首次启动"体验有直接影响。

## 5. 建议修法（两处，均小改，未实施——等授权）

1. `stop()` 改为安全清理：`loop = getattr(self, '_loop', None)`；`if loop is not None: loop.call_soon_threadsafe(...)`；并让超时分支抛出**带上真实原因**的错误（"gateway startup timed out" 不再被 `AttributeError` 顶掉）。
2. 放宽/前置预算：把 `_ready.wait(10)` 提到 ≥30 s，或把"import 依赖"移到预算之外（例如在 `start()` 前预热 `aiohttp`/`gateway`，或先建 loop+事件再 import），使冻结包冷启动不再踩线。
