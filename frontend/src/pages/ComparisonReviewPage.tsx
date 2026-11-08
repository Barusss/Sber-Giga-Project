import {useEffect, useMemo, useState} from 'react'
import {Link, useParams, useSearchParams} from 'react-router-dom'
import {json, request} from '../api/client'
import {ComparisonPair, RunSummary} from './ComparisonsPage'

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
  validation_details?: {model_reasoning?: string; items?: Array<{
    item_id: number; text?: string; status?: string
    evidence?: Array<{clause_number?: string; quote: string}>
  }>}|null
  is_confirmed_by_human: boolean
  human_override_status?: Status|null
  human_comment?: string|null
  confidence_score?: number
}
type AnalysisResult = {
  checked_requirements: VerificationItem[]
  stats: {total: number; fulfilled: number; missing: number; needs_review: number}
  trace_info?: {degraded?: boolean}|null
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
  const [view, setView] = useState<'remarks'|'history'>('remarks')
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

  async function rerun() {
    if (busy || approved) return
    setBusy(true);setError('')
    try {
      const result = await request<RunDetail>(`${root}/runs`, {method:'POST'})
      await load()
      setSearchParams({run:result.run.id})
    } catch(e) {setError(e instanceof Error ? e.message : String(e))}
    finally {setBusy(false)}
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
    <section className="panel workspace-review-header">
      <div className="workspace-section-heading">
        <div>
          <Link to={base} className="workspace-back">Все сверки</Link>
          <h2>Сверка документов</h2>
        </div>
        {pair && !approved && <button disabled={busy} onClick={rerun}>
          {busy ? 'Выполняется…' : runs.length ? 'Повторить проверку' : 'Запустить проверку'}
        </button>}
      </div>
      {pair && <div className="workspace-review-files">
        <div><small>Технические условия</small><strong>{pair.tu_filename}</strong></div>
        <div><small>Задание на проектирование</small><strong>{pair.znp_filename}</strong></div>
      </div>}
    </section>
    {error && <p role="alert" className="error">{error}</p>}
    {!pair ? <p className="muted">Загружаем сверку…</p> :
      <>
        <div className="workspace-review-tabs" role="tablist" aria-label="Данные сверки">
          <button type="button" role="tab" aria-selected={view === 'remarks'}
            className={view === 'remarks' ? 'active' : ''}
            onClick={() => setView('remarks')}>Замечания</button>
          <button type="button" role="tab" aria-selected={view === 'history'}
            className={view === 'history' ? 'active' : ''}
            onClick={() => setView('history')}>История запусков <span>{runs.length}</span></button>
        </div>
        {view === 'history' && <section className="panel workspace-history">
          <div className="workspace-section-heading">
            <h3>История запусков</h3><span className="panel-counter">{runs.length}</span>
          </div>
          {runs.length ? <div className="workspace-run-list">
            {runs.map(run => <div key={run.id} className={'workspace-run-row '+(requestedRun === run.id ? 'active' : '')}>
              <button type="button" className="workspace-run-select" onClick={() => {
                setSearchParams({run:run.id})
                setView('remarks')
              }}>
                <strong>{date(run.created_at)}</strong>
                <small>{run.is_current ? 'Текущий результат' : 'Исторический снимок'} ·
                  {run.stats.total || 0} требований · {run.confirmed} решений инженера
                </small>
              </button>
              {!approved && <button type="button" className="workspace-danger" disabled={busy}
                onClick={() => deleteRun(run)}>Удалить запуск</button>}
            </div>)}
          </div> : <p className="muted">Запусков пока нет. Выполните сверку, чтобы получить результаты.</p>}
        </section>}

        {view === 'remarks' && !detail && !runs.length &&
          <section className="panel workspace-empty"><p>Проверка ещё не запускалась.</p></section>}

        {view === 'remarks' && detail && <div className="workspace-verification">
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
          {detail.result.trace_info?.degraded && <p className="error">
            ИИ-анализ был недоступен или неполон. Выводы требуют ручного подтверждения.
          </p>}
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
