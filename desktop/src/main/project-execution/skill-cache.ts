/**
 * The device's cache of verified skill versions (task 8.9).
 *
 * The server decides which skill versions a run is authorized with; the device
 * is the machine that actually runs them, so it is the machine that has to be
 * able to *prove* it is running those versions and not others. That is the whole
 * point of this file, and it is why the cache is keyed by digest and never by
 * name: "excel" names whatever excel the device happens to hold, while
 * `sha256:…` names the exact bytes the run was authorized with.
 *
 * The failure this prevents is quiet, which is what makes it worth this much
 * machinery: a device whose snapshot is stale would otherwise run the version it
 * has, produce a plausible result, and report success. Nothing downstream could
 * tell. So a declared version that is not in the cache is a **refusal**
 * (`incompatible_skill`), never a fallback to a same-named directory and never a
 * "close enough" version.
 *
 * Layout, all of it inside `root`:
 *
 *     <root>/<skill>-<id digest>/<digest hex>/
 *
 * The `skill` component is a readable prefix plus a digest of the full, untrusted
 * skill id, for the reason `agent/desktop_local/skill_cache.py::_component`
 * spells out: sanitising alone can collide (`a:b` and `a/b` becoming one
 * directory, silently mixing two skills) and cannot express a value with no safe
 * characters at all. The version component is the digest itself, so two versions
 * can never share a directory.
 *
 * Installing is atomic: the package is written into a `.partial-*` staging
 * directory and then renamed into place with a single `renameSync`, so an
 * interrupted install is never visible as a complete version. That matters
 * because the *next* run's check is "does this version exist", and a
 * half-written directory would answer yes.
 */

import * as crypto from 'node:crypto'
import * as fs from 'node:fs'
import * as path from 'node:path'

import { EXECUTION_LIMITS } from './generated-contract'
import {
  contentDigest,
  packageResources,
  SkillPackageError,
  type PackageEntry,
} from './skill-package'

/** A refused cache operation, with a stable machine-readable `code`. */
export class SkillCacheError extends Error {
  readonly code: string

  constructor(code: string, message: string) {
    super(`${code}: ${message}`)
    this.name = 'SkillCacheError'
    this.code = code
  }
}

/** One skill version a run was authorized with, as the frame declares it. */
export interface DeclaredSkill {
  skillId: string
  digest: string
}

/** The package budgets from `contracts/desktop/v2.json`. */
export interface PackageLimits {
  transferMaxBytes: number
  expandedMaxBytes: number
  filesMax: number
}

function defaultLimits(): PackageLimits {
  return {
    transferMaxBytes: EXECUTION_LIMITS.skill_package_transfer_max_bytes,
    expandedMaxBytes: EXECUTION_LIMITS.skill_package_expanded_max_bytes,
    filesMax: EXECUTION_LIMITS.skill_package_files_max,
  }
}

const SLUG_UNSAFE = /[^A-Za-z0-9._-]+/g

/**
 * One safe, collision-free path segment for an untrusted skill id.
 *
 * Mirrors the Python component rule: a readable prefix for a human reading the
 * cache, plus a digest of the full value so the mapping is injective and cannot
 * traverse. Only an empty value is refused -- that means an unset identity, and
 * caching under it would mix skills together.
 */
export function skillComponent(skillId: unknown): string {
  const text = String(skillId ?? '').trim()
  if (!text) {
    throw new SkillCacheError('invalid_skill', 'skill id is empty')
  }
  const digest = crypto.createHash('sha256').update(text, 'utf8').digest('hex').slice(0, 16)
  const slash = text.lastIndexOf('/')
  const colon = text.lastIndexOf(':')
  const readable = text.slice(Math.max(slash, colon) + 1)
  const prefix = readable.replace(SLUG_UNSAFE, '_').replace(/^[.-]+|[.-]+$/g, '').slice(0, 48)
  return prefix ? `${prefix}-${digest}` : digest
}

/** A declared digest, validated as the `sha256:<hex>` spelling both ends use. */
export function validDigest(digest: unknown): string {
  const text = String(digest ?? '').trim().toLowerCase()
  if (!/^sha256:[0-9a-f]{64}$/.test(text)) {
    throw new SkillCacheError(
      'incompatible_skill', `${JSON.stringify(digest)} is not a sha256: digest`)
  }
  return text
}

export class SkillCache {
  readonly root: string
  readonly limits: PackageLimits

  constructor(root: string, limits?: Partial<PackageLimits>) {
    this.root = path.resolve(root)
    this.limits = { ...defaultLimits(), ...(limits ?? {}) }
  }

  /** The directory one verified version lives in. Contains a path, never escapes. */
  versionDir(skillId: string, digest: string): string {
    const version = validDigest(digest).slice('sha256:'.length)
    return path.join(this.root, skillComponent(skillId), version)
  }

  /**
   * Whether a version is present *and complete*.
   *
   * Both halves matter. A directory with no `SKILL.md` is not a skill the worker
   * could run, and treating its existence as "the version is here" would let a
   * wrongly-installed package satisfy the requirement and then fail deep inside
   * the sandbox for a reason nobody could attribute.
   */
  has(skillId: string, digest: string): boolean {
    let dir: string
    try {
      dir = this.versionDir(skillId, digest)
    } catch {
      return false
    }
    try {
      const entry = fs.statSync(path.join(dir, 'SKILL.md'))
      return entry.isFile()
    } catch {
      return false
    }
  }

  /**
   * The directory for each declared version, or a refusal naming the first miss.
   *
   * This is the check the whole task exists for: the device is *required* to run
   * the versions the run was authorized with. An empty declaration is not "no
   * requirements" here -- the server records the set on the command, so a device
   * that was handed a frame with skills and declares none is asking to run
   * nothing, which is a mismatch rather than a smaller requirement.
   */
  resolveDeclared(declared: readonly DeclaredSkill[]): string[] {
    return declared.map((entry) => {
      const digest = validDigest(entry.digest)
      if (!this.has(entry.skillId, digest)) {
        throw new SkillCacheError(
          'incompatible_skill',
          `技能 ${entry.skillId} 的版本 ${digest} 不在本机缓存中：`
          + '设备只运行本运行已授权的版本，不会退回同名目录或其它版本。')
      }
      return this.versionDir(entry.skillId, digest)
    })
  }

  /**
   * Verify a package against the digest the server declared, then publish it.
   *
   * Returns the version directory. The order is deliberate: the package is fully
   * verified *before* anything is written, so a package that fails the digest
   * check leaves no directory behind for a later run to mistake for a version
   * that was installed.
   *
   * The budgets are the contract's, and they are checked here rather than only at
   * the transport, because this is the last place that can see the whole package:
   * a device that trusted the sender's accounting would be enforcing nothing.
   */
  install(skillId: string, declaredDigest: string, entries: readonly PackageEntry[]): string {
    const expected = validDigest(declaredDigest)
    if (entries.length > this.limits.filesMax) {
      throw new SkillCacheError(
        'limit_exceeded',
        `技能包文件数 ${entries.length} 超过上限 ${this.limits.filesMax}`)
    }
    let total = 0
    for (const entry of entries) total += entry.body.length
    if (total > this.limits.expandedMaxBytes) {
      throw new SkillCacheError(
        'limit_exceeded',
        `技能包展开后 ${total} 字节，超过上限 ${this.limits.expandedMaxBytes}`)
    }

    let resources
    try {
      resources = packageResources(entries)
    } catch (err) {
      if (err instanceof SkillPackageError) {
        throw new SkillCacheError(err.code, err.message)
      }
      throw err
    }

    const actual = contentDigest(resources)
    if (actual !== expected) {
      throw new SkillCacheError(
        'incompatible_skill',
        `技能包摘要不符：服务器声明 ${expected}，本机按内容算出 ${actual}；已拒绝写入`)
    }
    if (!entries.some((entry) => entry.relativePath.replace(/\\/g, '/') === 'SKILL.md')) {
      throw new SkillCacheError(
        'skill_unavailable', '技能包缺少 SKILL.md，无法作为技能挂载')
    }

    const target = this.versionDir(skillId, expected)
    if (this.has(skillId, expected)) {
      return target
    }
    fs.mkdirSync(path.dirname(target), { recursive: true })
    const staging = fs.mkdtempSync(path.join(path.dirname(target), '.partial-'))
    try {
      for (const entry of entries) {
        const relative = entry.relativePath.replace(/\\/g, '/')
        const full = path.join(staging, ...relative.split('/'))
        fs.mkdirSync(path.dirname(full), { recursive: true })
        fs.writeFileSync(full, entry.body, { mode: 0o444 })
      }
      // One rename, so a complete version never coexists with a partial one.
      fs.renameSync(staging, target)
    } catch (err) {
      try {
        fs.rmSync(staging, { recursive: true, force: true })
      } catch {
        /* the staging dir is inert; failing to remove it must not mask the cause */
      }
      throw err
    }
    return target
  }

  /** Every version of one skill currently installed, newest first by name. */
  listVersions(skillId: string): string[] {
    try {
      const dir = path.join(this.root, skillComponent(skillId))
      return fs.readdirSync(dir).filter((name) => /^[0-9a-f]{64}$/.test(name)).sort()
    } catch {
      return []
    }
  }
}
