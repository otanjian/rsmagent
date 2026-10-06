/** The project plugin install must upgrade a previously shipped revision and
 * never block workbench startup. A Windows checkout stores the identical
 * revision with CRLF, which is what broke the raw-byte digest comparison. */
import {expect, test} from "bun:test"
import {mkdtemp, mkdir, readFile, rm, writeFile} from "node:fs/promises"
import {tmpdir} from "node:os"
import {join, dirname} from "node:path"
import {guidanceDigest, installNavigationGuidance, normaliseGuidance, upgradeableGuidance} from "./native-host"

const shipped = await readFile(new URL("./navigation-guidance.js", import.meta.url), "utf8")
const pluginPath = (directory: string) => join(directory, ".opencode/plugins/rsm-sap-workbench-navigation.js")
const normalised = (text: string) => normaliseGuidance(text)

async function withProject(run: (directory: string) => Promise<void>) {
  const directory = await mkdtemp(join(tmpdir(), "sap-guidance-"))
  try {
    await run(directory)
  } finally {
    await rm(directory, {recursive: true, force: true})
  }
}

test("line endings and a BOM never change a revision's identity", () => {
  expect(normaliseGuidance("a\r\nb\r\n")).toBe("a\nb\n")
  expect(normaliseGuidance("a\rb")).toBe("a\nb")
  expect(normaliseGuidance("\ufeffa\nb")).toBe("a\nb")
  // Matches an independently computed SHA-256 of "x".
  expect(guidanceDigest("x")).toBe("2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881")
  expect(guidanceDigest("x\r\n")).toBe(guidanceDigest("x\n"))
})

test("the installed revision of the shipped guidance is not a foreign file", () => {
  // What this change ships must never be treated as a predecessor to overwrite.
  expect(upgradeableGuidance.has(guidanceDigest(shipped))).toBe(false)
  for (const digest of upgradeableGuidance) expect(digest).toMatch(/^[0-9a-f]{64}$/)
})

test("a CRLF copy of the installed revision is recognised instead of blocking startup", async () => {
  await withProject(async directory => {
    const target = pluginPath(directory)
    await mkdir(dirname(target), {recursive: true})
    // Exactly what a Windows checkout leaves behind: same text, CRLF endings.
    await writeFile(target, normalised(shipped).replace(/\n/g, "\r\n"))
    await installNavigationGuidance(directory)
    expect(process.env.RSM_SAP_WORKBENCH_NAVIGATION).toBe("1")
    expect(normalised(await readFile(target, "utf8"))).toBe(normalised(shipped))
  })
})

test("a fresh project receives the current guidance", async () => {
  await withProject(async directory => {
    await installNavigationGuidance(directory)
    expect(await readFile(pluginPath(directory), "utf8")).toBe(shipped)
  })
})

test("a recorded predecessor revision is upgraded in place", async () => {
  await withProject(async directory => {
    const target = pluginPath(directory)
    await mkdir(dirname(target), {recursive: true})
    const predecessor = "// earlier revision of the workbench plugin\nexport default {}\n"
    upgradeableGuidance.add(guidanceDigest(predecessor))
    try {
      await writeFile(target, predecessor)
      await installNavigationGuidance(directory)
      expect(await readFile(target, "utf8")).toBe(shipped)
    } finally {
      upgradeableGuidance.delete(guidanceDigest(predecessor))
    }
  })
})

test("a different file with the same plugin name is never replaced", async () => {
  await withProject(async directory => {
    const target = pluginPath(directory)
    await mkdir(dirname(target), {recursive: true})
    const foreign = "// the operator's own plugin\nexport default {}\n"
    await writeFile(target, foreign)
    await expect(installNavigationGuidance(directory)).rejects.toThrow()
    expect(await readFile(target, "utf8")).toBe(foreign)
  })
})

test("the standard-service plugin is recognised rather than blocking this host", async () => {
  // This host is replaced by a standard OpenCode plugin with the same file name
  // (`Scene/sap_workbench/project/plugins/rsm-sap-workbench-navigation.js`).
  // Registering its digest here removes deploy order as a correctness
  // requirement: whichever of the two writes the file first, the other
  // recognises it instead of refusing to start.
  const standard = await readFile(new URL("../project/plugins/rsm-sap-workbench-navigation.js", import.meta.url), "utf8")
  // Pinned as a literal because the entry must outlive the file it was computed
  // from: the retired host is deleted before this digest may be dropped. The
  // earlier revision of the same plugin stays registered next to it, so a lane
  // that already wrote it still upgrades instead of blocking.
  expect(guidanceDigest(standard)).toBe("647c3bbef423bd19aaba6ea0a5bb837c380ee645a402768017d18928efffd206")
  expect(upgradeableGuidance.has("358a9018a753ecd72d8c53240fc1b2f78370f64e68e7e594e598322a66e548f8")).toBe(true)
  expect(upgradeableGuidance.has(guidanceDigest(standard))).toBe(true)
  await withProject(async directory => {
    const target = pluginPath(directory)
    await mkdir(dirname(target), {recursive: true})
    await writeFile(target, standard)
    // Startup must not throw while the replacement plugin is in place.
    await installNavigationGuidance(directory)
    expect(await readFile(target, "utf8")).toBe(shipped)
  })
})
