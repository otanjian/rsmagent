"""Instance-bound vendor callbacks sharing a listener per configured port.

The path selects a channel; its existing handler still verifies the vendor
signature with that channel's credentials. No callback resolves a singleton.
"""

import re
import threading
from urllib.parse import quote

import web

from common.log import logger


WEBHOOK_CONFIG = {
    "wechatcom_app": ("/wxcomapp", "wechatcomapp_port", 9898),
    "wechat_kf": ("/wxkf", "wechat_kf_port", 9888),
    "wechatmp": ("/wx", "wechatmp_port", 8080),
    "wechatmp_service": ("/wx", "wechatmp_port", 8080),
}
_listeners = {}
_guard = threading.RLock()


def callback_path(channel_type, instance_id=""):
    base = WEBHOOK_CONFIG[channel_type][0]
    return base + ("/" + quote(instance_id, safe="") if instance_id else "")


class _Listener:
    def __init__(self, port):
        self.routes = {}
        self.server = web.httpserver.WSGIServer(("0.0.0.0", port), self.dispatch)
        # prepare() binds synchronously: an occupied port is a failed startup,
        # never a channel reported connected while its listener thread dies.
        self.server.prepare()
        self.thread = threading.Thread(target=self.serve, daemon=True,
                                       name=f"channel-webhook-{port}")

    def dispatch(self, environ, start_response):
        route = self.routes.get(environ.get("PATH_INFO", "").rstrip("/"))
        if route is None:
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"Not Found"]
        return route.app(environ, start_response)

    def serve(self):
        try:
            self.server.serve()
        except Exception:
            logger.exception("[ChannelWebhook] listener stopped unexpectedly")
        finally:
            for route in list(self.routes.values()):
                route.channel.report_startup_error("callback listener stopped")
                route.stopped.set()


class WebhookRegistration:
    def __init__(self, channel, handler, port, path):
        self.channel = channel
        self.port = port
        self.path = path
        self.stopped = threading.Event()
        bound_handler = type("InstanceQuery", (handler,), {"channel": channel})
        self.app = web.application(
            (re.escape(path) + "/?", "Query"),
            {"Query": bound_handler}, autoreload=False).wsgifunc()

    def stop(self):
        with _guard:
            listener = _listeners.get(self.port)
            if listener and listener.routes.get(self.path) is self:
                del listener.routes[self.path]
                if not listener.routes:
                    del _listeners[self.port]
                    listener.server.stop()
            self.stopped.set()


def register_webhook(channel, handler):
    _, port_key, default_port = WEBHOOK_CONFIG[channel.channel_type]
    port = int(channel.cfg(port_key, default_port))
    if not 1 <= port <= 65535:
        raise ValueError("callback port must be between 1 and 65535")
    path = callback_path(channel.channel_type, channel.instance_id)
    registration = WebhookRegistration(channel, handler, port, path)
    with _guard:
        listener = _listeners.get(port)
        new_listener = listener is None
        if new_listener:
            listener = _Listener(port)
            _listeners[port] = listener
        if path in listener.routes:
            raise RuntimeError("callback path is already registered")
        listener.routes[path] = registration
        if new_listener:
            listener.thread.start()
    return registration


def run_webhook(channel, handler):
    try:
        with _guard:
            if getattr(channel, "_webhook_stopped", False):
                channel.report_startup_error("channel stopped before callback startup")
                return
            registration = register_webhook(channel, handler)
            channel._instance_webhook = registration
            channel.report_startup_success()
        logger.info("[ChannelWebhook] listening on port %s at %s",
                    registration.port, registration.path)
        registration.stopped.wait()
    except Exception as error:
        channel.report_startup_error(str(error))
        raise


def stop_webhook(channel):
    with _guard:
        channel._webhook_stopped = True
        registration = getattr(channel, "_instance_webhook", None)
        if registration:
            registration.stop()
            channel._instance_webhook = None
