"""Run the existing device gateway beside the bundled desktop Web server.

The Web server is WSGI and cannot upgrade WebSockets. Remote deployments use
their reverse proxy; the bundled backend advertises a private loopback port.
Authentication and command handling remain in gateway.build_app().
"""
import asyncio
import threading


class LocalGateway:
    def __init__(self):
        self.port = None
        self._thread = None

    def start(self, identity_service):
        if self._thread and self._thread.is_alive():
            return
        self._ready = threading.Event()
        self._error = None

        async def serve():
            from aiohttp import web
            from integrations.desktop.gateway import build_app
            self._loop = asyncio.get_running_loop()
            self._stopped = asyncio.Event()
            runner = web.AppRunner(build_app(identity_service=identity_service), shutdown_timeout=1)
            try:
                await runner.setup()
                await web.TCPSite(runner, "127.0.0.1", 0).start()
                self.port = runner.addresses[0][1]
                self._ready.set()
                await self._stopped.wait()
            finally:
                self.port = None
                await runner.cleanup()

        def run():
            try:
                asyncio.run(serve())
            except Exception as exc:
                self._error = exc
            finally:
                self._ready.set()

        self._thread = threading.Thread(target=run, name="desktop-gateway", daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            self.stop()
            raise RuntimeError("desktop gateway startup timed out")
        if self._error:
            raise RuntimeError("desktop gateway startup failed") from self._error

    def stop(self):
        if self._thread and self._thread.is_alive():
            self._loop.call_soon_threadsafe(self._stopped.set)
            self._thread.join(timeout=5)


local_gateway = LocalGateway()
