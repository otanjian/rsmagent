"""Public readiness projection: booleans only, separate from Desktop liveness."""
import json
import web


class ReadyHandler:
    def GET(self):
        from common.readiness import check
        try:
            result = check()
        except Exception:
            result = {'ready': False, 'checks': {'configuration': False}}
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Cache-Control', 'no-store')
        if not result['ready']:
            web.ctx.status = '503 Service Unavailable'
        return json.dumps(result)
