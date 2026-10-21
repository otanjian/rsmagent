import React, { useEffect, useState, useSyncExternalStore } from 'react'
import { Check, Copy, Eye, EyeOff, Plug } from 'lucide-react'
import { t, getLang } from '../../i18n'
import apiClient, { type ExternalApiState } from '../../api/client'
import desktopContext from '../../api/context'
import { askConfirm } from '../../store/confirmStore'
import Tooltip from '../../components/Tooltip'
import { Card, FieldTip, Toggle } from './primitives'

type ExampleLang = 'curl' | 'python' | 'node'

const LANG_LABELS: Record<ExampleLang, string> = { curl: 'cURL', python: 'Python', node: 'Node.js' }

function example(lang: ExampleLang, base: string, key: string): string {
  if (lang === 'python') {
    return [
      'import requests',
      '',
      'resp = requests.post(',
      `    "${base}/chat/completions",`,
      `    headers={"Authorization": "Bearer ${key}"},`,
      '    json={"messages": [{"role": "user", "content": "Hello"}]},',
      ')',
      'print(resp.json()["choices"][0]["message"]["content"])',
    ].join('\n')
  }
  if (lang === 'node') {
    return [
      `const resp = await fetch("${base}/chat/completions", {`,
      '  method: "POST",',
      `  headers: { "Authorization": "Bearer ${key}", "Content-Type": "application/json" },`,
      '  body: JSON.stringify({ messages: [{ role: "user", content: "Hello" }] }),',
      '});',
      'const data = await resp.json();',
      'console.log(data.choices[0].message.content);',
    ].join('\n')
  }
  return [
    `curl ${base}/chat/completions \\`,
    `  -H "Authorization: Bearer ${key}" \\`,
    '  -H "Content-Type: application/json" \\',
    `  -d '{"messages": [{"role": "user", "content": "Hello"}]}'`,
  ].join('\n')
}

const CopyButton: React.FC<{ text: string; className?: string }> = ({ text, className }) => {
  const [copied, setCopied] = useState(false)
  return (
    <Tooltip label={t('config_api_copy')}>
      <button
        type="button"
        onClick={() => {
          void navigator.clipboard.writeText(text).then(() => {
            setCopied(true)
            setTimeout(() => setCopied(false), 1200)
          })
        }}
        className={className ?? ICON_BTN}
      >
        {copied ? <Check size={13} /> : <Copy size={13} />}
      </button>
    </Tooltip>
  )
}

const ICON_BTN =
  'flex-shrink-0 w-9 flex items-center justify-center rounded-btn border border-strong text-content-tertiary hover:text-content-secondary hover:bg-surface-2 cursor-pointer transition-colors'
const READONLY =
  'w-full px-3 py-2 rounded-btn border border-strong bg-inset text-sm text-content font-mono focus:outline-none'

/** Service-account credentials use the existing IAM lifecycle, with one-time plaintext. */
const ChatApiCard: React.FC<{ baseUrl: string }> = ({ baseUrl }) => {
  const context = useSyncExternalStore(desktopContext.subscribe, desktopContext.getSnapshot)
  const revision = context.featureRevision
  const [state, setState] = useState<ExternalApiState | null>(null)
  const [userId, setUserId] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [revealed, setRevealed] = useState(false)
  const [lang, setLang] = useState<ExampleLang>('curl')

  const load = async (target: string) => {
    const started = desktopContext.getSnapshot().featureRevision
    setBusy(true); setError(''); setState(null); setRevealed(false)
    try {
      const res = await apiClient.getExternalApi(target)
      if (started !== desktopContext.getSnapshot().featureRevision) return
      if (res.status !== 'success') throw new Error(res.message || t('config_api_failed'))
      setState(res); setUserId(res.user_id)
    } catch (e) {
      if (started === desktopContext.getSnapshot().featureRevision) setError(String(e))
    } finally {
      if (started === desktopContext.getSnapshot().featureRevision) setBusy(false)
    }
  }

  useEffect(() => {
    setUserId(''); setState(null); setRevealed(false)
    void load('')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [baseUrl, revision])

  const post = async (action: 'enable' | 'disable' | 'create' | 'rotate' | 'revoke', credentialId?: string) => {
    const started = desktopContext.getSnapshot().featureRevision
    const target = state?.user_id
    if (!target || busy) return
    if (action === 'rotate' || action === 'revoke') {
      const confirmed = await askConfirm({ titleKey: 'config_api_' + action,
        msgKey: 'config_api_key_change_confirm', okKey: 'config_api_' + action })
      if (!confirmed || started !== desktopContext.getSnapshot().featureRevision) return
    }
    setBusy(true); setError(''); setRevealed(false)
    setState(previous => previous ? { ...previous, issued: undefined } : null)
    try {
      const res = await apiClient.updateExternalApi(action, target, credentialId)
      if (started !== desktopContext.getSnapshot().featureRevision) return
      if (res.status !== 'success') throw new Error(res.message || t('config_api_failed'))
      setState(res)
    } catch (e) {
      if (started === desktopContext.getSnapshot().featureRevision) setError(String(e))
    } finally {
      if (started === desktopContext.getSnapshot().featureRevision) setBusy(false)
    }
  }

  const key = state?.issued?.api_key || ''
  const apiBase = `${baseUrl.replace(/\/$/, '')}/v1`
  const docsUrl = `${getLang() === 'zh' ? 'https://docs.cowagent.ai/zh' : 'https://docs.cowagent.ai'}/extensions/chat-api`

  return (
    <Card icon={<Plug size={16} />} title={t('config_api')} subtitle={t('config_api_identity_hint')}
      action={state?.can_configure && !busy ? <Toggle checked={state.enabled} onChange={on => void post(on ? 'enable' : 'disable')} /> : undefined}>
      <div className="space-y-4">
        <label className="block text-sm text-content-secondary">
          {t('config_api_account')}
          <div className="flex gap-2 mt-1.5">
            <input value={userId} onChange={e => { setUserId(e.target.value); setState(null); setRevealed(false) }} className={READONLY} />
            <button type="button" disabled={busy} onClick={() => void load(userId)}>{t('ws_refresh')}</button>
          </div>
        </label>
        {error && <p role="alert" className="text-sm text-red-500">{error}</p>}
        <div className="flex gap-2"><input readOnly value={apiBase} className={READONLY} /><CopyButton text={apiBase} /></div>
        {state && <>
          {!state.enabled && <p className="text-sm text-content-tertiary">{t('config_api_off_hint')}</p>}
          <button type="button" disabled={busy} onClick={() => void post('create')}>{t('config_api_create_key')}</button>
          {state.keys.map(credential => <div key={credential.id} className="flex gap-3 text-sm items-center">
            <code>{credential.masked}</code>
            <button type="button" disabled={busy} onClick={() => void post('rotate', credential.id)}>{t('config_api_rotate')}</button>
            <button type="button" disabled={busy} onClick={() => void post('revoke', credential.id)}>{t('config_api_revoke')}</button>
          </div>)}
        </>}
        {key && <div className="flex gap-2">
          <input readOnly type={revealed ? 'text' : 'password'} value={key} className={READONLY} />
          <button type="button" className={ICON_BTN} onClick={() => setRevealed(v => !v)}>{revealed ? <EyeOff size={14} /> : <Eye size={14} />}</button>
          <CopyButton text={key} />
        </div>}
        <div>
          <div className="flex items-center justify-between mb-1.5">
            <span className="text-sm text-content-secondary">{t('config_api_example')} <FieldTip tip={t('config_api_example_hint')} /></span>
            <div className="flex gap-2">{(Object.keys(LANG_LABELS) as ExampleLang[]).map(l =>
              <button key={l} type="button" onClick={() => setLang(l)} className={lang === l ? 'text-accent' : 'text-content-tertiary'}>{LANG_LABELS[l]}</button>)}</div>
          </div>
          <pre className="text-xs font-mono rounded-btn border border-strong bg-inset p-3 overflow-x-auto whitespace-pre">{example(lang, apiBase, 'sak_YOUR_SERVICE_ACCOUNT_KEY')}</pre>
          <a href={docsUrl} target="_blank" rel="noopener noreferrer" className="text-xs text-accent">{t('config_api_docs')}</a>
        </div>
      </div>
    </Card>
  )
}

export default ChatApiCard
