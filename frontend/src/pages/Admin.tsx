import { Fragment, FormEvent, useEffect, useState } from 'react'
import Layout from '../components/Layout'
import { AdminJob, api, Config, User } from '../api'
import { useAuth } from '../contexts/auth'

type Tab = 'users' | 'settings' | 'jobs'

const STAGE_COLORS: Record<string, string> = {
  fetch:  'bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400',
  unpack: 'bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-400',
  map:    'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-400',
  load:   'bg-teal-100 text-teal-700 dark:bg-teal-900/30 dark:text-teal-400',
}

const STATUS_COLORS: Record<string, string> = {
  queued:    'bg-slate-100 text-slate-600 dark:bg-zinc-700 dark:text-zinc-300',
  running:   'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400',
  succeeded: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400',
  failed:    'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400',
  cancelled: 'bg-slate-100 text-slate-500 dark:bg-zinc-700 dark:text-zinc-400',
}

const POLICY_LABELS: Record<string, string> = {
  open: 'Open — anyone can register',
  approval: 'Approval required — admin must approve',
  closed: 'Closed — admin creates accounts only',
}

const SOURCE_LABELS: Record<string, string> = {
  google_takeout: 'Google',
  icloud_direct: 'iCloud',
  icloud_bundle: 'iCloud bundle',
}

type StageName = 'fetch' | 'unpack' | 'map' | 'load' | 'rollback'

const STAGE_CONFIGS: { stage: StageName; label: string; description: string }[] = [
  {
    stage: 'fetch',
    label: 'Fetch',
    description: 'Downloads a Google Takeout archive from the user URL, or pulls new photos from an iCloud connection. Bottleneck is outbound bandwidth and the remote provider’s rate limits.',
  },
  {
    stage: 'unpack',
    label: 'Unpack',
    description: 'Streams the archive to disk (Google / iCloud export bundles). I/O-bound on the staging volume; CPU is minimal. Skipped for iCloud direct pulls.',
  },
  {
    stage: 'map',
    label: 'Map',
    description: 'Writes correct EXIF via exiftool from Google sidecars, the iCloud manifest, or embedded metadata. CPU-bound — benefits most from additional workers.',
  },
  {
    stage: 'load',
    label: 'Load',
    description: 'Uploads assets to the destination (Immich API or WebDAV). Bottleneck is network throughput and the destination’s ingestion speed.',
  },
  {
    stage: 'rollback',
    label: 'Rollback',
    description: 'Removes a job’s previously uploaded assets from the destination (Immich or WebDAV). Usually fast; 1–2 workers is sufficient.',
  },
]

const INPUT = 'w-full border border-slate-300 dark:border-zinc-600 rounded-md px-3 py-2 text-sm bg-white dark:bg-zinc-800 text-slate-900 dark:text-zinc-100 placeholder-slate-400 dark:placeholder-zinc-500 focus:outline-none focus:ring-2 focus:ring-red-500'
const BTN_PRIMARY = 'bg-red-600 hover:bg-red-700 dark:bg-red-700 dark:hover:bg-red-600 text-white px-4 py-2 rounded-md text-sm font-medium disabled:opacity-50'

export default function Admin() {
  const { user: me } = useAuth()
  const [tab, setTab] = useState<Tab>('users')
  const [users, setUsers] = useState<User[]>([])
  const [loadingUsers, setLoadingUsers] = useState(true)
  const [loadingConfig, setLoadingConfig] = useState(true)

  const [showCreate, setShowCreate] = useState(false)
  const [newUsername, setNewUsername] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [newEmail, setNewEmail] = useState('')
  const [newRole, setNewRole] = useState<'user' | 'admin'>('user')
  const [createError, setCreateError] = useState('')
  const [createLoading, setCreateLoading] = useState(false)

  const [editUserId, setEditUserId] = useState<string | null>(null)
  const [editEmail, setEditEmail] = useState('')
  const [editPassword, setEditPassword] = useState('')
  const [editError, setEditError] = useState('')
  const [editSaving, setEditSaving] = useState(false)

  const [policy, setPolicy] = useState<Config['signup_policy']>('open')
  const [workerPct, setWorkerPct] = useState<Config['worker_pct']>({ fetch: 100, unpack: 100, map: 100, load: 100, rollback: 100 })
  const [workerCounts, setWorkerCounts] = useState<Config['worker_counts']>({ fetch: 0, unpack: 0, map: 0, load: 0, rollback: 0 })
  const [maxTakeoutGb, setMaxTakeoutGb] = useState(15)
  const [retentionDays, setRetentionDays] = useState(7)
  const [cleanupHour, setCleanupHour] = useState(3)
  const [configSaving, setConfigSaving] = useState(false)
  const [configMsg, setConfigMsg] = useState('')

  const [allJobs, setAllJobs] = useState<AdminJob[]>([])
  const [loadingJobs, setLoadingJobs] = useState(false)

  useEffect(() => {
    api.admin.listUsers().then(setUsers).finally(() => setLoadingUsers(false))
    api.admin.getConfig()
      .then(cfg => {
        setPolicy(cfg.signup_policy)
        setWorkerPct(cfg.worker_pct)
        setWorkerCounts(cfg.worker_counts)
        setMaxTakeoutGb(cfg.max_takeout_gb)
        setRetentionDays(cfg.staging_retention_days)
        setCleanupHour(cfg.cleanup_hour)
      })
      .finally(() => setLoadingConfig(false))
  }, [])

  async function handleCreateUser(e: FormEvent) {
    e.preventDefault()
    setCreateError('')
    setCreateLoading(true)
    try {
      const u = await api.admin.createUser({ username: newUsername, password: newPassword, email: newEmail || undefined, role: newRole })
      setUsers(prev => [...prev, u])
      setShowCreate(false)
      setNewUsername(''); setNewPassword(''); setNewEmail(''); setNewRole('user')
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : 'Failed to create user')
    } finally {
      setCreateLoading(false)
    }
  }

  async function updateUser(id: string, data: Parameters<typeof api.admin.updateUser>[1]) {
    const updated = await api.admin.updateUser(id, data).catch(err => {
      alert(err instanceof Error ? err.message : 'Update failed')
      return null
    })
    if (updated) setUsers(prev => prev.map(u => u.id === id ? updated : u))
  }

  async function deleteUser(id: string, username: string) {
    if (!confirm(`Delete user "${username}"? This cannot be undone.`)) return
    await api.admin.deleteUser(id).catch(err => { alert(err instanceof Error ? err.message : 'Delete failed') })
    setUsers(prev => prev.filter(u => u.id !== id))
  }

  function startEditUser(u: User) {
    setEditUserId(u.id)
    setEditEmail(u.email ?? '')
    setEditPassword('')
    setEditError('')
  }

  function cancelEditUser() {
    setEditUserId(null)
    setEditEmail(''); setEditPassword(''); setEditError('')
  }

  async function saveEditUser(e: FormEvent) {
    e.preventDefault()
    if (!editUserId) return
    setEditError('')
    setEditSaving(true)
    try {
      const data: { email?: string; new_password?: string } = {}
      if (editEmail !== (users.find(u => u.id === editUserId)?.email ?? '')) data.email = editEmail
      if (editPassword) data.new_password = editPassword
      if (Object.keys(data).length === 0) { cancelEditUser(); return }
      const updated = await api.admin.updateUser(editUserId, data)
      setUsers(prev => prev.map(u => u.id === editUserId ? updated : u))
      cancelEditUser()
    } catch (err) {
      setEditError(err instanceof Error ? err.message : 'Save failed')
    } finally {
      setEditSaving(false)
    }
  }

  useEffect(() => {
    if (tab !== 'jobs') return
    setLoadingJobs(true)
    api.admin.listJobs().then(setAllJobs).finally(() => setLoadingJobs(false))
  }, [tab])

  async function deleteAdminJob(job: AdminJob) {
    const running = job.status === 'running' || job.status === 'queued'
    const msg = running
      ? `This job (${job.username}) is currently ${job.status}. Deleting it will remove all staged files and cannot be undone. Continue?`
      : `Delete job for "${job.username}" and all associated staged files? This cannot be undone.`
    if (!confirm(msg)) return
    await api.admin.deleteJob(job.job_id).catch(err => alert(err instanceof Error ? err.message : 'Delete failed'))
    setAllJobs(prev => prev.filter(j => j.job_id !== job.job_id))
  }

  async function saveConfig() {
    setConfigSaving(true)
    setConfigMsg('')
    try {
      await api.admin.updateConfig({ signup_policy: policy, worker_pct: workerPct, max_takeout_gb: maxTakeoutGb, staging_retention_days: retentionDays, cleanup_hour: cleanupHour })
      setConfigMsg('Settings saved.')
    } catch (err) {
      setConfigMsg(err instanceof Error ? err.message : 'Save failed')
    } finally {
      setConfigSaving(false)
    }
  }

  return (
    <Layout>
      <h1 className="text-xl font-semibold text-slate-900 dark:text-zinc-100 mb-6">Admin</h1>

      <div className="flex gap-2 mb-6 border-b border-slate-200 dark:border-zinc-700">
        {(['users', 'settings', 'jobs'] as Tab[]).map(t => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`pb-2 px-1 text-sm font-medium border-b-2 -mb-px ${
              tab === t
                ? 'border-red-600 text-red-600 dark:border-red-500 dark:text-red-400'
                : 'border-transparent text-slate-500 dark:text-zinc-400 hover:text-slate-900 dark:hover:text-zinc-100'
            }`}
          >
            {t.charAt(0).toUpperCase() + t.slice(1)}
          </button>
        ))}
      </div>

      {tab === 'users' && (
        <div>
          <div className="flex items-center justify-between mb-4">
            <p className="text-sm text-slate-500 dark:text-zinc-400">{users.length} user{users.length !== 1 ? 's' : ''}</p>
            <button onClick={() => setShowCreate(v => !v)} className={BTN_PRIMARY}>
              {showCreate ? 'Cancel' : 'Create user'}
            </button>
          </div>

          {showCreate && (
            <form onSubmit={handleCreateUser} className="bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg p-5 mb-4 space-y-3">
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm font-medium text-slate-700 dark:text-zinc-300 mb-1">Username</label>
                  <input value={newUsername} onChange={e => setNewUsername(e.target.value)} required minLength={3} className={INPUT} />
                </div>
                <div>
                  <label className="block text-sm font-medium text-slate-700 dark:text-zinc-300 mb-1">Email (optional)</label>
                  <input type="email" value={newEmail} onChange={e => setNewEmail(e.target.value)} className={INPUT} />
                </div>
                <div>
                  <label className="block text-sm font-medium text-slate-700 dark:text-zinc-300 mb-1">Password</label>
                  <input type="password" value={newPassword} onChange={e => setNewPassword(e.target.value)} required minLength={8} className={INPUT} />
                </div>
                <div>
                  <label className="block text-sm font-medium text-slate-700 dark:text-zinc-300 mb-1">Role</label>
                  <select value={newRole} onChange={e => setNewRole(e.target.value as 'user' | 'admin')} className={INPUT}>
                    <option value="user">User</option>
                    <option value="admin">Admin</option>
                  </select>
                </div>
              </div>
              {createError && <p className="text-sm text-red-600 dark:text-red-400">{createError}</p>}
              <button type="submit" disabled={createLoading} className={BTN_PRIMARY}>
                {createLoading ? 'Creating…' : 'Create user'}
              </button>
            </form>
          )}

          {loadingUsers ? (
            <p className="text-sm text-slate-500 dark:text-zinc-400">Loading…</p>
          ) : (
            <div className="bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg overflow-hidden">
              <table className="w-full text-sm">
                <thead className="bg-slate-50 dark:bg-zinc-800 border-b border-slate-200 dark:border-zinc-700">
                  <tr>
                    {['Username', 'Email', 'Role', 'Status', 'Joined', 'Actions'].map(h => (
                      <th key={h} className="text-left px-4 py-3 font-medium text-slate-600 dark:text-zinc-400">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 dark:divide-zinc-700">
                  {users.map(u => (
                    <Fragment key={u.id}>
                      <tr className="odd:bg-white dark:odd:bg-zinc-900 even:bg-slate-50 dark:even:bg-zinc-800/40">
                        <td className="px-4 py-3 font-medium text-slate-900 dark:text-zinc-100">{u.username}</td>
                        <td className="px-4 py-3 text-slate-500 dark:text-zinc-400">{u.email ?? '—'}</td>
                        <td className="px-4 py-3">
                          <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${
                            u.role === 'admin'
                              ? 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400'
                              : 'bg-slate-100 text-slate-600 dark:bg-zinc-700 dark:text-zinc-300'
                          }`}>
                            {u.role}
                          </span>
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex flex-col gap-1">
                            {!u.is_approved && (
                              <span className="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400">pending</span>
                            )}
                            {!u.is_active && (
                              <span className="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium bg-red-100 text-red-600 dark:bg-red-900/30 dark:text-red-400">disabled</span>
                            )}
                            {u.is_approved && u.is_active && (
                              <span className="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400">active</span>
                            )}
                          </div>
                        </td>
                        <td className="px-4 py-3 text-slate-500 dark:text-zinc-400">{new Date(u.created_at).toLocaleDateString()}</td>
                        <td className="px-4 py-3">
                          <div className="flex flex-wrap gap-2">
                            {!u.is_approved && (
                              <button onClick={() => updateUser(u.id, { is_approved: true })} className="text-xs text-green-600 dark:text-green-400 hover:underline">Approve</button>
                            )}
                            <button onClick={() => updateUser(u.id, { is_active: !u.is_active })} className="text-xs text-slate-600 dark:text-zinc-400 hover:underline">
                              {u.is_active ? 'Disable' : 'Enable'}
                            </button>
                            {u.id !== me?.id && (
                              <button onClick={() => updateUser(u.id, { role: u.role === 'admin' ? 'user' : 'admin' })} className="text-xs text-slate-600 dark:text-zinc-400 hover:underline">
                                {u.role === 'admin' ? 'Demote' : 'Promote'}
                              </button>
                            )}
                            <button
                              onClick={() => editUserId === u.id ? cancelEditUser() : startEditUser(u)}
                              className="text-xs text-slate-600 dark:text-zinc-400 hover:underline"
                            >
                              {editUserId === u.id ? 'Cancel' : 'Edit'}
                            </button>
                            {u.id !== me?.id && (
                              <button onClick={() => deleteUser(u.id, u.username)} className="text-xs text-red-500 dark:text-red-400 hover:underline">Delete</button>
                            )}
                          </div>
                        </td>
                      </tr>
                      {editUserId === u.id && (
                        <tr className="bg-slate-50 dark:bg-zinc-800/60">
                          <td colSpan={6} className="px-4 py-4">
                            <form onSubmit={saveEditUser} className="flex flex-wrap items-end gap-3">
                              <div>
                                <label className="block text-xs font-medium text-slate-600 dark:text-zinc-400 mb-1">Email</label>
                                <input
                                  type="email"
                                  value={editEmail}
                                  onChange={e => setEditEmail(e.target.value)}
                                  placeholder="Leave blank to clear"
                                  className="border border-slate-300 dark:border-zinc-600 rounded-md px-3 py-1.5 text-sm bg-white dark:bg-zinc-800 text-slate-900 dark:text-zinc-100 placeholder-slate-400 dark:placeholder-zinc-500 focus:outline-none focus:ring-2 focus:ring-red-500 w-56"
                                />
                              </div>
                              <div>
                                <label className="block text-xs font-medium text-slate-600 dark:text-zinc-400 mb-1">New password <span className="font-normal text-slate-400 dark:text-zinc-500">(leave blank to keep)</span></label>
                                <input
                                  type="password"
                                  value={editPassword}
                                  onChange={e => setEditPassword(e.target.value)}
                                  minLength={8}
                                  placeholder="Min 8 characters"
                                  className="border border-slate-300 dark:border-zinc-600 rounded-md px-3 py-1.5 text-sm bg-white dark:bg-zinc-800 text-slate-900 dark:text-zinc-100 placeholder-slate-400 dark:placeholder-zinc-500 focus:outline-none focus:ring-2 focus:ring-red-500 w-48"
                                />
                              </div>
                              <div className="flex items-end gap-2">
                                <button type="submit" disabled={editSaving} className={BTN_PRIMARY}>
                                  {editSaving ? 'Saving…' : 'Save'}
                                </button>
                                <button type="button" onClick={cancelEditUser} className="text-sm text-slate-500 dark:text-zinc-400 hover:text-slate-800 dark:hover:text-zinc-200 px-2 py-2">
                                  Cancel
                                </button>
                              </div>
                              {editError && <p className="w-full text-sm text-red-600 dark:text-red-400">{editError}</p>}
                            </form>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {tab === 'settings' && (
        <div className="space-y-8 max-w-2xl">
          {loadingConfig ? (
            <p className="text-sm text-slate-500 dark:text-zinc-400">Loading…</p>
          ) : (
            <>
              <div>
                <h3 className="text-sm font-semibold text-slate-900 dark:text-zinc-100 mb-3">New User Policy</h3>
                <div className="space-y-2">
                  {(['open', 'approval', 'closed'] as const).map(p => (
                    <label key={p} className="flex items-center gap-3 cursor-pointer">
                      <input
                        type="radio"
                        name="policy"
                        value={p}
                        checked={policy === p}
                        onChange={() => setPolicy(p)}
                        className="accent-red-600"
                      />
                      <span className="text-sm text-slate-700 dark:text-zinc-300">{POLICY_LABELS[p]}</span>
                    </label>
                  ))}
                </div>
              </div>

              <div>
                <h3 className="text-sm font-semibold text-slate-900 dark:text-zinc-100 mb-1">Pipeline Concurrency</h3>
                <p className="text-xs text-slate-500 dark:text-zinc-400 mb-3">
                  Percentage of available workers allowed to run simultaneously per stage. The effective
                  slot count is <code className="font-mono">max(1, round(workers × pct%))</code> and is
                  recalculated live whenever workers start or stop — no redeploy needed.
                </p>
                <div className="flex flex-wrap gap-2 mb-5">
                  {([100, 75, 50, 25] as const).map(pct => (
                    <button
                      key={pct}
                      type="button"
                      onClick={() => setWorkerPct({ fetch: pct, unpack: pct, map: pct, load: pct, rollback: pct })}
                      className="text-xs px-3 py-1.5 rounded-md border border-slate-300 dark:border-zinc-600 hover:bg-slate-50 dark:hover:bg-zinc-800 text-slate-700 dark:text-zinc-300 font-medium"
                    >
                      {pct === 100 ? 'All (100%)' : `${pct}%`}
                    </button>
                  ))}
                </div>
                <div className="space-y-5">
                  {STAGE_CONFIGS.map(({ stage, label, description }) => {
                    const online = workerCounts[stage] ?? 0
                    const pct = workerPct[stage] ?? 100
                    const effective = Math.max(1, Math.round(online * pct / 100))
                    return (
                      <div key={stage}>
                        <div className="flex items-center justify-between mb-1">
                          <div className="flex items-center gap-2">
                            <span className="text-sm font-medium text-slate-800 dark:text-zinc-200">{label}</span>
                            <span className={`text-xs px-1.5 py-0.5 rounded-full font-medium ${
                              online > 0
                                ? 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400'
                                : 'bg-slate-100 text-slate-500 dark:bg-zinc-700 dark:text-zinc-400'
                            }`}>
                              {online} worker{online !== 1 ? 's' : ''}
                            </span>
                          </div>
                          <div className="flex items-center gap-2">
                            <input
                              type="number"
                              min={1}
                              max={100}
                              value={pct}
                              onChange={e => {
                                const v = Math.min(100, Math.max(1, Number(e.target.value)))
                                setWorkerPct(prev => ({ ...prev, [stage]: v }))
                              }}
                              className="w-14 border border-slate-300 dark:border-zinc-600 rounded-md px-2 py-1 text-sm text-center bg-white dark:bg-zinc-800 text-slate-900 dark:text-zinc-100 focus:outline-none focus:ring-2 focus:ring-red-500"
                            />
                            <span className="text-xs text-slate-400 dark:text-zinc-500">%</span>
                            <span className="text-xs text-slate-500 dark:text-zinc-400 min-w-[4rem] text-right">
                              → {effective} active
                            </span>
                          </div>
                        </div>
                        <input
                          type="range"
                          min={1}
                          max={100}
                          value={pct}
                          onChange={e => setWorkerPct(prev => ({ ...prev, [stage]: Number(e.target.value) }))}
                          className="w-full accent-red-600"
                        />
                        <p className="text-xs text-slate-400 dark:text-zinc-500 mt-0.5">{description}</p>
                      </div>
                    )
                  })}
                </div>
              </div>

              <div>
                <h3 className="text-sm font-semibold text-slate-900 dark:text-zinc-100 mb-1">Storage & Limits</h3>
                <p className="text-xs text-slate-500 dark:text-zinc-400 mb-4">
                  Controls how much data can be ingested and how long jobs are retained.
                  A daily background task first removes all job records and staging files older than this threshold,
                  then does a second pass to remove any orphaned files or folders left behind.
                </p>
                <div className="space-y-5">
                  <div>
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-sm font-medium text-slate-800 dark:text-zinc-200">Max takeout file size</span>
                      <div className="flex items-center gap-2">
                        <input
                          type="number"
                          min={1}
                          max={200}
                          value={maxTakeoutGb}
                          onChange={e => setMaxTakeoutGb(Math.min(200, Math.max(1, Number(e.target.value))))}
                          className="w-16 border border-slate-300 dark:border-zinc-600 rounded-md px-2 py-1 text-sm text-center bg-white dark:bg-zinc-800 text-slate-900 dark:text-zinc-100 focus:outline-none focus:ring-2 focus:ring-red-500"
                        />
                        <span className="text-xs text-slate-400 dark:text-zinc-500">GB</span>
                      </div>
                    </div>
                    <input type="range" min={1} max={200} value={maxTakeoutGb} onChange={e => setMaxTakeoutGb(Number(e.target.value))} className="w-full accent-red-600" />
                    <p className="text-xs text-slate-400 dark:text-zinc-500 mt-0.5">
                      Downloads/uploads that exceed this size are rejected immediately. A typical Google Photos export is 10–50 GB. (Applies to Google &amp; export-bundle archives; iCloud direct pulls stream photo-by-photo and are not bound by this limit.)
                    </p>
                  </div>
                  <div>
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-sm font-medium text-slate-800 dark:text-zinc-200">Staging retention</span>
                      <div className="flex items-center gap-2">
                        <input
                          type="number"
                          min={1}
                          max={365}
                          value={retentionDays}
                          onChange={e => setRetentionDays(Math.min(365, Math.max(1, Number(e.target.value))))}
                          className="w-16 border border-slate-300 dark:border-zinc-600 rounded-md px-2 py-1 text-sm text-center bg-white dark:bg-zinc-800 text-slate-900 dark:text-zinc-100 focus:outline-none focus:ring-2 focus:ring-red-500"
                        />
                        <span className="text-xs text-slate-400 dark:text-zinc-500">days</span>
                      </div>
                    </div>
                    <input type="range" min={1} max={90} value={Math.min(retentionDays, 90)} onChange={e => setRetentionDays(Number(e.target.value))} className="w-full accent-red-600" />
                    <p className="text-xs text-slate-400 dark:text-zinc-500 mt-0.5">
                      Completed and failed job records are deleted from the database and their staging files removed after this many days.
                      Set higher to allow more time for users to retry failed jobs.
                    </p>
                  </div>
                  <div>
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-sm font-medium text-slate-800 dark:text-zinc-200">Cleanup time</span>
                      <div className="flex items-center gap-2">
                        <input
                          type="number"
                          min={0}
                          max={23}
                          value={cleanupHour}
                          onChange={e => setCleanupHour(Math.min(23, Math.max(0, Number(e.target.value))))}
                          className="w-16 border border-slate-300 dark:border-zinc-600 rounded-md px-2 py-1 text-sm text-center bg-white dark:bg-zinc-800 text-slate-900 dark:text-zinc-100 focus:outline-none focus:ring-2 focus:ring-red-500"
                        />
                        <span className="text-xs text-slate-400 dark:text-zinc-500">:00 UTC</span>
                      </div>
                    </div>
                    <input
                      type="range"
                      min={0}
                      max={23}
                      value={cleanupHour}
                      onChange={e => setCleanupHour(Number(e.target.value))}
                      className="w-full accent-red-600"
                    />
                    <p className="text-xs text-slate-400 dark:text-zinc-500 mt-0.5">
                      Hour of day (UTC) when the daily cleanup task runs. Default is 3:00 UTC (off-peak).
                      The task next runs at {String(cleanupHour).padStart(2, '0')}:00 UTC.
                    </p>
                  </div>
                </div>
              </div>

              <div className="flex items-center gap-4">
                <button onClick={saveConfig} disabled={configSaving} className={BTN_PRIMARY}>
                  {configSaving ? 'Saving…' : 'Save settings'}
                </button>
                {configMsg && (
                  <p className={`text-sm ${configMsg.includes('failed') || configMsg.includes('Failed') ? 'text-red-600 dark:text-red-400' : 'text-green-600 dark:text-green-400'}`}>
                    {configMsg}
                  </p>
                )}
              </div>
            </>
          )}
        </div>
      )}
      {tab === 'jobs' && (
        <div>
          <div className="flex items-center justify-between mb-4">
            <p className="text-sm text-slate-500 dark:text-zinc-400">{allJobs.length} job{allJobs.length !== 1 ? 's' : ''} across all users</p>
            <button
              onClick={() => { setLoadingJobs(true); api.admin.listJobs().then(setAllJobs).finally(() => setLoadingJobs(false)) }}
              className="text-xs text-slate-500 dark:text-zinc-400 hover:text-slate-800 dark:hover:text-zinc-200"
            >
              Refresh
            </button>
          </div>
          {loadingJobs ? (
            <p className="text-sm text-slate-500 dark:text-zinc-400">Loading…</p>
          ) : allJobs.length === 0 ? (
            <p className="text-sm text-slate-500 dark:text-zinc-400">No jobs yet.</p>
          ) : (
            <div className="bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg overflow-hidden">
              <table className="w-full text-sm">
                <thead className="bg-slate-50 dark:bg-zinc-800 border-b border-slate-200 dark:border-zinc-700">
                  <tr>
                    {['User', 'Source', 'Stage', 'Status', 'Progress', 'Started', 'Error', ''].map((h, i) => (
                      <th key={i} className="text-left px-4 py-3 font-medium text-slate-600 dark:text-zinc-400">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 dark:divide-zinc-700">
                  {allJobs.map(job => {
                    const isParked = job.stage === 'fetch' && job.status === 'succeeded' && !job.auto_ingest
                    return (
                      <tr key={job.job_id} className="odd:bg-white dark:odd:bg-zinc-900 even:bg-slate-50 dark:even:bg-zinc-800/40">
                        <td className="px-4 py-3 font-medium text-slate-900 dark:text-zinc-100">{job.username}</td>
                        <td className="px-4 py-3 text-slate-500 dark:text-zinc-400 whitespace-nowrap">
                          {SOURCE_LABELS[job.source ?? 'google_takeout'] ?? 'Google'}
                          <span className="text-slate-400 dark:text-zinc-500"> → {job.destination_kind === 'webdav' ? 'WebDAV' : 'Immich'}</span>
                        </td>
                        <td className="px-4 py-3">
                          <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${STAGE_COLORS[job.stage] ?? 'bg-slate-100 text-slate-600 dark:bg-zinc-700 dark:text-zinc-300'}`}>
                            {job.stage}
                          </span>
                        </td>
                        <td className="px-4 py-3">
                          {isParked ? (
                            <span className="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium bg-teal-100 text-teal-700 dark:bg-teal-900/30 dark:text-teal-400">Ready</span>
                          ) : (
                            <div className="flex flex-col gap-0.5">
                              <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_COLORS[job.status] ?? ''}`}>
                                {job.status === 'running' && <span className="w-1.5 h-1.5 bg-blue-500 rounded-full animate-pulse" />}
                                {job.status}
                              </span>
                              {job.stage === 'fetch' && job.status === 'running' && job.processed_items > 0 && (
                                <span className="text-xs text-slate-400 dark:text-zinc-500 pl-1">
                                  {job.source === 'icloud_direct'
                                    ? `${job.processed_items.toLocaleString()} photos`
                                    : `${(job.processed_items / 1048576).toFixed(1)}${job.total_items != null ? ` / ${(job.total_items / 1048576).toFixed(1)}` : ''} MB`}
                                </span>
                              )}
                            </div>
                          )}
                        </td>
                        <td className="px-4 py-3 text-slate-600 dark:text-zinc-300">
                          {job.stage === 'fetch'
                            ? '—'
                            : job.total_items != null
                              ? `${job.processed_items} / ${job.total_items}`
                              : job.processed_items > 0 ? job.processed_items : '—'}
                        </td>
                        <td className="px-4 py-3 text-slate-500 dark:text-zinc-400 whitespace-nowrap">{new Date(job.created_at).toLocaleString()}</td>
                        <td className="px-4 py-3 text-red-600 dark:text-red-400 max-w-xs">
                          <span className="break-words whitespace-normal line-clamp-4" title={job.error ?? undefined}>{job.error ?? '—'}</span>
                        </td>
                        <td className="px-4 py-3">
                          <button
                            onClick={() => deleteAdminJob(job)}
                            title="Delete job and staged files"
                            className="text-slate-300 dark:text-zinc-600 hover:text-red-500 dark:hover:text-red-400 text-base leading-none"
                          >
                            ×
                          </button>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </Layout>
  )
}
