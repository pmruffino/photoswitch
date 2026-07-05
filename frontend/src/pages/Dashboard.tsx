import { Fragment, FormEvent, useCallback, useEffect, useState } from 'react'
import Layout from '../components/Layout'
import ICloudSection from '../components/ICloudSection'
import DestinationsSection from '../components/DestinationsSection'
import { api, DestOption, Job, uploadJobChunked } from '../api'

function stageProgress(job: Job): string {
  if (job.stage === 'fetch') return '—'
  const p = job.processed_items ?? 0
  const t = job.total_items
  if (t == null || t === 0) return p > 0 ? p.toLocaleString() : '—'
  const pct = Math.min(100, Math.round((p / t) * 100))
  const unit = job.stage === 'unpack' ? 'files' : job.stage === 'rollback' ? 'assets' : 'items'
  return `${p.toLocaleString()} / ${t.toLocaleString()} ${unit} (${pct}%)`
}

const STAGE_COLORS: Record<string, string> = {
  fetch:  'bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400',
  unpack: 'bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-400',
  map:    'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-400',
  load:     'bg-teal-100 text-teal-700 dark:bg-teal-900/30 dark:text-teal-400',
  rollback: 'bg-rose-100 text-rose-700 dark:bg-rose-900/30 dark:text-rose-400',
}

const STATUS_COLORS: Record<string, string> = {
  queued:    'bg-slate-100 text-slate-600 dark:bg-zinc-700 dark:text-zinc-300',
  running:   'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400',
  succeeded: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400',
  failed:    'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400',
  cancelled: 'bg-slate-100 text-slate-500 dark:bg-zinc-700 dark:text-zinc-400',
}

const SOURCE_LABELS: Record<string, string> = {
  google_takeout: 'Google',
  icloud_direct: 'iCloud',
  icloud_bundle: 'iCloud bundle',
}
const SOURCE_COLORS: Record<string, string> = {
  google_takeout: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400',
  icloud_direct:  'bg-sky-100 text-sky-700 dark:bg-sky-900/30 dark:text-sky-400',
  icloud_bundle:  'bg-sky-100 text-sky-700 dark:bg-sky-900/30 dark:text-sky-400',
}
function destinationLabel(job: Job): string {
  return job.destination_kind === 'webdav' ? 'WebDAV' : 'Immich'
}

const INPUT = 'w-full border border-slate-300 dark:border-zinc-600 rounded-md px-3 py-2 text-sm bg-white dark:bg-zinc-800 text-slate-900 dark:text-zinc-100 placeholder-slate-400 dark:placeholder-zinc-500 focus:outline-none focus:ring-2 focus:ring-red-500'
const BTN_PRIMARY = 'bg-red-600 hover:bg-red-700 dark:bg-red-700 dark:hover:bg-red-600 text-white px-4 py-2 rounded-md text-sm font-medium disabled:opacity-50'

function Badge({ text, color }: { text: string; color: string }) {
  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${color}`}>
      {text}
    </span>
  )
}

export default function Dashboard() {
  // Destination options (Immich + WebDAV) reported up by DestinationsSection.
  const [destOptions, setDestOptions] = useState<DestOption[]>([])
  const [jobs, setJobs] = useState<Job[]>([])
  const [showNewJob, setShowNewJob] = useState(false)

  // New bundle form. jobDest holds a combined "kind:id" destination key.
  const [jobUrl, setJobUrl] = useState('')
  const [jobDest, setJobDest] = useState('')
  const [jobAfterDate, setJobAfterDate] = useState('')
  const [jobBeforeDate, setJobBeforeDate] = useState('')
  const [jobAutoIngest, setJobAutoIngest] = useState(true)
  const [jobError, setJobError] = useState('')
  const [jobLoading, setJobLoading] = useState(false)
  const [jobMethod, setJobMethod] = useState<'url' | 'upload'>('url')
  const [jobFile, setJobFile] = useState<File | null>(null)
  const [uploadPct, setUploadPct] = useState(0)

  // Re-run state (adjust date filter on a completed date-filtered job)
  const [rerunJobId, setRerunJobId] = useState<string | null>(null)
  const [rerunAfter, setRerunAfter] = useState('')
  const [rerunBefore, setRerunBefore] = useState('')
  const [rerunUndated, setRerunUndated] = useState(true)
  const [rerunError, setRerunError] = useState('')
  const [rerunLoading, setRerunLoading] = useState(false)

  // Rollback state
  const [rollbackJobId, setRollbackJobId] = useState<string | null>(null)
  const [rollbackAfter, setRollbackAfter] = useState('')
  const [rollbackBefore, setRollbackBefore] = useState('')
  const [rollbackUndated, setRollbackUndated] = useState(true)
  const [rollbackError, setRollbackError] = useState('')
  const [rollbackLoading, setRollbackLoading] = useState(false)

  const fetchJobs = useCallback(async () => {
    try { setJobs(await api.jobs.list()) } catch {}
  }, [])

  useEffect(() => { fetchJobs() }, [fetchJobs])

  useEffect(() => {
    if (destOptions.length > 0 && !jobDest) {
      const saved = localStorage.getItem('psw:last-dest')
      if (saved && destOptions.some(o => `${o.kind}:${o.id}` === saved)) setJobDest(saved)
    }
  }, [destOptions, jobDest])

  const hasActiveJobs = jobs.some(j => j.status === 'running' || j.status === 'queued')
  useEffect(() => {
    if (!hasActiveJobs) return
    const id = setInterval(fetchJobs, 5000)
    return () => clearInterval(id)
  }, [hasActiveJobs, fetchJobs])

  async function createJob(e: FormEvent) {
    e.preventDefault()
    setJobError('')
    if (jobAfterDate && jobBeforeDate && jobAfterDate > jobBeforeDate) {
      setJobError('"After" date must be on or before "Before" date')
      return
    }
    if (!jobDest) { setJobError('Please choose a destination'); return }
    const sep = jobDest.indexOf(':')
    const destKind = jobDest.slice(0, sep)
    const destId = jobDest.slice(sep + 1)
    setJobLoading(true)
    try {
      let j: Job
      if (jobMethod === 'upload') {
        if (!jobFile) { setJobError('Please select a file to upload'); setJobLoading(false); return }
        setUploadPct(0)
        j = await uploadJobChunked(
          jobFile,
          {
            credential_id: destId,
            destination_kind: destKind,
            auto_ingest: jobAutoIngest,
            after_date: jobAfterDate || undefined,
            before_date: jobBeforeDate || undefined,
          },
          pct => setUploadPct(pct),
        )
        setUploadPct(0)
        setJobFile(null)
      } else {
        j = await api.jobs.create({
          takeout_url: jobUrl,
          credential_id: destId,
          destination_kind: destKind,
          auto_ingest: jobAutoIngest,
          after_date: jobAfterDate || undefined,
          before_date: jobBeforeDate || undefined,
        })
        setJobUrl('')
      }
      setJobs(prev => [j, ...prev])
      setShowNewJob(false)
      setJobAfterDate(''); setJobBeforeDate('')
    } catch (err) {
      setJobError(err instanceof Error ? err.message : 'Failed to create bundle')
    } finally {
      setJobLoading(false)
    }
  }

  async function resumeJob(jobId: string) {
    try {
      const j = await api.jobs.resume(jobId)
      setJobs(prev => prev.map(jb => jb.job_id === jobId ? j : jb))
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Resume failed')
    }
  }

  function startRerun(job: Job) {
    setRollbackJobId(null)
    setRerunJobId(job.job_id)
    setRerunAfter(job.date_filter?.after_date ?? '')
    setRerunBefore(job.date_filter?.before_date ?? '')
    setRerunUndated(job.date_filter?.include_undated ?? true)
    setRerunError('')
  }

  function startRollback(job: Job) {
    setRerunJobId(null)
    setRollbackJobId(job.job_id)
    setRollbackAfter('')
    setRollbackBefore('')
    setRollbackUndated(true)
    setRollbackError('')
  }

  async function submitRollback(e: FormEvent) {
    e.preventDefault()
    if (!rollbackJobId) return
    setRollbackError('')
    setRollbackLoading(true)
    try {
      const j = await api.jobs.rollback(rollbackJobId, {
        after_date: rollbackAfter || undefined,
        before_date: rollbackBefore || undefined,
        include_undated: rollbackUndated,
      })
      setJobs(prev => [j, ...prev])
      setRollbackJobId(null)
    } catch (err) {
      setRollbackError(err instanceof Error ? err.message : 'Rollback failed')
    } finally {
      setRollbackLoading(false)
    }
  }

  async function submitRerun(e: FormEvent) {
    e.preventDefault()
    if (!rerunJobId) return
    setRerunError('')
    setRerunLoading(true)
    try {
      const j = await api.jobs.rerun(rerunJobId, {
        after_date: rerunAfter || undefined,
        before_date: rerunBefore || undefined,
        include_undated: rerunUndated,
      })
      setJobs(prev => prev.map(jb => jb.job_id === rerunJobId ? j : jb))
      setRerunJobId(null)
    } catch (err) {
      setRerunError(err instanceof Error ? err.message : 'Re-run failed')
    } finally {
      setRerunLoading(false)
    }
  }

  function deleteJobMessage(job: Job): string {
    const blocks: string[] = []
    if (job.status === 'running' || job.status === 'queued') {
      blocks.push(`This job is currently ${job.status}.`)
    }
    if (job.is_sync_anchor) {
      blocks.push(
        'This import anchors a recurring iCloud sync. Deleting it will:\n' +
        '  • Stop the scheduled sync on this iCloud connection\n' +
        '  • Remove this job and its staged files',
      )
      blocks.push(
        'Your iCloud connection and any photos already uploaded to Immich are NOT affected — ' +
        'but automatic syncing will stop until you start a new import.',
      )
    } else {
      blocks.push('This will remove the job and all of its staged files.')
    }
    blocks.push('This cannot be undone. Delete anyway?')
    return blocks.join('\n\n')
  }

  async function deleteJob(job: Job) {
    if (!confirm(deleteJobMessage(job))) return
    await api.jobs.delete(job.job_id).catch(err => alert(err instanceof Error ? err.message : 'Delete failed'))
    setJobs(prev => prev.filter(j => j.job_id !== job.job_id))
  }

  return (
    <Layout>
      {/* Image Destinations */}
      <DestinationsSection onDestinationsChange={setDestOptions} />

      {/* Image Sources */}
      <h2 className="text-xl font-bold text-slate-900 dark:text-zinc-100 mb-1">Image Sources</h2>
      <p className="text-xs text-slate-500 dark:text-zinc-400 mb-5">Where your photos are imported from — Apple iCloud or Google Photos.</p>

      {/* Apple iCloud */}
      <ICloudSection destinations={destOptions} onJobCreated={fetchJobs} />

      {/* Google Takeout Bundles */}
      <section>
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-slate-900 dark:text-zinc-100">Google Takeout Bundles</h2>
          <button
            onClick={() => { setShowNewJob(v => !v); setJobMethod('url'); setJobFile(null); setJobError('') }}
            disabled={destOptions.length === 0}
            className={BTN_PRIMARY + ' disabled:opacity-40 disabled:cursor-not-allowed'}
            title={destOptions.length === 0 ? 'Add an Immich or WebDAV destination first' : undefined}
          >
            {showNewJob ? 'Cancel' : 'Add bundle'}
          </button>
        </div>

        {showNewJob && (
          <form onSubmit={createJob} className="bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg p-5 mb-4 space-y-3">
            {jobMethod === 'url' ? (
              <div>
                <label className="block text-sm font-medium text-slate-700 dark:text-zinc-300 mb-1">Google Takeout URL</label>
                <input type="url" value={jobUrl} onChange={e => setJobUrl(e.target.value)} required placeholder="https://drive.google.com/…" className={INPUT} />
                <div className="mt-2 bg-slate-50 dark:bg-zinc-800/60 border border-slate-200 dark:border-zinc-700 rounded-md px-3 py-2.5 space-y-1">
                  <p className="text-xs font-medium text-slate-700 dark:text-zinc-200">How to create and share your Google Takeout archive:</p>
                  <ol className="text-xs text-slate-600 dark:text-zinc-300 space-y-0.5 list-decimal list-inside">
                    <li>Go to <a href="https://takeout.google.com" target="_blank" rel="noreferrer" className="font-semibold underline hover:text-slate-900 dark:hover:text-zinc-100">takeout.google.com</a> and sign in to your Google account</li>
                    <li>Click <strong>Deselect all</strong>, then check <strong>Google Photos</strong> only</li>
                    <li>Click <strong>Next step</strong></li>
                    <li>Under <strong>Destination</strong>, choose <strong>Add to Drive</strong> — the archive goes directly to your Google Drive without a manual download</li>
                    <li>Click <strong>Create export</strong> — Google will email you when it's ready (can take hours to days for large libraries)</li>
                    <li>Once ready, open <strong><a href="https://drive.google.com" target="_blank" rel="noreferrer" className="underline hover:text-slate-900 dark:hover:text-zinc-100">Google Drive</a></strong> and navigate to the <strong>Takeout</strong> folder</li>
                    <li>Right-click the archive file → <strong>Share</strong> → change access to <strong>Anyone with the link</strong> → <strong>Copy link</strong></li>
                    <li>Paste the link above, then <strong>revoke sharing</strong> in Drive after this import completes to protect your data</li>
                  </ol>
                </div>
                <button
                  type="button"
                  onClick={() => { setJobMethod('upload'); setJobUrl('') }}
                  className="mt-2 text-xs text-slate-500 dark:text-zinc-400 hover:text-red-600 dark:hover:text-red-400 underline underline-offset-2"
                >
                  Upload a file directly instead →
                </button>
              </div>
            ) : (
              <div>
                <label className="block text-sm font-medium text-slate-700 dark:text-zinc-300 mb-1">Archive file</label>
                <input
                  type="file"
                  accept=".zip,.tgz,.tar.gz,.tar"
                  required
                  onChange={e => setJobFile(e.target.files?.[0] ?? null)}
                  className="w-full text-sm text-slate-600 dark:text-zinc-300 file:mr-3 file:py-1.5 file:px-3 file:rounded-md file:border-0 file:text-sm file:font-medium file:bg-red-50 file:text-red-700 dark:file:bg-red-900/30 dark:file:text-red-400 hover:file:bg-red-100 dark:hover:file:bg-red-900/50 cursor-pointer"
                />
                <p className="mt-1 text-xs text-slate-400 dark:text-zinc-500">
                  The file will be uploaded directly to the server. Large archives may take several minutes on a LAN connection.
                </p>
                {jobLoading && uploadPct > 0 && (
                  <div className="mt-2">
                    <div className="flex justify-between text-xs text-slate-500 dark:text-zinc-400 mb-1">
                      <span>Uploading…</span>
                      <span>{uploadPct}%</span>
                    </div>
                    <div className="w-full bg-slate-200 dark:bg-zinc-700 rounded-full h-1.5">
                      <div
                        className="bg-red-500 h-1.5 rounded-full transition-all duration-300"
                        style={{ width: `${uploadPct}%` }}
                      />
                    </div>
                  </div>
                )}
                <button
                  type="button"
                  onClick={() => { setJobMethod('url'); setJobFile(null) }}
                  className="mt-2 text-xs text-slate-500 dark:text-zinc-400 hover:text-red-600 dark:hover:text-red-400 underline underline-offset-2"
                >
                  ← Use a Google Drive URL instead
                </button>
              </div>
            )}
            <div>
              <label className="block text-sm font-medium text-slate-700 dark:text-zinc-300 mb-1">Destination server</label>
              <select
                value={jobDest}
                onChange={e => { setJobDest(e.target.value); if (e.target.value) localStorage.setItem('psw:last-dest', e.target.value) }}
                required
                className={INPUT}
              >
                <option value="">Select a destination…</option>
                {destOptions.map(o => <option key={`${o.kind}:${o.id}`} value={`${o.kind}:${o.id}`}>{o.label}</option>)}
              </select>
            </div>
            <div className="border border-slate-200 dark:border-zinc-600 rounded-md p-4 space-y-3 bg-slate-50 dark:bg-zinc-800/30">
              <p className="text-xs font-medium text-slate-700 dark:text-zinc-300">Date filter <span className="font-normal text-slate-500 dark:text-zinc-500">(optional)</span></p>
              <p className="text-xs text-slate-500 dark:text-zinc-400">
                Restrict which photos get uploaded by their <em>taken</em> date. Useful if you've already
                imported recent photos from your phone and only want the older Google archive. Photos with no date are always included.
              </p>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-medium text-slate-600 dark:text-zinc-400 mb-1">Import photos taken after</label>
                  <input type="date" value={jobAfterDate} onChange={e => setJobAfterDate(e.target.value)} max={jobBeforeDate || undefined} className={INPUT} />
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-600 dark:text-zinc-400 mb-1">Import photos taken before</label>
                  <input type="date" value={jobBeforeDate} onChange={e => setJobBeforeDate(e.target.value)} min={jobAfterDate || undefined} className={INPUT} />
                </div>
              </div>
              {(jobAfterDate || jobBeforeDate) && (
                <p className="text-xs text-slate-700 dark:text-zinc-300 bg-slate-100 dark:bg-zinc-700/50 border border-slate-200 dark:border-zinc-600 rounded px-2 py-1">
                  Will import photos
                  {jobAfterDate ? ` from ${jobAfterDate}` : ''}
                  {jobAfterDate && jobBeforeDate ? ' to' : jobBeforeDate ? ' up to' : ''}
                  {jobBeforeDate ? ` ${jobBeforeDate}` : ''}
                  {' '}(inclusive). Photos with no timestamp are always included.
                </p>
              )}
            </div>
            <div className="border border-slate-200 dark:border-zinc-600 rounded-md p-4 bg-slate-50 dark:bg-zinc-800/30">
              <label className="flex items-start gap-3 cursor-pointer">
                <input type="checkbox" checked={jobAutoIngest} onChange={e => setJobAutoIngest(e.target.checked)} className="mt-0.5 rounded accent-red-600" />
                <div>
                  <p className="text-sm font-medium text-slate-800 dark:text-zinc-200">
                    {jobMethod === 'upload' ? 'Automatically process after upload' : 'Automatically process after download'}
                  </p>
                  <p className="text-xs text-slate-500 dark:text-zinc-400 mt-0.5">
                    {jobMethod === 'upload'
                      ? 'When unchecked, the file is uploaded and parked. You can start processing manually from the table below.'
                      : 'When unchecked, the file is downloaded and parked. You can start processing manually from the table below.'}
                  </p>
                </div>
              </label>
            </div>
            {jobError && <p className="text-sm text-red-600 dark:text-red-400">{jobError}</p>}
            <button type="submit" disabled={jobLoading} className={BTN_PRIMARY}>
              {jobLoading
                ? jobMethod === 'upload'
                  ? (uploadPct > 0 && uploadPct < 100 ? `Uploading… ${uploadPct}%` : 'Processing…')
                  : (jobAutoIngest ? 'Starting…' : 'Downloading…')
                : jobMethod === 'upload'
                  ? (jobAutoIngest ? 'Upload & process' : 'Upload only')
                  : (jobAutoIngest ? 'Start import' : 'Download only')}
            </button>
          </form>
        )}

      </section>

      {/* Imports (all sources: Google, iCloud direct + bundle, rollbacks) */}
      <section>
        <h2 className="text-lg font-semibold text-slate-900 dark:text-zinc-100 mb-4">Imports</h2>
        {jobs.length === 0 ? (
          <p className="text-sm text-slate-500 dark:text-zinc-400">No imports yet.</p>
        ) : (
          <div className="bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-slate-50 dark:bg-zinc-800 border-b border-slate-200 dark:border-zinc-700">
                <tr>
                  {['Source', 'Stage', 'Status', 'Progress', 'Date filter', 'Started', 'Error', 'Action'].map(h => (
                    <th key={h} className="text-left px-4 py-3 font-medium text-slate-600 dark:text-zinc-400">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 dark:divide-zinc-700">
                {jobs.map(job => {
                  const isParked = job.stage === 'fetch' && job.status === 'succeeded' && !job.auto_ingest
                  const canResume = isParked || job.status === 'failed'
                  const canRerun = job.date_filter !== null && job.stage === 'load' && job.status === 'succeeded'
                  const canRollback = job.stage === 'load' && job.status === 'succeeded'
                  return (
                    <Fragment key={job.job_id}>
                      <tr className="odd:bg-white dark:odd:bg-zinc-900 even:bg-slate-50 dark:even:bg-zinc-800/40">
                        <td className="px-4 py-3">
                          <div className="flex flex-col gap-0.5">
                            <Badge
                              text={SOURCE_LABELS[job.source ?? 'google_takeout'] ?? 'Google'}
                              color={SOURCE_COLORS[job.source ?? 'google_takeout'] ?? SOURCE_COLORS.google_takeout}
                            />
                            <span className="text-xs text-slate-400 dark:text-zinc-500 pl-0.5">→ {destinationLabel(job)}</span>
                          </div>
                        </td>
                        <td className="px-4 py-3">
                          <Badge text={job.stage} color={STAGE_COLORS[job.stage] ?? 'bg-slate-100 text-slate-600 dark:bg-zinc-700 dark:text-zinc-300'} />
                        </td>
                        <td className="px-4 py-3">
                          {isParked ? (
                            <span className="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium bg-teal-100 text-teal-700 dark:bg-teal-900/30 dark:text-teal-400">
                              Ready
                            </span>
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
                        <td className="px-4 py-3 text-slate-600 dark:text-zinc-300 tabular-nums">
                          {stageProgress(job)}
                        </td>
                        <td className="px-4 py-3 text-slate-500 dark:text-zinc-400 text-xs whitespace-nowrap">
                          {job.date_filter
                            ? [
                                job.date_filter.after_date ? `from ${job.date_filter.after_date}` : null,
                                job.date_filter.before_date ? `to ${job.date_filter.before_date}` : null,
                              ].filter(Boolean).join(' ') || '—'
                            : '—'}
                        </td>
                        <td className="px-4 py-3 text-slate-500 dark:text-zinc-400">{new Date(job.created_at).toLocaleString()}</td>
                        <td className="px-4 py-3 text-red-600 dark:text-red-400 max-w-xs">
                          <span className="break-words whitespace-normal line-clamp-4" title={job.error ?? undefined}>{job.error ?? '—'}</span>
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-3">
                            {canResume && (
                              <button onClick={() => resumeJob(job.job_id)} className="text-xs text-red-600 dark:text-red-400 hover:text-red-800 dark:hover:text-red-300 font-medium">
                                {isParked ? 'Start ingest' : 'Retry'}
                              </button>
                            )}
                            {canRerun && (
                              <button
                                onClick={() => rerunJobId === job.job_id ? setRerunJobId(null) : startRerun(job)}
                                className="text-xs text-slate-600 dark:text-zinc-400 hover:text-slate-900 dark:hover:text-zinc-100 font-medium"
                              >
                                {rerunJobId === job.job_id ? 'Cancel' : 'Adjust dates'}
                              </button>
                            )}
                            {canRollback && (
                              <button
                                onClick={() => rollbackJobId === job.job_id ? setRollbackJobId(null) : startRollback(job)}
                                className="text-xs text-rose-600 dark:text-rose-400 hover:text-rose-800 dark:hover:text-rose-300 font-medium"
                              >
                                {rollbackJobId === job.job_id ? 'Cancel' : 'Rollback'}
                              </button>
                            )}
                            <button
                              onClick={() => deleteJob(job)}
                              title="Delete job and staged files"
                              className="text-slate-300 dark:text-zinc-600 hover:text-red-500 dark:hover:text-red-400 text-base leading-none"
                            >
                              ×
                            </button>
                          </div>
                        </td>
                      </tr>
                      {rerunJobId === job.job_id && (
                        <tr className="bg-slate-50 dark:bg-zinc-800/60 border-t border-slate-100 dark:border-zinc-700">
                          <td colSpan={8} className="px-4 py-4">
                            <form onSubmit={submitRerun} className="flex flex-wrap items-end gap-3">
                              <p className="w-full text-xs text-slate-500 dark:text-zinc-400">
                                Adjust the date range and re-run the upload step. Staging files are preserved — no re-download needed.
                              </p>
                              <div>
                                <label className="block text-xs font-medium text-slate-600 dark:text-zinc-400 mb-1">After date</label>
                                <input
                                  type="date"
                                  value={rerunAfter}
                                  onChange={e => setRerunAfter(e.target.value)}
                                  max={rerunBefore || undefined}
                                  className="border border-slate-300 dark:border-zinc-600 rounded-md px-3 py-1.5 text-sm bg-white dark:bg-zinc-800 text-slate-900 dark:text-zinc-100 focus:outline-none focus:ring-2 focus:ring-red-500"
                                />
                              </div>
                              <div>
                                <label className="block text-xs font-medium text-slate-600 dark:text-zinc-400 mb-1">Before date</label>
                                <input
                                  type="date"
                                  value={rerunBefore}
                                  onChange={e => setRerunBefore(e.target.value)}
                                  min={rerunAfter || undefined}
                                  className="border border-slate-300 dark:border-zinc-600 rounded-md px-3 py-1.5 text-sm bg-white dark:bg-zinc-800 text-slate-900 dark:text-zinc-100 focus:outline-none focus:ring-2 focus:ring-red-500"
                                />
                              </div>
                              {(rerunAfter || rerunBefore) && (
                                <label className="flex items-center gap-2 text-xs text-slate-600 dark:text-zinc-400 self-end pb-2 cursor-pointer">
                                  <input
                                    type="checkbox"
                                    checked={rerunUndated}
                                    onChange={e => setRerunUndated(e.target.checked)}
                                    className="rounded border-slate-300 dark:border-zinc-600 text-red-600 focus:ring-red-500"
                                  />
                                  Include items with no date
                                </label>
                              )}
                              <div className="flex items-end gap-2">
                                <button type="submit" disabled={rerunLoading} className={BTN_PRIMARY}>
                                  {rerunLoading ? 'Starting…' : 'Re-run upload'}
                                </button>
                                <button type="button" onClick={() => setRerunJobId(null)} className="text-sm text-slate-500 dark:text-zinc-400 hover:text-slate-800 dark:hover:text-zinc-200 px-2 py-2">
                                  Cancel
                                </button>
                              </div>
                              {rerunError && <p className="w-full text-sm text-red-600 dark:text-red-400">{rerunError}</p>}
                            </form>
                          </td>
                        </tr>
                      )}
                      {rollbackJobId === job.job_id && (
                        <tr className="bg-rose-50 dark:bg-rose-950/20 border-t border-rose-100 dark:border-rose-900/30">
                          <td colSpan={8} className="px-4 py-4">
                            <form onSubmit={submitRollback} className="flex flex-wrap items-end gap-3">
                              <div className="w-full space-y-1">
                                <p className="text-xs font-semibold text-rose-700 dark:text-rose-400">Remove imported assets from {destinationLabel(job)}</p>
                                <p className="text-xs text-slate-500 dark:text-zinc-400">
                                  {job.destination_kind === 'webdav'
                                    ? 'This deletes the files this job uploaded to the WebDAV destination, by path (including album-folder copies).'
                                    : 'This permanently deletes assets from Immich that were uploaded by this job. Assets that were pre-existing duplicates in Immich are not affected.'}
                                </p>
                              </div>
                              <div>
                                <label className="block text-xs font-medium text-slate-600 dark:text-zinc-400 mb-1">Remove photos taken after</label>
                                <input
                                  type="date"
                                  value={rollbackAfter}
                                  onChange={e => setRollbackAfter(e.target.value)}
                                  max={rollbackBefore || undefined}
                                  className="border border-slate-300 dark:border-zinc-600 rounded-md px-3 py-1.5 text-sm bg-white dark:bg-zinc-800 text-slate-900 dark:text-zinc-100 focus:outline-none focus:ring-2 focus:ring-red-500"
                                />
                              </div>
                              <div>
                                <label className="block text-xs font-medium text-slate-600 dark:text-zinc-400 mb-1">Remove photos taken before</label>
                                <input
                                  type="date"
                                  value={rollbackBefore}
                                  onChange={e => setRollbackBefore(e.target.value)}
                                  min={rollbackAfter || undefined}
                                  className="border border-slate-300 dark:border-zinc-600 rounded-md px-3 py-1.5 text-sm bg-white dark:bg-zinc-800 text-slate-900 dark:text-zinc-100 focus:outline-none focus:ring-2 focus:ring-red-500"
                                />
                              </div>
                              <div className="w-full">
                                <label className="flex items-center gap-2 cursor-pointer text-sm text-slate-700 dark:text-zinc-300">
                                  <input
                                    type="checkbox"
                                    checked={rollbackUndated}
                                    onChange={e => setRollbackUndated(e.target.checked)}
                                    className="rounded accent-red-600"
                                  />
                                  Also remove assets that had no date/time
                                </label>
                              </div>
                              <div className="flex items-end gap-2">
                                <button
                                  type="submit"
                                  disabled={rollbackLoading}
                                  className="bg-rose-600 hover:bg-rose-700 text-white px-4 py-2 rounded-md text-sm font-medium disabled:opacity-50"
                                >
                                  {rollbackLoading ? 'Rolling back…' : `Remove from ${destinationLabel(job)}`}
                                </button>
                                <button
                                  type="button"
                                  onClick={() => setRollbackJobId(null)}
                                  className="text-sm text-slate-500 dark:text-zinc-400 hover:text-slate-800 dark:hover:text-zinc-200 px-2 py-2"
                                >
                                  Cancel
                                </button>
                              </div>
                              {rollbackError && <p className="w-full text-sm text-red-600 dark:text-red-400">{rollbackError}</p>}
                            </form>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </Layout>
  )
}
