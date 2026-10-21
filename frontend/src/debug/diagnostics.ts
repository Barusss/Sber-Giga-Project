export type DiagnosticStatus = 'running' | 'success' | 'error'

export type DiagnosticStep = {
  at: number
  message: string
}

export type DiagnosticEntry = {
  id: number
  title: string
  detail: string
  history: DiagnosticStep[]
  status: DiagnosticStatus
  startedAt: number
  updatedAt: number
}

const MAX_ENTRIES = 100
let sequence = 0
let entries: DiagnosticEntry[] = []
const listeners = new Set<() => void>()

function publish() {
  for (const listener of listeners) listener()
}

function change(id: number, status: DiagnosticStatus, message: string) {
  const now = Date.now()
  entries = entries.map(entry => entry.id === id ? {
    ...entry,
    status,
    detail: message,
    updatedAt: now,
    history: [...entry.history, {at: now, message}]
  } : entry)
  publish()
}

export function beginDiagnostic(title: string, detail = '') {
  const id = ++sequence
  const now = Date.now()
  entries = [{
    id, title, detail, history: [{at: now, message: detail || 'Начало операции'}],
    status: 'running', startedAt: now, updatedAt: now
  }, ...entries].slice(0, MAX_ENTRIES)
  publish()

  return {
    step: (message: string) => change(id, 'running', message),
    success: (message: string) => change(id, 'success', message),
    error: (message: string) => change(id, 'error', message),
  }
}

export function subscribeDiagnostics(listener: () => void) {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

export function getDiagnostics() {
  return entries
}

export function clearDiagnostics() {
  // Running operations remain visible so their final status is not lost.
  entries = entries.filter(entry => entry.status === 'running')
  publish()
}
