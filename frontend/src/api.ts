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

export interface DateFilter {
  after_date: string | null
  before_date: string | null
  include_undated: boolean
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
  params: { credential_id: string; auto_ingest: boolean; after_date?: string; before_date?: string },
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
  },
  jobs: {
    list: () => req<Job[]>('GET', '/jobs/'),
    create: (data: { takeout_url: string; credential_id: string; auto_ingest?: boolean; after_date?: string; before_date?: string }) =>
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
