"""Literal secret filtering before process output enters buffers or callbacks."""
import re
from agent.tools.bash.decode import _fallback_encoding


class StreamRedactor:
    def __init__(self, values):
        encoded = set()
        for value in values:
            if not value:
                continue
            for encoding in {'utf-8', _fallback_encoding()} - {''}:
                try:
                    encoded.add(str(value).encode(encoding))
                except (UnicodeEncodeError, LookupError):
                    continue
        self.values = sorted(encoded, key=len, reverse=True)
        self.pattern = re.compile(b'|'.join(re.escape(value) for value in self.values)) if self.values else None
        self.pending = b''

    def feed(self, chunk, *, final=False):
        if self.pattern is None:
            return chunk
        data = self.pending + chunk
        boundary = len(data)
        if not final:
            # Only retain a suffix that could become a secret. Holding a fixed
            # max-token-length tail would stall short, harmless progress output.
            for value in self.values:
                start = data.find(value[:1], max(0, len(data) - len(value) + 1))
                while start >= 0:
                    if value.startswith(data[start:]):
                        boundary = min(boundary, start)
                        break
                    start = data.find(value[:1], start + 1)
            # Never split an already complete match at that boundary. Prefer
            # the longest literal when one secret is a prefix of another.
            for match in self.pattern.finditer(data):
                if match.start() >= boundary:
                    break
                if match.end() > boundary:
                    boundary = match.start()
                    break
        self.pending = data[boundary:]
        return self.pattern.sub(b'[REDACTED]', data[:boundary])


def redact_text(text, values):
    return StreamRedactor(values).feed(text.encode(), final=True).decode()
