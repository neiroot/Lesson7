import { useState } from 'react'
import { AudioLines, Check, ChevronLeft, ChevronRight, CircleAlert, LoaderCircle, UserRound, X } from 'lucide-react'
import { active, clock, message, submit, type Health, type Profile, type Speaker } from './api'

export function Notice({ text }: { text: string }) {
  return <div className="notice" role="alert"><CircleAlert size={17} /><span>{text}</span></div>
}
export function Consent({ text, checked, setChecked }: {
  text: string; checked: boolean; setChecked: (value: boolean) => void
}) {
  return <label className="check consent"><input type="checkbox" checked={checked}
    onChange={event => setChecked(event.target.checked)} />{text}</label>
}

const speakerState = {
  needs_review: 'Нужно уточнить', auto_matched: 'Автоимя', confirmed: 'Подтверждён', skipped: 'Пропущен',
}

export function Review({ meeting, filename, speakers, selected, select, profiles, health, refresh, close }: {
  meeting: string; filename: string; speakers: Speaker[]; selected: string | null;
  select: (id: string) => void; profiles: Profile[]; health: Health | null;
  refresh: () => Promise<void>; close: () => void;
}) {
  const speaker = speakers.find(item => item.id === selected) ?? speakers.find(item => item.status === 'needs_review')
    ?? speakers[0]
  return <aside className="review">
    <div className="review-heading"><div><span className="eyebrow">ГОЛОСА В ЗАПИСИ</span>
      <h2>Проверка спикеров</h2></div><button className="icon-button" title="Закрыть проверку"
        aria-label="Закрыть проверку" onClick={close}><X size={18} /></button></div>
    {!speaker ? <p className="muted">Карточки появятся после расшифровки.</p> : <>
      <nav className="speaker-options" aria-label="Спикеры">
        {speakers.map(item => <button key={item.id} title={speakerState[item.status]}
          className={item.id === speaker.id ? 'speaker-option selected' : 'speaker-option'}
          onClick={() => select(item.id)}><UserRound size={14} />{item.label}</button>)}
      </nav>
      <SpeakerForm key={speaker.id + ':' + speaker.version} {...{
        meeting, filename, speaker, speakers, profiles, health, refresh,
      }} />
    </>}
  </aside>
}

function SpeakerForm({ meeting, filename, speaker, speakers, profiles, health, refresh }: {
  meeting: string; filename: string; speaker: Speaker; speakers: Speaker[];
  profiles: Profile[]; health: Health | null; refresh: () => Promise<void>;
}) {
  const suggestions = speaker.suggested_profiles ?? []
  const suggested = suggestions.length === 1 ? suggestions[0] : undefined
  const initialProfile = speaker.profile_id ?? suggested?.id
  const [mode, setMode] = useState(initialProfile ? 'profile' : 'name')
  const [name, setName] = useState(speaker.name ?? '')
  const [profile, setProfile] = useState(initialProfile ?? '')
  const [index, setIndex] = useState(0)
  const [chosen, setChosen] = useState<string[]>([])
  const [saveVoice, setSaveVoice] = useState(false)
  const [consent, setConsent] = useState(false)
  const [confirmed, setConfirmed] = useState(false)
  const [shared, setShared] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [audioError, setAudioError] = useState('')
  const clip = speaker.clips[index]
  const target = mode === 'name' ? name.trim() : profiles.find(item => item.id === profile)?.name
  const peers = mode === 'profile' && profile ? speakers.filter(item => item.id !== speaker.id && item.profile_id === profile) : []
  const running = active(speaker.job?.state)
  const eligible = speaker.clips.filter(item => item.eligible && item.audio_available)
  const selectedClips = chosen.filter(id => eligible.some(item => item.id === id))
  const pending = running || busy
  const clearConfirmations = () => { setConfirmed(false); setConsent(false); setShared(false) }
  async function resolve(action: string) {
    setBusy(true); setError('')
    const assignment = action === 'assign_name' || action === 'assign_profile'
    const payload = {
      action, expected_version: speaker.version,
      ...(assignment ? { confirm_name: confirmed, ...(mode === 'name' ? { name: name.trim() } : { profile_id: profile }),
        confirm_shared_profile: shared } : {}),
      ...(assignment && saveVoice ? { save_voice: true, clip_ids: selectedClips, consent_confirmed: consent,
        consent_text_version: health?.consent_text_version } : {}),
    }
    try {
      await submit(`/meetings/${meeting}/speakers/${speaker.id}/resolve`, payload, true)
      await refresh()
    } catch (issue) { setError(message(issue)); await refresh().catch(() => undefined) }
    finally { setBusy(false) }
  }
  return <div className="speaker-form">
    <div className="card-top"><div><h3>{speaker.name ?? speaker.label}</h3><p title={filename}>{filename}</p></div>
      <span className={'tag ' + (speaker.status === 'needs_review' || speaker.status === 'auto_matched' ? 'amber' : 'green')}>
        {speakerState[speaker.status]}</span></div>
    {speaker.reason && <p className="small muted">{speaker.reason}</p>}
    {suggestions.length > 0 && <div className="voice-suggestion">
      <strong>{suggested ? 'Возможное совпадение с голосовой базой' : 'В голосовой базе несколько похожих голосов'}</strong>
      <p>{suggested ? `Это ${suggested.name}? Прослушайте фрагмент и подтвердите имя.`
        : 'Прослушайте фрагмент и выберите профиль, если узнали голос.'}</p>
      {!suggested && <div className="speaker-options">
        {suggestions.map(item => <button key={item.id} type="button" disabled={pending}
          className={mode === 'profile' && profile === item.id ? 'speaker-option selected' : 'speaker-option'}
          onClick={() => { setMode('profile'); setProfile(item.id); clearConfirmations() }}>
          {item.name}
        </button>)}
      </div>}
    </div>}
    <div className="clip">
      <div className="clip-title"><span><AudioLines size={16} /> Фрагмент речи</span>
        {clip && <span className="mono">{clock(clip.start_ms)}–{clock(clip.end_ms)}</span>}</div>
      {clip?.audio_available
        ? <audio key={clip.id} controls preload="none" onError={() => setAudioError('Фрагмент недоступен. Можно сохранить только имя.')}
            src={`/api/meetings/${meeting}/clips/${clip.id}/audio`} />
        : <p className="small muted">Аудиофрагмент недоступен.</p>}
      {audioError && <p className="small muted">{audioError}</p>}
      {speaker.clips.length > 1 && <div className="clip-nav">
        <button className="icon-button" aria-label="Предыдущий фрагмент" disabled={index === 0}
          onClick={() => { setIndex(index - 1); setAudioError('') }}><ChevronLeft size={16} /></button>
        <span>{index + 1} из {speaker.clips.length}</span>
        <button className="icon-button" aria-label="Следующий фрагмент" disabled={index + 1 >= speaker.clips.length}
          onClick={() => { setIndex(index + 1); setAudioError('') }}><ChevronRight size={16} /></button>
      </div>}
      {clip && <p className="small quality">{clip.eligible ? 'Пригоден для голосового профиля' :
        clip.quality.reasons.join('. ') || 'Недоступен для сохранения голоса'}</p>}
      {clip?.segments.length ? <div className="clip-words">{clip.segments.map(segment => <p key={segment.id}>
        <span className="mono muted">{clock(segment.start_ms)}–{clock(segment.end_ms)}</span> {segment.text}
        {segment.partial && <small> Частичное совпадение</small>}
      </p>)}</div> : <p className="small muted">Расшифровка этого фрагмента недоступна</p>}
    </div>
    {running && <p className="activity"><LoaderCircle size={17} className="spin" /> Сохраняем голосовой профиль</p>}
    {(error || speaker.job?.state === 'failed') && <Notice text={error ||
      `${speaker.job?.error?.message ?? 'Ошибка сохранения'}. Профиль и назначение не изменились.`} />}
    <form onSubmit={event => { event.preventDefault(); void resolve(mode === 'name' ? 'assign_name' : 'assign_profile') }}>
      <fieldset disabled={pending}>
        <label>Назначение<select value={mode} onChange={event => { setMode(event.target.value); clearConfirmations() }}>
          <option value="name">Имя участника</option><option value="profile">Существующий профиль</option>
        </select></label>
        {mode === 'name' ? <label>Имя<input value={name} maxLength={100} placeholder="Например, Анна"
          onChange={event => { setName(event.target.value); clearConfirmations() }} /></label>
          : <label>Голосовой профиль<select value={profile}
            onChange={event => { setProfile(event.target.value); clearConfirmations() }}>
            <option value="">Выберите профиль</option>
            {profiles.map(item => <option key={item.id} value={item.id}>
              {item.name} · {item.id.slice(0, 6)}
            </option>)}
          </select></label>}
        {target && <label className="check"><input type="checkbox" checked={confirmed}
          onChange={event => setConfirmed(event.target.checked)} />Подтверждаю имя «{target}»</label>}
        {!!peers.length && <label className="check"><input type="checkbox" checked={shared}
          onChange={event => setShared(event.target.checked)} />
          Один профиль для {speaker.label} и {peers.map(item => item.label).join(', ')}</label>}
        <label className="check"><input type="checkbox" checked={saveVoice} disabled={!eligible.length || !health}
          onChange={event => { setSaveVoice(event.target.checked); setConsent(false) }} />
          {mode === 'name' ? 'Сохранить голосовой профиль' : 'Добавить образцы к профилю'}</label>
        {saveVoice && <>
          <div className="clip-choices">{eligible.map(item => <label className="check" key={item.id}>
            <input type="checkbox" checked={chosen.includes(item.id)}
              onChange={event => { setChosen(event.target.checked ? [...chosen, item.id] : chosen.filter(id => id !== item.id))
                setConsent(false) }} />
            {clock(item.start_ms)}–{clock(item.end_ms)}
          </label>)}</div>
          {health && <Consent text={health.consent_text} checked={consent} setChecked={setConsent} />}
        </>}
        <div className="form-actions">
          <button className="button primary" disabled={!target || !confirmed || (!!peers.length && !shared)
            || (saveVoice && (!consent || !selectedClips.length || !health))}><Check size={16} />
            {saveVoice ? 'Сохранить голос' : 'Подтвердить имя'}</button>
          <button className="button subtle" type="button" onClick={() => void resolve('skip')}>Пропустить</button>
          {speaker.name && <button className="text-button" type="button" onClick={() => void resolve('reset')}>Сбросить имя</button>}
        </div>
      </fieldset>
    </form>
  </div>
}
