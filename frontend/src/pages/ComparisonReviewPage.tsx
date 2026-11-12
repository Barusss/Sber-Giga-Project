import {useEffect, useMemo, useRef, useState} from 'react'
import {useOperations} from '../operations/OperationsPanel'
import {Link, useParams, useSearchParams} from 'react-router-dom'
import {json, request} from '../api/client'
import {ComparisonPair, RunSummary} from './ComparisonsPage'
import {AnalysisConfidence, AnalysisFallbackNotice, AnalysisScoreExplanation} from '../components/AnalysisQuality'
import type {AnalysisTrace} from '../components/AnalysisQuality'

type Status = 'fulfilled'|'missing'|'needs_review'
type VerificationItem = {
  id: string
  requirement: {id: string; clause_number: string; category: string; text: string}
  status: Status
  automatic_status?: Status|null
  reasoning?: string|null
  tu_quote?: string|null
  znp_quote?: string|null
  matched_clause?: {clause_number: string; text: string}|null
  validation_details?: {
    pipeline?: string
    matches?: Array<{
      atom_id: string; clause_number: string; relation: string; quote: string
      reasoning?: string; page?: number|null; fragment_id?: string|null
      confidence?: number; param_check?: {mismatches?: string[]}
    }>
    candidate_limit_reached?: boolean
    model_error?: string|null
    model_reasoning?: string; items?: Array<{
    item_id: number; text?: string; status?: string
    evidence?: Array<{clause_number?: string; quote: string}>
  }>}|null
  is_confirmed_by_human: boolean
  human_override_status?: Status|null
  human_comment?: string|null
  confidence_score?: number|null
  method?: string|null
}
type AnalysisResult = {
  checked_requirements: VerificationItem[]
  stats: {total: number; fulfilled: number; missing: number; needs_review: number}
  trace_info?: AnalysisTrace|null
}
type RunDetail = {run: RunSummary; result: AnalysisResult}

const labels: Record<Status, string> = {
  fulfilled: 'Соответствует',
  missing: 'Не соответствует',
  needs_review: 'Требует рассмотрения',
}
const date = (stamp: string) => new Date(stamp).toLocaleString('ru-RU', {
  year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit',
})

export function ComparisonReviewPage({projectId, approved}: {projectId: string; approved: boolean}) {
  const {pairId} = useParams<{pairId:string}>()
  const {operations, start} = useOperations()
  const processedRuns = useRef(new Set<string>())
  const [searchParams, setSearchParams] = useSearchParams()
  const [pair, setPair] = useState<ComparisonPair|null>(null)
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [detail, setDetail] = useState<RunDetail|null>(null)
  const [status, setStatus] = useState<'all'|Status>('all')
  const [search, setSearch] = useState('')
  const [comments, setComments] = useState<Record<string,string>>({})
  const [decisions, setDecisions] = useState<Record<string,Status>>({})
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const base = `/projects/${projectId}/comparisons`
  const root = `${base}/${pairId}`
  const requestedRun = searchParams.get('run') || ''

  const load = async () => {
    if (!pairId) return
    const [nextPair, nextRuns] = await Promise.all([
      request<ComparisonPair>(root),
      request<RunSummary[]>(`${root}/runs`),
    ])
    setPair(nextPair)
    setRuns(nextRuns)
    if (!requestedRun && nextRuns.length) {
      setSearchParams({run: nextPair.current_run_id || nextRuns[0].id}, {replace: true})
    } else if (!nextRuns.length) {
      setDetail(null)
    }
  }

  useEffect(() => {
    let active = true
    if (!pairId) return
    setPair(null);setRuns([]);setDetail(null);setError('')
    Promise.all([
      request<ComparisonPair>(root),
      request<RunSummary[]>(`${root}/runs`),
    ]).then(([p,r]) => {
      if (!active) return
      setPair(p);setRuns(r)
      if (!searchParams.get('run') && r.length) {
        setSearchParams({run:p.current_run_id || r[0].id}, {replace:true})
      }
    }).catch(e => {if (active) setError(e instanceof Error ? e.message : String(e))})
    return () => {active = false}
  }, [root])

  useEffect(() => {
    if (!requestedRun || !pairId) {
      setDetail(null)
      return
    }
    let active = true
    setDetail(null);setError('')
    request<RunDetail>(`${root}/runs/${requestedRun}`)
      .then(next => {
        if (!active) return
        setDetail(next)
        setComments(Object.fromEntries(next.result.checked_requirements.map(
          item => [item.id,item.human_comment || '']
        )))
        setDecisions(Object.fromEntries(next.result.checked_requirements.map(
          item => [item.id,item.human_override_status || item.status]
        )))
      }).catch(e => {if (active) setError(e instanceof Error ? e.message : String(e))})
    return () => {active = false}
  }, [root, requestedRun])

  const items = detail?.result.checked_requirements || []
  const filtered = useMemo(() => items.filter(item =>
    (status === 'all' || item.status === status)
    && (!search.trim() || [item.requirement.text, item.requirement.clause_number,
      item.matched_clause?.text || '', item.reasoning || '']
      .some(text => text.toLocaleLowerCase('ru').includes(search.trim().toLocaleLowerCase('ru'))))
  ), [items, status, search])

  const running = operations.some(job =>
    (job.kind === 'comparison' || job.kind === 'comparison_batch') &&
    (job.status === 'queued' || job.status === 'running') &&
    !!pairId && job.pair_ids.includes(pairId))

  useEffect(() => {
    if (!pairId) return
    for (const job of operations) {
      if ((job.kind !== 'comparison' && job.kind !== 'comparison_batch') ||
          !job.pair_ids.includes(pairId) ||
          (job.status !== 'completed' && job.status !== 'failed') ||
          processedRuns.current.has(job.id)) continue
      processedRuns.current.add(job.id)
      if (job.status === 'completed') {
        const saved = job.result?.runs
        const selected = Array.isArray(saved)
          ? saved.find(item => typeof item === 'object' && item !== null &&
              'pair_id' in item && item.pair_id === pairId)
          : undefined
        if (selected && typeof selected.run_id === 'string') {
          if (!requestedRun || requestedRun === pair?.current_run_id) {
            setSearchParams({run:selected.run_id}, {replace:true})
          }
        }
      }
      void load().catch(e => setError(e instanceof Error ? e.message : String(e)))
    }
  }, [operations, pairId, requestedRun, pair?.current_run_id])

  async function rerun() {
    if (busy || approved || running) return
    setBusy(true)
    setError('')
    try {
      await start({kind:'comparison', pair_ids:[pairId!]})
    } catch(e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function deleteRun(run: RunSummary) {
    if (busy || approved || !window.confirm(
      `Удалить запуск от ${date(run.created_at)}? Это удалит только его снимок, остальные запуски сохранятся.`
    )) return
    setBusy(true);setError('')
    try {
      await request(`${root}/runs/${run.id}`, {method:'DELETE'})
      const nextRuns = runs.filter(item => item.id !== run.id)
      setRuns(nextRuns)
      if (requestedRun === run.id) {
        setSearchParams(nextRuns.length ? {run:nextRuns[0].id} : {}, {replace:true})
        if (!nextRuns.length) setDetail(null)
      }
      const current = await request<ComparisonPair>(root)
      setPair(current)
    } catch(e) {setError(e instanceof Error ? e.message : String(e))}
    finally {setBusy(false)}
  }

  async function save(item: VerificationItem) {
    if (!detail?.run.is_current || busy || approved) return
    setBusy(true);setError('')
    try {
      const next = await request<RunDetail>(`${root}/runs/${detail.run.id}/confirm`,
        json('POST', {
          verification_id:item.id,
          status:decisions[item.id] || item.status,
          comment:comments[item.id] || null,
        }))
      setDetail(next)
      const [nextRuns, nextPair] = await Promise.all([
        request<RunSummary[]>(`${root}/runs`),
        request<ComparisonPair>(root),
      ])
      setRuns(nextRuns);setPair(nextPair)
    } catch(e) {setError(e instanceof Error ? e.message : String(e))}
    finally {setBusy(false)}
  }

  if (!pairId) return <p className="error">Сверка не выбрана.</p>
  return <div className="workspace-comparison-review">
    <div className="workspace-section-heading">
      <div>
        <Link to={base} className="workspace-back">Все сверки</Link>
        <h2>Сверка документов</h2>
        {pair && <p>{pair.tu_filename} / {pair.znp_filename}</p>}
      </div>
      {pair && !approved && <button disabled={busy || running} onClick={rerun}>
        {running ? 'Проверка выполняется' : busy ? 'Запускаем…' : 'Повторить проверку'}
      </button>}
    </div>
    {error && <p role="alert" className="error">{error}</p>}
    {!pair ? <p className="muted">Загружаем сверку…</p> :
      <>
        <section className="panel workspace-history">
          <div className="workspace-section-heading">
            <h3>История запусков</h3><span className="panel-counter">{runs.length}</span>
          </div>
          {runs.length ? <div className="workspace-run-list">
            {runs.map(run => <div key={run.id} className={'workspace-run-row '+(requestedRun === run.id ? 'active' : '')}>
              <button type="button" className="workspace-run-select" onClick={() => setSearchParams({run:run.id})}>
                <strong>{date(run.created_at)}</strong>
                <small>{run.is_current ? 'Текущий результат' : 'Исторический снимок'} ·
                  {run.stats.total || 0} требований · {run.confirmed} решений инженера
                </small>
              </button>
              {!approved && <button type="button" className="workspace-danger" disabled={busy}
                onClick={() => deleteRun(run)}>Удалить запуск</button>}
            </div>)}
          </div> : <p className="muted">Запусков пока нет. Выполните сверку, чтобы получить результаты.</p>}
        </section>

        {detail && <div className="workspace-verification">
          <div className="workspace-section-heading">
            <div><h3>Замечания и соответствия</h3><p>Результат от {date(detail.run.created_at)}</p></div>
            {!detail.run.is_current && <span className="workspace-status queued">Исторический результат, только чтение</span>}
          </div>
          <div className="workspace-stats">
            <div><strong>{detail.result.stats.total}</strong><span>Требований</span></div>
            <div><strong>{detail.result.stats.fulfilled}</strong><span>Соответствуют</span></div>
            <div><strong>{detail.result.stats.missing}</strong><span>Не соответствуют</span></div>
            <div><strong>{detail.result.stats.needs_review}</strong><span>Требуют рассмотрения</span></div>
          </div>
          <AnalysisFallbackNotice trace={detail.result.trace_info} items={detail.result.checked_requirements}/>
          <AnalysisScoreExplanation/>
          <div className="workspace-toolbar">
            <input type="search" value={search} onChange={e => setSearch(e.target.value)}
              placeholder="Поиск по требованиям" aria-label="Поиск по требованиям"/>
            <select value={status} aria-label="Фильтр статусов"
              onChange={e => setStatus(e.target.value as typeof status)}>
              <option value="all">Все требования</option>
              <option value="fulfilled">Соответствуют</option>
              <option value="missing">Не соответствуют</option>
              <option value="needs_review">Требуют рассмотрения</option>
            </select>
          </div>
          {filtered.map(item => <article className="panel workspace-requirement" key={item.id}>
            <div className="workspace-requirement-head">
              <h3>Требование {item.requirement.clause_number}</h3>
              <span className={'workspace-status '+item.status}>{labels[item.status]}</span>
            </div>
            <AnalysisConfidence method={item.method} score={item.confidence_score}/>
            <div className="workspace-evidence-grid">
              <section>
                <h4>Технические условия</h4>
                <p>{item.requirement.text}</p>
                {item.tu_quote && <blockquote>{item.tu_quote}</blockquote>}
              </section>
              <section>
                <h4>Задание на проектирование</h4>
                {item.matched_clause ? <>
                  <p>{item.matched_clause.text}</p>
                  {item.znp_quote && <blockquote>{item.znp_quote}</blockquote>}
                </> : <p className="muted">Подтверждающий пункт не найден.</p>}
              </section>
            </div>
            {item.validation_details?.pipeline === 'znp_first_conservative' && <>
              <div className="workspace-analysis-note">
                <strong>Сверка ЗнП → ТУ</strong>
                <p>Положения ЗнП сопоставлены с требованиями ТУ; ниже сохранены все найденные
                  связи. Отсутствие связи не доказывает отсутствие требования.</p>
                {item.validation_details.candidate_limit_reached &&
                  <p>Не все поисковые кандидаты были рассмотрены. Итог требует проверки инженера.</p>}
                {item.validation_details.model_error &&
                  <p>ИИ-анализ не завершён: {item.validation_details.model_error}</p>}
              </div>
              {item.validation_details.matches?.length ? <details className="workspace-evidence-details" open>
                <summary>Все связанные положения ЗнП ({item.validation_details.matches.length})</summary>
                {item.validation_details.matches.map((match, index) => <div key={match.atom_id + '-' + index}>
                  <p><strong>Пункт {match.clause_number}</strong> — {
                    match.relation === 'supports' ? 'подтверждение' :
                    match.relation === 'conflicts' ? 'возможное противоречие' :
                    'частичное соответствие'
                  }</p>
                  <blockquote>{match.quote}</blockquote>
                  {match.reasoning && <p>{match.reasoning}</p>}
                  {match.param_check?.mismatches?.length ?
                    <p>Параметры: {match.param_check.mismatches.join('; ')}</p> : null}
                </div>)}
              </details> : <p className="muted">Подтверждающих связей не обнаружено.</p>}
            </>}
            {item.validation_details?.items?.length ? <details className="workspace-evidence-details">
              <summary>Подтверждения по пунктам</summary>
              {item.validation_details.items.map(part => <div key={part.item_id}>
                <p><strong>{part.text}</strong> {part.status && <span>{part.status}</span>}</p>
                {part.evidence?.map((e,index)=><blockquote key={index}>
                  {e.clause_number && `Пункт ${e.clause_number}: `}{e.quote}
                </blockquote>)}
              </div>)}
            </details> : null}
            <div className="workspace-analysis-note">
              <strong>Вывод проверки</strong>
              <p>{item.reasoning || 'Дополнительное объяснение не сохранено.'}</p>
              {item.automatic_status && item.automatic_status !== item.status &&
                <small>Автоматический статус: {labels[item.automatic_status]}</small>}
            </div>
            <div className="workspace-engineer">
              <h4>Решение инженера</h4>
              {detail.run.is_current && !approved ? <>
                <div className="workspace-engineer-fields">
                  <label>Результат
                    <select value={decisions[item.id] || item.status} disabled={busy}
                      onChange={e => setDecisions(prev => ({...prev,[item.id]:e.target.value as Status}))}>
                      <option value="fulfilled">Соответствует</option>
                      <option value="missing">Не соответствует</option>
                      <option value="needs_review">Требует рассмотрения</option>
                    </select>
                  </label>
                  <label>Комментарий
                    <textarea rows={2} value={comments[item.id] ?? ''} disabled={busy}
                      onChange={e => setComments(prev => ({...prev,[item.id]:e.target.value}))}/>
                  </label>
                  <button type="button" disabled={busy} onClick={() => save(item)}>
                    Сохранить решение
                  </button>
                </div>
              </> : <p>{item.is_confirmed_by_human
                ? `${labels[item.status]}. ${item.human_comment || ''}`
                : 'Решение инженера не сохранено.'}</p>}
              {item.is_confirmed_by_human && detail.run.is_current &&
                <small className="workspace-confirmed">Решение инженера сохранено</small>}
            </div>
          </article>)}
          {!filtered.length && <p className="muted">По фильтру требований не найдено.</p>}
        </div>}
      </>}
  </div>
}
