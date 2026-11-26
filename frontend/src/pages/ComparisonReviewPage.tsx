import {useEffect, useMemo, useRef, useState} from 'react'
import {useOperations} from '../operations/OperationsPanel'
import {Link, useParams, useSearchParams} from 'react-router-dom'
import {json, request} from '../api/client'
import type {RequirementStatus as Status, RequirementCheckItem as VerificationItem} from '../api/client'
import {ComparisonPair, RunSummary} from './ComparisonsPage'
import {AnalysisConfidence, AnalysisFallbackNotice, AnalysisScoreExplanation} from '../components/AnalysisQuality'
import type {AnalysisTrace} from '../components/AnalysisQuality'

type AnalysisResult = {
  checked_requirements: VerificationItem[]
  stats: {total: number; fulfilled: number; missing: number; needs_review: number}
  trace_info?: AnalysisTrace|null
}
type RunDetail = {run: RunSummary; result: AnalysisResult}
type SourceCoveragePreflight = {
  available: boolean
  complete: boolean
  total_fragments?: number
  unclassified_count: number
  unclassified_by_block_type?: Record<string, number>
  review_hint_counts?: Record<string, number>
  table_structure_unverified?: string[]
  message?: string
}
type AtomReview = {
  id:string
  atom_id:string
  created_at:string
  result:{
    model_checked:boolean
    candidate_count:number
    proposed_links:number
    validated_links:Array<{
      tu_requirement_id:string
      tu_clause_number:string
      tu_text:string
      relation:string
      tu_quote:string
      znp_quote:string
      reasoning:string
      confidence?:number|null
      warnings:string[]
    }>
    rejected_links:Record<string,number>
    notice:string
  }
}

const labels: Record<Status, string> = {
  fulfilled: 'Соответствует',
  missing: 'Не соответствует',
  needs_review: 'Требует рассмотрения',
}
const atomOutcomes: Record<string,string> = {
  accepted_links: 'Проверенные связи сохранены',
  no_links: 'DeepSeek ответил: связей нет',
  rejected_links: 'Предложенные связи отклонены проверкой',
  model_error: 'Запрос к DeepSeek завершился ошибкой',
  targeted_error: 'Дополнительный запрос к DeepSeek завершился ошибкой',
  audit_error: 'Проверка противоречий завершилась ошибкой',
  audit_uncertain: 'Проверка противоречий не дала однозначного ответа',
  model_skipped: 'ИИ-проверка пропущена после ошибки',
  request_limit: 'Достигнут лимит запросов',
  invalid_response: 'Ответ DeepSeek имеет неверный формат',
  no_candidates: 'Поиск не вернул кандидатов',
  not_checked: 'ИИ-проверка не завершена',
}
const date = (stamp: string) => new Date(stamp).toLocaleString('ru-RU', {
  year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit',
})

export function ComparisonReviewPage({projectId, approved}: {projectId: string; approved: boolean}) {
  const {pairId} = useParams<{pairId:string}>()
  const {operations, start} = useOperations()
  const processedRuns = useRef(new Set<string>())
  const processedAtomJobs = useRef(new Set<string>())
  const [searchParams, setSearchParams] = useSearchParams()
  const [pair, setPair] = useState<ComparisonPair|null>(null)
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [detail, setDetail] = useState<RunDetail|null>(null)
  const [atomReviews, setAtomReviews] = useState<AtomReview[]>([])
  const [sourceCoverage, setSourceCoverage] = useState<SourceCoveragePreflight|null>(null)
  const layoutUnverified = Boolean(sourceCoverage?.table_structure_unverified?.length)
  const [status, setStatus] = useState<'all'|Status>('all')
  const [perspective, setPerspective] = useState<'znp'|'tu'>('znp')
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
    setPair(null);setRuns([]);setDetail(null);setAtomReviews([]);setError('')
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
    Promise.all([
      request<RunDetail>(`${root}/runs/${requestedRun}`),
      request<AtomReview[]>(`${root}/runs/${requestedRun}/atom-reviews`),
    ]).then(([next, reviews]) => {
        if (!active) return
        setDetail(next)
        setAtomReviews(reviews)
        setComments(Object.fromEntries(next.result.checked_requirements.map(
          item => [item.id,item.human_comment || '']
        )))
        setDecisions(Object.fromEntries(next.result.checked_requirements.map(
          item => [item.id,item.human_override_status || item.status]
        )))
      }).catch(e => {if (active) setError(e instanceof Error ? e.message : String(e))})
    return () => {active = false}
  }, [root, requestedRun])

  useEffect(() => {
    if (!pairId) return
    let active = true
    setSourceCoverage(null)
    void request<SourceCoveragePreflight>(`${root}/source-coverage`)
      .then(result => {if (active) setSourceCoverage(result)})
      .catch(() => {if (active) setSourceCoverage(null)})
    return () => {active = false}
  }, [root, pairId])

  const items = detail?.result.checked_requirements || []
  const atomResults = detail?.result.trace_info?.znp_atom_results || []
  const stageSummary = detail?.result.trace_info?.comparison_stages
  const llmUsage = detail?.result.trace_info?.llm_usage
  const showZnp = perspective === 'znp' && atomResults.length > 0
  const filteredAtoms = atomResults.filter(atom => !search.trim() ||
    [atom.text, atom.clause_number, ...atom.links.map(link =>
      link.tu_clause_number + ' ' + link.tu_text)].some(text =>
      text.toLocaleLowerCase('ru').includes(search.trim().toLocaleLowerCase('ru'))))
  const linkedAtoms = atomResults.filter(atom => atom.links.length > 0).length
  const checkedAtoms = atomResults.filter(atom => atom.model_checked).length
  const checkedTu = items.filter(item =>
    item.validation_details?.pipeline === 'znp_first_conservative' &&
    ((item.validation_details.primary_candidate_evaluations || 0) > 0 ||
      item.validation_details.targeted_diagnostics?.checked)).length
  const tuCoverageChecked = items.filter(item =>
    item.validation_details?.obligation_coverage?.every(condition => condition.checked !== false)).length
  const filtered = useMemo(() => items.filter(item =>
    (status === 'all' || item.status === status)
    && (!search.trim() || [item.requirement.text, item.requirement.clause_number,
      item.matched_clause?.text || '', item.reasoning || '']
      .some(text => text.toLocaleLowerCase('ru').includes(search.trim().toLocaleLowerCase('ru'))))
  ), [items, status, search])

  const running = operations.some(job =>
    (job.kind === 'comparison' || job.kind === 'comparison_batch' ||
     job.kind === 'comparison_atom_recheck') &&
    (job.status === 'queued' || job.status === 'running' ||
      job.status === 'paused') &&
    !!pairId && job.pair_ids.includes(pairId))
  const atomInProgress = new Set(operations.filter(job =>
    job.kind === 'comparison_atom_recheck' && !!pairId &&
    job.pair_ids.includes(pairId) && job.run_id === requestedRun &&
    ['queued', 'running', 'paused'].includes(job.status)
  ).map(job => job.atom_id))

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

  useEffect(() => {
    if (!requestedRun || !pairId) return
    const finished = operations.filter(job =>
      job.kind === 'comparison_atom_recheck' &&
      job.run_id === requestedRun &&
      job.pair_ids.includes(pairId) &&
      (job.status === 'completed' || job.status === 'failed') &&
      !processedAtomJobs.current.has(job.id))
    if (!finished.length) return
    finished.forEach(job => processedAtomJobs.current.add(job.id))
    void request<AtomReview[]>(`${root}/runs/${requestedRun}/atom-reviews`)
      .then(setAtomReviews)
      .catch(e => setError(e instanceof Error ? e.message : String(e)))
  }, [operations, root, pairId, requestedRun])

  async function rerunAtom(atomId: string) {
    if (!pairId || !requestedRun || busy || running || approved) return
    setBusy(true)
    setError('')
    try {
      await start({
        kind:'comparison_atom_recheck',
        pair_ids:[pairId], run_id:requestedRun, atom_id:atomId,
      })
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {setBusy(false)}
  }

  async function rerun(testMode = false) {
    if (busy || approved || running) return
    setBusy(true)
    setError('')
    try {
      let allowIncompleteSources = false
      if (!testMode) {
        // Fresh audit prevents spending on a document changed after page load.
        const audit = await request<SourceCoveragePreflight>(`${root}/source-coverage`)
        setSourceCoverage(audit)

        if (!audit.available) throw new Error(
          'Нет индексированных исходных фрагментов ЗнП. Сверку запускать нельзя.'
        )
        if (audit.unclassified_count > 0 && !window.confirm(
          `В ЗнП осталось ${audit.unclassified_count} исходных фрагментов без проверенной роли. ` +
          'Они могут содержать технические требования. DeepSeek не проверит их автоматически, ' +
          'а результат не получит автоматические подтверждения соответствия. ' +
          'Продолжить платную сверку с этими ограничениями?'
        )) return
        allowIncompleteSources = audit.unclassified_count > 0
      }
      await start({
        kind:'comparison', pair_ids:[pairId!], test_mode:testMode,
        allow_incomplete_sources:allowIncompleteSources,
      })
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
      {pair && !approved && <div className="workspace-row-actions">
        <button type="button" className="secondary" disabled={busy || running || layoutUnverified}
          onClick={() => rerun(true)} title="До 10 технических требований ТУ и 10 атомарных положений ЗнП. Преамбулы и перечни актов пропускаются. Основной результат не меняется.">
          Тест: 10 ТУ × 10 ЗнП
        </button>
        <button type="button" disabled={busy || running} onClick={() => rerun(false)}>
          {running ? 'Проверка выполняется' : busy ? 'Запускаем…' : 'Полная проверка'}
        </button>
      </div>}
    </div>
    {layoutUnverified && <p className="workspace-analysis-note">
      Для части PDF принадлежность значений строкам и столбцам требует проверки. Такие источники не дают автоматического подтверждения.
    </p>}
    {!layoutUnverified && sourceCoverage && sourceCoverage.available && sourceCoverage.unclassified_count > 0 &&
      <p className="workspace-analysis-note">
        Исходный ЗнП: {sourceCoverage.total_fragments} фрагментов,
        {sourceCoverage.unclassified_count} не классифицированы после извлечения.
        В них могут содержаться параметры и ограничения из приложений.
        {(sourceCoverage.review_hint_counts?.normative_reference_candidate ?? 0) > 0 &&
          <> Из них {sourceCoverage.review_hint_counts?.normative_reference_candidate}
          похожи на позиции нормативного каталога — это только подсказка для проверки,
          а не разрешение исключить их.</>}
        Полная сверка потребует подтверждения этих ограничений перед платным запуском.
      </p>}
    {sourceCoverage && !sourceCoverage.available &&
      <p className="workspace-analysis-note">
        В ЗнП нет индексированных фрагментов. Проверьте загрузку и извлечение документа.
      </p>}
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
                  {run.test_mode ? 'Тест 10 × 10 · ' : ''}
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
            {detail.run.test_mode && <span className="workspace-status queued">
              Тест 10 ТУ × 10 ЗнП · только чтение · основная проверка не изменена
            </span>}
            {!detail.run.is_current && !detail.run.test_mode &&
              <span className="workspace-status queued">Исторический результат, только чтение</span>}
          </div>
          <div className="workspace-stats">
            <div><strong>{detail.result.stats.total}</strong><span>Требований</span></div>
            <div><strong>{detail.result.stats.fulfilled}</strong><span>Соответствуют</span></div>
            <div><strong>{detail.result.stats.missing}</strong><span>Не соответствуют</span></div>
            <div><strong>{detail.result.stats.needs_review}</strong><span>Требуют рассмотрения</span></div>
          </div>
          {detail.run.test_mode && <p className="workspace-analysis-note">
            Тестовый запуск: выбрано до 10 технических требований ТУ
            и до 10 исходных технических положений ЗнП со всеми их атомами, без преамбул и перечней нормативных актов.
            Атомы, оставшиеся за лимитом запросов, помечены как не проверенные ИИ. Результат не распространяется
            на остальные пункты документов и не изменяет основные замечания.
          </p>}
          {detail.run.test_mode && <details className="workspace-evidence-details">
            <summary>Выбранные требования ТУ ({items.length})</summary>
            {items.map(item => <p key={item.id}><strong>ТУ {item.requirement.clause_number}</strong> — {item.requirement.text}</p>)}
          </details>}
          <AnalysisFallbackNotice trace={detail.result.trace_info} items={detail.result.checked_requirements}/>
          {detail.result.trace_info?.atomization && <details className="workspace-evidence-details">
            <summary>Атомизация ЗнП через DeepSeek: {detail.result.trace_info.atomization.atoms_total} положений</summary>
            <p>
              Исходных пунктов: {detail.result.trace_info.atomization.clauses_total}.
              Разделено моделью: {detail.result.trace_info.atomization.clauses_split}.
              Получено проверяемых положений: {detail.result.trace_info.atomization.atoms_total}.
              Сохранено целиком: {detail.result.trace_info.atomization.fallback_clauses}.
            </p>
            {detail.result.trace_info.atomization.degraded &&
              <p>DeepSeek был недоступен. Необработанные пункты сохранены целиком; качество атомизации снижено.</p>}
            {detail.result.trace_info.atomization.fallback_reasons &&
              Object.entries(detail.result.trace_info.atomization.fallback_reasons).map(([reason, count]) =>
                <p key={reason}>{reason}: {count}</p>)}
          </details>}
          <AnalysisScoreExplanation/>
          {stageSummary && <section className="workspace-stage-overview" aria-label="Этапы сверки">
            <div className="workspace-stage-summary">
              <h4>Поиск ЗнП → ТУ</h4>
              <strong>{stageSummary.matching.checked} из {stageSummary.matching.total}</strong>
              <p>Положений ЗнП проверено моделью. Принято связей: {stageSummary.matching.validated_links}.</p>
              <small>Запросов: {llmUsage?.attempted_by_phase.matching ?? 'нет данных'}.
                {stageSummary.matching.unreviewed > 0
                  ? ` Не проверено: ${stageSummary.matching.unreviewed}.`
                  : ' Основной проход завершён.'}</small>
            </div>
            <div className="workspace-stage-summary">
              <h4>Покрытие условий ТУ</h4>
              <strong>{stageSummary.coverage.fulfilled} из {stageSummary.coverage.total}</strong>
              <p>Условий подтверждено доказательствами. Не проверено: {stageSummary.coverage.unreviewed}.</p>
              <small>Запросов: {llmUsage?.attempted_by_phase.coverage ?? 'нет данных'}.
                {stageSummary.coverage.unreviewed > 0
                  ? ' Проверка покрытия неполная, отсутствие подтверждения не означает несоответствие.'
                  : ` Без доказательств: ${stageSummary.coverage.uncovered}.`}</small>
            </div>
            <div className="workspace-stage-summary">
              <h4>Дополнительный поиск ТУ → ЗнП</h4>
              {stageSummary.targeted.enabled ? <>
                <strong>{llmUsage?.attempted_by_phase.targeted ?? stageSummary.targeted.attempted} запросов</strong>
                <p>Вспомогательный обратный поиск: принято связей {stageSummary.targeted.validated_links}.</p>
                <small>{stageSummary.targeted.limit_reached
                  ? 'Лимит дополнительного поиска достигнут. Основной поиск от этого не становится незавершённым.'
                  : stageSummary.targeted.incomplete_requirements
                    ? `Дополнительный поиск не завершён для ${stageSummary.targeted.incomplete_requirements} требований ТУ. Нужна проверка инженера.`
                    : 'Отдельный этап, не показатель качества основного поиска.'}</small>
              </> : <>
                <strong>Отключён</strong>
                <p>В тесте 10 × 10 выполняются только поиск ЗнП → ТУ и оценка покрытия.</p>
                <small>Дополнительные платные запросы не выполнялись.</small>
              </>}
            </div>
          </section>}
          {atomResults.length > 0 && <>
            <div className="workspace-toolbar">
              <button type="button" onClick={() => {setPerspective('znp');setSearch('')}}
                aria-pressed={showZnp} className={showZnp ? 'primary' : ''}>
                ЗнП → ТУ ({atomResults.length} положений)
              </button>
              <button type="button" onClick={() => {setPerspective('tu');setSearch('')}}
                aria-pressed={!showZnp} className={!showZnp ? 'primary' : ''}>
                ТУ → ЗнП · проверка требований ({items.length})
              </button>
            </div>
            {showZnp ? <div className="workspace-stats">
              <div><strong>{checkedAtoms}</strong><span>Положений ЗнП рассмотрено ИИ</span></div>
              <div><strong>{linkedAtoms}</strong><span>Положений со связями с ТУ</span></div>
              <div><strong>{atomResults.length - checkedAtoms}</strong><span>Ещё не рассмотрено ИИ</span></div>
            </div> : <div className="workspace-stats">
              <div><strong>{checkedTu}</strong><span>Требований ТУ рассмотрено ИИ при поиске связей</span></div>
              <div><strong>{tuCoverageChecked}</strong><span>Требований с завершённой проверкой покрытия</span></div>
              <div><strong>{items.length - checkedTu}</strong><span>Не рассмотрено ИИ при поиске связей</span></div>
            </div>}
            {detail.result.trace_info?.matching_limit_reached && <p className="workspace-analysis-note">
              Достигнут лимит запросов основного поиска ЗнП → ТУ.
              Необработанные положения сохранены, отсутствие связи не означает отсутствие соответствия.
            </p>}
          </>}
          {showZnp ? <>
            <div className="workspace-analysis-note">
              <strong>Основная сверка: от положений ЗнП к требованиям ТУ</strong>
              <p>Каждое положение ЗнП является исходным запросом. Поиск подбирает кандидатов ТУ,
                DeepSeek оценивает соответствие, а сервер проверяет цитаты. Ниже показаны
                именно положения ЗнП и найденные для них связи — включая частичные и спорные.
                Итоговое покрытие требований ТУ доступно на соседней вкладке.</p>
            </div>
            <div className="workspace-toolbar">
              <input type="search" value={search} onChange={e => setSearch(e.target.value)}
                placeholder="Поиск по ЗнП и связанным ТУ" aria-label="Поиск по положениям ЗнП"/>
            </div>
            {filteredAtoms.map(atom => <article className="panel workspace-requirement" key={atom.atom_id}>
              <div className="workspace-requirement-head">
                <h3>Положение ЗнП {atom.clause_number}</h3>
                <span className={'workspace-status '+(atom.links.length ? 'completed' : 'queued')}>
                  {atom.links.length ? `Проверяемых связей: ${atom.links.length}` :
                    atom.model_checked ? 'Основной поиск: связей не найдено' : 'Основной поиск не завершён'}
                </span>
              </div>
              {!approved && <div className="workspace-row-actions">
                <button type="button" className="secondary"
                  disabled={busy || running || !atom.sources?.length}
                  onClick={() => void rerunAtom(atom.atom_id)}
                  title={!atom.sources?.length
                    ? 'В старом отчёте отсутствуют сохранённые исходные фрагменты'
                    : 'Новая отдельная ИИ-проверка связей; опубликованный итог не изменяется'}>
                  {atomInProgress.has(atom.atom_id) ? 'Перепроверяется…' : 'Повторить ИИ-проверку ЗнП'}
                </button>
                {!atom.sources?.length && <small className="muted">
                  Для этой записи нет достаточных данных источника. Доступно в новых отчётах.
                </small>}
              </div>}
              {atomReviews.filter(review => review.atom_id === atom.atom_id).map(review =>
                <details key={review.id} className="workspace-evidence-details">
                  <summary>Отдельная повторная проверка от {date(review.created_at)} ·
                    связей: {review.result.validated_links.length}</summary>
                  <p className="workspace-analysis-note">{review.result.notice}</p>
                  <p>Кандидатов: {review.result.candidate_count}.
                    Предложено: {review.result.proposed_links}.
                    Проверенных связей: {review.result.validated_links.length}.</p>
                  {review.result.validated_links.map((link, index) =>
                    <div className="workspace-znp-link"
                      key={link.tu_requirement_id + '-' + index}>
                      <strong>ТУ {link.tu_clause_number} · {link.relation}</strong>
                      <p>{link.tu_text}</p>
                      <blockquote>ТУ: {link.tu_quote}</blockquote>
                      <blockquote>ЗнП: {link.znp_quote}</blockquote>
                      <p>{link.reasoning}</p>
                      {!!link.warnings.length && <small>
                        Предупреждения контекста: {link.warnings.join(', ')}
                      </small>}
                    </div>)}
                </details>)}
              <p className="muted">
                {atom.model_checked ? 'Основной поиск: DeepSeek проверил кандидатов ТУ' :
                  'Основной поиск: DeepSeek не завершил проверку положения'}.
                {atom.outcome && atom.outcome !== 'accepted_links' && atom.outcome !== 'no_links'
                  ? ` ${atomOutcomes[atom.outcome] || atom.outcome}.` : ''}
                Кандидатов в первом поиске: {atom.candidates_count}.
                {atom.targeted_checked ? ' Дополнительный поиск ТУ → ЗнП также рассматривал это положение.' : ''}
                {atom.links.filter(link => link.source_phase === 'targeted').length > 0
                  ? ` Дополнительным обратным поиском найдено связей: ${atom.links.filter(link => link.source_phase === 'targeted').length}.`
                  : ''}
                {atom.page != null ? ` Страница ЗнП: ${atom.page}.` : ''}
              </p>
              {atom.search_channels?.length === 1 && atom.search_channels[0] === 'lexical' &&
                <p className="workspace-analysis-note">Семантический поиск недоступен; использован лексический поиск.</p>}
              {atom.error && <p className="workspace-analysis-note">Ошибка: {atom.error}</p>}
              {(atom.proposed_links ?? 0) > 0 && <p className="muted">
                Основной и дополнительный поиск предложили: {atom.proposed_links}.
                Прошло проверку: {atom.validated_links ?? 0}.
              </p>}
              {(atom.independent_conflict_links ?? 0) > 0 &&
                <p className="workspace-analysis-note">Независимо подтверждено противоречий: {atom.independent_conflict_links}.</p>}
              {atom.rejected_links && Object.keys(atom.rejected_links).length > 0 &&
                <p className="workspace-analysis-note">Причины отклонения: {Object.entries(atom.rejected_links).map(
                  ([reason, count]) => `${reason}: ${count}`
                ).join('; ')}.</p>}
              {atom.candidates && atom.candidates.length > 0 && <details className="workspace-evidence-details">
                <summary>Найденные кандидаты ТУ ({atom.candidates.length})</summary>
                {atom.candidates.map(candidate => <p key={candidate.requirement_id}>
                  ТУ {candidate.clause_number} · показатель поиска {Math.round(candidate.similarity_score * 100)}%
                </p>)}
              </details>}
              {stageSummary?.targeted.limit_reached && !atom.targeted_checked &&
                <p className="workspace-analysis-note">Дополнительный поиск ограничен лимитом запросов. Отсутствие связи не является выводом о несоответствии.</p>}
              {atom.links.length ? atom.links.map((link, index) =>
                <div className="workspace-znp-link" key={link.tu_requirement_id + '-' + index}>
                  <div className="workspace-requirement-head">
                    <strong>Связь с ТУ {link.tu_clause_number}
                      {link.source_phase === 'targeted' ? ' · дополнительный поиск ТУ → ЗнП' :
                       link.source_phase === 'matching' ? ' · основной поиск ЗнП → ТУ' :
                       link.source_phase === 'audit' ? ' · независимая проверка противоречий' : ''}
                    </strong>
                    <span className={'workspace-status '+(
                      link.relation === 'supports' ? 'fulfilled' : 'needs_review'
                    )}>
                      {link.relation === 'supports' ? 'Подтверждающая связь' :
                        link.relation === 'conflicts' ? 'Возможное противоречие' :
                        'Частичное соответствие'}
                    </span>
                  </div>
                  <AnalysisConfidence scope="link" method="llm" score={link.confidence}/>
                  <div className="workspace-evidence-grid">
                    <section>
                      <h4>Задание на проектирование</h4>
                      <p>{atom.text}</p>
                      <blockquote>{link.znp_quote}</blockquote>
                    </section>
                    <section>
                      <h4>Технические условия</h4>
                      <p>{link.tu_text}</p>
                      <blockquote>{link.tu_quote}</blockquote>
                    </section>
                  </div>
                  {(() => {
                    const tuItem = items.find(item => item.requirement.id === link.tu_requirement_id)
                    const conditions = tuItem?.validation_details?.obligation_coverage || []
                    return conditions.length ? <details className="workspace-evidence-details">
                      <summary>Проверка покрытия связанного требования ТУ ({conditions.filter(c => c.status === 'fulfilled').length} из {conditions.length})</summary>
                      {conditions.map(condition => <p key={condition.obligation_id}>
                        <strong>{condition.checked === false ? 'Не проверено' : condition.status === 'fulfilled' ? 'Подтверждено' : 'Требует рассмотрения'}</strong> — {condition.text}
                        {condition.reasoning ? ` · ${condition.reasoning}` : ''}
                      </p>)}
                    </details> : null
                  })()}
                  <div className="workspace-analysis-note">
                    <strong>Вывод сопоставления</strong>
                    <p>{link.reasoning || 'Пояснение модели не сохранено.'}</p>
                    {typeof link.search_score === 'number' && Number.isFinite(link.search_score) &&
                      <small title="Поисковая оценка кандидата, не уверенность DeepSeek">
                        Ранжирование поиска: {Math.round(link.search_score * 100)}%
                      </small>}
                  </div>
                </div>) :
                <div className="workspace-evidence-grid">
                  <section>
                    <h4>Задание на проектирование</h4>
                    <p>{atom.text}</p>
                  </section>
                  <section>
                    <h4>Технические условия</h4>
                    <p className="muted">Проверяемых связей пока нет. Это не вывод об отсутствии соответствия.</p>
                  </section>
                </div>}
            </article>)}
            {!filteredAtoms.length && <p className="muted">Положения ЗнП по запросу не найдены.</p>}
          </> : <>
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
            <AnalysisConfidence method={item.method} score={
              item.method === 'llm' && item.automatic_status !== 'fulfilled'
                ? null : item.confidence_score
            }/>
            {item.validation_details?.pipeline === 'znp_first_conservative' && (() => {
              const diagnostic = item.validation_details
              const primaryCount = diagnostic.primary_candidate_evaluations || 0
              const targeted = diagnostic.targeted_diagnostics
              const covered = diagnostic.obligation_coverage || []
              const checkedCount = covered.filter(row => row.checked !== false).length
              const limit = stageSummary?.targeted.limit_reached || llmUsage?.limit_reached
              return <section className="workspace-stage-overview" aria-label="Этапы проверки требования ТУ">
                <div className="workspace-stage-summary">
                  <h4>Основной поиск ЗнП → ТУ</h4>
                  <strong>{primaryCount} проверок кандидатов</strong>
                  <p>{primaryCount > 0 ? 'DeepSeek рассматривал это требование среди кандидатов ЗнП.' :
                    'Это требование не попадало в проверенные моделью кандидаты основного поиска.'}</p>
                  {stageSummary?.matching.limited && <small>Основной поиск прерван лимитом запросов.</small>}
                </div>
                <div className="workspace-stage-summary">
                  <h4>Дополнительный поиск ТУ → ЗнП</h4>
                  <strong>{!stageSummary?.targeted.enabled ? 'Отключён' :
                    targeted?.checked ? 'DeepSeek проверил кандидатов' : 'Не завершён'}</strong>
                  <p>Кандидатов: {targeted?.candidates || 0}
                    {targeted?.candidates_total != null && targeted.candidates_total > targeted.candidates
                      ? ` из ${targeted.candidates_total} найденных` : ''}.
                    Найденных проверяемых связей: {diagnostic.matches?.filter(m =>
                      m.relation === 'supports' || m.relation === 'partial' || m.relation === 'conflicts'
                    ).length || 0}.</p>
                  {targeted?.skip_reason === 'limit' && <small>Внутренний лимит запросов DeepSeek для дополнительного поиска исчерпан.</small>}
                  {targeted?.skip_reason === 'truncated' && <small>DeepSeek обрезал ответ; после разделения пакета часть кандидатов осталась непроверенной. Нужна проверка инженера.</small>}
                  {!!targeted?.truncated_responses && <small>Обрезанных ответов: {targeted.truncated_responses}. Проверено кандидатов: {targeted.checked_candidates || 0} из {targeted.candidates}.</small>}
                  {targeted?.skip_reason === 'model_unavailable' && <small>DeepSeek был недоступен на этом этапе.</small>}
                  {targeted?.skip_reason === 'invalid_response' && <small>Ответ DeepSeek не прошёл проверку формата.</small>}
                  {!targeted?.checked && targeted?.skip_reason == null && stageSummary?.targeted.enabled &&
                    <small>Дополнительный поиск не требовался или кандидаты отсутствовали.</small>}
                  {limit && !targeted?.checked && <small>Остальные этапы могли быть выполнены частично.</small>}
                </div>
                <div className="workspace-stage-summary">
                  <h4>Проверка условий ТУ</h4>
                  <strong>{checkedCount} из {covered.length}</strong>
                  <p>Подтверждено: {covered.filter(row => row.status === 'fulfilled').length}.
                    Требуют внимания: {covered.filter(row => row.status !== 'fulfilled').length}.</p>
                  {checkedCount < covered.length &&
                    <small>Смысловая проверка покрытия не завершена для всех условий.</small>}
                  {covered.length > 0 && !diagnostic.matches?.length &&
                    <small>Нет проверенных связей: без доказательств автоматическое соответствие запрещено.</small>}
                </div>
              </section>
            })()}
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
                <strong>Доказательства и диагностика ТУ</strong>
                <p>Положения ЗнП сопоставлены с требованиями ТУ; ниже сохранены все найденные
                  связи. Отсутствие связи не доказывает отсутствие требования.</p>
                {item.validation_details.candidate_limit_reached &&
                  <p>Не все поисковые кандидаты были рассмотрены. Итог требует проверки инженера.</p>}
                {item.validation_details.model_error &&
                  <p>ИИ-анализ не завершён: {item.validation_details.model_error}</p>}
                {stageSummary?.targeted.limit_reached &&
                  <p>Лимит дополнительного поиска достигнут. Отсутствие подтверждения не доказывает несоответствие.</p>}
              </div>
              {item.validation_details.obligation_coverage?.length ? <details className="workspace-evidence-details" open>
                <summary>Покрытие условий ТУ ({item.validation_details.obligation_coverage.filter(x => x.status === 'fulfilled').length} из {item.validation_details.obligation_coverage.length})</summary>
                {item.validation_details.obligation_coverage.map(condition => <div key={condition.obligation_id}>
                  <p><strong>{condition.checked === false ? 'Не проверено (этап покрытия не завершён)' :
                    condition.status === 'fulfilled' ? 'Подтверждено' :
                    condition.status === 'uncovered' ? 'Доказательств не найдено' :
                    'Требует проверки'}</strong> — {condition.text}</p>
                  {condition.reasoning && <p>{condition.reasoning}</p>}
                  {condition.evidence_ids?.length ? <p>Связанных доказательств: {condition.evidence_ids.length}</p> : null}
                </div>)}
              </details> : null}
              {item.validation_details.contradiction_audit && <div className="workspace-analysis-note">
                <strong>Независимая проверка противоречий</strong>
                <p>Просмотрено кандидатов: {item.validation_details.contradiction_audit.candidates_checked}.
                  Противоречий: {item.validation_details.contradiction_audit.confirmed.length}.
                  Неоднозначных: {item.validation_details.contradiction_audit.uncertain_atom_ids.length}.</p>
              </div>}
              {item.validation_details.matches?.length ? <details className="workspace-evidence-details" open>
                <summary>Все связанные положения ЗнП ({item.validation_details.matches.length})</summary>
                {item.validation_details.matches.map((match, index) => <div key={match.atom_id + '-' + index}>
                  <p><strong>Пункт {match.clause_number}</strong> — {
                    match.relation === 'supports' ? 'подтверждение' :
                    match.relation === 'conflicts' ? 'возможное противоречие' :
                    'частичное соответствие'
                  }</p>
                  <div className="workspace-evidence-grid">
                    <section>
                      <h4>Задание на проектирование</h4>
                      {match.atom_text && <p>{match.atom_text}</p>}
                      <blockquote>{match.quote}</blockquote>
                    </section>
                    <section>
                      <h4>Технические условия</h4>
                      {match.tu_quote ? <blockquote>{match.tu_quote}</blockquote> :
                        <p className="muted">Точная цитата ТУ не сохранена.</p>}
                    </section>
                  </div>
                  {match.source_phase &&
                    <small>Источник связи: {match.source_phase === 'targeted' ?
                      'дополнительный поиск ТУ → ЗнП' :
                      match.source_phase === 'matching' ? 'основной поиск ЗнП → ТУ' :
                      'проверка противоречий'}.</small>}
                  <AnalysisConfidence scope="link" method="llm" score={match.confidence}/>
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
          </>}
        </div>}
      </>}
  </div>
}
