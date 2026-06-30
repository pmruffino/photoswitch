import { FormEvent, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import Layout from '../components/Layout'
import { api } from '../api'
import { useAuth } from '../contexts/auth'

const INPUT = 'w-full border border-slate-300 dark:border-zinc-600 rounded-md px-3 py-2 text-sm bg-white dark:bg-zinc-800 text-slate-900 dark:text-zinc-100 placeholder-slate-400 dark:placeholder-zinc-500 focus:outline-none focus:ring-2 focus:ring-red-500'
const BTN_PRIMARY = 'bg-red-600 hover:bg-red-700 dark:bg-red-700 dark:hover:bg-red-600 text-white px-4 py-2 rounded-md text-sm font-medium disabled:opacity-50'

function StatusMsg({ msg }: { msg: string }) {
  if (!msg) return null
  const isError = /fail|error|incorrect|match|least/i.test(msg)
  return (
    <p className={`text-sm ${isError ? 'text-red-600 dark:text-red-400' : 'text-green-600 dark:text-green-400'}`}>
      {msg}
    </p>
  )
}

export default function Profile() {
  const { user, setUser } = useAuth()
  const navigate = useNavigate()

  const [email, setEmail] = useState(user?.email ?? '')
  const [emailMsg, setEmailMsg] = useState('')
  const [emailSaving, setEmailSaving] = useState(false)

  const [currentPw, setCurrentPw] = useState('')
  const [newPw, setNewPw] = useState('')
  const [confirmPw, setConfirmPw] = useState('')
  const [pwMsg, setPwMsg] = useState('')
  const [pwSaving, setPwSaving] = useState(false)

  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false)
  const [deleteLoading, setDeleteLoading] = useState(false)
  const [deleteError, setDeleteError] = useState('')

  useEffect(() => { setEmail(user?.email ?? '') }, [user])

  async function saveEmail(e: FormEvent) {
    e.preventDefault()
    setEmailMsg('')
    setEmailSaving(true)
    try {
      await api.user.updateProfile({ email })
      if (user) setUser({ ...user, email: email || null })
      setEmailMsg('Email updated.')
    } catch (err) {
      setEmailMsg(err instanceof Error ? err.message : 'Failed to update email')
    } finally {
      setEmailSaving(false)
    }
  }

  async function deleteAccount() {
    setDeleteError('')
    setDeleteLoading(true)
    try {
      await api.user.deleteAccount()
      setUser(null)
      navigate('/login', { replace: true })
    } catch (err) {
      setDeleteError(err instanceof Error ? err.message : 'Failed to delete account')
      setDeleteLoading(false)
    }
  }

  async function savePassword(e: FormEvent) {
    e.preventDefault()
    setPwMsg('')
    if (newPw !== confirmPw) { setPwMsg('Passwords do not match'); return }
    setPwSaving(true)
    try {
      await api.user.updateProfile({ current_password: currentPw, new_password: newPw })
      setPwMsg('Password updated.')
      setCurrentPw(''); setNewPw(''); setConfirmPw('')
    } catch (err) {
      setPwMsg(err instanceof Error ? err.message : 'Failed to update password')
    } finally {
      setPwSaving(false)
    }
  }

  return (
    <Layout>
      <h1 className="text-xl font-semibold text-slate-900 dark:text-zinc-100 mb-1">Account</h1>
      <p className="text-sm text-slate-500 dark:text-zinc-400 mb-6">{user?.username}</p>

      <div className="max-w-md space-y-6">
        <div className="bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg p-5">
          <h2 className="text-sm font-semibold text-slate-900 dark:text-zinc-100 mb-4">Email address</h2>
          <form onSubmit={saveEmail} className="space-y-3">
            <input
              type="email"
              value={email}
              onChange={e => setEmail(e.target.value)}
              placeholder="you@example.com"
              className={INPUT}
            />
            <StatusMsg msg={emailMsg} />
            <button type="submit" disabled={emailSaving} className={BTN_PRIMARY}>
              {emailSaving ? 'Saving…' : 'Save email'}
            </button>
          </form>
        </div>

        <div className="bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg p-5">
          <h2 className="text-sm font-semibold text-slate-900 dark:text-zinc-100 mb-4">Change password</h2>
          <form onSubmit={savePassword} className="space-y-3">
            <div>
              <label className="block text-sm font-medium text-slate-700 dark:text-zinc-300 mb-1">Current password</label>
              <input type="password" value={currentPw} onChange={e => setCurrentPw(e.target.value)} required className={INPUT} />
            </div>
            <div>
              <label className="block text-sm font-medium text-slate-700 dark:text-zinc-300 mb-1">New password</label>
              <input type="password" value={newPw} onChange={e => setNewPw(e.target.value)} required minLength={8} className={INPUT} />
            </div>
            <div>
              <label className="block text-sm font-medium text-slate-700 dark:text-zinc-300 mb-1">Confirm new password</label>
              <input type="password" value={confirmPw} onChange={e => setConfirmPw(e.target.value)} required minLength={8} className={INPUT} />
            </div>
            <StatusMsg msg={pwMsg} />
            <button type="submit" disabled={pwSaving} className={BTN_PRIMARY}>
              {pwSaving ? 'Saving…' : 'Change password'}
            </button>
          </form>
        </div>

        <div className="border border-red-200 dark:border-red-900/50 rounded-lg p-5">
          <h2 className="text-sm font-semibold text-red-700 dark:text-red-400 mb-1">Delete account</h2>
          <p className="text-xs text-slate-500 dark:text-zinc-400 mb-4">
            Permanently removes your account, all jobs, all staged files, and all saved Immich connections. This cannot be undone.
          </p>
          {!showDeleteConfirm ? (
            <button
              onClick={() => setShowDeleteConfirm(true)}
              className="text-sm text-red-600 dark:text-red-400 border border-red-300 dark:border-red-700 rounded-md px-4 py-2 hover:bg-red-50 dark:hover:bg-red-900/20"
            >
              Delete my account
            </button>
          ) : (
            <div className="space-y-3">
              <p className="text-sm font-medium text-red-700 dark:text-red-400">Are you sure? This cannot be undone.</p>
              {deleteError && <p className="text-sm text-red-600 dark:text-red-400">{deleteError}</p>}
              <div className="flex gap-3">
                <button
                  onClick={deleteAccount}
                  disabled={deleteLoading}
                  className="text-sm bg-red-600 hover:bg-red-700 text-white rounded-md px-4 py-2 font-medium disabled:opacity-50"
                >
                  {deleteLoading ? 'Deleting…' : 'Yes, delete my account'}
                </button>
                <button
                  onClick={() => { setShowDeleteConfirm(false); setDeleteError('') }}
                  className="text-sm text-slate-600 dark:text-zinc-400 hover:text-slate-900 dark:hover:text-zinc-100 px-2 py-2"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </Layout>
  )
}
