import type { Cookie, Session, WebContents, WebFrameMain } from 'electron'
import { createHash } from 'crypto'
import * as fs from 'fs'
import * as path from 'path'

export type SapLoginContext = { binding_id: string; sap_url: string; client: string }
export type SapLoginScope = { server: string; user: string; tenant: string; epoch: number }
export type SapCredentials = { user: string; password: string; client: string; language: string }
type RecordData = { version: 1; updated: number; cookies: Cookie[]; credentials?: SapCredentials }
type Cipher = { isEncryptionAvailable(): boolean; encryptString(value: string): Buffer; decryptString(value: Buffer): string }
const fail = (code: string): never => { throw Object.assign(new Error(code), {code}) }

export function loginScopeKey(scope: SapLoginScope, target: SapLoginContext): string {
  const url = new URL(target.sap_url)
  if (url.protocol !== 'https:' || url.username || url.password || !/^\d{3}$/.test(target.client) ||
      !scope.user || !scope.tenant || !scope.server || url.origin === new URL(scope.server).origin) return fail('sap_login_unavailable')
  return createHash('sha256').update(JSON.stringify([scope.server, scope.user, scope.tenant,
    url.origin, url.pathname, target.client])).digest('hex')
}

/** Only the exact SAP host's cookies are persisted, never platform or SSO cookies. */
export function sapCookie(cookie: Cookie, target: string): boolean {
  return cookie.domain?.replace(/^\./, '') === new URL(target).hostname &&
    (!cookie.expirationDate || cookie.expirationDate > Date.now() / 1000)
}

export function loginSubmission(body: string, client: string): SapCredentials | undefined {
  if (Buffer.byteLength(body) > 16384) return
  const data = new URLSearchParams(body)
  if (['sap-user', 'sap-password', 'sap-client'].some(key => data.getAll(key).length !== 1)) return
  const user = data.get('sap-user') || '', password = data.get('sap-password') || ''
  if (!user || user.length > 128 || !password || password.length > 1024 || data.get('sap-client') !== client) return
  return {user, password, client, language: (data.get('sap-language') || '').slice(0, 8)}
}

/** macOS safeStorage protects the encryption key in Keychain; disk contains ciphertext only. */
export class SapLoginVault {
  constructor(private directory: string, private cipher: Cipher) {}
  available(): boolean { return this.cipher.isEncryptionAvailable() }
  private file(key: string): string {
    if (!/^[a-f0-9]{64}$/.test(key)) return fail('sap_login_unavailable')
    return path.join(this.directory, key + '.bin')
  }
  read(key: string): RecordData | undefined {
    if (!this.available()) return fail('sap_keychain_unavailable')
    try {
      const bytes = fs.readFileSync(this.file(key))
      if (bytes.length > 1024 * 1024) return fail('sap_login_unavailable')
      const record = JSON.parse(this.cipher.decryptString(bytes)) as RecordData
      if (record.version !== 1 || !Array.isArray(record.cookies)) return fail('sap_login_unavailable')
      // SAP still decides whether a session is valid. Old session cookies are not replayed indefinitely.
      if (Date.now() - record.updated > 30 * 86400000) record.cookies = []
      return record
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return
      return fail('sap_login_unavailable')
    }
  }
  write(key: string, record: RecordData): void {
    if (!this.available()) return fail('sap_keychain_unavailable')
    const bytes = this.cipher.encryptString(JSON.stringify(record))
    fs.mkdirSync(this.directory, {recursive: true, mode: 0o700})
    const file = this.file(key), temporary = file + '.tmp'
    fs.writeFileSync(temporary, bytes, {mode: 0o600})
    fs.renameSync(temporary, file)
  }
  forget(key: string): void { fs.rmSync(this.file(key), {force: true}) }
}

export function sapLoginFormScript(target: string, credentials?: SapCredentials): string {
  const url = new URL(target)
  return `(() => {
    if (location.origin !== ${JSON.stringify(url.origin)} || location.pathname !== ${JSON.stringify(url.pathname)}) return {};
    const form = document.querySelector('form[name="loginForm"]');
    const password = form?.querySelector('input[name="sap-password"][type="password"]');
    const user = form?.querySelector('input[name="sap-user"]');
    const client = form?.querySelector('input[name="sap-client"]');
    if (!password || !user || !client) return {login:!!form || !!document.querySelector('input[type="password"]'), sap:!form && !!document.querySelector('[ct], [role="toolbar"]')};
    const saved = ${JSON.stringify(credentials || null)};
    if (!saved || client.value !== saved.client || password.value || (user.value && user.value !== saved.user)) return {login:true};
    const set = (field, value) => { if (!field || field.value === value) return;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(field, value);
      field.dispatchEvent(new Event('input', {bubbles:true})); field.dispatchEvent(new Event('change', {bubbles:true})); };
    set(user, saved.user); set(password, saved.password);
    return {login:true, filled:true};
  })()`
}

/** Native-only password manager. The shell receives status, never cookies or credentials. */
export class SapLoginMemory {
  private active?: {key: string; target: SapLoginContext; scope: SapLoginScope; record?: RecordData;
    pending?: {credentials: SapCredentials; loaded: boolean; at: number}}
  private filled = new WeakSet<WebFrameMain>()
  private busy = false
  private error = ''
  private session: Session
  private disposed = false
  private suspended = false
  private timer: ReturnType<typeof setInterval>
  constructor(private contents: WebContents, private vault: SapLoginVault,
    private currentScope: () => SapLoginScope | undefined) {
    this.session = contents.session
    this.session.cookies.on('changed', this.cookieChanged)
    this.session.webRequest.onBeforeRequest(this.beforeRequest)
    contents.on('did-frame-navigate', this.navigated)
    contents.on('did-frame-finish-load', this.loaded)
    this.timer = setInterval(() => { void this.tick() }, 1000)
  }
  private live(): boolean {
    const now = this.currentScope(), active = this.active
    return !this.disposed && !this.suspended && !!now && !!active && now.epoch === active.scope.epoch && now.user === active.scope.user &&
      now.tenant === active.scope.tenant && now.server === active.scope.server && !this.contents.isDestroyed()
  }
  private frame(): WebFrameMain | undefined {
    if (!this.live()) return
    const matches = this.contents.mainFrame.frames.filter(f => f.name === 'sap-gui-' + this.active!.target.binding_id)
    const frame = matches.length === 1 ? matches[0] : undefined
    if (!frame || frame.detached || new URL(frame.url || 'about:blank').origin !== new URL(this.active!.target.sap_url).origin) return
    return frame
  }
  private persist(): void {
    if (!this.live() || !this.active?.record) return
    try { this.vault.write(this.active.key, this.active.record); this.error = '' }
    catch { this.error = 'sap_keychain_unavailable' }
  }
  private cookieChanged = (_event: unknown, cookie: Cookie, _cause: string, removed: boolean): void => {
    const active = this.active
    if (!this.live() || !active?.record || !sapCookie({...cookie, expirationDate: undefined}, active.target.sap_url)) return
    const cookies = active.record.cookies.filter(c => !(c.name === cookie.name && c.path === cookie.path && c.domain === cookie.domain))
    if (!removed && sapCookie(cookie, active.target.sap_url)) cookies.push(cookie)
    active.record.cookies = cookies.slice(-100); active.record.updated = Date.now(); this.persist()
  }
  private beforeRequest = (details: Electron.OnBeforeRequestListenerDetails, done: (answer: Electron.CallbackResponse) => void): void => {
    try {
      const active = this.active, frame = this.frame()
      if (active?.record && frame && details.webContentsId === this.contents.id && details.frame === frame && details.method === 'POST') {
        const request = new URL(details.url), target = new URL(active.target.sap_url)
        if (request.origin === target.origin && request.pathname === target.pathname &&
            details.uploadData?.every(part => part.bytes && !part.file)) {
          const chunks = details.uploadData.map(part => part.bytes!)
          if (chunks.reduce((n, part) => n + part.length, 0) <= 16384) {
            const credentials = loginSubmission(Buffer.concat(chunks).toString('utf8'), active.target.client)
            if (credentials) active.pending = {credentials, loaded:false, at:Date.now()}
          }
        }
      }
    } catch { /* Never block or log a SAP request. */ }
    done({cancel: false})
  }
  private navigated = (): void => { this.filled = new WeakSet() }
  private loaded = (_event: unknown, _main: boolean, processId: number, routingId: number): void => {
    const frame = this.frame()
    if (frame?.processId === processId && frame.routingId === routingId && this.active?.pending) this.active.pending.loaded = true
  }
  private async tick(): Promise<void> {
    if (this.busy) return
    const active = this.active, frame = this.frame()
    if (!active?.record || !frame) return
    this.busy = true
    try {
      const result = await frame.executeJavaScript(sapLoginFormScript(active.target.sap_url,
        this.filled.has(frame) || active.pending ? undefined : active.record.credentials)) as {filled?: boolean; sap?: boolean; login?: boolean}
      if (this.active !== active || !this.live() || this.frame() !== frame) return
      if (result?.filled) this.filled.add(frame)
      if (active.pending?.loaded && result?.sap && !result?.login) {
        active.record.credentials = active.pending.credentials; active.pending = undefined; this.persist()
      } else if (active.pending && ((active.pending.loaded && result?.login) || Date.now() - active.pending.at > 30000)) active.pending = undefined
    } catch { /* A navigating frame can disappear. Retry after the next load. */ }
    finally { this.busy = false }
  }
  async manage(target: SapLoginContext, scope: SapLoginScope, action: string): Promise<object> {
    if (this.disposed) return fail('sap_login_unavailable')
    const key = loginScopeKey(scope, target)
    this.suspended = false
    if (action === 'forget') {
      this.vault.forget(key)
      if (this.active?.key === key) { this.active.record = undefined; this.active.pending = undefined }
      this.error = ''
      // Forgetting does not require unlocking Keychain or signing out the open SAP page.
      return {enabled:false, credentialsSaved:false, error:''}
    }
    if (!this.vault.available()) return fail('sap_keychain_unavailable')
    if (this.active?.key !== key || this.active.scope.epoch !== scope.epoch) {
      const previous = this.active
      this.active = undefined
      // The container is memory-only. A tenant/account switch must not reuse another scope's SAP cookies.
      if (previous && previous.key !== key) await this.clearCookies(previous.target.sap_url)
      const record = this.vault.read(key)
      this.active = {key, target, scope, record}; this.filled = new WeakSet()
      if (record) for (const cookie of record.cookies.slice()) {
        if (!this.live()) return fail('sap_login_unavailable')
        if (!sapCookie(cookie, target.sap_url)) continue
        try {
          const {domain, name, value, path: cookiePath, secure, httpOnly, sameSite, expirationDate, hostOnly} = cookie
          await this.contents.session.cookies.set({url: new URL(cookiePath || '/', target.sap_url).href,
            ...(hostOnly ? {} : {domain}), name, value, path: cookiePath, secure, httpOnly, sameSite, expirationDate})
        } catch { /* An expired/rejected cookie simply requires normal SAP login. */ }
      }
    }
    const active = this.active!
    active.target = target
    if (!this.live()) return fail('sap_login_unavailable')
    if (action === 'enable' && !active.record) {
      active.record = {version:1, updated:Date.now(), cookies:[]}
      const cookies = await this.contents.session.cookies.get({url: target.sap_url})
      if (!this.live() || this.active !== active) return fail('sap_login_unavailable')
      active.record.cookies = cookies.filter(c => sapCookie(c, target.sap_url)).slice(-100)
      this.vault.write(key, active.record)
    }
    return {enabled:!!active.record, credentialsSaved:!!active.record?.credentials, error:this.error}
  }
  private async clearCookies(url: string): Promise<void> {
    for (const cookie of await this.contents.session.cookies.get({url})) {
      if (sapCookie({...cookie, expirationDate:undefined}, url)) await this.contents.session.cookies.remove(new URL(cookie.path || '/', url).href, cookie.name)
    }
  }
  suspend(): void {
    this.suspended = true
    if (this.active) this.active.pending = undefined
  }
  dispose(): void {
    if (this.disposed) return
    this.disposed = true
    clearInterval(this.timer); this.active = undefined
    this.session.cookies.removeListener('changed', this.cookieChanged)
    this.session.webRequest.onBeforeRequest(null)
    this.contents.removeListener('did-frame-navigate', this.navigated)
    this.contents.removeListener('did-frame-finish-load', this.loaded)
  }
}
