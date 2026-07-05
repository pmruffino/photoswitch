import { FormEvent, ReactNode, useEffect, useState } from 'react'
import { api, ImmichCred, WebDavDest, DestOption, toDestOptions, webdavServiceLabel } from '../api'

const INPUT = 'w-full border border-slate-300 dark:border-zinc-600 rounded-md px-3 py-2 text-sm bg-white dark:bg-zinc-800 text-slate-900 dark:text-zinc-100 placeholder-slate-400 dark:placeholder-zinc-500 focus:outline-none focus:ring-2 focus:ring-red-500'
const BTN_PRIMARY = 'bg-red-600 hover:bg-red-700 dark:bg-red-700 dark:hover:bg-red-600 text-white px-4 py-2 rounded-md text-sm font-medium disabled:opacity-50'
const BTN_SECONDARY = 'bg-white dark:bg-zinc-800 border border-slate-300 dark:border-zinc-600 text-slate-700 dark:text-zinc-300 px-4 py-2 rounded-md text-sm hover:bg-slate-50 dark:hover:bg-zinc-700'
const LBL = 'block text-sm font-medium text-slate-700 dark:text-zinc-300 mb-1'

// Selectable destination types. The four WebDAV variants share the `webdav` transport
// but are stored with a distinct `service` so they can be treated differently later.
type DestType = 'immich' | 'nextcloud' | 'owncloud' | 'photoprism' | 'webdav-other'
const DEST_TYPES: { value: DestType; label: string }[] = [
  { value: 'immich', label: 'Immich' },
  { value: 'nextcloud', label: 'Nextcloud' },
  { value: 'owncloud', label: 'ownCloud' },
  { value: 'photoprism', label: 'PhotoPrism' },
  { value: 'webdav-other', label: 'WebDAV-Other' },
]
const isWebdav = (t: DestType) => t !== 'immich'
const serviceOf = (t: DestType) => (t === 'webdav-other' ? 'other' : t)

function pathTemplate(t: DestType, username: string): string {
  const u = username || '<username>'
  return t === 'photoprism' ? '/originals' : `/remote.php/dav/files/${u}`
}

function Card({ typeLabel, title, sub, tr, testing, onTest, onRemove, onEdit }: {
  typeLabel: string; title: string; sub: string
  tr?: { ok: boolean; detail?: string }; testing: boolean
  onTest: () => void; onRemove: () => void; onEdit?: () => void
}): ReactNode {
  return (
    <div className="bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg p-4 flex flex-col gap-1">
      <div className="flex items-center gap-2">
        <p className="text-sm font-medium text-slate-900 dark:text-zinc-100 truncate">{title}</p>
        <span className="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium bg-slate-100 text-slate-600 dark:bg-zinc-700 dark:text-zinc-300">{typeLabel}</span>
      </div>
      <p className="text-xs text-slate-500 dark:text-zinc-400 truncate">{sub}</p>
      {tr && (
        <p className={`text-xs mt-1 ${tr.ok ? 'text-green-600 dark:text-green-400' : 'text-red-500 dark:text-red-400'}`}>
          {tr.ok ? `Connected${tr.detail ? ` as ${tr.detail}` : ''}` : tr.detail}
        </p>
      )}
      <div className="flex items-center gap-3 mt-2 pt-2 border-t border-slate-100 dark:border-zinc-700">
        <button onClick={onTest} disabled={testing} className="text-xs text-slate-500 dark:text-zinc-400 hover:text-slate-800 dark:hover:text-zinc-200 disabled:opacity-50">
          {testing ? 'Testing…' : 'Test'}
        </button>
        {onEdit && <button onClick={onEdit} className="text-xs text-slate-600 dark:text-zinc-400 hover:text-slate-900 dark:hover:text-zinc-100">Modify</button>}
        <button onClick={onRemove} className="text-xs text-red-500 dark:text-red-400 hover:text-red-700 dark:hover:text-red-300">Remove</button>
      </div>
    </div>
  )
}

export default function DestinationsSection({ onDestinationsChange }: { onDestinationsChange: (o: DestOption[]) => void }) {
  const [immich, setImmich] = useState<ImmichCred[]>([])
  const [webdavs, setWebdavs] = useState<WebDavDest[]>([])

  const [adding, setAdding] = useState(false)
  const [addType, setAddType] = useState<DestType>('immich')
  // Immich add fields
  const [imUrl, setImUrl] = useState('')
  const [imKey, setImKey] = useState('')
  const [imKeyVisible, setImKeyVisible] = useState(false)
  // WebDAV add fields
  const [serverUrl, setServerUrl] = useState('')
  const [path, setPath] = useState(pathTemplate('nextcloud', ''))
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [basePath, setBasePath] = useState('')
  const [label, setLabel] = useState('')
  const [addError, setAddError] = useState('')
  const [addLoading, setAddLoading] = useState(false)

  // Immich edit
  const [editId, setEditId] = useState<string | null>(null)
  const [edUrl, setEdUrl] = useState('')
  const [edLabel, setEdLabel] = useState('')
  const [edKey, setEdKey] = useState('')
  const [edError, setEdError] = useState('')
  const [edLoading, setEdLoading] = useState(false)

  const [testingId, setTestingId] = useState<string | null>(null)
  const [testResults, setTestResults] = useState<Record<string, { ok: boolean; detail?: string }>>({})

  function publish(im: ImmichCred[], wd: WebDavDest[]) {
    setImmich(im); setWebdavs(wd); onDestinationsChange(toDestOptions(im, wd))
  }

  useEffect(() => {
    Promise.all([
      api.user.listImmich().catch(() => [] as ImmichCred[]),
      api.user.listWebdav().catch(() => [] as WebDavDest[]),
    ]).then(([im, wd]) => publish(im, wd))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function chooseType(t: DestType) {
    setAddType(t)
    setAddError('')
    if (isWebdav(t)) setPath(pathTemplate(t, username))
  }
  function changeUsername(v: string) {
    setUsername(v)
    if (isWebdav(addType) && addType !== 'webdav-other') setPath(pathTemplate(addType, v))
  }
  function resetAdd() {
    setAddType('immich'); setImUrl(''); setImKey(''); setImKeyVisible(false)
    setServerUrl(''); setPath(pathTemplate('nextcloud', '')); setUsername(''); setPassword(''); setBasePath(''); setLabel('')
  }

  async function submitAdd(e: FormEvent) {
    e.preventDefault()
    setAddError('')
    setAddLoading(true)
    try {
      if (addType === 'immich') {
        const c = await api.user.addImmich({ server_url: imUrl, api_key: imKey, label: label || undefined })
        publish([...immich, c], webdavs)
      } else {
        if (path.includes('<username>')) { setAddError('Enter a username to complete the path'); setAddLoading(false); return }
        const base_url = serverUrl.replace(/\/+$/, '') + path
        const d = await api.user.addWebdav({
          base_url, username, password, service: serviceOf(addType),
          base_path: basePath || undefined, label: label || undefined,
        })
        publish(immich, [...webdavs, d])
      }
      setAdding(false); resetAdd()
    } catch (err) {
      setAddError(err instanceof Error ? err.message : 'Failed to save')
    } finally {
      setAddLoading(false)
    }
  }

  function startEdit(c: ImmichCred) {
    setAdding(false); setEditId(c.id); setEdUrl(c.server_url); setEdLabel(c.label ?? ''); setEdKey(''); setEdError('')
  }
  function cancelEdit() { setEditId(null); setEdUrl(''); setEdLabel(''); setEdKey('') }
  async function saveEdit(e: FormEvent) {
    e.preventDefault(); if (!editId) return
    setEdError(''); setEdLoading(true)
    try {
      const u = await api.user.updateImmich(editId, { server_url: edUrl || undefined, label: edLabel || undefined, api_key: edKey || undefined })
      publish(immich.map(c => c.id === editId ? u : c), webdavs)
      cancelEdit()
    } catch (err) {
      setEdError(err instanceof Error ? err.message : 'Failed to update')
    } finally { setEdLoading(false) }
  }

  async function test(kind: 'immich' | 'webdav', id: string) {
    setTestingId(id)
    try {
      const r = kind === 'immich' ? await api.user.testImmich(id) : await api.user.testWebdav(id)
      setTestResults(p => ({ ...p, [id]: { ok: true, detail: r.user } }))
    } catch (err) {
      setTestResults(p => ({ ...p, [id]: { ok: false, detail: err instanceof Error ? err.message : 'Failed' } }))
    } finally { setTestingId(null) }
  }
  async function removeImmich(id: string) {
    if (!confirm('Delete this Immich destination?')) return
    await api.user.deleteImmich(id).catch(() => {})
    publish(immich.filter(c => c.id !== id), webdavs)
  }
  async function removeWebdav(id: string) {
    if (!confirm('Delete this WebDAV destination?')) return
    await api.user.deleteWebdav(id).catch(() => {})
    publish(immich, webdavs.filter(d => d.id !== id))
  }

  const total = immich.length + webdavs.length
  const fullUrl = serverUrl.replace(/\/+$/, '') + path

  return (
    <section className="mb-12">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h2 className="text-xl font-bold text-slate-900 dark:text-zinc-100">Image Destinations</h2>
          <p className="text-xs text-slate-500 dark:text-zinc-400 mt-0.5">Where imported photos are uploaded — Immich, or a WebDAV server (Nextcloud, ownCloud, PhotoPrism).</p>
        </div>
        <button onClick={() => { setAdding(v => !v); cancelEdit(); setAddError('') }} className={BTN_PRIMARY}>
          {adding ? 'Cancel' : 'Add destination'}
        </button>
      </div>

      {adding && (
        <form onSubmit={submitAdd} className="bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg p-5 mb-4 space-y-3">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label className={LBL}>Destination type</label>
              <select value={addType} onChange={e => chooseType(e.target.value as DestType)} className={INPUT}>
                {DEST_TYPES.map(t => <option key={t.value} value={t.value}>{t.label}</option>)}
              </select>
            </div>
            <div>
              <label className={LBL}>Label (optional)</label>
              <input type="text" value={label} onChange={e => setLabel(e.target.value)} placeholder="Friendly name" className={INPUT} />
            </div>
          </div>

          {addType === 'immich' ? (
            <>
              <div>
                <label className={LBL}>Server URL</label>
                <input type="url" value={imUrl} onChange={e => setImUrl(e.target.value)} required placeholder="https://immich.example.com" className={INPUT} />
              </div>
              <div>
                <label className={LBL}>API Key</label>
                <div className="relative">
                  <input type={imKeyVisible ? 'text' : 'password'} value={imKey} onChange={e => setImKey(e.target.value)} required placeholder="Paste your Immich API key" className={INPUT + ' pr-16'} />
                  <button type="button" onClick={() => setImKeyVisible(v => !v)} className="absolute right-2 top-1/2 -translate-y-1/2 text-xs text-slate-500 dark:text-zinc-400 hover:text-slate-800 dark:hover:text-zinc-200">
                    {imKeyVisible ? 'Hide' : 'Show'}
                  </button>
                </div>
                <p className="text-xs text-slate-400 dark:text-zinc-500 mt-1">Generate under Immich → Account Settings → API Keys (shown once — copy it immediately).</p>
              </div>
            </>
          ) : (
            <>
              <div>
                <label className={LBL}>Server</label>
                <input type="url" value={serverUrl} onChange={e => setServerUrl(e.target.value)} required placeholder="https://cloud.example.com" className={INPUT} />
              </div>
              <div>
                <label className={LBL}>
                  WebDAV path
                  {addType !== 'webdav-other' && <span className="font-normal text-slate-400 dark:text-zinc-500"> (auto-filled — choose “WebDAV-Other” to edit)</span>}
                </label>
                <input type="text" value={path} onChange={e => setPath(e.target.value)} readOnly={addType !== 'webdav-other'} required
                  className={INPUT + (addType !== 'webdav-other' ? ' opacity-70 cursor-not-allowed' : '')} />
                {serverUrl && <p className="text-xs text-slate-400 dark:text-zinc-500 mt-1 break-all">Full URL: <span className="font-mono text-slate-500 dark:text-zinc-400">{fullUrl}</span></p>}
                {addType === 'photoprism' && (
                  <p className="text-xs text-slate-400 dark:text-zinc-500 mt-1">
                    PhotoPrism serves WebDAV only at <code>/originals</code>. Its albums are virtual and can't be set over WebDAV, so album metadata is written as <strong>folders</strong> under originals (PhotoPrism shows them under “Folders” and indexes them automatically).
                  </p>
                )}
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                <div><label className={LBL}>Username</label><input type="text" value={username} onChange={e => changeUsername(e.target.value)} required className={INPUT} /></div>
                <div><label className={LBL}>Password / app-password</label><input type="password" value={password} onChange={e => setPassword(e.target.value)} required className={INPUT} /></div>
                <div><label className={LBL}>Base folder <span className="font-normal text-slate-400 dark:text-zinc-500">(optional)</span></label><input type="text" value={basePath} onChange={e => setBasePath(e.target.value)} placeholder="root of the server" className={INPUT} /></div>
              </div>
              <p className="text-xs text-slate-500 dark:text-zinc-400">
                Leave <strong>Base folder</strong> empty to upload into the root; set it to keep everything under one folder. Photos with album metadata always go into an album subfolder. Prefer an app-specific password if your server supports one.
              </p>
            </>
          )}
          {addError && <p className="text-sm text-red-600 dark:text-red-400">{addError}</p>}
          <button type="submit" disabled={addLoading} className={BTN_PRIMARY}>{addLoading ? 'Saving…' : 'Save destination'}</button>
        </form>
      )}

      {total === 0 && !adding ? (
        <p className="text-sm text-slate-500 dark:text-zinc-400">No destinations yet. Add one to start importing.</p>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {immich.map(c => editId === c.id ? (
            <div key={c.id} className="col-span-full bg-white dark:bg-zinc-900 border border-red-300 dark:border-red-800 rounded-lg p-5 space-y-3">
              <p className="text-sm font-semibold text-slate-800 dark:text-zinc-100">Edit Immich destination</p>
              <form onSubmit={saveEdit} className="space-y-3">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div><label className={LBL}>Server URL</label><input type="url" value={edUrl} onChange={e => setEdUrl(e.target.value)} required className={INPUT} /></div>
                  <div><label className={LBL}>Label (optional)</label><input type="text" value={edLabel} onChange={e => setEdLabel(e.target.value)} className={INPUT} /></div>
                </div>
                <div>
                  <label className={LBL}>New API Key <span className="font-normal text-slate-400 dark:text-zinc-500">(leave blank to keep existing)</span></label>
                  <input type="password" value={edKey} onChange={e => setEdKey(e.target.value)} placeholder="Enter new key to replace" className={INPUT} />
                </div>
                {edError && <p className="text-sm text-red-600 dark:text-red-400">{edError}</p>}
                <div className="flex gap-2">
                  <button type="submit" disabled={edLoading} className={BTN_PRIMARY}>{edLoading ? 'Saving…' : 'Save changes'}</button>
                  <button type="button" onClick={cancelEdit} className={BTN_SECONDARY}>Cancel</button>
                </div>
              </form>
            </div>
          ) : (
            <Card key={`i:${c.id}`} typeLabel="Immich" title={c.label ?? c.server_url} sub={c.server_url}
              tr={testResults[c.id]} testing={testingId === c.id}
              onTest={() => test('immich', c.id)} onRemove={() => removeImmich(c.id)} onEdit={() => startEdit(c)} />
          ))}
          {webdavs.map(d => (
            <Card key={`w:${d.id}`} typeLabel={webdavServiceLabel(d.service)} title={d.label ?? d.base_url}
              sub={`${d.username} · ${d.base_path ? '/' + d.base_path : 'root'}`}
              tr={testResults[d.id]} testing={testingId === d.id}
              onTest={() => test('webdav', d.id)} onRemove={() => removeWebdav(d.id)} />
          ))}
        </div>
      )}
    </section>
  )
}
