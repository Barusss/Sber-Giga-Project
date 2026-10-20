import { useEffect, useState } from 'react'
import { Document, Project, json, request } from '../api/client'

type RequirementStatus = 'fulfilled' | 'missing' | 'needs_review'
type RequirementCategory =
  | 'power'
  | 'reliability'
  | 'voltage'
  | 'connection_point'
  | 'metering'
  | 'protection'
  | 'equipment'
  | 'other'

type TURequirement = {
  id: string
  clause_number: string
  category: RequirementCategory
  text: string
  fragment_id?: string | null
  page?: number | null
  fragment_text?: string | null
}

type ZNPClause = {
  id: string
  clause_number: string
  text: string
  fragment_id?: string | null
  page?: number | null
  fragment_text?: string | null
}

type RequirementCheckItem = {
  id: string
  requirement: TURequirement
  status: RequirementStatus
  confidence_score: number
  matched_clause?: ZNPClause | null
  tu_quote?: string | null
  znp_quote?: string | null
  reasoning?: string | null
  is_confirmed_by_human: boolean
  human_comment?: string | null
  human_override_status?: RequirementStatus | null
  tu_fragment_id?: string | null
  tu_page?: number | null
  tu_fragment_text?: string | null
  znp_fragment_id?: string | null
  znp_page?: number | null
  znp_fragment_text?: string | null
  channels?: string[]
  method?: string | null
}

type RequirementVerification = {
  id: string
  project_id: string
  requirement_id: string
  status: RequirementStatus
  confidence_score: number | null
  is_confirmed_by_human: boolean
  human_override_status: RequirementStatus | null
  human_comment: string | null
}

type CheckRequirementsResponse = {
  checked_requirements: RequirementCheckItem[]
  stats: {
    fulfilled?: number
    missing?: number
    needs_review?: number
    total?: number
  }
  trace_info?: {
    execution_time_ms?: number
    analysis?: {
      created: number
      updated: number
      preserved_human: number
      preserved_findings: number
      removed: number
      force: boolean
    }
    embedding?: {
      enabled: boolean
      model?: string
      dim?: number
      reason?: string | null
    }
    agent_runs?: Array<{
      requirement_id: string
      clause_number: string
      steps: Array<Record<string, unknown>>
    }>
  } | null
}

const CATEGORY_LABELS: Record<RequirementCategory, string> = {
  power: 'Мощность',
  reliability: 'Категория надёжности',
  voltage: 'Класс напряжения',
  connection_point: 'Точка присоединения',
  metering: 'Учёт электроэнергии',
  protection: 'РЗА и автоматика',
  equipment: 'Оборудование',
  other: 'Общее требование',
}

const STATUS_META: Record<
  RequirementStatus,
  { label: string; bg: string; color: string; border: string }
> = {
  fulfilled: {
    label: 'Отражено в ЗнП',
    bg: '#ecfdf5',
    color: '#065f46',
    border: '#a7f3d0',
  },
  needs_review: {
    label: 'Требует проверки',
    bg: '#fffbeb',
    color: '#92400e',
    border: '#fde68a',
  },
  missing: {
    label: 'Отсутствует в ЗнП',
    bg: '#fef2f2',
    color: '#991b1b',
    border: '#fecaca',
  },
}

const AI_PROGRESS_STAGES = [
  { threshold: 20, text: 'Шаг 1/4: Извлечение и классификация требований ТУ...' },
  { threshold: 50, text: 'Шаг 2/4: Семантический поиск релевантных пунктов в ЗнП...' },
  { threshold: 80, text: 'Шаг 3/4: Сверка инженерных параметров (мощность, напряжение, категории)...' },
  { threshold: 96, text: 'Шаг 4/4: Валидация точности цитат и формирование заключений ИИ...' },
]

const DEMO_PAYLOAD = {
  tu_requirements: [
    {
      id: 'demo-tu-1',
      clause_number: 'п. 1.1',
      category: 'voltage',
      text: 'Класс напряжения электрических сетей в точке технологического присоединения составляет 10 кВ.',
    },
    {
      id: 'demo-tu-2',
      clause_number: 'п. 2.4',
      category: 'power',
      text: 'Максимальная мощность присоединяемых энергопринимающих устройств заявителя составляет 150 кВт (II категория надёжности).',
    },
    {
      id: 'demo-tu-3',
      clause_number: 'п. 4.1',
      category: 'metering',
      text: 'На границе балансовой принадлежности установить трёхфазный прибор учёта электрической энергии класса точности не ниже 0.5S.',
    },
  ],
  znp_clauses: [
    {
      id: 'demo-znp-1',
      clause_number: 'Раздел 2.1',
      text: 'Присоединение проектируемой трансформаторной подстанции выполнить от сети 10 кВ.',
    },
    {
      id: 'demo-znp-2',
      clause_number: 'Раздел 3.2',
      text: 'Расчётная максимальная мощность энергопринимающих устройств по заданию составляет 100 кВт по II категории надёжности.',
    },
  ],
}

function HighlightedQuote({ text, quote }: { text: string; quote?: string | null }) {
  const needle = (quote ?? '').trim()
  if (!needle) return <>{text}</>
  const index = text.toLocaleLowerCase().indexOf(needle.toLocaleLowerCase())
  if (index < 0) return <>{text}</>
  return (
    <>
      {text.slice(0, index)}
      <mark style={{ background: '#fde68a', padding: 0 }}>
        {text.slice(index, index + needle.length)}
      </mark>
      {text.slice(index + needle.length)}
    </>
  )
}

function SourceFragment({
  title,
  text,
  quote,
  page,
  fragmentId,
}: {
  title: string
  text?: string | null
  quote?: string | null
  page?: number | null
  fragmentId?: string | null
}) {
  return (
    <div
      style={{
        border: '1px solid #e2e8f0',
        borderRadius: 10,
        padding: 14,
        background: '#fff',
      }}
    >
      <div className="horizontal" style={{ justifyContent: 'space-between', gap: 8 }}>
        <b style={{ fontSize: 13 }}>{title}</b>
        <small className="muted">
          {page ? `стр. ${page}` : 'страница не определена'}
          {fragmentId ? ` · фрагмент ${fragmentId.slice(0, 8)}` : ''}
        </small>
      </div>
      {text ? (
        <p
          style={{
            margin: '8px 0 0',
            whiteSpace: 'pre-wrap',
            fontSize: 13,
            maxHeight: 220,
            overflow: 'auto',
          }}
        >
          <HighlightedQuote text={text} quote={quote} />
        </p>
      ) : (
        <p className="muted" style={{ margin: '8px 0 0', fontSize: 13 }}>
          Исходный фрагмент не сохранён (старый результат или демо-набор).
        </p>
      )}
    </div>
  )
}

export function RequirementsPage() {
  const [projects, setProjects] = useState<Project[]>([])
  const [selectedProjectId, setSelectedProjectId] = useState('')
  const [docs, setDocs] = useState<Document[]>([])
  const [tuDocId, setTuDocId] = useState('')
  const [znpDocId, setZnpDocId] = useState('')

  const [items, setItems] = useState<RequirementCheckItem[]>([])
  const [stats, setStats] = useState<{
    fulfilled: number
    missing: number
    needs_review: number
    total: number
  }>({
    fulfilled: 0,
    missing: 0,
    needs_review: 0,
    total: 0,
  })
  const [traceInfo, setTraceInfo] = useState<CheckRequirementsResponse['trace_info']>(null)
  const [statusFilter, setStatusFilter] = useState<'all' | RequirementStatus>('all')
  const [categoryFilter, setCategoryFilter] = useState<'all' | RequirementCategory>('all')
  const [searchQuery, setSearchQuery] = useState('')
  const [comments, setComments] = useState<Record<string, string>>({})
  const [isDemoMode, setIsDemoMode] = useState(false)
  const [forceReanalyze, setForceReanalyze] = useState(false)
  const [busy, setBusy] = useState(false)
  const [taskProgress, setTaskProgress] = useState(0)
  const [error, setError] = useState('')

  useEffect(() => {
    request<Project[]>('/projects')
      .then(setProjects)
      .catch((e: Error) => setError(e.message))
  }, [])

  // Анимация полосы выполнения во время работы ИИ-агента
  useEffect(() => {
    if (!busy) return
    setTaskProgress(8)
    const timer = window.setInterval(() => {
      setTaskProgress((prev) => {
        if (prev >= 92) return prev
        const step = prev < 40 ? 9 : prev < 75 ? 5 : 2
        return Math.min(92, prev + step)
      })
    }, 280)
    return () => window.clearInterval(timer)
  }, [busy])

  useEffect(() => {
    if (!selectedProjectId) {
      setDocs([])
      setTuDocId('')
      setZnpDocId('')
      setItems([])
      setTraceInfo(null)
      return
    }
    setError('')
    setIsDemoMode(false)

    request<Document[]>(`/projects/${selectedProjectId}/documents`)
      .then((loadedDocs) => {
        setDocs(loadedDocs)
        const tu = loadedDocs.find((d) => d.kind === 'tu')
        const znp = loadedDocs.find((d) => d.kind === 'znp')
        setTuDocId(tu?.id ?? '')
        setZnpDocId(znp?.id ?? '')
      })
      .catch((e: Error) => setError(e.message))

    request<CheckRequirementsResponse>(`/requirements/projects/${selectedProjectId}`)
      .then((res) => applyResponse(res))
      .catch((e: Error) => setError(e.message))
  }, [selectedProjectId])

  function applyResponse(res: CheckRequirementsResponse) {
    setItems(res.checked_requirements)
    setStats({
      fulfilled: res.stats.fulfilled ?? 0,
      missing: res.stats.missing ?? 0,
      needs_review: res.stats.needs_review ?? 0,
      total: res.stats.total ?? res.checked_requirements.length,
    })
    if (res.trace_info) {
      setTraceInfo(res.trace_info)
    } else {
      setTraceInfo(null)
    }
    const initialComments: Record<string, string> = {}
    for (const item of res.checked_requirements) {
      if (item.human_comment) {
        initialComments[item.requirement.id] = item.human_comment
      }
    }
    setComments((prev) => ({ ...prev, ...initialComments }))
  }

  function recomputeStats(list: RequirementCheckItem[]) {
    setStats({
      fulfilled: list.filter((i) => i.status === 'fulfilled').length,
      missing: list.filter((i) => i.status === 'missing').length,
      needs_review: list.filter((i) => i.status === 'needs_review').length,
      total: list.length,
    })
  }

  async function runProjectAnalysis() {
    if (!selectedProjectId) return
    setBusy(true)
    setError('')
    setIsDemoMode(false)
    try {
      const payload: Record<string, unknown> = {
        project_id: selectedProjectId,
        force: forceReanalyze,
      }
      if (tuDocId) payload.tu_document_id = tuDocId
      if (znpDocId) payload.znp_document_id = znpDocId

      const res = await request<CheckRequirementsResponse>(
        '/requirements/analyze',
        json('POST', payload)
      )
      setTaskProgress(100)
      applyResponse(res)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function runDemoAnalysis() {
    setBusy(true)
    setError('')
    setIsDemoMode(true)
    try {
      const res = await request<CheckRequirementsResponse>(
        '/requirements/analyze',
        json('POST', DEMO_PAYLOAD)
      )
      setTaskProgress(100)
      applyResponse(res)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function handleConfirm(item: RequirementCheckItem, newStatus: RequirementStatus) {
    const commentText = comments[item.requirement.id] ?? item.human_comment ?? ''
    setError('')

    if (isDemoMode || !selectedProjectId) {
      const updatedList = items.map((it) =>
        it.id === item.id
          ? {
              ...it,
              status: newStatus,
              is_confirmed_by_human: true,
              human_override_status: newStatus,
              human_comment: commentText || null,
            }
          : it
      )
      setItems(updatedList)
      recomputeStats(updatedList)
      return
    }

    try {
      const updated = await request<RequirementVerification>(
        `/requirements/projects/${selectedProjectId}/confirm`,
        json('POST', {
          requirement_id: item.requirement.id,
          status: newStatus,
          comment: commentText || null,
        })
      )
      const updatedList = items.map((it) =>
        it.requirement.id === item.requirement.id
          ? {
              ...it,
              status: updated.human_override_status ?? newStatus,
              is_confirmed_by_human: updated.is_confirmed_by_human,
              human_override_status: updated.human_override_status,
              human_comment: updated.human_comment,
            }
          : it
      )
      setItems(updatedList)
      recomputeStats(updatedList)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const tuDocs = docs.filter((d) => d.kind === 'tu')
  const znpDocs = docs.filter((d) => d.kind === 'znp')
  const selectedProject = projects.find((p) => p.id === selectedProjectId)
  const isApproved = selectedProject?.state === 'approved'

  // Метрики для полосок прогресса
  const confirmedCount = items.filter((i) => i.is_confirmed_by_human).length
  const reviewProgressPercent = stats.total > 0 ? Math.round((confirmedCount / stats.total) * 100) : 0
  const fulfilledPercent = stats.total > 0 ? Math.round((stats.fulfilled / stats.total) * 100) : 0
  const needsReviewPercent = stats.total > 0 ? Math.round((stats.needs_review / stats.total) * 100) : 0
  const missingPercent = stats.total > 0 ? Math.max(0, 100 - fulfilledPercent - needsReviewPercent) : 0

  const currentStageLabel =
    AI_PROGRESS_STAGES.find((s) => taskProgress <= s.threshold)?.text ??
    AI_PROGRESS_STAGES[AI_PROGRESS_STAGES.length - 1].text

  const filteredItems = items.filter((item) => {
    const matchesStatus = statusFilter === 'all' ? true : item.status === statusFilter
    const matchesCategory =
      categoryFilter === 'all' ? true : item.requirement.category === categoryFilter
    const q = searchQuery.trim().toLowerCase()
    const matchesSearch =
      !q ||
      item.requirement.text.toLowerCase().includes(q) ||
      item.requirement.clause_number.toLowerCase().includes(q) ||
      (item.matched_clause?.text.toLowerCase().includes(q) ?? false) ||
      (item.reasoning?.toLowerCase().includes(q) ?? false)
    return matchesStatus && matchesCategory && matchesSearch
  })

  return (
    <>
      <h1>Сопоставление ТУ и ЗнП</h1>
      <p className="muted">
        Семантический и параметрический ИИ-анализ соответствия Задания на проектирование (ЗнП)
        требованиям Технических условий (ТУ) с проверкой цитат.
      </p>

      <section className="panel stack">
        <div className="horizontal" style={{ justifyContent: 'space-between' }}>
          <label style={{ flex: 1 }}>
            Проект
            <select
              value={selectedProjectId}
              onChange={(e) => setSelectedProjectId(e.target.value)}
            >
              <option value="">Выберите проект...</option>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} ({p.state === 'draft' ? 'В работе' : 'Утверждён'})
                </option>
              ))}
            </select>
          </label>

          {selectedProjectId && (
            <>
              <label style={{ flex: 1 }}>
                Документ ТУ
                <select value={tuDocId} onChange={(e) => setTuDocId(e.target.value)}>
                  <option value="">Автовыбор ТУ ({tuDocs.length})</option>
                  {tuDocs.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.filename}
                    </option>
                  ))}
                </select>
              </label>

              <label style={{ flex: 1 }}>
                Документ ЗнП
                <select value={znpDocId} onChange={(e) => setZnpDocId(e.target.value)}>
                  <option value="">Автовыбор ЗнП ({znpDocs.length})</option>
                  {znpDocs.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.filename}
                    </option>
                  ))}
                </select>
              </label>
            </>
          )}
        </div>

        {selectedProjectId && (!tuDocs.length || !znpDocs.length) && (
          <p className="muted" style={{ margin: 0 }}>
            Для запуска проектной проверки загрузите оба документа с типами <b>ТУ (tu)</b> и{' '}
            <b>ЗнП (znp)</b> на вкладке «Документы».
          </p>
        )}

        <div className="horizontal">
          <button
            onClick={runProjectAnalysis}
            disabled={busy || !selectedProjectId || !tuDocs.length || !znpDocs.length || isApproved}
          >
            {busy ? 'Выполняется ИИ-сверка...' : 'Запустить сверку проекта'}
          </button>

          <button className="secondary" onClick={runDemoAnalysis} disabled={busy}>
            Демо-проверка (на тестовом наборе)
          </button>

          <label
            className="horizontal"
            style={{ gap: 8, width: 'auto', fontSize: 13 }}
            title="Сбросить решения инженера и замечания, затем пересчитать всё заново"
          >
            <input
              type="checkbox"
              checked={forceReanalyze}
              onChange={(e) => setForceReanalyze(e.target.checked)}
              disabled={busy || isApproved}
              style={{ width: 'auto', margin: 0 }}
            />
            Полный пересчёт (сбросить решения инженера)
          </label>
        </div>

        {/* ПОЛОСА ПРОГРЕССА ВЫПОЛНЕНИЯ ЗАДАЧИ ИИ-АГЕНТОМ */}
        {busy && (
          <div
            style={{
              background: '#f8fafc',
              border: '1px solid #cbd5e1',
              borderRadius: 10,
              padding: '14px 16px',
            }}
          >
            <div
              className="horizontal"
              style={{ justifyContent: 'space-between', marginBottom: 8 }}
            >
              <b style={{ fontSize: 14, color: '#186c52' }}>{currentStageLabel}</b>
              <span style={{ fontWeight: 700, fontSize: 14 }}>{taskProgress}%</span>
            </div>
            <div
              style={{
                width: '100%',
                height: 10,
                background: '#e2e8f0',
                borderRadius: 999,
                overflow: 'hidden',
              }}
            >
              <div
                style={{
                  width: `${taskProgress}%`,
                  height: '100%',
                  background: 'linear-gradient(90deg, #186c52 0%, #10b981 100%)',
                  transition: 'width 0.25s ease',
                }}
              />
            </div>
          </div>
        )}
      </section>

      {error && <p className="error">{error}</p>}

      {items.length > 0 && (
        <section className="panel stack">
          <div className="horizontal" style={{ justifyContent: 'space-between' }}>
            <h2 style={{ margin: 0 }}>
              Сводка и прогресс проверки {isDemoMode ? '(Демо-режим)' : ''}
            </h2>
            {traceInfo?.execution_time_ms !== undefined && (
              <small>Время анализа ИИ: {traceInfo.execution_time_ms} мс</small>
            )}
          </div>

          {traceInfo?.analysis && (
            <div
              className="horizontal"
              style={{ flexWrap: 'wrap', gap: 14, fontSize: 13 }}
            >
              <span>Создано: <b>{traceInfo.analysis.created}</b></span>
              <span>Обновлено: <b>{traceInfo.analysis.updated}</b></span>
              <span style={{ color: '#065f46' }}>
                Сохранено решений инженера: <b>{traceInfo.analysis.preserved_human}</b>
              </span>
              <span style={{ color: '#92400e' }}>
                Сохранено замечаний: <b>{traceInfo.analysis.preserved_findings}</b>
              </span>
              <span>Удалено устаревших: <b>{traceInfo.analysis.removed}</b></span>
              {traceInfo.analysis.force && (
                <span style={{ color: '#991b1b', fontWeight: 600 }}>Полный пересчёт</span>
              )}
            </div>
          )}

          {traceInfo?.embedding && !traceInfo.embedding.enabled && (
            <small className="muted">
              Семантический канал отключён: {traceInfo.embedding.reason ?? 'лексический режим'}
            </small>
          )}

          {/* ДВЕ ПОЛОСЫ ПРОГРЕССА: ВЕРИФИКАЦИЯ ИНЖЕНЕРОМ И СООТВЕТСТВИЕ ТРЕБОВАНИЙ */}
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))',
              gap: 16,
            }}
          >
            {/* 1. Прогресс проверки человеком (Human-in-the-Loop) */}
            <div
              style={{
                border: '1px solid #e2e8f0',
                borderRadius: 10,
                padding: 14,
                background: '#f8fafc',
              }}
            >
              <div
                className="horizontal"
                style={{ justifyContent: 'space-between', marginBottom: 6 }}
              >
                <span style={{ fontSize: 13, fontWeight: 600 }}>
                  Прогресс согласования инженером
                </span>
                <b style={{ fontSize: 14, color: reviewProgressPercent === 100 ? '#065f46' : '#1a2533' }}>
                  {confirmedCount} из {stats.total} ({reviewProgressPercent}%)
                </b>
              </div>
              <div
                style={{
                  width: '100%',
                  height: 10,
                  background: '#e2e8f0',
                  borderRadius: 999,
                  overflow: 'hidden',
                }}
              >
                <div
                  style={{
                    width: `${reviewProgressPercent}%`,
                    height: '100%',
                    background: reviewProgressPercent === 100 ? '#059669' : '#0284c7',
                    transition: 'width 0.3s ease',
                  }}
                />
              </div>
              <small style={{ display: 'block', marginTop: 6 }}>
                {reviewProgressPercent === 100
                  ? 'Все пункты проверены инженером — раздел готов к утверждению.'
                  : `Осталось проверить вручную: ${stats.total - confirmedCount} п.`}
              </small>
            </div>

            {/* 2. Мультисегментная полоса полноты выполнения ТУ в ЗнП */}
            <div
              style={{
                border: '1px solid #e2e8f0',
                borderRadius: 10,
                padding: 14,
                background: '#f8fafc',
              }}
            >
              <div
                className="horizontal"
                style={{ justifyContent: 'space-between', marginBottom: 6 }}
              >
                <span style={{ fontSize: 13, fontWeight: 600 }}>
                  Полнота отражения требований ТУ в ЗнП
                </span>
                <b style={{ fontSize: 14, color: '#065f46' }}>{fulfilledPercent}% учтено</b>
              </div>
              <div
                style={{
                  display: 'flex',
                  width: '100%',
                  height: 10,
                  background: '#e2e8f0',
                  borderRadius: 999,
                  overflow: 'hidden',
                }}
              >
                <div
                  title={`Отражено: ${stats.fulfilled}`}
                  style={{
                    width: `${fulfilledPercent}%`,
                    background: '#10b981',
                    transition: 'width 0.3s ease',
                  }}
                />
                <div
                  title={`Спорные / На проверку: ${stats.needs_review}`}
                  style={{
                    width: `${needsReviewPercent}%`,
                    background: '#f59e0b',
                    transition: 'width 0.3s ease',
                  }}
                />
                <div
                  title={`Пропущено: ${stats.missing}`}
                  style={{
                    width: `${missingPercent}%`,
                    background: '#ef4444',
                    transition: 'width 0.3s ease',
                  }}
                />
              </div>
              <div className="horizontal" style={{ marginTop: 6, gap: 14 }}>
                <small style={{ color: '#065f46' }}>● Учтено: {stats.fulfilled}</small>
                <small style={{ color: '#92400e' }}>● Спорные: {stats.needs_review}</small>
                <small style={{ color: '#991b1b' }}>● Пропущено: {stats.missing}</small>
              </div>
            </div>
          </div>

          {/* ФИЛЬТРЫ ПО СТАТУСУ, КАТЕГОРИИ И ТЕКСТОВЫЙ ПОИСК */}
          <div
            className="horizontal"
            style={{ justifyContent: 'space-between', marginTop: 4 }}
          >
            <div className="horizontal">
              <button
                className={statusFilter === 'all' ? '' : 'secondary'}
                onClick={() => setStatusFilter('all')}
              >
                Все ({stats.total})
              </button>
              <button
                className={statusFilter === 'fulfilled' ? '' : 'secondary'}
                onClick={() => setStatusFilter('fulfilled')}
              >
                Отражено ({stats.fulfilled})
              </button>
              <button
                className={statusFilter === 'needs_review' ? '' : 'secondary'}
                onClick={() => setStatusFilter('needs_review')}
              >
                Спорные / На проверку ({stats.needs_review})
              </button>
              <button
                className={statusFilter === 'missing' ? '' : 'secondary'}
                onClick={() => setStatusFilter('missing')}
              >
                Пропущено ({stats.missing})
              </button>
            </div>

            <div className="horizontal">
              <select
                value={categoryFilter}
                onChange={(e) =>
                  setCategoryFilter(e.target.value as 'all' | RequirementCategory)
                }
              >
                <option value="all">Все категории</option>
                {Object.entries(CATEGORY_LABELS).map(([key, label]) => (
                  <option key={key} value={key}>
                    {label}
                  </option>
                ))}
              </select>

              <input
                placeholder="Поиск по тексту или пункту..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
              />
            </div>
          </div>
        </section>
      )}

      {items.length > 0 && filteredItems.length === 0 && (
        <section className="panel">
          <p className="muted" style={{ margin: 0 }}>
            По выбранным фильтрам требований не найдено.
          </p>
        </section>
      )}

      {filteredItems.map((item) => {
        const meta = STATUS_META[item.status]
        const confPercent = Math.round(item.confidence_score * 100)
        const confColor =
          confPercent >= 75 ? '#10b981' : confPercent >= 55 ? '#f59e0b' : '#ef4444'

        return (
          <section key={item.id} className="panel stack">
            <div className="horizontal" style={{ justifyContent: 'space-between' }}>
              <div className="horizontal">
                <b style={{ fontSize: 16 }}>{item.requirement.clause_number}</b>
                <span
                  style={{
                    fontSize: 12,
                    padding: '4px 10px',
                    borderRadius: 6,
                    background: '#eef2f6',
                    color: '#334155',
                    fontWeight: 600,
                  }}
                >
                  {CATEGORY_LABELS[item.requirement.category] ?? item.requirement.category}
                </span>
                <span
                  style={{
                    fontSize: 12,
                    padding: '4px 10px',
                    borderRadius: 6,
                    background: meta.bg,
                    color: meta.color,
                    border: `1px solid ${meta.border}`,
                    fontWeight: 600,
                  }}
                >
                  {meta.label}
                </span>
                {item.method && (
                  <span
                    title={
                      item.channels?.length
                        ? `Каналы поиска: ${item.channels.join(', ')}`
                        : undefined
                    }
                    style={{
                      fontSize: 12,
                      padding: '4px 10px',
                      borderRadius: 6,
                      background: item.method === 'llm' ? '#eef2ff' : '#f1f5f9',
                      color: item.method === 'llm' ? '#3730a3' : '#475569',
                      fontWeight: 600,
                    }}
                  >
                    {item.method === 'llm' ? 'ИИ-агент' : 'Детерминированный расчёт'}
                  </span>
                )}
              </div>

              <div className="horizontal">
                {/* МИНИ-ПОЛОСКА УВЕРЕННОСТИ ИИ */}
                <div className="horizontal" style={{ gap: 6 }}>
                  <small>
                    Уверенность ИИ: <b>{confPercent}%</b>
                  </small>
                  <div
                    style={{
                      width: 60,
                      height: 6,
                      background: '#e2e8f0',
                      borderRadius: 999,
                      overflow: 'hidden',
                    }}
                  >
                    <div
                      style={{
                        width: `${confPercent}%`,
                        height: '100%',
                        background: confColor,
                      }}
                    />
                  </div>
                </div>

                <small
                  style={{
                    padding: '3px 8px',
                    borderRadius: 6,
                    background: item.is_confirmed_by_human ? '#dcfce7' : '#f1f5f9',
                    color: item.is_confirmed_by_human ? '#166534' : '#475569',
                    fontWeight: item.is_confirmed_by_human ? 600 : 400,
                  }}
                >
                  {item.is_confirmed_by_human ? '✓ Проверено инженером' : 'Ожидает решения'}
                </small>
              </div>
            </div>

            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))',
                gap: 16,
              }}
            >
              <div
                style={{
                  border: '1px solid #e2e8f0',
                  borderRadius: 10,
                  padding: 16,
                  background: '#fafbfc',
                }}
              >
                <h3 style={{ marginTop: 0 }}>
                  Требование ТУ ({item.requirement.clause_number})
                </h3>
                {item.tu_page != null && <small className="muted">Страница {item.tu_page}</small>}
                <p style={{ margin: '8px 0' }}>{item.requirement.text}</p>
                {item.tu_quote && (
                  <blockquote>
                    <small>Ключевая цитата ТУ:</small>
                    <br />«{item.tu_quote}»
                  </blockquote>
                )}
              </div>

              <div
                style={{
                  border: '1px solid #e2e8f0',
                  borderRadius: 10,
                  padding: 16,
                  background: '#fafbfc',
                }}
              >
                <h3 style={{ marginTop: 0 }}>
                  Сопоставленный пункт ЗнП{' '}
                  {item.matched_clause ? `(${item.matched_clause.clause_number})` : ''}
                </h3>
                {item.znp_page != null && <small className="muted">Страница {item.znp_page}</small>}
                {item.matched_clause ? (
                  <>
                    <p style={{ margin: '8px 0' }}>{item.matched_clause.text}</p>
                    {item.znp_quote && (
                      <blockquote>
                        <small>Подтверждающая цитата ЗнП:</small>
                        <br />«{item.znp_quote}»
                      </blockquote>
                    )}
                  </>
                ) : (
                  <p className="muted">
                    Соответствующий пункт в Задании на проектирование не найден.
                  </p>
                )}
              </div>
            </div>

            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))',
                gap: 16,
              }}
            >
              <SourceFragment
                title="Исходный фрагмент ТУ"
                text={item.tu_fragment_text ?? item.requirement.fragment_text}
                quote={item.tu_quote}
                page={item.tu_page}
                fragmentId={item.tu_fragment_id ?? item.requirement.fragment_id}
              />
              <SourceFragment
                title="Исходный фрагмент ЗнП"
                text={item.znp_fragment_text ?? item.matched_clause?.fragment_text}
                quote={item.znp_quote}
                page={item.znp_page}
                fragmentId={item.znp_fragment_id ?? item.matched_clause?.fragment_id}
              />
            </div>

            <div
              style={{
                background: '#f8fafc',
                borderLeft: '4px solid #186c52',
                padding: '10px 14px',
                borderRadius: 6,
              }}
            >
              <b>Заключение ИИ-агента:</b> {item.reasoning || 'Нет дополнительного описания.'}
            </div>

            {!isApproved && (
              <div className="horizontal" style={{ marginTop: 4 }}>
                <input
                  style={{ flex: 1 }}
                  placeholder="Комментарий инженера (необязательно)..."
                  value={comments[item.requirement.id] ?? ''}
                  onChange={(e) =>
                    setComments((prev) => ({
                      ...prev,
                      [item.requirement.id]: e.target.value,
                    }))
                  }
                />
                <button onClick={() => handleConfirm(item, 'fulfilled')}>
                  Подтвердить соответствие
                </button>
                <button
                  className="secondary"
                  onClick={() => handleConfirm(item, 'needs_review')}
                >
                  На уточнение
                </button>
                <button
                  className="secondary"
                  style={{ background: '#fee2e2', color: '#991b1b' }}
                  onClick={() => handleConfirm(item, 'missing')}
                >
                  Зафиксировать пропуск
                </button>
              </div>
            )}
          </section>
        )
      })}

      {traceInfo && (
        <section className="panel">
          <details>
            <summary style={{ cursor: 'pointer', fontWeight: 600 }}>
              Журнал трассировки ИИ-агента (время выполнения: {traceInfo.execution_time_ms ?? 0} мс)
            </summary>
            <pre style={{ marginTop: 12, maxHeight: 360, overflow: 'auto' }}>
              {JSON.stringify(traceInfo, null, 2)}
            </pre>
          </details>
        </section>
      )}
    </>
  )
}