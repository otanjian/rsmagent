# encoding:utf-8
"""The numbers ``evidence.md`` quotes for tasks 7.3/7.4/8.2/8.3/8.4.

Not a pytest file: this is the acceptance script the evidence section runs. It
drives the console's own WSGI app the way the browser does -- **one file per
request, three in flight** -- and prints what it observed, so the scale claim
("5000 small files and one 200MB file share one code path"), the byte
accounting (the panel's readout vs the bytes on disk) and the destructive walk
(delete -> restore -> occupied restore -> purge) rest on measurements rather
than on a restatement of the design.

Two things it deliberately does not measure, because neither is observable from
in-process WSGI:

* the browser's ``upload.onprogress`` arithmetic -- that is the Node suite
  (``tests/test_console_workspace_upload_frontend.cjs``);
* the reverse proxy's body limit -- that is a separate probe over real HTTP
  (``scripts/verify_workspace_proxy_limit.py``), because this script never
  touches the proxy.

Run::

    python scripts/verify_workspace_upload_scale.py
    python scripts/verify_workspace_upload_scale.py --files 200 --big-mb 8   # smoke
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._helpers import WebAppHarness  # noqa: E402

SHARED = "shared-agent"
PRIVATE = "private-agent"
BOUNDARY = "----cowscale"

#: A 1 KiB block that is *not* one repeated character, so a file that arrives
#: with the right length and the wrong contents still fails a digest comparison.
BLOCK = "0123456789abcdef" * 64


def payload(size: int) -> str:
    """Deterministic ASCII content of exactly ``size`` bytes."""
    whole, rest = divmod(size, len(BLOCK))
    return BLOCK * whole + BLOCK[:rest]


def multipart(fields, filename=None, content=""):
    """One multipart body, exactly as the panel's XHR sends it.

    A ``str`` because web.py's ``application.request`` re-encodes one; every
    payload here is ASCII, so the round-trip is byte-exact.
    """
    body = ""
    for name, value in fields:
        body += ("--%s\r\n"
                 "Content-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                 % (BOUNDARY, name, value))
    if filename is not None:
        body += ("--%s\r\n"
                 "Content-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
                 "Content-Type: application/octet-stream\r\n\r\n"
                 % (BOUNDARY, filename))
        body += content
        body += "\r\n"
    body += "--%s--\r\n" % BOUNDARY
    return body


class Panel:
    """One logged-in member driving the panel's endpoints.

    ``agent_rel`` is the Agent's directory relative to the file API's root (the
    tenant's shared root in database mode), which is the prefix every panel
    request carries. ``ws`` is the same directory on disk.
    """

    def __init__(self, web, username, agent, agent_rel, ws):
        self.web = web
        self.agent = agent
        self.agent_rel = agent_rel
        self.ws = ws
        # Logged in once and reused, as the browser does. Re-authenticating per
        # request would measure password hashing instead of the endpoint.
        self.token = web.login(username)
        self.user_id = web.user_id(username)

    # -- filesystem ------------------------------------------------------
    def disk(self, rel):
        """``rel`` relative to the Agent's own directory, as a disk path."""
        return os.path.join(self.ws, rel.replace("/", os.sep))

    def user_rel(self):
        return "%s/user/%s" % (self.agent_rel, self.user_id)

    # -- wire ------------------------------------------------------------
    def upload(self, target_dir, *, relative=None, filename="a.txt", content="x"):
        fields = [("dir", target_dir)]
        if relative:
            fields.append(("relative_path", relative))
        body = multipart(fields, filename=filename, content=content)
        response = self.web.post(
            "/api/workspace/upload?agent=" + self.agent, body, token=self.token,
            headers={"Content-Type": "multipart/form-data; boundary=" + BOUNDARY})
        return len(body.encode("utf-8")), json.loads(response.data.decode("utf-8"))

    def delete(self, targets):
        response = self.web.post("/api/workspace/delete?agent=" + self.agent,
                                 {"targets": list(targets)}, token=self.token)
        return json.loads(response.data.decode("utf-8"))

    def trash(self):
        response = self.web.get("/api/workspace/trash?agent=" + self.agent,
                                token=self.token)
        return json.loads(response.data.decode("utf-8"))

    def restore(self, batch_id, indices=None):
        body = {"batch_id": batch_id}
        if indices is not None:
            body["indices"] = list(indices)
        response = self.web.post("/api/workspace/trash/restore?agent=" + self.agent,
                                 body, token=self.token)
        return json.loads(response.data.decode("utf-8"))

    def purge(self, batch_id=None):
        body = {} if batch_id is None else {"batch_id": batch_id}
        response = self.web.post("/api/workspace/trash/purge?agent=" + self.agent,
                                 body, token=self.token)
        return json.loads(response.data.decode("utf-8"))

    def tree(self, path, *, show_hidden=False):
        response = self.web.get(
            "/api/workspace/tree?agent=%s&path=%s&show_hidden=%d"
            % (self.agent, path, 1 if show_hidden else 0), token=self.token)
        return json.loads(response.data.decode("utf-8"))

    def search(self, query, *, limit=100):
        response = self.web.get(
            "/api/workspace/search?agent=%s&q=%s&limit=%d"
            % (self.agent, query, limit), token=self.token)
        return json.loads(response.data.decode("utf-8"))


def make_harness(root):
    """The pytest fixture's shape: one shared Agent the tenant owns, plus one
    private Agent of Alice's, whose whole directory is her writable range."""
    web = WebAppHarness(root)
    shared_ws = os.path.join(web.shared_root, "agents", SHARED)
    web.write_roster([{"id": SHARED, "name": "Shared", "workspace": shared_ws}])
    web.add_agent(SHARED)
    web.member("alice", ["member"])
    web.member("bob", ["member"])
    web.private_agent(web.user_id("alice"), PRIVATE)
    private_ws = os.path.join(web.shared_root, "agents", PRIVATE)
    for path in (os.path.join(shared_ws, "memory"), os.path.join(private_ws, "memory")):
        os.makedirs(path, exist_ok=True)
    return web, shared_ws, private_ws


def walk(base):
    """``{relative path: size}`` for every file under ``base``."""
    found = {}
    for dirpath, _dirnames, filenames in os.walk(base):
        for name in filenames:
            full = os.path.join(dirpath, name)
            found[os.path.relpath(full, base).replace("\\", "/")] = os.path.getsize(full)
    return found


def digests(base):
    out = {}
    for rel in walk(base):
        with open(os.path.join(base, rel.replace("/", os.sep)), "rb") as handle:
            out[rel] = hashlib.sha256(handle.read()).hexdigest()
    return out


# ----------------------------------------------------------------------
# 8.2 -- scale: many small files, and one 200MB file, on one code path
# ----------------------------------------------------------------------
def scale_plan(count, folders=12, subs=3):
    """``[(relative_path, content)]`` with structure several levels deep."""
    plan = []
    for index in range(count):
        size = 1 + (index * 7919) % 16384
        plan.append(("tree/g%02d/s%02d/f%04d.txt"
                     % (index % folders, (index // folders) % subs, index),
                     payload(size)))
    return plan


def measure_scale(panel, target_dir, disk_dir, count, concurrency, folders=12, subs=3):
    plan = scale_plan(count, folders, subs)
    expected_total = sum(len(content.encode("utf-8")) for _rel, content in plan)
    sent = [0] * len(plan)
    results = [None] * len(plan)
    settled = []
    started = time.perf_counter()

    def one(index):
        rel, content = plan[index]
        return index, panel.upload(target_dir, relative=rel,
                                   filename=rel.rpartition("/")[2],
                                   content=content)

    running = 0

    def absorb(index, body_bytes, result):
        nonlocal running
        sent[index] = body_bytes
        results[index] = result
        # The panel's readout is "bytes of files already settled, plus the
        # current file's own progress", so the running total of settled file
        # sizes is exactly the number it prints.
        running += len(plan[index][1].encode("utf-8"))
        settled.append(running)

    workers = max(1, concurrency)
    if workers == 1:
        for index in range(len(plan)):
            body_bytes, result = one(index)[1]
            absorb(index, body_bytes, result)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for index, (body_bytes, result) in pool.map(one, range(len(plan))):
                absorb(index, body_bytes, result)

    elapsed = time.perf_counter() - started
    ok = [r for r in results if r and r.get("status") == "success"]
    on_disk = walk(panel.disk(disk_dir))
    plan_sizes = dict((rel, len(content.encode("utf-8"))) for rel, content in plan)
    mismatches = sorted(
        rel for rel in set(list(on_disk) + list(plan_sizes))
        if on_disk.get(rel) != plan_sizes.get(rel))
    return {
        "files": count,
        "folders": folders,
        "concurrency": workers,
        "requests": len(plan),
        "saved": len(ok),
        "failed": len(plan) - len(ok),
        "failure_details": [
            {"rel": plan[i][0], "status": r.get("status"), "code": r.get("code"),
             "message": (r.get("message") or "")}
            for i, r in enumerate(results) if not (r and r.get("status") == "success")],
        "renamed": sum(1 for r in ok if r.get("renamed")),
        "declared_bytes": expected_total,
        "sum_served_bytes": sum(r.get("size") or 0 for r in ok),
        "on_disk_files": len(on_disk),
        "on_disk_bytes": sum(on_disk.values()),
        "structure_mismatches": len(mismatches),
        "request_body_bytes": sum(sent),
        "request_overhead_ratio": round(sum(sent) / max(1, expected_total), 4),
        "wall_seconds": round(elapsed, 3),
        "mean_request_ms": round(1000 * elapsed / max(1, len(plan)), 2),
        "progress_events": len(settled),
        "progress_monotonic": all(b >= a for a, b in zip(settled, settled[1:])),
        "progress_final_bytes": settled[-1] if settled else 0,
        "progress_final_vs_disk": round(
            abs((settled[-1] if settled else 0) - sum(on_disk.values()))
            / max(1, expected_total), 4),
    }


def measure_big(panel, target_dir, disk_dir, size_bytes):
    content = payload(size_bytes)
    expected_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    started = time.perf_counter()
    _sent, result = panel.upload(target_dir, relative="big/one.bin",
                                 filename="one.bin", content=content)
    elapsed = time.perf_counter() - started
    landed = panel.disk(disk_dir + "/big/one.bin")
    actual_digest = None
    if os.path.isfile(landed):
        with open(landed, "rb") as handle:
            actual_digest = hashlib.sha256(handle.read()).hexdigest()
    return {
        "declared_bytes": size_bytes,
        "status": result.get("status"),
        "code": result.get("code"),
        "response_bytes": result.get("size"),
        "on_disk_bytes": os.path.getsize(landed) if os.path.isfile(landed) else None,
        "digest_match": actual_digest == expected_digest,
        "wall_seconds": round(elapsed, 3),
        "mb_per_second": round(size_bytes / 1e6 / max(elapsed, 1e-6), 1),
    }


def measure_over_limit(panel, target_dir, disk_dir, size_bytes):
    """One byte over the server's ceiling, then a small file behind it.

    The small file is the point (design D6): a refused upload is one refused
    *request*, so the rest of the drop still lands.
    """
    _sent, refused = panel.upload(target_dir, relative="over/too-big.bin",
                                  filename="too-big.bin",
                                  content=payload(size_bytes))
    _sent2, neighbour = panel.upload(target_dir, relative="over/fine.txt",
                                     filename="fine.txt", content="ok")
    directory = panel.disk(disk_dir + "/over")
    names = sorted(os.listdir(directory)) if os.path.isdir(directory) else []
    return {
        "oversize_bytes": size_bytes,
        "status": refused.get("status"),
        "code": refused.get("code"),
        "message": (refused.get("message") or "")[:70],
        "no_partial_file": "too-big.bin" not in names,
        "leftovers": [n for n in names if n.endswith(".part")],
        "neighbour_status": neighbour.get("status"),
        "directory": names,
    }


# ----------------------------------------------------------------------
# 8.3 -- retry carries only what failed, and the residual is visible
# ----------------------------------------------------------------------
def measure_retry(panel, target_dir, disk_dir):
    good = [("retry/ok-%d.txt" % i, payload(1024)) for i in range(5)]
    # Two items that will keep failing: an escaping relative path and the bin
    # itself. Neither is transient, which is what makes them useful here -- the
    # point is the *shape* of the second round, not that it eventually succeeds.
    bad = [("retry/../escape.txt", payload(64)),
           ("retry/../../user/x/.trash/hidden.txt", payload(64))]
    first_round = []
    for rel, content in good + bad:
        _bytes, result = panel.upload(target_dir, relative=rel,
                                      filename=os.path.basename(rel),
                                      content=content)
        first_round.append({rel: result.get("code") or result.get("status")})
    failed = list(bad)

    # Round two re-sends the failed items and nothing else. That is the whole of
    # the client's retry logic, and it needs no server state: the first round's
    # response already said which items those were.
    second_round = []
    for rel, content in failed:
        _bytes, result = panel.upload(target_dir, relative=rel,
                                      filename=os.path.basename(rel),
                                      content=content)
        second_round.append({rel: result.get("code") or result.get("status")})

    # The D5 residual, measured rather than asserted: re-sending a file the
    # server already saved produces a *visible* ``(1)`` copy, not a silent one.
    before_duplicate = sorted(walk(panel.disk(disk_dir + "/retry")))
    _bytes, duplicate = panel.upload(target_dir, relative=good[0][0],
                                     filename=os.path.basename(good[0][0]),
                                     content=good[0][1])
    return {
        "round1": first_round,
        "round2": second_round,
        "on_disk_before_duplicate": before_duplicate,
        "duplicate_result": {k: duplicate.get(k)
                             for k in ("status", "name", "renamed")},
        "on_disk_after_duplicate": sorted(walk(panel.disk(disk_dir + "/retry"))),
    }


# ----------------------------------------------------------------------
# 8.4 -- destructive verification, on a real tree
# ----------------------------------------------------------------------
def measure_destructive(panel, target_dir, disk_dir):
    base = panel.disk(disk_dir)
    seed = {
        "dir-a/a.txt": payload(4096),
        "dir-a/b.bin": payload(1024 * 1024 + 7),
        "dir-a/sub/c.txt": payload(33),
        "dir-a/sub/deep/d.txt": payload(70000),
        "dir-a/中文名.txt": payload(19),
    }
    for rel, content in seed.items():
        full = os.path.join(base, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as handle:
            handle.write(content.encode("utf-8"))
    before = digests(base)

    target = "%s/dir-a" % target_dir
    out = {}

    deleted = panel.delete([target])
    out["delete_deleted"] = deleted.get("deleted")
    out["delete_reclaimed"] = deleted.get("reclaimed")
    out["delete_source_gone"] = not os.path.exists(panel.disk(disk_dir + "/dir-a"))

    batch = deleted.get("batch_id")
    entries = [e for e in panel.trash().get("entries", []) if e.get("rel") == target]
    out["trash_holds_it"] = len(entries) == 1
    out["trash_entry"] = ({k: entries[0].get(k) for k in ("rel", "kind", "size")}
                          if entries else None)

    restored = panel.restore(batch, indices=[entries[0]["index"]] if entries else None)
    out["restore_restored"] = restored.get("restored")
    out["restore_failed"] = restored.get("failed")
    after = digests(base)
    out["restore_identical"] = after == before
    out["restore_paths"] = sorted(after)
    out["batch_removed_after_full_restore"] = not [
        e for e in panel.trash().get("entries", []) if e.get("batch_id") == batch]

    # -- occupied destination: the occupant must survive, the restore renames --
    second = panel.delete([target])
    occupied = panel.disk(disk_dir + "/dir-a/occupant.txt")
    os.makedirs(os.path.dirname(occupied), exist_ok=True)
    with open(occupied, "wb") as handle:
        handle.write(b"occupier")
    restored_two = panel.restore(second.get("batch_id"))
    out["occupied_restore"] = restored_two.get("restored")
    out["occupier_untouched"] = (
        os.path.isfile(occupied)
        and open(occupied, "rb").read() == b"occupier")
    out["original_location_now"] = sorted(walk(panel.disk(disk_dir + "/dir-a")))
    renamed = digests(panel.disk(disk_dir + "/dir-a (1)"))
    out["renamed_copy_identical"] = renamed == {
        rel[len("dir-a/"):]: digest for rel, digest in before.items()
        if rel.startswith("dir-a/")}

    # -- purge: the bin's own bytes are the only thing destroyed ------------
    third = panel.delete(["%s/dir-a (1)" % target_dir])
    purge = panel.purge(third.get("batch_id"))
    out["purge"] = {"status": purge.get("status"),
                    "purged": len(purge.get("purged") or []),
                    "failed": purge.get("failed")}
    out["purge_removed_from_bin"] = [
        e.get("rel") for e in panel.trash().get("entries", [])]
    out["purge_left_live_files_alone"] = sorted(walk(base))
    empty = panel.purge()
    out["empty_all"] = {"status": empty.get("status"),
                        "purged": len(empty.get("purged") or []),
                        "failed": empty.get("failed")}
    out["trash_now"] = [e.get("rel") for e in panel.trash().get("entries", [])]
    return out


def measure_guards(panel):
    """Every refusal the spec names, by its stable code."""
    cases = {
        "agent_internal(file)": "%s/AGENT.md" % panel.agent_rel,
        "agent_internal(dir)": "%s/memory" % panel.agent_rel,
        "user_container": "%s/user" % panel.agent_rel,
        "own_user_dir": panel.user_rel(),
        "trash_not_targetable": "%s/.trash" % panel.user_rel(),
        "range_root": panel.agent_rel,
        "escape": "%s/drop/../../AGENT.md" % panel.agent_rel,
        "absolute": "/etc/passwd",
        "missing": "%s/drop/nope.txt" % panel.agent_rel,
    }
    out = {}
    for label, target in cases.items():
        response = panel.delete([target])
        failed = response.get("failed") or []
        out[label] = failed[0].get("code") if failed else "MISSED"
    _bytes, uploaded = panel.upload("%s/.trash" % panel.user_rel(),
                                    relative="x.txt", filename="x.txt")
    out["upload.into_trash"] = uploaded.get("code") or "MISSED"
    return out


def measure_bin_hidden(panel, marked):
    """The bin is hidden by rule, not by its dot prefix (``show_hidden=1``)."""
    # A visible neighbour first, so a listing that omits ``.trash`` is proof of
    # an exclusion rather than of an empty directory.
    panel.upload(panel.user_rel(), relative="mine.txt", filename="mine.txt")
    panel.delete(["%s/%s" % (panel.agent_rel, marked)])
    tree = panel.tree(panel.user_rel(), show_hidden=True)
    names = [e.get("name") for e in tree.get("entries", [])]
    hit = panel.search(marked.rpartition("/")[2], limit=100)
    return {
        "tree_with_show_hidden": names,
        "dot_trash_listed": ".trash" in names,
        "search_hits_for_trashed_file": [r.get("path") for r in hit.get("results", [])],
        "search_leaks": [r.get("path") for r in hit.get("results", [])
                         if ".trash" in (r.get("path") or "")],
    }


def measure_other_member(web, shared_ws):
    """Alice must not reach the shared Agent's internals or Bob's subtree."""
    alice = Panel(web, "alice", SHARED, "agents/%s" % SHARED, shared_ws)
    bob_id = web.user_id("bob")
    out = {}
    for label, target in {
        "shared_agent_internal": "agents/%s/memory" % SHARED,
        "colleague_subtree": "agents/%s/user/%s" % (SHARED, bob_id),
        "shared_root": "agents/%s" % SHARED,
    }.items():
        response = alice.delete([target])
        failed = response.get("failed") or []
        out[label] = failed[0].get("code") if failed else "MISSED"
    _bytes, uploaded = alice.upload("agents/%s/user/%s" % (SHARED, bob_id),
                                    relative="x.txt", filename="x.txt")
    out["upload_to_colleague"] = uploaded.get("code") or "MISSED"
    out["own_subtree_writable"] = alice.upload(
        alice.user_rel(), relative="mine.txt", filename="mine.txt")[1].get("status")
    return out


# ----------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--files", type=int, default=5000)
    parser.add_argument("--big-mb", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--only", default="")
    args = parser.parse_args()

    try:  # the console here is cp936; the results carry UTF-8 paths
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - old interpreters
        pass

    root = tempfile.mkdtemp(prefix="rsm-scale-")
    web, shared_ws, private_ws = make_harness(root)
    panel = Panel(web, "alice", PRIVATE, "agents/%s" % PRIVATE, private_ws)
    results = {}
    try:
        if args.only in ("", "scale"):
            results.update({"scale." + k: v for k, v in measure_scale(
                panel, "%s/drop" % panel.agent_rel, "drop",
                args.files, args.concurrency).items()})
        if args.only in ("", "big"):
            results.update({"big." + k: v for k, v in measure_big(
                panel, "%s/drop" % panel.agent_rel, "drop",
                args.big_mb * 1024 * 1024).items()})
        if args.only in ("", "limit"):
            results.update({"over_limit." + k: v for k, v in measure_over_limit(
                panel, "%s/drop" % panel.agent_rel, "drop",
                args.big_mb * 1024 * 1024 + 1024).items()})
        if args.only in ("", "retry"):
            results.update({"retry." + k: v for k, v in measure_retry(
                panel, "%s/retrydrop" % panel.agent_rel, "retrydrop").items()})
        if args.only in ("", "guards"):
            results.update({"guards." + k: v for k, v in measure_guards(panel).items()})
        if args.only in ("", "hidden"):
            results.update({"hidden." + k: v for k, v in measure_bin_hidden(
                panel, "drop/big/one.bin").items()})
        if args.only in ("", "boundary"):
            results.update({"boundary." + k: v for k, v in measure_other_member(
                web, shared_ws).items()})
        if args.only in ("", "destructive"):
            results.update({"destructive." + k: v for k, v in measure_destructive(
                panel, "%s/trashwalk" % panel.agent_rel, "trashwalk").items()})
    finally:
        web.close()
        shutil.rmtree(root, ignore_errors=True)

    for key in sorted(results):
        print("%s: %s" % (key, results[key]))


if __name__ == "__main__":
    main()
