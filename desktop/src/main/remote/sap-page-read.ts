import type { WebContents, WebFrameMain } from 'electron'
import { SAP_PAGE_DOM } from './sap-page-dom'

export type ReadRequest = { binding_id: string; read_id: string; view_id: string }
export type ReadContext = { binding_id: string; id: string; view_id: string; expires_at: number;
  sap_url: string; allowed_origins: string[] }
type Snapshot = { capturedAt: string; source: string; scope: string; title: string; fields: unknown[];
  currentUser?: {account: string; client: string | null; systemId: string | null; source: 'sap_session_ui'; sourceDocument?: number} | null;
  tables: unknown[]; selection: unknown[]; activeTabs: unknown[]; messages: unknown[]; text: string; limitations: string[] }

const fail = (code: string): never => { throw Object.assign(new Error(code), { code }) }
const origin = (url: string): string => { try { return new URL(url).origin } catch { return '' } }

/** Reads only a server-verified pending request in this container. */
export async function readSapPage(contents: WebContents, request: ReadRequest,
  context: () => Promise<ReadContext>, platform = process.platform): Promise<Snapshot> {
  if (platform !== 'darwin') return fail('page_read_unsupported')
  const allowed = await context()
  if (allowed.binding_id !== request.binding_id || allowed.id !== request.read_id || allowed.view_id !== request.view_id ||
      allowed.expires_at <= Date.now()) return fail('page_changed')
  const main = contents.mainFrame
  const candidates = main.frames.filter(f => f.name === 'sap-gui-' + request.binding_id)
  if (candidates.length !== 1) return fail('page_read_unavailable')
  const root = candidates[0]
  const currentRoot = (): boolean => {
    const current = main.frames.filter(f => f.name === 'sap-gui-' + request.binding_id)
    return current.length === 1 && current[0] === root
  }
  if (root.detached || origin(root.url) !== origin(allowed.sap_url)) return fail('page_changed')
  const origins = new Set([origin(allowed.sap_url), ...allowed.allowed_origins])
  const allFrames = root.framesInSubtree
  const frames = allFrames.filter(f => origins.has(origin(f.url))).slice(0, 8)
  const identities = frames.map(f => ({frame: f, process: f.processId, routing: f.routingId, url: f.url}))
  let changed = false
  const navigated = (_event: unknown, _url: string, _httpResponseCode: number, _httpStatusText: string,
    isMainFrame: boolean, processId: number, routingId: number): void => {
    if (isMainFrame || identities.some(i => i.process === processId && i.routing === routingId)) changed = true
  }
  contents.on('did-frame-navigate', navigated)
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    const samples: Snapshot[] = await Promise.race([
      Promise.all(frames.map(async (frame: WebFrameMain) => {
        const sample = await frame.executeJavaScript(SAP_PAGE_DOM) as Snapshot & {error?: string}
        if (sample?.error) return fail(sample.error === 'login_required' ? 'login_required' : 'extract_failed')
        if (!sample || sample.source !== 'sap_page_dom') return fail('extract_failed')
        return sample as Snapshot
      })),
      new Promise<never>((_resolve, reject) => { timer = setTimeout(() => reject(Object.assign(new Error('extract_failed'), {code: 'extract_failed'})), 5000) }),
    ])
    if (!samples.length || contents.isDestroyed() || contents.mainFrame !== main || changed ||
        identities.some(i => i.frame.detached || i.frame.processId !== i.process || i.frame.routingId !== i.routing || i.frame.url !== i.url)) return fail('page_changed')
    // Recheck the authenticated pending request after collection, before bytes leave the host.
    await context()
    if (changed || contents.isDestroyed() || contents.mainFrame !== main || !currentRoot() || identities.some(i => i.frame.detached)) return fail('page_changed')
    if (samples.length > 1) samples.forEach((sample, index) => {
      for (const key of ['fields', 'tables'] as const) {
        sample[key] = sample[key].map(item => ({...(item as object), sourceDocument: index + 1}))
      }
      for (const key of ['selection', 'activeTabs', 'messages'] as const) {
        sample[key] = sample[key].map(item => `SAP 文档 ${index + 1}: ${String(item)}`)
      }
      sample.text = `SAP 文档 ${index + 1}:\n${sample.text}`
    })
    const users = samples.flatMap((sample, index) => sample.currentUser && origin(frames[index].url) === origin(allowed.sap_url)
      ? [{...sample.currentUser, sourceDocument:index + 1}] : [])
    const userConflict = samples.some(sample => sample.limitations.includes('页面会话用户信息冲突，无法确认当前 SAP 登录账号。')) ||
      (['account', 'client', 'systemId'] as const).some(key => new Set(users.map(user => user[key]).filter(Boolean)).size > 1)
    const result = samples[0]
    result.currentUser = userConflict ? null : users.sort((a,b) => Number(!!b.client)+Number(!!b.systemId)-Number(!!a.client)-Number(!!a.systemId))[0] || null
    if (userConflict) result.limitations.push('SAP 文档间的会话用户信息冲突，当前登录账号未知。')
    for (let i = 1; i < samples.length; i++) {
      const sample = samples[i]
      for (const key of ['fields', 'tables', 'selection', 'activeTabs', 'messages'] as const) result[key].push(...sample[key])
      result.text += '\n' + sample.text
      for (const limitation of sample.limitations) {
        if (!result.limitations.includes(limitation)) result.limitations.push(limitation)
      }
      result.limitations.push(`SAP 文档 ${i + 1}（${origin(frames[i].url)}）采集于 ${sample.capturedAt}；不是跨文档原子快照。`)
    }
    if (frames.length !== allFrames.length) result.limitations.push('部分嵌套文档未读取。')
    if (result.currentUser) result.limitations = result.limitations.filter(note =>
      note !== '页面未显示可确认的 SAP 会话用户信息；当前账号未知，不使用已保存账号或业务字段推断。')
    if (Buffer.byteLength(JSON.stringify(result), 'utf8') > 128 * 1024) {
      result.limitations.push('结果超过大小上限，部分表格/字段/文本已截断。')
      result.text = result.text.slice(0, 8000)
      while (Buffer.byteLength(JSON.stringify(result), 'utf8') > 128 * 1024) {
        const table = result.tables.find(t => Array.isArray((t as {rows?: unknown[]}).rows) && (t as {rows: unknown[]}).rows.length)
        if (table) (table as {rows: unknown[]}).rows.pop()
        else if (result.fields.length) result.fields.pop()
        else return fail('result_too_large')
      }
    }
    return result
  } finally {
    clearTimeout(timer)
    if (!contents.isDestroyed()) contents.removeListener('did-frame-navigate', navigated)
  }
}
