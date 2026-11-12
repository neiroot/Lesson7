import { useEffect, useRef, useState } from 'react'
import { AudioLines, CirclePlus, LoaderCircle, Mic, Trash2, X } from 'lucide-react'
import { active, localDate, message, request, submit, upload, type Draft, type Health, type Job, type Profile } from './api'
import { Consent, Notice } from './Review'

const draftKey = 'meeting-voice-drafts'
const jobKey = 'meeting-voice-job'
function storedDrafts(): string[] {
  try { return JSON.parse(localStorage.getItem(draftKey) ?? '[]') as string[] } catch { return [] }
}

export function Profiles({ profiles, health, refresh, close }: {
  profiles: Profile[]; health: Health | null; refresh: () => Promise<void>; close: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [ids, setIds] = useState(storedDrafts)
  const [drafts, setDrafts] = useState<Draft[]>([])
  const [jobId, setJobId] = useState(() => localStorage.getItem(jobKey))
  const [job, setJob] = useState<Job | null>(null)
  const [mode, setMode] = useState<string | null>(() => storedDrafts().length || localStorage.getItem(jobKey) ? '' : null)
  const [name, setName] = useState('')
  const [chosen, setChosen] = useState<string[]>([])
  const [confirmed, setConfirmed] = useState(false)
  const [consent, setConsent] = useState(false)
  const [heard, setHeard] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<Profile | null>(null)
  const refreshRef = useRef(refresh)
  useEffect(() => { refreshRef.current = refresh }, [refresh])
  useEffect(() => {
    const element = dialog.current
    element?.showModal()
    return () => element?.close()
  }, [])
  useEffect(() => { localStorage.setItem(draftKey, JSON.stringify(ids)) }, [ids])
  useEffect(() => {
    if (jobId) localStorage.setItem(jobKey, jobId)
    else localStorage.removeItem(jobKey)
  }, [jobId])
  useEffect(() => {
    if (!ids.length && !jobId) return
    let stale = false
    let timer: number | undefined
    async function tick() {
      try {
        const [items, progress] = await Promise.all([
          Promise.all(ids.map(id => request<Draft>(`/voice-sample-drafts/${id}`).catch(() => null))),
          jobId ? request<Job>(`/jobs/${jobId}`) : Promise.resolve(null),
        ])
        if (stale) return
        const updated = items.filter((item): item is Draft => Boolean(item))
        setDrafts(updated)
        setJob(progress)
        if (progress?.state === 'succeeded') {
          setIds(updated.filter(item => item.status !== 'expired').map(item => item.id))
          setChosen([]); setJobId(null); setMode(null); setJob(null)
          await refreshRef.current()
          return
        }
        if (progress?.state === 'failed') {
          setError(`${progress.error?.message ?? 'Ошибка сохранения'}. Профиль не изменился.`)
          setJobId(null)
        }
        if (active(progress?.state) || updated.some(item => item.status === 'checking')) {
          timer = window.setTimeout(() => void tick(), 2000)
        }
      } catch (issue) {
        if (!stale) { setError(message(issue)); timer = window.setTimeout(() => void tick(), 2000) }
      }
    }
    void tick()
    return () => { stale = true; window.clearTimeout(timer) }
  }, [ids, jobId])
  const pending = busy || Boolean(jobId && (!job || active(job.state)))
  const selection = chosen.filter(id => drafts.some(item => item.id === id && item.status === 'eligible'))
  const target = mode ? profiles.find(item => item.id === mode)?.name : name.trim()
  function changeTarget(id: string) {
    setMode(id); setConfirmed(false); setConsent(false)
  }
  async function addFiles(files: FileList | null) {
    if (!files?.length) return
    setBusy(true); setError('')
    try {
      for (const file of files) {
        const response = await upload<{ draft_id: string }>('/voice-sample-drafts', file)
        setIds(current => [...current, response.draft_id])
      }
    } catch (issue) { setError(message(issue)) }
    finally { setBusy(false) }
  }
  async function discard(id: string) {
    try {
      await request(`/voice-sample-drafts/${id}`, { method: 'DELETE' })
      setIds(current => current.filter(item => item !== id))
      setDrafts(current => current.filter(item => item.id !== id))
      setChosen(current => current.filter(item => item !== id))
      setConsent(false)
    } catch (issue) { setError(message(issue)) }
  }
  async function enroll() {
    setBusy(true); setError('')
    try {
      const response = await submit<{ job_id: string }>(mode ? `/voice-profiles/${mode}/samples` : '/voice-profiles', {
        ...(mode ? {} : { name: name.trim() }), confirm_name: confirmed, draft_ids: selection,
        consent_confirmed: consent, consent_text_version: health?.consent_text_version,
      }, true)
      setJob(null); setJobId(response.job_id)
    } catch (issue) { setError(message(issue)) }
    finally { setBusy(false) }
  }
  async function removeProfile(id: string) {
    setError('')
    try {
      await request(`/voice-profiles/${id}`, { method: 'DELETE' })
      setDeleteTarget(null)
      if (mode === id) changeTarget('')
      await refresh()
    } catch (issue) { setError(message(issue)) }
  }
  return <dialog ref={dialog} className="profiles-dialog" onCancel={event => { event.preventDefault(); close() }}>
    <div className="modal-heading"><div className="round-icon"><Mic size={21} /></div>
      <div><h2>Голосовые профили</h2><p>Помогают узнавать участников в будущих встречах.</p></div>
      <button className="icon-button" aria-label="Закрыть профили" onClick={close}><X size={20} /></button></div>
    <div className="modal-body">
      {error && <Notice text={error} />}
      {profiles.length ? <div className="profile-list">{profiles.map(profile => <div className="profile-row" key={profile.id}>
        <span className="avatar"><Mic size={16} /></span><div><strong>{profile.name}</strong>
          <small>Образцов — {profile.sample_count} · {localDate(profile.created_at)} · {profile.id.slice(0, 6)}</small></div>
        <button className="button subtle" disabled={pending} onClick={() => changeTarget(profile.id)}>Добавить образцы</button>
        <button className="icon-button danger" title="Удалить профиль" aria-label={`Удалить профиль ${profile.name}`}
          onClick={() => setDeleteTarget(profile)}><Trash2 size={16} /></button>
      </div>)}</div> : <p className="muted">Голосовых профилей пока нет.</p>}
      {deleteTarget && <div className="confirm-box">
        <strong>Удалить профиль «{deleteTarget.name}»?</strong>
        <p>Голосовые отпечатки и записи согласия будут удалены. Имя в старых расшифровках сохранится.</p>
        <button className="button danger" onClick={() => void removeProfile(deleteTarget.id)}>Удалить профиль</button>{' '}
        <button className="button subtle" onClick={() => setDeleteTarget(null)}>Отмена</button>
      </div>}
      {mode === null ? <button className="button primary" onClick={() => changeTarget('')}>
        <CirclePlus size={17} /> Новый профиль</button> : <form className="profile-form"
        onSubmit={event => { event.preventDefault(); void enroll() }}>
        <h3>{mode ? `Добавить образцы · ${target ?? 'Профиль удалён'}` : 'Новый голосовой профиль'}</h3>
        {pending && <p className="activity"><LoaderCircle size={16} className="spin" /> Выполняется локальная операция</p>}
        <fieldset disabled={pending}>
          {!mode && <label>Имя участника<input value={name} maxLength={100} placeholder="Например, Анна"
            onChange={event => { setName(event.target.value); setConfirmed(false); setConsent(false) }} /></label>}
          <label>Добавить аудиофайлы<input type="file" multiple accept=".wav,.mp3,.m4a"
            onChange={event => { void addFiles(event.target.files); event.target.value = '' }} />
            <small>WAV, MP3 или M4A · желательно 10–30 секунд речи одного человека</small></label>
          {drafts.filter(item => ids.includes(item.id)).map(draft => <div className="draft" key={draft.id}>
            <div className="draft-title"><AudioLines size={17} /><strong>{{
              checking: 'Проверяем образец', eligible: 'Образец пригоден', rejected: 'Образец отклонён', expired: 'Образец недоступен',
            }[draft.status]}</strong><button type="button" className="icon-button" aria-label="Убрать образец"
              onClick={() => void discard(draft.id)}><X size={16} /></button></div>
            {draft.reasons.length > 0 && <p className="small muted">{draft.reasons.join('. ')}</p>}
            {draft.audio_available && <audio controls preload="none" src={`/api/voice-sample-drafts/${draft.id}/audio`} />}
            {draft.status === 'eligible' && <label className="check">
              <input type="checkbox" checked={chosen.includes(draft.id)}
                onChange={event => { setChosen(event.target.checked ? [...chosen, draft.id] : chosen.filter(id => id !== draft.id))
                  setHeard(false); setConsent(false) }} /> Использовать этот образец</label>}
          </div>)}
          {target && <label className="check"><input type="checkbox" checked={confirmed}
            onChange={event => setConfirmed(event.target.checked)} />Подтверждаю имя «{target}»</label>}
          {selection.length > 0 && <>
            <label className="check"><input type="checkbox" checked={heard} onChange={event => setHeard(event.target.checked)} />
              Я прослушал выбранные образцы. Слышен только нужный голос, без сильного шума и музыки.</label>
            {health && <Consent text={health.consent_text} checked={consent} setChecked={setConsent} />}
          </>}
          <div className="form-actions"><button className="button primary"
            disabled={!health || !target || !confirmed || !consent || !heard || !selection.length}>Сохранить голос</button>
            <button className="button subtle" type="button" onClick={() => setMode(null)}>Свернуть</button></div>
        </fieldset>
      </form>}
    </div>
  </dialog>
}
