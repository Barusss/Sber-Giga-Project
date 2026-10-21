export type DiagnosticStatus = 'running' | 'success' | 'error'

export type DiagnosticEntry = {
  id: number
  title: string
  detail: string
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

function change(id: number, changes: Partial<DiagnosticEntry>) {
  entries = entries.map(entry => entry.id === id ? {
    ...entry, ...changes, updatedAt: Date.now()
  } : entry)
  publish()
}

export function beginDiagnostic(title: string, detail = '') {
  const id = ++sequence
  const now = Date.now()
  entries = [{id, title, detail, status: 'running', startedAt: now, updatedAt: now}, ...entries].slice(0, MAX_ENTRIES)
  publish()

  return {
    step: (message: string) => change(id, {detail: message}),
    success: (message: string) => change(id, {status: 'success', detail: message}),
    error: (message: string) => change(id, {status: 'error', detail: message}),
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
