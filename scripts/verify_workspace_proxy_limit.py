# encoding:utf-8
"""Task 7.2/7.5's deployment numbers: what the reverse proxy actually accepts.

The repository ships no proxy configuration, so the body ceiling that actually
applies to the console lives in whatever nginx the operator runs. This script
reads that value out of the config and then *proves* it over real HTTPS, because
"the config says 100M" and "a 100M+1 request is refused" are different claims
and only the second one is acceptance evidence.

It also pins down the three failure modes task 7.5 requires the panel to tell
apart, by which layer answers:

* **413** -- the proxy refused it; the request never reached the application, so
  the response carries no ``code`` for the panel to read (nginx's own HTML);
* **the application's own over-limit message** -- the request *did* arrive, and
  the handler refused it after counting bytes (see
  ``scripts/verify_workspace_upload_scale.py``, which measures that one);
* **``outside_own_directory``** -- the application accepted the bytes and
  refused the *destination*, which is a JSON body with a stable code.

Run (defaults match the deployment on this host)::

    python scripts/verify_workspace_proxy_limit.py
    python scripts/verify_workspace_proxy_limit.py --host 127.0.0.1 --port 8081
"""

import argparse
import os
import re
import socket
import ssl
import sys
import time

NGINX_CONF = r"C:\nginx\conf\nginx.conf"

#: nginx's ``<n>M`` is * 1024 * 1024, not 1e6 -- the boundary is off by 4.9% if
#: that is guessed wrong, which is wider than the margin the panel cares about.
UNITS = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}


def parse_limits(path):
    """``[(line number, directive line)]`` for every ``client_max_body_size``.

    Read as text rather than by loading an nginx parser: the question is only
    what the deployed file says, and the http-block value is the one that
    applies wherever a server block does not override it.
    """
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, 1):
            stripped = line.split("#", 1)[0].strip()
            if stripped.startswith("client_max_body_size"):
                out.append((number, stripped))
    return out


def size_value(text):
    """``'100M;'`` -> ``104857600``. Returns ``None`` when unreadable."""
    match = re.search(r"client_max_body_size\s+(\d+)\s*([kKmMgG]?)\s*;", text)
    if not match:
        return None
    return int(match.group(1)) * UNITS.get(match.group(2).lower(), 1)


def probe(host, port, sni, path, declared_bytes, *, body_bytes=0, timeout=20.0,
          secure=True):
    """One raw request. The body is sent only as far as it is needed.

    A ``413`` from nginx is decided from the declared ``Content-Length`` alone,
    before a byte of the body is read -- so a probe can ask "would you take
    this?" by sending the headers and nothing else, which is what keeps a
    multi-hundred-megabyte question from becoming a multi-hundred-megabyte
    transfer.
    """
    raw = socket.create_connection((host, port), timeout=timeout)
    try:
        if secure:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            connection = context.wrap_socket(raw, server_hostname=sni or host)
        else:
            connection = raw
        connection.settimeout(timeout)
        head = (
            "POST %s HTTP/1.1\r\n"
            "Host: %s\r\n"
            "Content-Type: application/octet-stream\r\n"
            "Content-Length: %d\r\n"
            "Connection: close\r\n"
            "\r\n" % (path, sni or host, declared_bytes))
        connection.sendall(head.encode("ascii"))
        if body_bytes:
            block = b"x" * 65536
            sent = 0
            while sent < body_bytes:
                chunk = min(len(block), body_bytes - sent)
                try:
                    connection.sendall(block[:chunk])
                except (BrokenPipeError, ConnectionResetError):
                    break  # the server answered and hung up; read what it said
                sent += chunk
        return read_response(connection)
    finally:
        try:
            raw.close()
        except OSError:
            pass


def read_response(connection):
    data = b""
    try:
        while len(data) < 65536:
            block = connection.recv(65536)
            if not block:
                break
            data += block
    except (socket.timeout, TimeoutError):
        return {"status": None, "note": "timed out waiting for a response"}
    except (ConnectionResetError, ssl.SSLError) as exc:
        return {"status": None, "note": "connection closed: %s" % exc}
    if not data:
        return {"status": None, "note": "empty response"}
    line = data.split(b"\r\n", 1)[0].decode("latin-1")
    status = line.split(" ")[1] if len(line.split(" ")) > 1 else None
    body = data.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in data else b""
    return {
        "status": status,
        "line": line,
        "body_head": body[:200].decode("utf-8", "replace").replace("\n", " "),
        "server": next((h.split(":", 1)[1].strip()
                        for h in data.decode("latin-1").split("\r\n")
                        if h.lower().startswith("server:")), ""),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081,
                        help="nginx's localhost TLS port for the console")
    parser.add_argument("--sni", default="ai.rsmxm.com.cn")
    parser.add_argument("--path", default="/api/workspace/upload?agent=probe")
    parser.add_argument("--conf", default=NGINX_CONF)
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

    results = {}
    limits = parse_limits(args.conf)
    results["config.path"] = args.conf
    results["config.directives"] = limits
    effective = None
    for _number, text in limits:
        value = size_value(text)
        if value and (effective is None or value < effective):
            effective = value
    results["config.smallest_bytes"] = effective
    results["config.smallest_mib"] = (round(effective / 1024 / 1024, 1)
                                      if effective else None)
    results["target"] = "%s://%s:%d%s" % ("https", args.host, args.port, args.path)

    # 1. The proxy forwards and the application answers.
    small = probe(args.host, args.port, args.sni, args.path, 1048576,
                  body_bytes=1048576)
    results["probe.1mib.status"] = small.get("status")
    results["probe.1mib.server"] = small.get("server")
    results["probe.1mib.body_head"] = small.get("body_head")

    # 2. One byte past the declared ceiling, decided from the header alone.
    over = effective + 1 if effective else 104857601
    started = time.perf_counter()
    refused = probe(args.host, args.port, args.sni, args.path, over)
    results["probe.over_ceiling.requested_bytes"] = over
    results["probe.over_ceiling.status"] = refused.get("status")
    results["probe.over_ceiling.server"] = refused.get("server")
    results["probe.over_ceiling.body_head"] = refused.get("body_head")
    results["probe.over_ceiling.note"] = refused.get("note")
    results["probe.over_ceiling.seconds"] = round(time.perf_counter() - started, 3)

    # 3. The single-file ceiling the panel promises, asked of the proxy.
    required = 200 * 1024 * 1024
    need = probe(args.host, args.port, args.sni, args.path, required + 1)
    results["probe.200mib_plus_1.status"] = need.get("status")
    results["probe.200mib_plus_1.server"] = need.get("server")
    results["probe.200mib_plus_1.note"] = need.get("note")
    results["verdict.proxy_accepts_single_file_ceiling"] = (
        need.get("status") != "413")
    results["verdict.configured_ceiling_meets_requirement"] = bool(
        effective and effective > required)

    for key in sorted(results):
        print("%s: %s" % (key, results[key]))


if __name__ == "__main__":
    main()
