import {expect, test} from "bun:test"
import {mkdtemp, rm} from "node:fs/promises"
import {tmpdir} from "node:os"
import {join} from "node:path"

const root = process.env.SAP_OPENCODE_ROOT

/**
 * A host must not outlive the interpreter that started it.
 *
 * The parent writes the bootstrap as one line and then holds the pipe open for
 * the host's whole life, so end-of-file on that pipe means the parent is gone.
 * The `process.ppid === 1` reparenting test in server.ts cannot cover this on
 * Windows, which keeps the dead parent's id: a host whose interpreter was killed
 * used to keep its listener and its whole engine (~450MB measured) until someone
 * noticed by hand, and that memory pressure on a small machine is what makes the
 * next engine start slow.
 */
test.skipIf(!root)("a host stops itself when the parent closes the bootstrap pipe", async () => {
  const directory = await mkdtemp(join(tmpdir(), "sap-host-parent-exit-"))
  // The host points its model and bridge here; neither is ever called.
  const deadEnd = Bun.serve({hostname: "127.0.0.1", port: 0, fetch: () => new Response("unused", {status: 503})})
  const child = Bun.spawn([Bun.which("bun")!, join(import.meta.dir, "server.ts")], {
    cwd: root!, stdin: "pipe", stdout: "pipe", stderr: "ignore",
    env: Object.fromEntries(["PATH", "HOME", "TMPDIR", "LANG"].flatMap(key => process.env[key] ? [[key, process.env[key]!]] : [])),
  })
  try {
    child.stdin.write(JSON.stringify({directory, root, token: crypto.randomUUID(),
      modelURL: `${deadEnd.url}model`, model: "test-only", bridgeURL: `${deadEnd.url}bridge/`,
      service: "parent-exit", displayMode: "screen"}) + "\n")
    // Wait until it is actually serving, so the exit below is attributable to the
    // closed pipe rather than to a start that never got that far.
    let pending = "", port = 0
    for await (const chunk of child.stdout) {
      pending += new TextDecoder().decode(chunk)
      const lines = pending.split("\n")
      pending = lines.pop()!
      for (const line of lines) {
        try { const result = JSON.parse(line); if (Number.isInteger(result.port)) port = result.port } catch {}
      }
      if (port) break
    }
    expect(port).toBeGreaterThan(0)
    expect((await fetch(`http://127.0.0.1:${port}/api/health`)).status).toBe(401)

    child.stdin.end()
    const exited = await Promise.race([child.exited.then(() => true), Bun.sleep(15000).then(() => false)])
    expect(exited, "the host must stop when the parent that holds the pipe goes away").toBe(true)
  } finally {
    child.kill()
    await child.exited
    deadEnd.stop(true)
    await rm(directory, {recursive: true, force: true})
  }
}, 60000)
