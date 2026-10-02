"""Phase 5.1 evidence: page size, server-side permission paging, query batching.

Runs the three read paths against a controlled tenant of 5,000 Agents with
1,000 assignments, twice:

* ``batched`` — the shipped code, which resolves the caller's administering
  standing once per request (``_assignment_reader``).
* ``per-row`` — the same code with the pre-resolved reader removed, so every
  Agent row re-asks the identity layer. This is the 逐行鉴权查询 the spec
  forbids, kept here only as the measured baseline.

Exits non-zero if the shipped path is not constant in query count or exceeds
the paging budget, so this doubles as a regression check.
"""

import os
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, ".")
os.environ["COW_CREDENTIAL_MASTER_KEY"] = "bench-master-key"

from config import conf  # noqa: E402
from integrations.external import service as service_module  # noqa: E402
from tests._helpers import build_identity  # noqa: E402

AGENTS = 5000
ASSIGNED = 1000
QUERY_BUDGET = 12          # constant per request; independent of the two counts
LATENCY_BUDGET_S = 0.5     # a 20-row page must not approach a perceptible stall


def build():
    tmp = pathlib.Path(tempfile.mkdtemp())
    agents = tuple("agent-%04d" % i for i in range(AGENTS))
    stack = build_identity(tmp, agents=agents)
    conf()["identity_db_path"] = str(tmp / "identity.db")
    service_module._SERVICE_CACHE.clear()
    svc = service_module.ExternalConnectionService(stack.service)
    conn = svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind="mcp", name="bench-conn",
        config={"transport": "streamable_http", "url": "https://mcp.example.com/mcp"},
        secrets={"header": "h"})
    now = int(time.time())
    # 1,000 relations, written directly: the delta API caps one request at 100.
    with svc._tx() as con:
        con.execute(
            "UPDATE external_connection_agent_assignment_sets SET configured=1,"
            " revision=1 WHERE tenant_id=? AND logical_connection_id=?",
            (stack.tenant_id, conn["id"]))
        for i in range(ASSIGNED):
            con.execute(
                "INSERT INTO external_connection_agent_assignments"
                " (tenant_id, logical_connection_id, agent_id, created_by,"
                "  created_at) VALUES (?,?,?,?,?)",
                (stack.tenant_id, conn["id"], agents[i], stack.root, now))
        con.commit()
    return svc, stack, conn


def measure(svc, stack, conn, *, batched):
    calls = {"n": 0}
    real = svc._store.execute

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    svc._store.execute = counting
    original_item = svc._agent_item
    if not batched:
        def per_row_item(*a, **k):
            k.pop("reader", None)   # forces the per-row identity lookups
            return original_item(*a, **k)

        svc._agent_item = per_row_item
    try:
        out = {}
        for label, fn in (
                ("assigned page 1",
                 lambda: svc.list_agent_assignments(
                     actor_user_id=stack.root, tenant_id=stack.tenant_id,
                     connection_id=conn["id"], page=1, page_size=20)),
                ("assigned page 50",
                 lambda: svc.list_agent_assignments(
                     actor_user_id=stack.root, tenant_id=stack.tenant_id,
                     connection_id=conn["id"], page=50, page_size=20)),
                ("assigned search",
                 lambda: svc.list_agent_assignments(
                     actor_user_id=stack.root, tenant_id=stack.tenant_id,
                     connection_id=conn["id"], q="agent-0999", page=1,
                     page_size=20)),
                ("candidate search",
                 lambda: svc.search_agent_candidates(
                     actor_user_id=stack.root, tenant_id=stack.tenant_id,
                     connection_id=conn["id"], q="agent-4999", page=1,
                     page_size=20)),
                ("candidate search (no match)",
                 lambda: svc.search_agent_candidates(
                     actor_user_id=stack.root, tenant_id=stack.tenant_id,
                     connection_id=conn["id"], q="nobody-here", page=1,
                     page_size=20)),
        ):
            calls["n"] = 0
            t0 = time.time()
            res = fn()
            elapsed = time.time() - t0
            out[label] = (elapsed, calls["n"], len(res["items"]), res["total"])
        return out
    finally:
        svc._store.execute = real
        svc._agent_item = original_item


def main():
    svc, stack, conn = build()
    batched = measure(svc, stack, conn, batched=True)
    per_row = measure(svc, stack, conn, batched=False)

    print("data: %d agents, %d assigned relations, page_size=20" % (AGENTS, ASSIGNED))
    print()
    print("%-30s %12s %10s %9s %8s" % ("path", "latency", "queries", "items", "total"))
    for label in batched:
        print("%-30s %11.3fs %10d %9d %8d"
              % (label, batched[label][0], batched[label][1],
                 batched[label][2], batched[label][3]))
    print()
    print("%-30s %12s %10s" % ("pre-fix baseline (per-row auth)", "latency", "queries"))
    for label in per_row:
        print("%-30s %11.3fs %10d"
              % (label, per_row[label][0], per_row[label][1]))
    print()

    problems = []
    counts = {label: row[1] for label, row in batched.items()}
    if len(set(counts.values())) != 1:
        problems.append("query count is not constant across paths: %r" % counts)
    if max(counts.values()) > QUERY_BUDGET:
        problems.append("query count %d exceeds budget %d"
                        % (max(counts.values()), QUERY_BUDGET))
    slowest = max(row[0] for row in batched.values())
    if slowest > LATENCY_BUDGET_S:
        problems.append("slowest path %.3fs exceeds %.3fs"
                        % (slowest, LATENCY_BUDGET_S))
    for label, (_, queries, items, total) in batched.items():
        if label == "assigned page 1" and (items, total) != (20, ASSIGNED):
            problems.append("%s: expected 20/%d, got %d/%d"
                            % (label, ASSIGNED, items, total))
    if problems:
        print("FAIL")
        for problem in problems:
            print("  - %s" % problem)
        return 1
    print("PASS: %d queries on every path, slowest %.3fs, paging is server-side"
          % (max(counts.values()), slowest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
