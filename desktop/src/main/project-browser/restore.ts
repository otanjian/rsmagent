// Resuming a bound local project after the page was (re)loaded (change
// align-desktop-project-execution-with-master, task 9.3).
//
// The page's own variables do not survive a reload; the *confirmation* does,
// because it lives in the main process. This is the decision that turns one into
// the other, and it is deliberately a pure function: the interesting questions
// are "whose project is this" and "is it still authorized", and both are easier
// to get wrong than to test through a few layers of IPC.
//
// Two properties are the whole point:
//
//   * the answer is derived from the *live* grant registry, never from the
//     record alone -- a record outlives the grant it names, so handing it back
//     unchecked would present a revoked directory as an open project;
//   * a chat is only ever resumed into *its own* project: a record left behind by
//     another Agent or chat answers ``none``, which is "nothing to resume",
//     never "here is the last thing this machine had open".
//
// A directory is not part of any answer. The page gets identifiers to name in
// later requests; where the project actually is stays in this process (task 9.2).

/** One confirmed local project, as the container recorded it. */
export interface LocalProjectRecord {
  workspaceId: string
  grantId: string
  bindingId: string
  /** The chat the confirmation was made for. */
  agentId: string
  businessSessionId: string
}

/** The live half of a binding, re-read on every call. */
export interface LiveLocalProject {
  workspaceId: string
  bindingId: string
  grantId: string
  grantVersion: number
  selectionGeneration: number
  label: string
  executable: boolean
  /**
   * Whether the device connection is up *now*.
   *
   * Reported, not enforced: a reconnecting device is a transport state, and
   * discarding an authorization the user deliberately created because the socket
   * blinked would be a worse answer than telling the page the truth and letting
   * its reads fail loudly if the connection does not come back.
   */
  connected: boolean
}

/** What the page is told. Identifiers only -- never a path. */
export interface LiveLocalProjectWire {
  workspace_id: string
  binding_id: string
  grant_id: string
  grant_version: number
  selection_generation: number
  label: string
  executable: boolean
  connected: boolean
}

export type LocalContextAnswer =
  | { state: 'live'; binding: LiveLocalProjectWire }
  | { state: 'stale'; reason: 'grant_revoked' }
  | { state: 'none' }

/**
 * The live local project for one chat, or why there is none.
 *
 * ``records`` is the container's confirmation table and ``live`` is the same
 * lookup the panel uses, so "is this project authorized" has one answer in the
 * process. ``none`` and ``stale`` are different answers on purpose: a chat that
 * never opened a local project is not a failure and must not produce a warning,
 * while a chat whose project went away is something the user needs to be told.
 */
export function restoreLocalContext(
  request: { agentId: string; businessSessionId: string },
  records: LocalProjectRecord[],
  live: (workspaceId: string) => LiveLocalProject | null,
): LocalContextAnswer {
  const mine = (records || []).filter((entry) => (
    entry.agentId === request.agentId
    && entry.businessSessionId === request.businessSessionId))
  if (mine.length === 0) return { state: 'none' }
  // Newest confirmation wins. A chat has one project open, so two records can
  // only be the leftovers of a re-pick, and the later one is the one in effect.
  const recorded = mine[mine.length - 1]
  const current = live(recorded.workspaceId)
  if (!current) return { state: 'stale', reason: 'grant_revoked' }
  return {
    state: 'live',
    binding: {
      workspace_id: current.workspaceId,
      binding_id: current.bindingId,
      grant_id: current.grantId,
      grant_version: current.grantVersion,
      selection_generation: current.selectionGeneration,
      label: current.label,
      executable: current.executable,
      connected: current.connected,
    },
  }
}
