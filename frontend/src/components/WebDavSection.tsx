import { FormEvent, useEffect, useState } from 'react'
import { api, WebDavDest } from '../api'

const INPUT = 'w-full border border-slate-300 dark:border-zinc-600 rounded-md px-3 py-2 text-sm bg-white dark:bg-zinc-800 text-slate-900 dark:text-zinc-100 placeholder-slate-400 dark:placeholder-zinc-500 focus:outline-none focus:ring-2 focus:ring-red-500'
const BTN_PRIMARY = 'bg-red-600 hover:bg-red-700 dark:bg-red-700 dark:hover:bg-red-600 text-white px-4 py-2 rounded-md text-sm font-medium disabled:opacity-50'

export default function WebDavSection({ onDestsChange }: { onDestsChange: (d: WebDavDest[]) => void }) {
  const [dests, setDests] = useState<WebDavDest[]>([])
  const [show, setShow] = useState(false)
  const [baseUrl, setBaseUrl] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [basePath, setBasePath] = useState('Photoswitch')
  const [label, setLabel] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [testingId, setTestingId] = useState<string | null>(null)
  const [testResults, setTestResults] = useState<Record<string, { ok: boolean; detail?: string }>>({})

  function publish(list: WebDavDest[]) { setDests(list); onDestsChange(list) }

  useEffect(() => { api.user.listWebdav().then(publish).catch(() => {}) }, [])

  async function add(e: FormEvent) {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const d = await api.user.addWebdav({
        base_url: baseUrl, username, password,
        base_path: basePath || undefined, label: label || undefined,
      })
      publish([...dests, d])
      setShow(false)
      setBaseUrl(''); setUsername(''); setPassword(''); setBasePath('Photoswitch'); setLabel('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save')
    } finally {
      setLoading(false)
    }
  }

  async function test(id: string) {
    setTestingId(id)
    try {
      const r = await api.user.testWebdav(id)
      setTestResults(p => ({ ...p, [id]: { ok: true, detail: r.user } }))
    } catch (err) {
      setTestResults(p => ({ ...p, [id]: { ok: false, detail: err instanceof Error ? err.message : 'Failed' } }))
    } finally {
      setTestingId(null)
    }
  }

  async function remove(id: string) {
    if (!confirm('Delete this WebDAV destination?')) return
    await api.user.deleteWebdav(id).catch(() => {})
    publish(dests.filter(d => d.id !== id))
  }

  return (
    <section className="mb-10">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h2 className="text-lg font-semibold text-slate-900 dark:text-zinc-100">WebDAV Destinations</h2>
          <p className="text-xs text-slate-500 dark:text-zinc-400 mt-0.5">Nextcloud, ownCloud, or PhotoPrism. Photos upload into folders; albums become subfolders.</p>
        </div>
        <button onClick={() => { setShow(v => !v); setError('') }} className={BTN_PRIMARY}>
          {show ? 'Cancel' : 'Add destination'}
        </button>
      </div>

      {show && (
        <form onSubmit={add} className="bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg p-5 mb-4 space-y-3">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label className="block text-sm font-medium text-slate-700 dark:text-zinc-300 mb-1">WebDAV URL</label>
              <input type="url" value={baseUrl} onChange={e => setBaseUrl(e.target.value)} required
                placeholder="https://cloud.example.com/remote.php/dav/files/alice" className={INPUT} />
              <p className="text-xs text-slate-400 dark:text-zinc-500 mt-1">
                Nextcloud/ownCloud: <code>…/remote.php/dav/files/&lt;user&gt;</code>. PhotoPrism: its WebDAV endpoint (e.g. <code>…/originals</code>).
              </p>
            </div>
            <div>
              <label className="block text-sm font-medium text-slate-700 dark:text-zinc-300 mb-1">Label (optional)</label>
              <input type="text" value={label} onChange={e => setLabel(e.target.value)} placeholder="My Nextcloud" className={INPUT} />
            </div>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <div>
              <label className="block text-sm font-medium text-slate-700 dark:text-zinc-300 mb-1">Username</label>
              <input type="text" value={username} onChange={e => setUsername(e.target.value)} required className={INPUT} />
            </div>
            <div>
              <label className="block text-sm font-medium text-slate-700 dark:text-zinc-300 mb-1">Password / app-password</label>
              <input type="password" value={password} onChange={e => setPassword(e.target.value)} required className={INPUT} />
            </div>
            <div>
              <label className="block text-sm font-medium text-slate-700 dark:text-zinc-300 mb-1">Upload folder</label>
              <input type="text" value={basePath} onChange={e => setBasePath(e.target.value)} placeholder="Photoswitch" className={INPUT} />
            </div>
          </div>
          <p className="text-xs text-slate-500 dark:text-zinc-400">
            Prefer an app-specific password if your server supports one. Album membership uses server-side copies, so a photo in several albums is uploaded only once.
          </p>
          {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
          <button type="submit" disabled={loading} className={BTN_PRIMARY}>{loading ? 'Saving…' : 'Save destination'}</button>
        </form>
      )}

      {dests.length === 0 && !show ? (
        <p className="text-sm text-slate-500 dark:text-zinc-400">No WebDAV destinations saved yet.</p>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {dests.map(d => {
            const tr = testResults[d.id]
            return (
              <div key={d.id} className="bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg p-4 flex flex-col gap-1">
                <p className="text-sm font-medium text-slate-900 dark:text-zinc-100 truncate">{d.label ?? d.base_url}</p>
                <p className="text-xs text-slate-500 dark:text-zinc-400 truncate">{d.username} · /{d.base_path}</p>
                {tr && (
                  <p className={`text-xs mt-1 ${tr.ok ? 'text-green-600 dark:text-green-400' : 'text-red-500 dark:text-red-400'}`}>
                    {tr.ok ? 'Connected' : tr.detail}
                  </p>
                )}
                <div className="flex items-center gap-3 mt-2 pt-2 border-t border-slate-100 dark:border-zinc-700">
                  <button onClick={() => test(d.id)} disabled={testingId !== null}
                    className="text-xs text-slate-500 dark:text-zinc-400 hover:text-slate-800 dark:hover:text-zinc-200 disabled:opacity-50">
                    {testingId === d.id ? 'Testing…' : 'Test'}
                  </button>
                  <button onClick={() => remove(d.id)} className="text-xs text-red-500 dark:text-red-400 hover:text-red-700 dark:hover:text-red-300">
                    Remove
                  </button>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}
