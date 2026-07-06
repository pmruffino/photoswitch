export interface User {
  id: string
  username: string
  email: string | null
  role: 'admin' | 'user'
  is_active: boolean
  is_approved: boolean
  created_at: string
  message?: string
}

export interface ImmichCred {
  id: string
  server_url: string
  label: string | null
  created_at: string
}

export interface WebDavDest {
  id: string
  base_url: string
  username: string
  base_path: string
  service: 'nextcloud' | 'owncloud' | 'photoprism' | 'other'
  label: string | null
  created_at: string
}

// A destination for selection UIs — unifies Immich + WebDAV by (kind, id).
export interface DestOption {
  kind: 'immich' | 'webdav'
  id: string
  label: string
}

export function webdavServiceLabel(service: string): string {
  return { nextcloud: 'Nextcloud', owncloud: 'ownCloud', photoprism: 'PhotoPrism' }[service] ?? 'WebDAV-Other'
}

export function toDestOptions(immich: ImmichCred[], webdav: WebDavDest[]): DestOption[] {
  return [
    ...immich.map(c => ({ kind: 'immich' as const, id: c.id, label: `${c.label ?? c.server_url} (Immich)` })),
    ...webdav.map(d => ({ kind: 'webdav' as const, id: d.id, label: `${d.label ?? d.base_url} (${webdavServiceLabel(d.service)})` })),
  ]
}

export interface DateFilter {
  after_date: string | null
  before_date: string | null
  include_undated: boolean
}

export interface ICloudConnection {
  id: string
  apple_id: string
  label: string | null
  status: 'pending_2fa' | '2fa_required' | 'active' | 'needs_reauth'
  sync_enabled: boolean
  sync_interval_minutes: number
  sync_credential_id: string | null
  sync_credential_kind?: 'immich' | 'webdav'
  sync_last_run_at: string | null
  anchor_job_id: string | null
  has_watermark: boolean
  created_at: string
}

export interface Job {
  job_id: string
  stage: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  takeout_url: string
  processed_items: number
  total_items: number | null
  error: string | null
  date_filter: DateFilter | null
  auto_ingest: boolean
  source?: 'google_takeout' | 'icloud_bundle' | 'icloud_direct'
  destination_kind?: 'immich' | 'webdav'
  is_sync_anchor?: boolean
  created_at: string
  updated_at?: string
}

export interface AdminJob extends Job {
  username: string
}

export interface Config {
  signup_policy: 'open' | 'approval' | 'closed'
  worker_pct: { fetch: number; unpack: number; map: number; load: number; rollback: number }
  worker_counts: { fetch: number; unpack: number; map: number; load: number; rollback: number }
  max_takeout_gb: number
  staging_retention_days: number
  cleanup_hour: number
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    credentials: 'include',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  if (res.status === 204) return undefined as unknown as T
  const data = await res.json().catch(() => ({ detail: res.statusText }))
  if (!res.ok) throw new Error(data.detail ?? `HTTP ${res.status}`)
  return data as T
}

const CHUNK_SIZE = 50 * 1024 * 1024  // 50 MB per chunk

export async function uploadJobChunked(
  file: File,
  params: { credential_id: string; destination_kind?: string; auto_ingest: boolean; after_date?: string; before_date?: string; source?: string },
  onProgress: (pct: number) => void,
): Promise<Job> {
  const initRes = await fetch('/api/jobs/upload/session', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filename: file.name, ...params }),
  })
  if (!initRes.ok) {
    const err = await initRes.json().catch(() => ({})) as { detail?: string }
    throw new Error(err.detail ?? `Failed to start upload (${initRes.status})`)
  }
  const { session_id } = await initRes.json() as { session_id: string }

  const totalChunks = Math.max(1, Math.ceil(file.size / CHUNK_SIZE))

  for (let i = 0; i < totalChunks; i++) {
    const chunk = file.slice(i * CHUNK_SIZE, Math.min((i + 1) * CHUNK_SIZE, file.size))
    const res = await fetch(`/api/jobs/upload/session/${session_id}`, {
      method: 'PUT',
      credentials: 'include',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: chunk,
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({})) as { detail?: string }
      throw new Error(err.detail ?? `Upload failed at chunk ${i + 1} (${res.status})`)
    }
    onProgress(Math.round(((i + 1) / totalChunks) * 100))
  }

  const doneRes = await fetch(`/api/jobs/upload/session/${session_id}/complete`, {
    method: 'POST',
    credentials: 'include',
  })
  if (!doneRes.ok) {
    const err = await doneRes.json().catch(() => ({})) as { detail?: string }
    throw new Error(err.detail ?? `Failed to complete upload (${doneRes.status})`)
  }
  return doneRes.json() as Promise<Job>
}

export const api = {
  auth: {
    login: (username: string, password: string, rememberMe?: boolean) =>
      req<User>('POST', '/auth/login', { username, password, remember_me: rememberMe ?? false }),
    register: (data: { username: string; password: string; email?: string }) =>
      req<User>('POST', '/auth/register', data),
    logout: () => req<void>('POST', '/auth/logout'),
    me: () => req<User>('GET', '/auth/me'),
    signupPolicy: () => req<{ signup_policy: 'open' | 'approval' | 'closed' }>('GET', '/auth/signup-policy'),
  },
  admin: {
    listUsers: () => req<User[]>('GET', '/admin/users'),
    createUser: (data: { username: string; password: string; email?: string; role?: string }) =>
      req<User>('POST', '/admin/users', data),
    updateUser: (id: string, data: { role?: string; is_active?: boolean; is_approved?: boolean; email?: string; new_password?: string }) =>
      req<User>('PATCH', `/admin/users/${id}`, data),
    deleteUser: (id: string) => req<void>('DELETE', `/admin/users/${id}`),
    getConfig: () => req<Config>('GET', '/admin/config'),
    updateConfig: (data: { signup_policy?: string; worker_pct?: Partial<Config['worker_pct']>; max_takeout_gb?: number; staging_retention_days?: number; cleanup_hour?: number }) => req<{ status: string }>('PATCH', '/admin/config', data),
    listJobs: () => req<AdminJob[]>('GET', '/admin/jobs'),
    deleteJob: (id: string) => req<void>('DELETE', `/admin/jobs/${id}`),
  },
  user: {
    updateProfile: (data: { email?: string; current_password?: string; new_password?: string }) =>
      req<{ id: string; username: string; email: string | null }>('PATCH', '/user/profile', data),
    deleteAccount: () => req<void>('DELETE', '/user/profile'),
    listImmich: () => req<ImmichCred[]>('GET', '/user/immich'),
    addImmich: (data: { server_url: string; api_key: string; label?: string }) =>
      req<ImmichCred>('POST', '/user/immich', data),
    updateImmich: (id: string, data: { server_url?: string; api_key?: string; label?: string }) =>
      req<ImmichCred>('PATCH', `/user/immich/${id}`, data),
    deleteImmich: (id: string) => req<void>('DELETE', `/user/immich/${id}`),
    testImmich: (id: string) => req<{ ok: boolean; user?: string }>('POST', `/user/immich/${id}/test`),
    listWebdav: () => req<WebDavDest[]>('GET', '/user/webdav'),
    addWebdav: (data: { base_url: string; username: string; password: string; base_path?: string; service?: string; label?: string }) =>
      req<WebDavDest>('POST', '/user/webdav', data),
    updateWebdav: (id: string, data: { base_url?: string; username?: string; password?: string; base_path?: string; service?: string; label?: string }) =>
      req<WebDavDest>('PATCH', `/user/webdav/${id}`, data),
    deleteWebdav: (id: string) => req<void>('DELETE', `/user/webdav/${id}`),
    testWebdav: (id: string) => req<{ ok: boolean; user?: string }>('POST', `/user/webdav/${id}/test`),
  },
  icloud: {
    listConnections: () => req<ICloudConnection[]>('GET', '/icloud/connections'),
    createConnection: (data: { apple_id: string; password: string; label?: string }) =>
      req<ICloudConnection>('POST', '/icloud/connections', data),
    restartConnection: (id: string, data: { apple_id: string; password: string; label?: string }) =>
      req<ICloudConnection>('POST', `/icloud/connections/${id}/restart`, data),
    verifyConnection: (id: string, code: string) =>
      req<ICloudConnection>('POST', `/icloud/connections/${id}/verify`, { code }),
    sendSmsCode: (id: string) =>
      req<{ sent: boolean; phone: string | null }>('POST', `/icloud/connections/${id}/send-sms`),
    testConnection: (id: string) =>
      req<{ ok: boolean; user?: string }>('POST', `/icloud/connections/${id}/test`),
    deleteConnection: (id: string) => req<void>('DELETE', `/icloud/connections/${id}`),
    configureSync: (id: string, data: { enabled: boolean; interval_minutes: number; credential_id?: string; destination_kind?: string }) =>
      req<ICloudConnection>('PUT', `/icloud/connections/${id}/sync`, data),
    importNow: (id: string, data: { credential_id: string; destination_kind?: string; as_sync_anchor?: boolean; after_date?: string; before_date?: string }) =>
      req<{ job_id: string; connection_id: string }>('POST', `/icloud/connections/${id}/import`, data),
    syncNow: (id: string) =>
      req<{ job_id: string; connection_id: string }>('POST', `/icloud/connections/${id}/sync-now`),
  },
  jobs: {
    list: () => req<Job[]>('GET', '/jobs/'),
    create: (data: { takeout_url: string; credential_id: string; destination_kind?: string; auto_ingest?: boolean; after_date?: string; before_date?: string }) =>
      req<Job>('POST', '/jobs/', data),
    get: (id: string) => req<Job>('GET', `/jobs/${id}`),
    resume: (id: string) => req<Job>('POST', `/jobs/${id}/resume`),
    rerun: (id: string, data: { after_date?: string; before_date?: string; include_undated: boolean }) =>
      req<Job>('POST', `/jobs/${id}/rerun`, data),
    rollback: (id: string, data: { after_date?: string; before_date?: string; include_undated: boolean }) =>
      req<Job>('POST', `/jobs/${id}/rollback`, data),
    delete: (id: string) => req<void>('DELETE', `/jobs/${id}`),
  },
}
