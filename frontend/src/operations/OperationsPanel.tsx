import {createContext, useContext, useEffect, useMemo, useRef, useState} from 'react'
import type {ReactNode} from 'react'
import {API, json, request} from '../api/client'

export type Operation = {
  id: string
  project_id: string
  kind: 'upload'|'normative_extract'|'normative_check'|'comparison'|'comparison_batch'|'comparison_atom_recheck'
  status: 'queued'|'running'|'paused'|'completed'|'failed'|'cancelled'
  stage: string
  message: string
  filename?: string|null
  document_id?: string|null
  pair_ids: string[]
  test_mode?: boolean
  atom_id?: string|null
  run_id?: string|null
  pause_requested?: boolean
  checkpoint?: {saved:number;uncertain:number;requests_attempted:number;by_phase:Record<string,number>}|null
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
  test_mode?: boolean
  run_id?: string
  atom_id?: string
}

type ContextValue = {
  operations: Operation[]
  loading: boolean
  connectionError: boolean
  reload: () => Promise<void>
  start: (payload: OperationInput) => Promise<Operation>
  upload: (data: FormData) => Promise<Operation>
  cancel: (id: string) => Promise<void>
  pause: (id: string) => Promise<void>
  resume: (id: string, acceptReplayRisk?: boolean) => Promise<void>
}

async function listJobs(base: string): Promise<Operation[]> {
  // Progress polling is intentionally silent in the debug log.
  const response = await fetch(`${API}${base}`, {cache:'no-store'})
  if (!response.ok) throw new Error(`Не удалось получить статус: ${response.status}`)
  return response.json() as Promise<Operation[]>
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
    const jobs = await listJobs(base)
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
        const jobs = await listJobs(base)
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

  async function pause(id: string) {
    await request(`${base}/${id}/pause`, {method:'POST'})
    await reload()
  }

  async function resume(id: string, acceptReplayRisk = false) {
    await request(`${base}/${id}/resume`,
      json('POST', {accept_replay_risk:acceptReplayRisk}))
    await reload()
  }

  const value = useMemo(() => ({
    operations, loading, connectionError, reload, start, upload, cancel, pause, resume,
  }), [operations, loading, connectionError, base])

  return <OperationsContext.Provider value={value}>{children}</OperationsContext.Provider>
}

const titles: Record<Operation['kind'],string> = {
  upload: 'Загрузка и распознавание',
  normative_extract: 'Извлечение нормативных ссылок',
  normative_check: 'Проверка нормативов',
  comparison: 'Сверка ТУ и ЗнП',
  comparison_batch: 'Пакетная сверка документов',
  comparison_atom_recheck: 'Повторная проверка одного ЗнП',
}

function StatusIcon({status}: {status: Operation['status']}) {
  if (status === 'running' || status === 'queued') {
    return <span className="operation-spinner" aria-hidden="true"/>
  }
  return <span className={'operation-result-dot '+status} aria-hidden="true"/>
}

type ComparisonProgressData = {
  pair_id: string
  pair_index: number
  pair_total: number
  stage: string
  requirements_current: number|null
  requirements_total: number|null
  current_clause: string|null
  remaining_seconds: number|null
}

const comparisonSteps = [
  {stage:'reading', label:'Разбор документов'},
  {stage:'embedding', label:'Поиск соответствий'},
  {stage:'verifying', label:'Поиск связей ЗнП → ТУ'},
  {stage:'coverage', label:'Проверка покрытия и обратный поиск'},
  {stage:'audit', label:'Аудит противоречий'},
  {stage:'persisting', label:'Формирование замечаний'},
  {stage:'snapshot', label:'Сохранение результата'},
]

function comparisonStageIndex(stage: string): number {
  if (stage === 'reading' || stage === 'preparing') return 0
  if (stage === 'embedding') return 1
  if (stage === 'verifying') return 2
  if (stage === 'coverage') return 3
  if (stage === 'audit') return 4
  if (stage === 'persisting') return 5
  if (stage === 'snapshot') return 6
  if (stage === 'complete' || stage === 'comparing') return 7
  return -1
}

function asComparisonProgress(job: Operation): ComparisonProgressData|null {
  const value = job.result?.comparison_progress
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const detail = value as Record<string,unknown>
  if (typeof detail.stage !== 'string' || typeof detail.pair_index !== 'number'
      || typeof detail.pair_total !== 'number') return null
  return {
    pair_id: String(detail.pair_id || ''),
    pair_index: detail.pair_index,
    pair_total: detail.pair_total,
    stage: detail.stage,
    requirements_current: typeof detail.requirements_current === 'number'
      ? detail.requirements_current : null,
    requirements_total: typeof detail.requirements_total === 'number'
      ? detail.requirements_total : null,
    current_clause: typeof detail.current_clause === 'string' ? detail.current_clause : null,
    remaining_seconds: typeof detail.remaining_seconds === 'number'
      ? detail.remaining_seconds : null,
  }
}

function formatRemaining(seconds: number): string {
  const minutes = Math.max(1, Math.ceil(seconds / 60))
  if (minutes < 60) return `около ${minutes} мин`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest ? `около ${hours} ч ${rest} мин` : `около ${hours} ч`
}

function ComparisonProgress({job}: {job: Operation}) {
  const data = asComparisonProgress(job)
  if (!data) return <p className="operation-waiting-detail">
    {job.status === 'queued' ? 'Ожидает свободного исполнителя' : 'Определяется состав документов и требований'}
  </p>
  const stage = comparisonStageIndex(data.stage)
  const current = data.requirements_current
  const total = data.requirements_total
  const hasCount = current !== null && total !== null && total > 0
  const percent = hasCount ? Math.min(100, Math.round(current / total * 100)) : null
  const sourceIsZnp = data.stage === 'verifying'
  const sourceIsTu = data.stage === 'coverage' || data.stage === 'audit'
  const countLabel = sourceIsZnp ? 'положений ЗнП' : sourceIsTu ? 'требований ТУ' : 'элементов'

  return <div className="operation-comparison-detail">
    <div className="operation-comparison-heading">
      <strong>Пара {data.pair_index} из {data.pair_total}</strong>
      {hasCount && <span>{countLabel}: {total}</span>}
    </div>
    <ol className="operation-stage-list" aria-label="Этапы сверки">
      {comparisonSteps.map((step,index) => <li key={step.stage}
        className={'operation-stage-item '+(index < stage ? 'done' : index === stage ? 'current' : 'pending')}
        aria-current={index === stage ? 'step' : undefined}>
        <span className="operation-stage-marker" aria-hidden="true"/>
        <span>{step.label}</span>
      </li>)}
    </ol>
    {hasCount && stage >= 2 && <div className="operation-requirements-progress">
      <div className="operation-requirements-head">
        <strong>Обработано {current} из {total} {countLabel}</strong>
        <span>{percent}%</span>
      </div>
      <div className="operation-progress" role="progressbar"
        aria-label={`Проверено ${countLabel} в текущей паре`}
        aria-valuemin={0} aria-valuemax={total} aria-valuenow={current}>
        <span className="operation-progress-fill" style={{width:`${percent}%`}}/>
      </div>
      <div className="operation-requirements-foot">
        {data.current_clause && (sourceIsZnp || sourceIsTu)
          ? <span>Сейчас: {sourceIsZnp ? 'положение ЗнП' : 'требование ТУ'} {data.current_clause}</span>
          : <span>Осталось: {Math.max(0, total-current)}</span>}
        {stage === 2 && data.remaining_seconds !== null &&
          <span>Примерно осталось: {formatRemaining(data.remaining_seconds)}</span>}
      </div>
      {data.current_clause && (sourceIsZnp || sourceIsTu) && <p className="operation-remaining-note">
        Осталось {countLabel}: {Math.max(0,total-current)}.
        Прогресс каждого этапа отдельно фиксируется в базе.
      </p>}
    </div>}
    {!hasCount && stage >= 0 && stage < 2 &&
      <p className="operation-waiting-detail">Число положений определяется по содержимому документов.</p>}
    {hasCount && stage === 1 && <p className="operation-waiting-detail">
      Выделено {total} положений ЗнП. Подготавливается поиск соответствующих требований ТУ.
    </p>}
  </div>
}

function OperationRow({job, cancel, pause, resume}: {
  job: Operation
  cancel: (id: string) => Promise<void>
  pause: (id: string) => Promise<void>
  resume: (id: string, acceptReplayRisk?: boolean) => Promise<void>
}) {
  const working = active(job)
  const knownProgress = job.progress_current !== null && job.progress_total !== null
    && job.progress_total > 0
  const percentage = knownProgress
    ? Math.min(100, Math.round((job.progress_current! / job.progress_total!) * 100))
    : null
  const isComparison = job.kind === 'comparison' || job.kind === 'comparison_batch'
  const canControl = isComparison || job.kind === 'comparison_atom_recheck'
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
      {(working || job.status === 'paused') && isComparison && <ComparisonProgress job={job}/>}
      {canControl && job.checkpoint && <p className="muted">
        Сохранено ответов DeepSeek: {job.checkpoint.saved}.
        Выполнено платных попыток: {job.checkpoint.requests_attempted}.
        {job.checkpoint.uncertain > 0 &&
          ` Запросов с неопределённым результатом: ${job.checkpoint.uncertain}.`}
      </p>}
      {working && !isComparison && <div className="operation-progress" role="progressbar"
        aria-label={titles[job.kind]}
        aria-valuemin={percentage !== null ? 0 : undefined}
        aria-valuemax={percentage !== null ? 100 : undefined}
        aria-valuenow={percentage === null ? undefined : percentage}>
        {percentage !== null
          ? <span className="operation-progress-fill" style={{width:`${percentage}%`}}/>
          : <span className="operation-progress-indeterminate"/>}
      </div>}
    </div>
    {knownProgress && working && !isComparison && <span className="operation-numbers">
      {job.progress_current} / {job.progress_total}
    </span>}
    {working && job.kind === 'normative_check' &&
      <button type="button" className="secondary operation-cancel"
        onClick={() => void cancel(job.id)}>Остановить</button>}
    {working && canControl && <button type="button" className="secondary operation-cancel"
      onClick={() => void pause(job.id)}>Пауза</button>}
    {job.status === 'paused' && canControl && <>
      <button type="button" className="secondary operation-cancel"
        onClick={() => {
          const risk = (job.checkpoint?.uncertain || 0) > 0
          if (risk && !window.confirm(
            'Последний ответ не сохранён: DeepSeek мог обработать и списать оплату. ' +
            'Повторить такой запрос и продолжить сверку?'
          )) return
          void resume(job.id, risk)
        }}>Продолжить</button>
      <button type="button" className="secondary operation-cancel"
        onClick={() => void cancel(job.id)}>Завершить без продолжения</button>
    </>}
  </div>
}

export function OperationsPanel() {
  const {operations, loading, connectionError, cancel, pause, resume} = useOperations()
  const current = operations.filter(active)
  const paused = operations.filter(job => job.status === 'paused')
  const completed = operations.filter(job => !active(job) && job.status !== 'paused')
  const [expanded, setExpanded] = useState(false)
  const [dismissed, setDismissed] = useState<string[]>([])
  const visibleDone = completed.filter(job => !dismissed.includes(job.id)).slice(0, 1)
  if (loading) return null
  if (!current.length && !paused.length && !visibleDone.length && !connectionError) return null
  return <section className="operations-panel" aria-label="Процессы проекта">
    {connectionError && <div className="operation-connection">
      Не удалось обновить статусы операций. Повторное подключение выполняется автоматически.
    </div>}
    {current.map(job => <OperationRow key={job.id} job={job} cancel={cancel}
      pause={pause} resume={resume}/>)}
    {paused.map(job => <OperationRow key={job.id} job={job} cancel={cancel}
      pause={pause} resume={resume}/>)}
    {!current.length && !paused.length && visibleDone.map(job => <div className="operation-finished" key={job.id}>
      <OperationRow job={job} cancel={cancel} pause={pause} resume={resume}/>
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
          <OperationRow key={job.id} job={job} cancel={cancel} pause={pause} resume={resume}/>)}
      </div>}
    </div>}
  </section>
}
