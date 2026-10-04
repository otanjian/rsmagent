"""Translate the pinned Session V2 wire events for its native Web reducer.

The installed backend still emits session.next.* and textID, while the Web
reducer consumes session.* and ordinal. Keep this compatibility at the scene
gateway: durable history and the OpenCode checkout remain unchanged.
"""


class NativeEvents:
    def __init__(self):
        self.ordinals = {}

    def __call__(self, event):
        kind = event.get('type', '')
        if not kind.startswith('session.next.'):
            return [event]
        kind = kind.replace('session.next.', 'session.', 1)
        data = dict(event.get('data') or {})
        result = {**event, 'type': kind, 'created': data.get('timestamp', 0), 'data': data}
        session = data.get('sessionID')

        def status(name):
            return {**result, 'id': event['id'] + ':' + name, 'type': 'session.execution.' + name,
                    'data': {'sessionID': session}}

        if kind == 'session.prompt.admitted':
            result['type'] = 'session.input.admitted'
            result['data'] = {'sessionID': session, 'inputID': data['messageID'],
                              'input': {'type': 'user', 'delivery': data.get('delivery', 'steer'),
                                        'data': data['prompt']}}
            # This build admits directly to context; it has no promoted event.
            promoted = {**result, 'id': event['id'] + ':promoted', 'type': 'session.input.promoted',
                        'data': {'sessionID': session, 'inputID': data['messageID']}}
            return [result, promoted, status('started')]
        if kind.startswith(('session.text.', 'session.reasoning.')):
            family = kind.split('.')[1]
            key = (session, data.get('assistantMessageID'), family)
            values = self.ordinals.setdefault(key, {})
            identifier = data.get('textID') if family == 'text' else data.get('reasoningID')
            data['ordinal'] = values.setdefault(identifier, len(values))
        if kind.startswith('session.tool.'):
            provider = data.get('provider') or {}
            data['executed'] = provider.get('executed', False)
            data.setdefault('metadata', {})
        if kind == 'session.step.started':
            return [status('started'), result]
        if kind in {'session.step.ended', 'session.step.failed'}:
            for key in list(self.ordinals):
                if key[:2] == (session, data.get('assistantMessageID')):
                    del self.ordinals[key]
            if kind.endswith('failed'):
                return [result, status('failed')]
            if data.get('finish') not in {'tool-calls', 'unknown'}:
                return [result, status('succeeded')]
        return [result]
