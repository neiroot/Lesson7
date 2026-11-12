import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { createRoot } from 'react-dom/client'
import {
  ArrowRight, AudioLines, CalendarDays, ChevronRight, CircleAlert, Clock3, CloudDownload, FileText,
  Film, FolderOpen, History, Link2, LoaderCircle, LockKeyhole, LogOut, Mic, RefreshCw, ShieldCheck,
  Sparkles, Trash2, Upload, Users, Video,
} from 'lucide-react'
import {
  active, clock, localDate, localDateTime, message, request, submit, upload,
  type CloudImport, type Health, type Meeting, type MeetingRow, type Profile, type Speaker,
  type YandexIntegration,
} from './api'
import { Notice, Review } from './Review'
import { Profiles } from './Profiles'
import './styles.css'

const stages: Record<string, string> = {
  validate: 'Проверяем видео', extract_audio: 'Извлекаем звук', diarize: 'Размечаем голоса',
  identify: 'Сопоставляем голоса', transcribe: 'Распознаём речь',
  queued: 'Ожидаем в очереди', downloading: 'Скачиваем из Яндекс Диска',
  summarize: 'Составляем конспект', finalize: 'Сохраняем результат',
}
const statusText = { queued: 'В очереди', processing: 'Обрабатывается', ready: 'Готово', error: 'Ошибка' }
const importStatus = { queued: 'В очереди', downloading: 'Скачивается', processing: 'Анализируется',
  ready: 'Готово', error: 'Ошибка', duplicate: 'Уже добавлено' }
const selectedKey = 'meeting-selected'
const findingTabs = {
  decisions: { label: 'Принятые решения', empty: 'Решений не зафиксировано' },
  tasks: { label: 'Задачи', empty: 'Задач не зафиксировано' },
  open_questions: { label: 'Открытые вопросы', empty: 'Открытых вопросов нет' },
} as const
type FindingTab = keyof typeof findingTabs
const findingKeys: FindingTab[] = ['decisions', 'tasks', 'open_questions']
const waveform = [8, 13, 19, 28, 23, 39, 33, 50, 42, 74, 63, 95, 76, 53, 69, 47, 58, 40, 24, 36, 21, 29, 17, 11, 9]

function Waveform() {
  return <svg className="hero-wave" viewBox="0 0 325 125" fill="none" aria-hidden="true">
    <path d="M8 62.5H317" stroke="currentColor" strokeOpacity=".25" strokeDasharray="2 5" />
    {waveform.map((height, index) => <line key={index} x1={17 + index * 12} x2={17 + index * 12}
      y1={(125 - height) / 2} y2={(125 + height) / 2} stroke="currentColor" strokeWidth="3"
      strokeLinecap="round" opacity={index < 4 || index > 20 ? '.45' : '.95'} />)}
  </svg>
}

function App() {
  const [meetings, setMeetings] = useState<MeetingRow[]>([])
  const [selected, setSelected] = useState(() => localStorage.getItem(selectedKey))
  const selectedRef = useRef(selected)
  const [meeting, setMeeting] = useState<Meeting | null>(null)
  const [speakers, setSpeakers] = useState<Speaker[]>([])
  const [profiles, setProfiles] = useState<Profile[]>([])
  const [health, setHealth] = useState<Health | null>(null)
  const [integration, setIntegration] = useState<YandexIntegration | null>(null)
  const [cloudImports, setCloudImports] = useState<CloudImport[]>([])
  const [cloudLink, setCloudLink] = useState('')
  const [cloudBusy, setCloudBusy] = useState(false)
  const [cloudError, setCloudError] = useState('')
  const [cloudNotice, setCloudNotice] = useState('')
  const [profilesOpen, setProfilesOpen] = useState(false)
  const [reviewOpen, setReviewOpen] = useState(false)
  const [speaker, setSpeaker] = useState<string | null>(null)
  const [tab, setTab] = useState<'summary' | 'transcript'>('summary')
  const [findingTab, setFindingTab] = useState<FindingTab>('decisions')
  const [highlight, setHighlight] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [dragging, setDragging] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [actionBusy, setActionBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const fileInput = useRef<HTMLInputElement>(null)
  const dragDepth = useRef(0)

  const refresh = useCallback(async () => {
    const selection = selectedRef.current
    const rows = await request<MeetingRow[]>('/meetings')
    setMeetings(rows)
    if (selection !== selectedRef.current) return
    if (selection && !rows.some(item => item.id === selection)) {
      selectedRef.current = null
      setSelected(null)
      setMeeting(null)
      setSpeakers([])
      localStorage.removeItem(selectedKey)
    } else if (selection) {
      const [details, people] = await Promise.all([
        request<Meeting>(`/meetings/${selection}`), request<Speaker[]>(`/meetings/${selection}/speakers`),
      ])
      if (selection !== selectedRef.current) return
      setMeeting(details); setSpeakers(people)
    }
  }, [])

  const refreshProfiles = useCallback(async () => setProfiles(await request<Profile[]>('/voice-profiles')), [])
  const refreshHealth = useCallback(async () => setHealth(await request<Health>('/health')), [])
  const refreshCloud = useCallback(async () => {
    const [status, imports] = await Promise.all([
      request<YandexIntegration>('/integrations/yandex'), request<CloudImport[]>('/cloud-imports'),
    ])
    setIntegration(status); setCloudImports(imports)
  }, [])
  useEffect(() => {
    void refreshProfiles().catch(issue => setError(message(issue)))
    void refreshHealth().catch(() => undefined)
    void refreshCloud().catch(issue => setCloudError(message(issue)))
    const oauth = new URLSearchParams(window.location.search).get('yandex')
    if (oauth === 'denied') setCloudError('Доступ к Яндекс Диску не был предоставлен.')
    if (oauth === 'error') setCloudError('Не удалось завершить OAuth-подключение.')
    if (oauth) window.history.replaceState({}, '', '/')
  }, [refreshProfiles, refreshHealth, refreshCloud])
  useEffect(() => {
    void refresh().catch(issue => setError(message(issue))).finally(() => setLoading(false))
  }, [selected, refresh])
  const processing = meetings.some(item => active(item.status))
  const savingVoice = speakers.some(item => active(item.job?.state))
  const polling = processing || active(meeting?.summary_status) || savingVoice || Boolean(integration?.connected)
  useEffect(() => {
    if (!polling) return
    let current = true
    let timer: number
    async function tick() {
      try {
        await Promise.all([refresh(), refreshCloud(), ...(savingVoice ? [refreshProfiles()] : [])])
      } catch (issue) { if (current) setError(message(issue)) }
      if (current) timer = window.setTimeout(() => void tick(), processing ? 2000 : 5000)
    }
    timer = window.setTimeout(() => void tick(), processing ? 2000 : 5000)
    return () => { current = false; window.clearTimeout(timer) }
  }, [polling, processing, refresh, refreshCloud, refreshProfiles, savingVoice])
  useEffect(() => {
    if (tab === 'transcript' && highlight) {
      document.getElementById(`segment-${highlight}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }
  }, [tab, highlight])

  function chooseMeeting(id: string) {
    if (selectedRef.current === id) return
    selectedRef.current = id
    localStorage.setItem(selectedKey, id)
    setSelected(id); setMeeting(null); setSpeakers([]); setTab('summary'); setFindingTab('decisions'); setHighlight(null)
    setReviewOpen(false); setSpeaker(null); setDeleting(false); setError('')
  }
  async function add(file?: File) {
    if (!file || uploading || processing) return
    setError('')
    if (!/\.(mp4|webm)$/i.test(file.name)) { setError('Выберите видео MP4 или WebM.'); return }
    setUploading(true)
    try {
      const result = await upload<{ meeting_id: string }>('/meetings', file)
      chooseMeeting(result.meeting_id)
      await refresh()
    } catch (issue) { setError(message(issue)) }
    finally { setUploading(false) }
  }
  async function meetingAction(action: 'retry' | 'regenerate' | 'delete') {
    if (!meeting) return
    setActionBusy(true); setError('')
    try {
      if (action === 'delete') await request(`/meetings/${meeting.id}`, { method: 'DELETE' })
      else {
        const query = action === 'regenerate' ? '?regenerate=true' : ''
        await submit(`/meetings/${meeting.id}/summary/retry${query}`, {})
      }
      setDeleting(false)
      await refresh()
    } catch (issue) { setError(message(issue)) }
    finally { setActionBusy(false) }
  }
  async function cloudAction(action: 'connect' | 'check' | 'disconnect') {
    setCloudBusy(true); setCloudError(''); setCloudNotice('')
    try {
      if (action === 'connect') {
        const result = await request<{ authorize_url: string }>('/integrations/yandex/oauth/start', { method: 'POST' })
        window.location.assign(result.authorize_url)
        return
      }
      if (action === 'disconnect') await request('/integrations/yandex', { method: 'DELETE' })
      else await request('/integrations/yandex/check', { method: 'POST' })
      await Promise.all([refreshCloud(), refresh()])
    } catch (issue) { setCloudError(message(issue)) }
    finally { setCloudBusy(false) }
  }
  async function importFromLink(event: FormEvent) {
    event.preventDefault()
    if (!cloudLink.trim() || cloudBusy) return
    setCloudBusy(true); setCloudError(''); setCloudNotice('')
    try {
      const result = await submit<{ meeting_id: string; duplicate: boolean }>('/cloud-imports/yandex-link', { url: cloudLink })
      if (result.duplicate) setCloudNotice('Эта запись уже добавлена. Открыта существующая встреча — повторный анализ не запускался.')
      chooseMeeting(result.meeting_id)
      setCloudLink('')
      await Promise.all([refreshCloud(), refresh()])
    } catch (issue) { setCloudError(message(issue)) }
    finally { setCloudBusy(false) }
  }
  async function retryImport(item: CloudImport) {
    setCloudBusy(true); setCloudError('')
    try {
      await submit(`/cloud-imports/${item.id}/retry`, {})
      if (item.meeting_id) chooseMeeting(item.meeting_id)
      await Promise.all([refreshCloud(), refresh()])
    } catch (issue) { setCloudError(message(issue)) }
    finally { setCloudBusy(false) }
  }
  async function refreshReview() { await Promise.all([refresh(), refreshProfiles()]) }
  const notes = tab === 'summary' ? meeting?.summary : null
  const findings = notes?.[findingTab] ?? []
  const setupMissing = health && (!health.storage || !health.ffmpeg || !health.weights || !health.runtimes ||
    !health.ollama || !health.ollama_model)
  const missing = health ? [
    !health.storage && 'хранилище', !health.ffmpeg && 'FFmpeg',
    (!health.weights || !health.runtimes) && 'речевые модели', (!health.ollama || !health.ollama_model) && 'Ollama',
  ].filter(Boolean).join(', ') : ''
  const folderDuplicate = cloudImports.find(item => item.status === 'duplicate')
  const duplicateNotice = cloudNotice || (folderDuplicate
    ? `«${folderDuplicate.filename}» уже есть в истории. Повторная обработка не запускалась.` : '')
  return <>
    <header className="topbar"><div className="topbar-content">
      <a href="/" className="brand"><span className="brand-icon"><AudioLines size={22} strokeWidth={2.2} /></span>
        <span>Анализатор встреч<small>ЛОКАЛЬНАЯ РАСШИФРОВКА</small></span></a>
      <div className="top-actions"><span className="local-pill"><span />Локальный режим</span>
        <button className="button outline" onClick={() => { setProfilesOpen(true); void refreshProfiles().catch(
          issue => setError(message(issue))) }}><Mic size={16} /> Голосовые профили</button></div>
    </div></header>
    <main className="workspace">
      <div className="intro"><div className="eyebrow"><span /> РАБОЧЕЕ ПРОСТРАНСТВО</div>
        <h1>Видеовстречи</h1><p>Расшифровка по спикерам, решения и задачи из ваших записей.</p></div>
      <section className={`upload-zone${dragging ? ' dragging' : ''}`}
        onDragOver={event => event.preventDefault()}
        onDragEnter={event => { event.preventDefault(); dragDepth.current++; setDragging(true) }}
        onDragLeave={event => { event.preventDefault(); if (--dragDepth.current <= 0) setDragging(false) }}
        onDrop={event => { event.preventDefault(); setDragging(false); dragDepth.current = 0; void add(event.dataTransfer.files[0]) }}>
        <Waveform />
        <div className="upload-icon"><Upload size={25} strokeWidth={1.7} /></div>
        <div className="upload-description"><h2>Добавить видеовстречу</h2>
          <p>Перетащите запись сюда или выберите файл.</p>
          <div className="specs"><span>MP4 / WebM</span><i /><span>Любая длительность</span></div></div>
        <input type="file" hidden accept=".mp4,.webm" ref={fileInput}
          onChange={event => { void add(event.target.files?.[0]); event.target.value = '' }} />
        <button className="button primary choose-file" disabled={uploading || processing}
          onClick={() => fileInput.current?.click()}>
          {uploading ? <LoaderCircle className="spin" size={17} /> : <FolderOpen size={17} />}
          {uploading ? 'Загружаем' : processing ? 'Идёт обработка' : 'Выбрать файл'}
        </button>
      </section>
      <section className="cloud-panel" aria-label="Импорт из Яндекс Диска">
        <div className="cloud-column"><div className="cloud-heading">
          <span className="cloud-icon"><Link2 size={19} /></span><div><h2>Импорт по ссылке</h2>
            <p>Публичный файл MP4 или WebM на Яндекс Диске.</p></div></div>
          <form className="cloud-link-form" onSubmit={event => void importFromLink(event)}>
            <input type="url" required placeholder="https://disk.yandex.ru/d/…" value={cloudLink}
              onChange={event => setCloudLink(event.target.value)} aria-label="Публичная ссылка Яндекс Диска" />
            <button className="button primary" disabled={cloudBusy || !cloudLink.trim()}>
              {cloudBusy ? <LoaderCircle className="spin" size={16} /> : <CloudDownload size={16} />} Импортировать
            </button>
          </form>
        </div>
        <div className="cloud-column"><div className="cloud-heading">
          <span className="cloud-icon"><CloudDownload size={19} /></span><div><h2>Автоимпорт из папки</h2>
            <p className="mono">{integration?.folder ?? 'disk:/Видеовстречи'}</p></div></div>
          <div className="cloud-connection">
            <span className={`connection-state ${integration?.connected ? 'connected' : ''}`}>
              <i />{integration?.connected ? 'Диск подключён' : 'Диск не подключён'}</span>
            <div className="cloud-actions">
              {integration?.connected ? <>
                <button className="button outline" disabled={cloudBusy} onClick={() => void cloudAction('check')}>
                  <RefreshCw size={15} /> Проверить</button>
                <button className="icon-button" title="Отключить Яндекс Диск" aria-label="Отключить Яндекс Диск"
                  disabled={cloudBusy} onClick={() => void cloudAction('disconnect')}><LogOut size={16} /></button>
              </> : <button className="button outline" disabled={cloudBusy || integration === null || !integration.configured}
                onClick={() => void cloudAction('connect')}><CloudDownload size={16} /> Подключить</button>}
            </div>
          </div>
          {!integration?.configured && integration !== null && <p className="cloud-hint">Добавьте YANDEX_CLIENT_ID в .env.</p>}
          {integration?.last_check_at && <p className="cloud-hint">Последняя проверка: {localDateTime(integration.last_check_at)}
            {' '}· каждые {integration.poll_seconds} с</p>}
        </div>
        {(cloudImports.length > 0) && <div className="cloud-imports">
          {cloudImports.slice(0, 4).map(item => <div className="cloud-import-row" key={item.id}>
            <button className="cloud-import-main" disabled={!item.meeting_id}
              onClick={() => item.meeting_id && chooseMeeting(item.meeting_id)}>
              <Film size={15} /><span><strong>{item.filename}</strong><small>
                {item.source_kind === 'public' ? 'Публичная ссылка' : 'Папка Диска'} · {importStatus[item.status]}</small></span>
            </button>
            {item.status === 'error' && <button className="button subtle" disabled={cloudBusy}
              onClick={() => void retryImport(item)}><RefreshCw size={14} /> Повторить</button>}
          </div>)}
        </div>}
      </section>
      {duplicateNotice && <div className="cloud-notice" role="status"><ShieldCheck size={17} />
        <span>{duplicateNotice}</span></div>}
      {cloudError && <div className="cloud-error"><Notice text={cloudError} />
        <button className="button outline" disabled={uploading || processing} onClick={() => fileInput.current?.click()}>
          <Upload size={16} /> Загрузить с компьютера</button></div>}
      {integration?.last_error && !cloudError && <Notice text={integration.last_error.message} />}
      {error && <Notice text={error} />}
      {setupMissing && <div className="setup" role="status"><CircleAlert size={16} />
        <span>Требуется настройка окружения — {missing}.</span>
        <button className="text-button" onClick={() => void refreshHealth().catch(issue => setError(message(issue)))}>
          <RefreshCw size={14} /> Проверить</button></div>}
      <div className="panels">
        <aside className="history">
          <div className="history-title"><History size={18} /><h2>История встреч</h2><span>{meetings.length}</span></div>
          {meetings.length ? <ul className="meeting-list">{meetings.map(item => <li key={item.id}>
            <button className={`meeting-row${selected === item.id ? ' selected' : ''}`}
              onClick={() => chooseMeeting(item.id)} aria-current={selected === item.id}>
              <span className="meeting-icon"><Film size={17} /></span>
              <span className="meeting-label"><strong title={item.filename}>{item.filename}</strong>
                <small>{localDate(item.created_at)}{item.source === 'yandex' ? ' · Яндекс Диск' : ''}</small>
                <span className="row-state"><span className={`state-dot ${item.status}`} />{statusText[item.status]}
                  {item.needs_review_count > 0 && <span title="Нужно проверить спикеров"> · <Users size={12} />{item.needs_review_count}</span>}
                </span>
              </span><ChevronRight size={15} />
            </button>
          </li>)}</ul> : <div className="empty-history"><p>{loading ? 'Загружаем историю…' : 'Встреч пока нет'}</p>
            <small>Загруженные записи появятся здесь.</small></div>}
        </aside>
        <section className="main-panel" aria-label="Результат встречи">
          {!meeting ? <div className="empty-result">
            <div className="empty-art" aria-hidden="true"><div className="paper-back" /><div className="paper">
              <AudioLines size={22} /><i /><i /><i /></div><span><Sparkles size={16} /></span></div>
            <h2>{selected ? 'Загружаем встречу…' : meetings.length ? 'Выберите встречу' : 'Здесь появится результат'}</h2>
            <p>{meetings.length ? 'Откройте запись из истории, чтобы посмотреть расшифровку и конспект.'
              : 'Загрузите видео, чтобы получить конспект и расшифровку с именами спикеров.'}</p>
          </div> : <>
            <div className="detail-header"><div className="detail-title">
              <span className="round-icon"><Video size={20} /></span><div>
                <h2 title={meeting.filename}>{meeting.filename}</h2>
                <div className="detail-meta"><span><CalendarDays size={14} />{localDate(meeting.created_at)}</span>
                  {meeting.duration_ms !== null && <span><Clock3 size={14} />{clock(meeting.duration_ms)}</span>}
                  <span><span className={`state-dot ${meeting.status}`} />{statusText[meeting.status]}</span>
                  {meeting.source === 'yandex' && <span className="tag green">Яндекс Диск</span>}</div>
              </div></div>
              <div className="detail-actions">
                <button className={reviewOpen ? 'button active-button' : 'button outline'}
                  disabled={!speakers.length} onClick={() => setReviewOpen(!reviewOpen)}><Users size={17} />
                  Спикеры {meeting.needs_review_count > 0 && <span className="count">{meeting.needs_review_count}</span>}</button>
                <button className="icon-button danger" title="Удалить встречу" aria-label="Удалить встречу"
                  disabled={active(meeting.status) || active(meeting.summary_status) || speakers.some(item => active(item.job?.state))}
                  onClick={() => setDeleting(!deleting)}><Trash2 size={17} /></button>
              </div>
            </div>
            {deleting && <div className="confirm-box in-detail"><strong>Удалить встречу и её аудиофрагменты?</strong>
              <div className="form-actions"><button className="button danger" disabled={actionBusy}
                onClick={() => void meetingAction('delete')}>Удалить</button>
                <button className="button subtle" onClick={() => setDeleting(false)}>Отмена</button></div></div>}
            <div className="tabs" role="group" aria-label="Раздел встречи">
              <button className={tab === 'summary' ? 'selected' : ''} aria-pressed={tab === 'summary'}
                onClick={() => setTab('summary')}><Sparkles size={16} />AI-конспект</button>
              <button className={tab === 'transcript' ? 'selected' : ''} aria-pressed={tab === 'transcript'}
                onClick={() => setTab('transcript')}><FileText size={16} />Расшифровка</button>
            </div>
            <div className={reviewOpen || notes ? 'detail-content with-side' : 'detail-content'}>
              <div className="detail-body">
                {!meeting.transcript.length && meeting.error && <>
                  <Notice text={meeting.error.message} />
                  <button className="button outline" disabled={uploading || processing}
                    onClick={() => fileInput.current?.click()}><Upload size={16} /> Выбрать исходный файл заново</button>
                </>}
                {!meeting.transcript.length && !meeting.error && <p className="activity" role="status">
                  <LoaderCircle size={18} className="spin" />{stages[meeting.stage ?? ''] ?? 'Ожидаем начала обработки'}</p>}
                {tab === 'summary' && meeting.transcript.length > 0 && <>
                  {active(meeting.summary_status) && <p className="activity" role="status">
                    <LoaderCircle size={18} className="spin" />{meeting.summary_revision ? 'Конспект обновляется' : 'Составляем конспект'}</p>}
                  {meeting.summary_error && <><Notice text={meeting.summary_error.message} />
                    <button className="button outline" disabled={actionBusy}
                      onClick={() => void meetingAction('retry')}><RefreshCw size={16} /> Повторить конспект</button></>}
                  {notes && <div className="notes">
                    <section className="overview"><h3><Sparkles size={17} /> Краткое содержание</h3>
                      <p>{notes.overview}</p></section>
                    {!meeting.summary_error && <button className="button outline refresh-notes" disabled={actionBusy || active(meeting.summary_status)}
                      onClick={() => void meetingAction('regenerate')}><RefreshCw size={16} /> Обновить конспект</button>}
                  </div>}
                </>}
                {tab === 'transcript' && meeting.transcript.length > 0 && <ol className="transcript">
                  {meeting.transcript.map(segment => <li className={highlight === segment.id ? 'highlighted' : ''}
                    id={`segment-${segment.id}`} key={segment.id}>
                    <span className="mono time">{clock(segment.start_ms)}–{clock(segment.end_ms)}</span>
                    <div><div className="segment-speaker">{segment.speaker_id ? <button className="text-button"
                      onClick={() => { setSpeaker(segment.speaker_id); setReviewOpen(true) }}>
                      {segment.speaker_name ?? segment.speaker_label}</button> : <strong>{segment.speaker_label}</strong>}
                      {segment.speaker_source === 'auto' && <span className="tag amber">Автоимя</span>}
                      {segment.overlap && segment.speaker_id && <span className="tag amber">Наложение</span>}</div>
                      <p>{segment.text}</p></div>
                  </li>)}
                </ol>}
              </div>
              {(reviewOpen || notes) && <aside className="detail-side">
                {reviewOpen && <Review meeting={meeting.id} filename={meeting.filename} speakers={speakers} selected={speaker}
                  select={setSpeaker} profiles={profiles} health={health} refresh={refreshReview} close={() => setReviewOpen(false)} />}
                {notes && <div className="notes findings">
                  <div className="tabs finding-tabs" role="group" aria-label="Итоги встречи">
                    {findingKeys.map(key => <button key={key} className={findingTab === key ? 'selected' : ''}
                      aria-pressed={findingTab === key} onClick={() => setFindingTab(key)}>
                      {findingTabs[key].label} <span className="count">{notes[key].length}</span>
                    </button>)}
                  </div>
                  <section aria-label={findingTabs[findingTab].label} key={findingTab}>
                    {!findings.length ? <p className="muted">{findingTabs[findingTab].empty}</p> : <ul>
                      {findings.map((finding, index) => <li key={index}>
                        <p>{finding.description}</p>
                        {findingTab === 'tasks' && <small className="muted">
                          {finding.assignee
                            ? (speakers.find(item => item.id === finding.assignee)?.name ??
                               speakers.find(item => item.id === finding.assignee)?.label ?? finding.assignee)
                            : 'Исполнитель не указан'} · {finding.due ?? 'Срок не указан'}</small>}
                        <details className="finding-evidence">
                          <summary>Фрагменты расшифровки</summary>
                          {finding.evidence.map(evidence => <button key={evidence.segment_id} className="citation"
                            onClick={() => { setHighlight(evidence.segment_id); setTab('transcript') }}>
                            <span>«{evidence.quote}»</span><ArrowRight size={15} /></button>)}
                        </details>
                      </li>)}
                    </ul>}
                  </section>
                </div>}
              </aside>}
            </div>
          </>}
        </section>
      </div>
      <footer><span><LockKeyhole size={14} /> Расшифровка и AI-анализ выполняются локально</span>
        <span><ShieldCheck size={15} /> Локальное хранилище</span></footer>
    </main>
    {profilesOpen && <Profiles profiles={profiles} health={health} refresh={refreshReview} close={() => setProfilesOpen(false)} />}
  </>
}

createRoot(document.getElementById('root')!).render(<App />)
