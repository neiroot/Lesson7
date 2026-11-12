export type ErrorInfo = { code: string; message: string; stage?: string; details?: { other_speakers?: string[] } }
export type Job = { id: string; state: 'queued' | 'running' | 'succeeded' | 'failed'; error?: ErrorInfo }
export type MeetingRow = {
  id: string; filename: string; created_at: string; duration_ms: number | null;
  status: 'queued' | 'processing' | 'ready' | 'error'; stage: string | null;
  needs_review_count: number; summary_status: string; source: 'upload' | 'yandex';
}
export type Segment = {
  id: string; start_ms: number; end_ms: number; speaker_id: string | null; speaker_name: string | null;
  speaker_label: string; speaker_source: 'auto' | 'manual' | null; overlap: boolean; text: string;
}
export type Evidence = { segment_id: string; quote: string }
export type Finding = { description: string; evidence: Evidence[]; assignee?: string | null; due?: string | null }
export type Notes = { overview: string; decisions: Finding[]; tasks: Finding[]; open_questions: Finding[] }
export type Meeting = MeetingRow & {
  transcript: Segment[]; transcript_revision: number; summary_revision: number | null; summary: Notes | null;
  error: ErrorInfo | null; summary_error: ErrorInfo | null;
}
export type Clip = {
  id: string; start_ms: number; end_ms: number; eligible: boolean; audio_available: boolean;
  quality: { reasons: string[]; speech_ms: number }; segments: (Segment & { partial: boolean })[];
}
export type Speaker = {
  id: string; number: number; label: string; status: 'needs_review' | 'auto_matched' | 'confirmed' | 'skipped';
  reason: string | null; name: string | null; profile_id: string | null; version: number;
  clips: Clip[]; job: Job | null; suggested_profiles: { id: string; name: string }[];
}
export type Profile = { id: string; name: string; created_at: string; sample_count: number }
export type Draft = {
  id: string; status: 'checking' | 'eligible' | 'rejected' | 'expired'; speech_ms: number | null;
  audio_available: boolean; reasons: string[];
}
export type Health = {
  storage: boolean; ffmpeg: boolean; weights: boolean; runtimes: boolean; ollama: boolean; ollama_model: boolean;
  consent_text: string; consent_text_version: string;
}
export type YandexIntegration = {
  configured: boolean; connected: boolean; folder: string; poll_seconds: number;
  connected_at: string | null; last_check_at: string | null; last_success_at: string | null;
  last_error: ErrorInfo | null;
}
export type CloudImport = {
  id: string; source_kind: 'public' | 'private'; filename: string; size: number;
  status: 'queued' | 'downloading' | 'processing' | 'ready' | 'error' | 'duplicate';
  meeting_id: string | null; created_at: string; updated_at: string;
  meeting_status: MeetingRow['status'] | null; meeting_stage: string | null; error: ErrorInfo | null;
}
export const active = (state?: string | null) => ['queued', 'running', 'processing', 'checking'].includes(state ?? '')
export const localDate = (date: string) => new Intl.DateTimeFormat('ru-RU', {
  day: 'numeric', month: 'short', year: 'numeric',
}).format(new Date(date))
export const localDateTime = (date: string) => new Intl.DateTimeFormat('ru-RU', {
  day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit',
}).format(new Date(date))
export const clock = (milliseconds: number) => {
  const seconds = Math.floor(milliseconds / 1000)
  return [Math.floor(seconds / 60), seconds % 60].map(value => String(value).padStart(2, '0')).join(':')
}

export class ApiError extends Error {
  info: ErrorInfo
  constructor(info: ErrorInfo) { super(info.message); this.info = info }
}
export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch('/api' + path, { cache: 'no-store', ...init })
  } catch {
    throw new Error('Нет связи с локальным сервером. Проверьте, что приложение запущено.')
  }
  if (!response.ok) {
    const info = await response.json().catch(() => ({
      code: 'server', message: 'Локальный сервер вернул ошибку.',
    })) as ErrorInfo
    throw new ApiError(info)
  }
  return response.status === 204 ? undefined as T : response.json()
}
const pendingKeys = new Map<string, string>()
export async function submit<T>(path: string, value: object, idempotent = false) {
  const body = JSON.stringify(value)
  const scope = path + body
  const key = pendingKeys.get(scope) ?? crypto.randomUUID()
  if (idempotent) pendingKeys.set(scope, key)
  try {
    const result = await request<T>(path, {
      method: 'POST', body, headers: { 'Content-Type': 'application/json',
        ...(idempotent ? { 'Idempotency-Key': key } : {}) },
    })
    pendingKeys.delete(scope)
    return result
  } catch (error) {
    if (error instanceof ApiError) pendingKeys.delete(scope)
    throw error
  }
}
export function upload<T>(path: string, file: File) {
  const body = new FormData()
  body.append('file', file)
  return request<T>(path, { method: 'POST', body })
}
export const message = (error: unknown) => error instanceof Error ? error.message : 'Произошла ошибка'
