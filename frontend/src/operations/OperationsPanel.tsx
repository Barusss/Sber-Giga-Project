import {createContext, useContext, useEffect, useMemo, useRef, useState} from 'react'
import type {ReactNode} from 'react'
import {json, request} from '../api/client'

export type Operation = {
  id: string
  project_id: string
  kind: 'upload'|'normative_extract'|'normative_check'|'comparison'|'comparison_batch'
  status: 'queued'|'running'|'completed'|'failed'|'cancelled'
  stage: string
  message: string
  filename?: string|null
  document_id?: string|null
  pair_ids: string[]
  progress_current: number|null
  progress_total: number|null
  error: string|null
  result: Record<string,unknown>|null
  created_at: string
  completed_at: string|null
}

type OperationInput = {
  kind: Exclude<Operation['kind'],'upload'>
  document_id?: string
  pair_ids?: string[]
  refresh?: boolean
}

type ContextValue = {
  operations: Operation[]
  loading: boolean
  connectionError: boolean
  reload: () => Promise<void>
  start: (payload: OperationInput) => Promise<Operation>
  upload: (data: FormData) => Promise<Operation>
  cancel: (id: string) => Promise<void>
}

const OperationsContext = createContext<ContextValue|null>(null)
const active = (job: Operation) => job.status === 'queued' || job.status === 'running'

export function useOptionalOperations() {
  return useContext(OperationsContext)
}

export function useOperations() {
  const value = useContext(OperationsContext)
  if (!value) throw new Error('OperationsProvider is required')
  return value
}

export function OperationsProvider({projectId, children}: {
  projectId: string
  children: ReactNode
}) {
  const [operations, setOperations] = useState<Operation[]>([])
  const [loading, setLoading] = useState(true)
  const [connectionError, setConnectionError] = useState(false)
  const mounted = useRef(true)
  const base = `/projects/${projectId}/operations`

  const reload = async () => {
    const jobs = await request<Operation[]>(base)
    if (mounted.current) {
      setOperations(jobs)
      setLoading(false)
      setConnectionError(false)
    }
  }

  useEffect(() => {
    mounted.current = true
    let timer: ReturnType<typeof setTimeout> | undefined
    let cancelled = false
    setLoading(true)
    setOperations([])
    async function poll() {
      let delay = 6000
      try {
        const jobs = await request<Operation[]>(base)
        if (cancelled) return
        setOperations(jobs)
        setConnectionError(false)
        setLoading(false)
        delay = jobs.some(active) ? 1500 : 6000
      } catch {
        if (cancelled) return
        setConnectionError(true)
        setLoading(false)
        delay = 5000
      }
      if (!cancelled) timer = setTimeout(poll, delay)
    }
    void poll()
    return () => {cancelled = true; mounted.current = false; if (timer) clearTimeout(timer)}
  }, [base])

  async function start(payload: OperationInput) {
    const job = await request<Operation>(base, json('POST', payload))
    setOperations(previous => [job, ...previous.filter(item => item.id !== job.id)].slice(0, 40))
    void reload().catch(() => setConnectionError(true))
    return job
  }

  async function upload(data: FormData) {
    const job = await request<Operation>(`${base}/upload`, {method:'POST', body:data})
    setOperations(previous => [job, ...previous.filter(item => item.id !== job.id)].slice(0, 40))
    void reload().catch(() => setConnectionError(true))
    return job
  }

  async function cancel(id: string) {
    await request(`${base}/${id}/cancel`, {method:'POST'})
    await reload()
  }

  const value = useMemo(() => ({
    operations, loading, connectionError, reload, start, upload, cancel,
  }), [operations, loading, connectionError, base])

  return <OperationsContext.Provider value={value}>{children}</OperationsContext.Provider>
}

const titles: Record<Operation['kind'],string> = {
  upload: 'Загрузка и распознавание',
  normative_extract: 'Извлечение нормативных ссылок',
  normative_check: 'Проверка нормативов',
  comparison: 'Сверка ТУ и ЗнП',
  comparison_batch: 'Пакетная сверка документов',
}

function StatusIcon({status}: {status: Operation['status']}) {
  if (status === 'running' || status === 'queued') {
    return <span className="operation-spinner" aria-hidden="true"/>
  }
  return <span className={'operation-result-dot '+status} aria-hidden="true"/>
}

function OperationRow({job, cancel}: {job: Operation; cancel: (id: string) => Promise<void>}) {
  const working = active(job)
  const knownProgress = job.progress_current !== null && job.progress_total !== null
    && job.progress_total > 0
  const percentage = knownProgress
    ? Math.min(100, Math.round((job.progress_current! / job.progress_total!) * 100))
    : null
  const description = job.filename || (job.kind === 'comparison_batch'
    ? `${job.pair_ids.length} сверок`
    : '')
  return <div className={'operation-row '+job.status} role={working ? 'status' : undefined}>
    <StatusIcon status={job.status}/>
    <div className="operation-row-body">
      <div className="operation-row-title">
        <strong>{titles[job.kind]}</strong>
        {description && <span>{description}</span>}
      </div>
      <p>{job.error || job.message}</p>
      {working && <div className="operation-progress" role="progressbar"
        aria-label={titles[job.kind]}
        aria-valuemin={percentage !== null ? 0 : undefined}
        aria-valuemax={percentage !== null ? 100 : undefined}
        aria-valuenow={percentage === null ? undefined : percentage}>
        {percentage !== null
          ? <span className="operation-progress-fill" style={{width:`${percentage}%`}}/>
          : <span className="operation-progress-indeterminate"/>}
      </div>}
    </div>
    {knownProgress && working && <span className="operation-numbers">
      {job.progress_current} / {job.progress_total}
    </span>}
    {working && job.kind === 'normative_check' &&
      <button type="button" className="secondary operation-cancel"
        onClick={() => void cancel(job.id)}>Остановить</button>}
  </div>
}

export function OperationsPanel() {
  const {operations, loading, connectionError, cancel} = useOperations()
  const current = operations.filter(active)
  const completed = operations.filter(job => !active(job))
  const [expanded, setExpanded] = useState(false)
  const [dismissed, setDismissed] = useState<string[]>([])
  const visibleDone = completed.filter(job => !dismissed.includes(job.id)).slice(0, 1)
  if (loading) return null
  if (!current.length && !visibleDone.length && !connectionError) return null
  return <section className="operations-panel" aria-label="Процессы проекта">
    {connectionError && <div className="operation-connection">
      Не удалось обновить статусы операций. Повторное подключение выполняется автоматически.
    </div>}
    {current.map(job => <OperationRow key={job.id} job={job} cancel={cancel}/>)}
    {!current.length && visibleDone.map(job => <div className="operation-finished" key={job.id}>
      <OperationRow job={job} cancel={cancel}/>
      <button type="button" className="operation-dismiss" onClick={() => setDismissed(
        previous => [...previous, job.id]
      )} aria-label="Скрыть завершённую операцию">Закрыть</button>
    </div>)}
    {completed.length > 1 && <div className="operation-history">
      <button type="button" className="operation-history-toggle"
        onClick={() => setExpanded(!expanded)} aria-expanded={expanded}>
        {expanded ? 'Скрыть историю операций' : 'История операций'}
      </button>
      {expanded && <div className="operation-history-items">
        {completed.slice(0, 10).map(job =>
          <OperationRow key={job.id} job={job} cancel={cancel}/>)}
      </div>}
    </div>}
  </section>
}
