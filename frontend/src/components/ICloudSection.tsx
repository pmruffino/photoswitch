import { FormEvent, useEffect, useState } from 'react'
import { api, DestOption, ICloudConnection, uploadJobChunked } from '../api'

// Encode/split the combined "kind:id" destination key used by the selects.
const destKey = (o: DestOption) => `${o.kind}:${o.id}`
function splitDest(v: string): { kind: string; id: string } {
  const sep = v.indexOf(':')
  return sep < 0 ? { kind: 'immich', id: v } : { kind: v.slice(0, sep), id: v.slice(sep + 1) }
}

// Local style constants, matching Dashboard's conventions.
const INPUT = 'w-full border border-slate-300 dark:border-zinc-600 rounded-md px-3 py-2 text-sm bg-white dark:bg-zinc-800 text-slate-900 dark:text-zinc-100 placeholder-slate-400 dark:placeholder-zinc-500 focus:outline-none focus:ring-2 focus:ring-red-500'
const BTN_PRIMARY = 'bg-red-600 hover:bg-red-700 dark:bg-red-700 dark:hover:bg-red-600 text-white px-4 py-2 rounded-md text-sm font-medium disabled:opacity-50'
const BTN_SECONDARY = 'bg-white dark:bg-zinc-800 border border-slate-300 dark:border-zinc-600 text-slate-700 dark:text-zinc-300 px-4 py-2 rounded-md text-sm hover:bg-slate-50 dark:hover:bg-zinc-700'

const SYNC_PRESETS: { value: number; label: string }[] = [
  { value: 15, label: '15 minutes' },
  { value: 60, label: '1 hour' },
  { value: 120, label: '2 hours' },
  { value: 240, label: '4 hours' },
  { value: 480, label: '8 hours' },
  { value: 720, label: '12 hours' },
  { value: 1440, label: '1 day' },
  { value: 2880, label: '2 days' },
  { value: 4320, label: '3 days' },
  { value: 10080, label: '1 week' },
]

const STATUS_BADGE: Record<ICloudConnection['status'], { text: string; color: string }> = {
  pending_2fa:  { text: 'Awaiting 2FA', color: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400' },
  '2fa_required': { text: 'Awaiting 2FA', color: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400' },
  active:       { text: 'Connected', color: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400' },
  needs_reauth: { text: 'Re-auth needed', color: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400' },
}

interface Props {
  destinations: DestOption[]
  onJobCreated: () => void
}

export default function ICloudSection({ destinations, onJobCreated }: Props) {
  const [conns, setConns] = useState<ICloudConnection[]>([])
  const [showConnect, setShowConnect] = useState(false)

  // Connect form
  const [appleId, setAppleId] = useState('')
  const [password, setPassword] = useState('')
  const [label, setLabel] = useState('')
  const [connectError, setConnectError] = useState('')
  const [connectLoading, setConnectLoading] = useState(false)

  // 2FA step: the connection awaiting a code
  const [pendingId, setPendingId] = useState<string | null>(null)
  const [code, setCode] = useState('')
  const [verifyError, setVerifyError] = useState('')
  const [verifyLoading, setVerifyLoading] = useState(false)

  // Per-connection expandable panel
  const [panel, setPanel] = useState<{ id: string; kind: 'import' | 'sync' } | null>(null)
  const [syncingId, setSyncingId] = useState<string | null>(null)
  const [testingId, setTestingId] = useState<string | null>(null)
  const [testResults, setTestResults] = useState<Record<string, { ok: boolean; detail?: string }>>({})

  useEffect(() => { api.icloud.listConnections().then(setConns).catch(() => {}) }, [])

  async function runTest(c: ICloudConnection) {
    setTestingId(c.id)
    try {
      const r = await api.icloud.testConnection(c.id)
      setTestResults(p => ({ ...p, [c.id]: { ok: true, detail: r.user } }))
    } catch (err) {
      setTestResults(p => ({ ...p, [c.id]: { ok: false, detail: err instanceof Error ? err.message : 'Test failed' } }))
    } finally {
      setTestingId(null)
      // The test may have flipped the connection's status (→ active or needs_reauth).
      api.icloud.listConnections().then(setConns).catch(() => {})
    }
  }

  async function runSyncNow(c: ICloudConnection) {
    setSyncingId(c.id)
    try {
      await api.icloud.syncNow(c.id)
      onJobCreated()
      // reflect the just-updated last-run time
      api.icloud.listConnections().then(setConns).catch(() => {})
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Sync failed')
    } finally {
      setSyncingId(null)
    }
  }

  async function connect(e: FormEvent) {
    e.preventDefault()
    setConnectError('')
    setConnectLoading(true)
    try {
      const c = await api.icloud.createConnection({ apple_id: appleId, password, label: label || undefined })
      setConns(prev => [c, ...prev.filter(p => p.id !== c.id)])
      setPassword('')
      if (c.status === '2fa_required' || c.status === 'pending_2fa') {
        setPendingId(c.id)
        setShowConnect(false)
      } else {
        // Rare: no 2FA needed.
        setShowConnect(false)
        setAppleId(''); setLabel('')
      }
    } catch (err) {
      setConnectError(err instanceof Error ? err.message : 'Failed to connect')
    } finally {
      setConnectLoading(false)
    }
  }

  async function verify(e: FormEvent) {
    e.preventDefault()
    if (!pendingId) return
    setVerifyError('')
    setVerifyLoading(true)
    try {
      const c = await api.icloud.verifyConnection(pendingId, code.trim())
      setConns(prev => prev.map(p => p.id === c.id ? c : p))
      setPendingId(null); setCode(''); setAppleId(''); setLabel('')
    } catch (err) {
      setVerifyError(err instanceof Error ? err.message : 'Verification failed')
    } finally {
      setVerifyLoading(false)
    }
  }

  async function removeConn(id: string) {
    if (!confirm('Delete this iCloud connection? Any periodic sync on it will stop.')) return
    await api.icloud.deleteConnection(id).catch(() => {})
    setConns(prev => prev.filter(c => c.id !== id))
    if (pendingId === id) setPendingId(null)
    if (panel?.id === id) setPanel(null)
  }

  return (
    <section className="mb-10">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h2 className="text-lg font-semibold text-slate-900 dark:text-zinc-100">Apple iCloud</h2>
          <p className="text-xs text-slate-500 dark:text-zinc-400 mt-0.5">
            Connect iCloud directly for one-off imports or a scheduled sync, or upload an Apple export bundle below.
          </p>
        </div>
        <button
          onClick={() => { setShowConnect(v => !v); setConnectError('') }}
          disabled={destinations.length === 0}
          className={BTN_PRIMARY + ' disabled:opacity-40 disabled:cursor-not-allowed'}
          title={destinations.length === 0 ? 'Add an Immich or WebDAV destination first' : undefined}
        >
          {showConnect ? 'Cancel' : 'Connect iCloud'}
        </button>
      </div>

      {showConnect && (
        <form onSubmit={connect} className="bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg p-5 mb-4 space-y-3">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label className="block text-sm font-medium text-slate-700 dark:text-zinc-300 mb-1">Apple ID</label>
              <input type="email" value={appleId} onChange={e => setAppleId(e.target.value)} required placeholder="you@icloud.com" className={INPUT} />
            </div>
            <div>
              <label className="block text-sm font-medium text-slate-700 dark:text-zinc-300 mb-1">Label (optional)</label>
              <input type="text" value={label} onChange={e => setLabel(e.target.value)} placeholder="My iCloud" className={INPUT} />
            </div>
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-700 dark:text-zinc-300 mb-1">Password</label>
            <input type="password" value={password} onChange={e => setPassword(e.target.value)} required placeholder="Apple ID password" className={INPUT} />
          </div>
          <div className="bg-slate-50 dark:bg-zinc-800/60 border border-slate-200 dark:border-zinc-700 rounded-md px-3 py-2.5 space-y-1">
            <p className="text-xs font-medium text-slate-700 dark:text-zinc-200">What happens next:</p>
            <ol className="text-xs text-slate-600 dark:text-zinc-300 space-y-0.5 list-decimal list-inside">
              <li>Apple sends a 6-digit code to your trusted devices</li>
              <li>Enter that code here to establish a trusted session</li>
              <li>The session is stored encrypted and reused — Apple re-prompts for a code roughly every two months</li>
            </ol>
            <p className="text-xs text-slate-500 dark:text-zinc-400 pt-1">
              Your password is stored encrypted and used only to talk to iCloud. Consider an
              app-specific password if your account supports it.
            </p>
          </div>
          {connectError && <p className="text-sm text-red-600 dark:text-red-400">{connectError}</p>}
          <button type="submit" disabled={connectLoading} className={BTN_PRIMARY}>
            {connectLoading ? 'Contacting Apple…' : 'Send 2FA code'}
          </button>
        </form>
      )}

      {conns.length === 0 && !showConnect ? (
        <p className="text-sm text-slate-500 dark:text-zinc-400 mb-4">No iCloud connections yet.</p>
      ) : (
        <div className="space-y-3 mb-4">
          {conns.map(c => (
            <div key={c.id} className="bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg p-4">
              <div className="flex items-center justify-between gap-3 flex-wrap">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <p className="text-sm font-medium text-slate-900 dark:text-zinc-100 truncate">{c.label ?? c.apple_id}</p>
                    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_BADGE[c.status].color}`}>
                      {STATUS_BADGE[c.status].text}
                    </span>
                    {c.sync_enabled && (
                      <span className="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400">
                        Sync every {formatInterval(c.sync_interval_minutes)}
                      </span>
                    )}
                  </div>
                  {c.label && <p className="text-xs text-slate-500 dark:text-zinc-400 truncate">{c.apple_id}</p>}
                  <p className="text-xs text-slate-400 dark:text-zinc-500 mt-0.5">
                    {c.sync_last_run_at ? `Last sync ${new Date(c.sync_last_run_at).toLocaleString()}` : 'Never synced'}
                  </p>
                  {testResults[c.id] && (
                    <p className={`text-xs mt-1 ${testResults[c.id].ok ? 'text-green-600 dark:text-green-400' : 'text-red-500 dark:text-red-400'}`}>
                      {testResults[c.id].ok
                        ? `Session valid${testResults[c.id].detail ? ` — ${testResults[c.id].detail}` : ''}`
                        : testResults[c.id].detail}
                    </p>
                  )}
                </div>
                <div className="flex items-center gap-3">
                  {(c.status === 'active' || c.status === 'needs_reauth') && (
                    <button onClick={() => runTest(c)} disabled={testingId === c.id}
                      className="text-xs text-slate-500 dark:text-zinc-400 hover:text-slate-800 dark:hover:text-zinc-200 disabled:opacity-50">
                      {testingId === c.id ? 'Testing…' : 'Test'}
                    </button>
                  )}
                  {c.status === 'active' && (
                    <>
                      <button onClick={() => setPanel(p => p?.id === c.id && p.kind === 'import' ? null : { id: c.id, kind: 'import' })}
                        className="text-xs text-red-600 dark:text-red-400 hover:text-red-800 dark:hover:text-red-300 font-medium">
                        {panel?.id === c.id && panel.kind === 'import' ? 'Close' : 'Import now'}
                      </button>
                      {c.has_watermark && (
                        <button onClick={() => runSyncNow(c)}
                          disabled={syncingId === c.id || !c.sync_credential_id}
                          title={!c.sync_credential_id ? 'Set a sync target under Configure sync first' : undefined}
                          className="text-xs text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-300 font-medium disabled:opacity-40 disabled:cursor-not-allowed">
                          {syncingId === c.id ? 'Syncing…' : 'Sync now'}
                        </button>
                      )}
                      <button onClick={() => setPanel(p => p?.id === c.id && p.kind === 'sync' ? null : { id: c.id, kind: 'sync' })}
                        className="text-xs text-slate-600 dark:text-zinc-400 hover:text-slate-900 dark:hover:text-zinc-100 font-medium">
                        {panel?.id === c.id && panel.kind === 'sync' ? 'Close' : 'Configure sync'}
                      </button>
                    </>
                  )}
                  {c.status === 'needs_reauth' && (
                    <span className="text-xs text-slate-500 dark:text-zinc-400">Delete &amp; reconnect to refresh the session</span>
                  )}
                  <button onClick={() => removeConn(c.id)} className="text-xs text-red-500 dark:text-red-400 hover:text-red-700 dark:hover:text-red-300">
                    Remove
                  </button>
                </div>
              </div>

              {/* Inline 2FA code entry */}
              {pendingId === c.id && (
                <form onSubmit={verify} className="mt-3 pt-3 border-t border-slate-100 dark:border-zinc-700 flex flex-wrap items-end gap-3">
                  <div>
                    <label className="block text-xs font-medium text-slate-600 dark:text-zinc-400 mb-1">6-digit code from your Apple device</label>
                    <input value={code} onChange={e => setCode(e.target.value)} required inputMode="numeric" placeholder="123456"
                      className="border border-slate-300 dark:border-zinc-600 rounded-md px-3 py-1.5 text-sm bg-white dark:bg-zinc-800 text-slate-900 dark:text-zinc-100 tracking-widest w-32 focus:outline-none focus:ring-2 focus:ring-red-500" />
                  </div>
                  <button type="submit" disabled={verifyLoading} className={BTN_PRIMARY}>{verifyLoading ? 'Verifying…' : 'Verify'}</button>
                  <button type="button" onClick={() => { setPendingId(null); setCode('') }} className={BTN_SECONDARY}>Cancel</button>
                  {verifyError && <p className="w-full text-sm text-red-600 dark:text-red-400">{verifyError}</p>}
                </form>
              )}

              {panel?.id === c.id && panel.kind === 'import' && (
                <ImportPanel conn={c} destinations={destinations} onDone={() => { setPanel(null); onJobCreated() }} />
              )}
              {panel?.id === c.id && panel.kind === 'sync' && (
                <SyncPanel conn={c} destinations={destinations} onSaved={u => { setConns(prev => prev.map(p => p.id === u.id ? u : p)); setPanel(null) }} />
              )}
            </div>
          ))}
        </div>
      )}

      <BundleUploader destinations={destinations} onJobCreated={onJobCreated} />
    </section>
  )
}

function ImportPanel({ conn, destinations, onDone }: { conn: ICloudConnection; destinations: DestOption[]; onDone: () => void }) {
  const [dest, setDest] = useState(conn.sync_credential_id ? `${conn.sync_credential_kind ?? 'immich'}:${conn.sync_credential_id}` : '')
  const [anchor, setAnchor] = useState(false)
  const [after, setAfter] = useState('')
  const [before, setBefore] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (!dest) { setError('Choose a destination'); return }
    const { kind, id } = splitDest(dest)
    setError('')
    setLoading(true)
    try {
      await api.icloud.importNow(conn.id, {
        credential_id: id,
        destination_kind: kind,
        as_sync_anchor: anchor,
        after_date: after || undefined,
        before_date: before || undefined,
      })
      onDone()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Import failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <form onSubmit={submit} className="mt-3 pt-3 border-t border-slate-100 dark:border-zinc-700 space-y-3">
      <p className="text-xs text-slate-500 dark:text-zinc-400">
        Pulls photos newer than the last sync (all photos on the first run) and uploads them to the destination.
      </p>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <div>
          <label className="block text-xs font-medium text-slate-600 dark:text-zinc-400 mb-1">Destination</label>
          <select value={dest} onChange={e => setDest(e.target.value)} required className={INPUT}>
            <option value="">Select…</option>
            {destinations.map(o => <option key={destKey(o)} value={destKey(o)}>{o.label}</option>)}
          </select>
        </div>
        <div>
          <label className="block text-xs font-medium text-slate-600 dark:text-zinc-400 mb-1">Taken after (optional)</label>
          <input type="date" value={after} onChange={e => setAfter(e.target.value)} max={before || undefined} className={INPUT} />
        </div>
        <div>
          <label className="block text-xs font-medium text-slate-600 dark:text-zinc-400 mb-1">Taken before (optional)</label>
          <input type="date" value={before} onChange={e => setBefore(e.target.value)} min={after || undefined} className={INPUT} />
        </div>
      </div>
      <label className="flex items-start gap-2 cursor-pointer">
        <input type="checkbox" checked={anchor} onChange={e => setAnchor(e.target.checked)} className="mt-0.5 rounded accent-red-600" />
        <span className="text-xs text-slate-600 dark:text-zinc-400">
          Make this the anchor for a recurring sync (its job is kept out of automatic cleanup). Configure the schedule under “Configure sync”.
        </span>
      </label>
      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
      <button type="submit" disabled={loading} className={BTN_PRIMARY}>{loading ? 'Starting…' : 'Import now'}</button>
    </form>
  )
}

function SyncPanel({ conn, destinations, onSaved }: { conn: ICloudConnection; destinations: DestOption[]; onSaved: (u: ICloudConnection) => void }) {
  const [enabled, setEnabled] = useState(conn.sync_enabled)
  const presetValues = SYNC_PRESETS.map(p => p.value)
  const [intervalMin, setIntervalMin] = useState(
    presetValues.includes(conn.sync_interval_minutes) ? conn.sync_interval_minutes : 1440,
  )
  const [dest, setDest] = useState(conn.sync_credential_id ? `${conn.sync_credential_kind ?? 'immich'}:${conn.sync_credential_id}` : '')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const parsed = dest ? splitDest(dest) : null
      const u = await api.icloud.configureSync(conn.id, {
        enabled,
        interval_minutes: intervalMin,
        credential_id: parsed?.id,
        destination_kind: parsed?.kind,
      })
      onSaved(u)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save sync settings')
    } finally {
      setLoading(false)
    }
  }

  return (
    <form onSubmit={submit} className="mt-3 pt-3 border-t border-slate-100 dark:border-zinc-700 space-y-3">
      <label className="flex items-center gap-2 cursor-pointer">
        <input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} className="rounded accent-red-600" />
        <span className="text-sm font-medium text-slate-800 dark:text-zinc-200">Enable periodic sync</span>
      </label>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div>
          <label className="block text-xs font-medium text-slate-600 dark:text-zinc-400 mb-1">Sync frequency</label>
          <select value={intervalMin} onChange={e => setIntervalMin(Number(e.target.value))} className={INPUT}>
            {SYNC_PRESETS.map(p => <option key={p.value} value={p.value}>Every {p.label}</option>)}
          </select>
        </div>
        <div>
          <label className="block text-xs font-medium text-slate-600 dark:text-zinc-400 mb-1">Destination</label>
          <select value={dest} onChange={e => setDest(e.target.value)} required={enabled} className={INPUT}>
            <option value="">Select…</option>
            {destinations.map(o => <option key={destKey(o)} value={destKey(o)}>{o.label}</option>)}
          </select>
        </div>
      </div>
      <p className="text-xs text-slate-400 dark:text-zinc-500">
        The scheduler pulls only photos added since the last sync. The destination is also used by “Sync now”.
      </p>
      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
      <button type="submit" disabled={loading} className={BTN_PRIMARY}>{loading ? 'Saving…' : 'Save sync settings'}</button>
    </form>
  )
}

function BundleUploader({ destinations, onJobCreated }: { destinations: DestOption[]; onJobCreated: () => void }) {
  const [open, setOpen] = useState(false)
  const [file, setFile] = useState<File | null>(null)
  const [dest, setDest] = useState('')
  const [pct, setPct] = useState(0)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (!file) { setError('Select an export archive'); return }
    if (!dest) { setError('Choose a destination'); return }
    const { kind, id } = splitDest(dest)
    setError('')
    setLoading(true)
    setPct(0)
    try {
      await uploadJobChunked(
        file,
        { credential_id: id, destination_kind: kind, auto_ingest: true, source: 'icloud_bundle' },
        p => setPct(p),
      )
      setFile(null); setOpen(false); setPct(0)
      onJobCreated()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg p-4">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm font-medium text-slate-900 dark:text-zinc-100">Apple export bundle</p>
          <p className="text-xs text-slate-500 dark:text-zinc-400 mt-0.5">
            Upload an archive from <a href="https://privacy.apple.com" target="_blank" rel="noreferrer" className="underline hover:text-slate-900 dark:hover:text-zinc-100">privacy.apple.com</a> → “Get a copy of your data”.
          </p>
        </div>
        <button onClick={() => { setOpen(v => !v); setError('') }} disabled={destinations.length === 0}
          className={BTN_SECONDARY + ' disabled:opacity-40 disabled:cursor-not-allowed'}
          title={destinations.length === 0 ? 'Add an Immich or WebDAV destination first' : undefined}>
          {open ? 'Cancel' : 'Upload bundle'}
        </button>
      </div>

      {open && (
        <form onSubmit={submit} className="mt-3 pt-3 border-t border-slate-100 dark:border-zinc-700 space-y-3">
          <input type="file" accept=".zip,.tar,.tar.gz,.tgz" required onChange={e => setFile(e.target.files?.[0] ?? null)}
            className="w-full text-sm text-slate-600 dark:text-zinc-300 file:mr-3 file:py-1.5 file:px-3 file:rounded-md file:border-0 file:text-sm file:font-medium file:bg-red-50 file:text-red-700 dark:file:bg-red-900/30 dark:file:text-red-400 hover:file:bg-red-100 dark:hover:file:bg-red-900/50 cursor-pointer" />
          <div>
            <label className="block text-xs font-medium text-slate-600 dark:text-zinc-400 mb-1">Destination</label>
            <select value={dest} onChange={e => setDest(e.target.value)} required className={INPUT}>
              <option value="">Select…</option>
              {destinations.map(o => <option key={destKey(o)} value={destKey(o)}>{o.label}</option>)}
            </select>
          </div>
          {loading && pct > 0 && (
            <div>
              <div className="flex justify-between text-xs text-slate-500 dark:text-zinc-400 mb-1"><span>Uploading…</span><span>{pct}%</span></div>
              <div className="w-full bg-slate-200 dark:bg-zinc-700 rounded-full h-1.5">
                <div className="bg-red-500 h-1.5 rounded-full transition-all duration-300" style={{ width: `${pct}%` }} />
              </div>
            </div>
          )}
          {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
          <button type="submit" disabled={loading} className={BTN_PRIMARY}>
            {loading ? (pct > 0 && pct < 100 ? `Uploading… ${pct}%` : 'Processing…') : 'Upload & process'}
          </button>
        </form>
      )}
    </div>
  )
}

function formatInterval(minutes: number): string {
  if (minutes % 1440 === 0) { const d = minutes / 1440; return d === 1 ? 'day' : `${d} days` }
  if (minutes % 60 === 0) { const h = minutes / 60; return h === 1 ? 'hour' : `${h} hours` }
  return `${minutes} min`
}
