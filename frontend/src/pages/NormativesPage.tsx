import { useEffect, useRef, useState } from 'react'
import { Normative, Project, Document, json, request } from '../api/client'

type CheckedStandard = {
  code: string
  reference_kind: string
  status: string
  registry_status: string | null
  replacement: string | null
  source_url: string | null
  checked_at: string | null
  pages: number[]
  reason: string | null
  source: string | null
}

type VerificationReport = {
  document_id: string
  total: number
  active: number
  obsolete: number
  unknown: number
  findings_created: number
  findings_already_existing: number
  checked: CheckedStandard[]
}

type VerificationBatch = VerificationReport & {
  document_total: number
  offset: number
  next_offset: number | null
}

type VerificationProgress = {
  checked: number
  total: number
}

const CHECK_BATCH_SIZE = 5

export function NormativesPage() {
  const [items, setItems] = useState<Normative[]>([])
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  
  const [editId, setEditId] = useState<string | null>(null)
  const [code, setCode] = useState('')
  const [title, setTitle] = useState('')
  const [status, setStatus] = useState<'active' | 'obsolete' | 'unknown'>('unknown')
  const [replacement, setReplacement] = useState('')
  const [sourceUrl, setSourceUrl] = useState('')
  const [error, setError] = useState('')

  const [projects, setProjects] = useState<Project[]>([])
  const [documents, setDocuments] = useState<Document[]>([])
  const [selectedProjectId, setSelectedProjectId] = useState('')
  const [selectedDocId, setSelectedDocId] = useState('')
  
  const [isExtracting, setIsExtracting] = useState(false)
  const [isChecking, setIsChecking] = useState(false)
  const [report, setReport] = useState<VerificationReport | null>(null)
  const [checkProgress, setCheckProgress] = useState<VerificationProgress | null>(null)
  const checkResume = useRef<{docId: string, offset: number, report: VerificationReport} | null>(null)
  const checkController = useRef<AbortController | null>(null)

  useEffect(() => {
    return () => checkController.current?.abort()
  }, [])

  useEffect(() => {
    checkResume.current = null
    setCheckProgress(null)
    setReport(null)
  }, [selectedDocId])

  const loadNormatives = () => {
    const params = new URLSearchParams()
    if (search) params.append('search', search)
    if (statusFilter) params.append('status', statusFilter)
    request<Normative[]>(`/normatives?${params.toString()}`)
      .then(setItems)
      .catch(e => setError(e.message))
  }

  useEffect(() => {
    request<Project[]>('/projects').then(setProjects).catch(e => setError(e.message))
  }, [])

  useEffect(() => { loadNormatives() }, [search, statusFilter])

  useEffect(() => {
    if (selectedProjectId) {
      request<Document[]>(`/projects/${selectedProjectId}/documents`)
        .then(setDocuments)
        .catch(() => setDocuments([]))
      setSelectedDocId('')
      setReport(null)
      setCheckProgress(null)
    } else {
      setDocuments([])
    }
  }, [selectedProjectId])

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError('')
    try {
      if (editId) {
        await request(`/normatives/${editId}`, json('PATCH', { 
          title, 
          status, 
          replacement: replacement || null, 
          source_url: sourceUrl || null 
        }))
        setEditId(null)
      } else {
        await request('/normatives', json('POST', { 
          code, 
          title, 
          status, 
          replacement: replacement || null, 
          source_url: sourceUrl || null 
        }))
      }
      setCode('')
      setTitle('')
      setReplacement('')
      setSourceUrl('')
      setStatus('unknown')
      loadNormatives()
    } catch (e: any) {
      setError(e.message || 'Ошибка сохранения')
    }
  }

  function startEdit(item: Normative) {
    setEditId(item.id)
    setCode(item.code)
    setTitle(item.title)
    setStatus(item.status)
    setReplacement(item.replacement || '')
    setSourceUrl(item.source_url || '')
    document.getElementById('normative-editor')?.scrollIntoView({behavior: 'smooth', block: 'start'})
  }

  async function handleDelete(id: string, normCode: string) {
    if (!confirm(`Удалить норматив ${normCode}?`)) return
    try {
      await request(`/normatives/${id}`, { method: 'DELETE' })
      loadNormatives()
    } catch (e: any) {
      alert(e.message || 'Не удалось удалить')
    }
  }

  async function handleExtract() {
    if (!selectedDocId) return
    setIsExtracting(true)
    setError('')
    setReport(null)
    setCheckProgress(null)
    // Re-extraction may change the set of codes, so old offsets cannot resume.
    checkResume.current = null
    try {
      await request(`/documents/${selectedDocId}/extract-facts`, json('POST', {}))
      alert('Ссылки извлечены! Теперь можно проверить актуальность.')
    } catch (e: any) {
      setError(e.message || 'Ошибка при извлечении')
    } finally {
      setIsExtracting(false)
    }
  }

  async function handleCheck() {
    if (!selectedDocId || isChecking) return
    const docId = selectedDocId
    const previous = checkResume.current?.docId === docId ? checkResume.current : null
    let offset = previous?.offset ?? 0
    let aggregate: VerificationReport = previous?.report ?? {
      document_id: docId,
      total: 0,
      active: 0,
      obsolete: 0,
      unknown: 0,
      findings_created: 0,
      findings_already_existing: 0,
      checked: []
    }
    const controller = new AbortController()
    checkController.current = controller
    setIsChecking(true)
    setError('')
    if (!previous) {
      setReport(null)
      setCheckProgress(null)
    }
    try {
      while (true) {
        const batch = await request<VerificationBatch>(
          `/documents/${docId}/check-normatives?offset=${offset}&limit=${CHECK_BATCH_SIZE}`,
          {method: 'POST', signal: controller.signal}
        )
        // Never silently skip, repeat or miscount codes when the API contract changes.
        if (
          batch.document_id !== docId || batch.offset !== offset ||
          batch.total !== batch.checked.length ||
          !Number.isInteger(batch.document_total) ||
          batch.document_total < offset + batch.checked.length ||
          (batch.next_offset !== null &&
            (batch.next_offset !== offset + batch.checked.length ||
             batch.next_offset <= offset || batch.next_offset >= batch.document_total))
        ) {
          throw new Error('Некорректный ответ проверки. Частичные результаты сохранены.')
        }
        aggregate = {
          document_id: docId,
          total: aggregate.total + batch.total,
          active: aggregate.active + batch.active,
          obsolete: aggregate.obsolete + batch.obsolete,
          unknown: aggregate.unknown + batch.unknown,
          findings_created: aggregate.findings_created + batch.findings_created,
          findings_already_existing:
            aggregate.findings_already_existing + batch.findings_already_existing,
          checked: [...aggregate.checked, ...batch.checked]
        }
        setReport(aggregate)
        setCheckProgress({checked: aggregate.total, total: batch.document_total})
        if (batch.next_offset === null) {
          if (aggregate.total !== batch.document_total) {
            throw new Error('Количество проверенных нормативов не совпадает с документом.')
          }
          checkResume.current = null
          loadNormatives()
          break
        }
        offset = batch.next_offset
        checkResume.current = {docId, offset, report: aggregate}
      }
    } catch (error) {
      const stopped = controller.signal.aborted
      const message = stopped
        ? 'Проверка остановлена.'
        : error instanceof Error ? error.message : 'Ошибка при проверке'
      setError(
        aggregate.total > 0
          ? `${message} Проверено ${aggregate.total} нормативов. Нажмите «Продолжить проверку», чтобы обработать оставшиеся.`
          : message
      )
      loadNormatives()
    } finally {
      if (checkController.current === controller) checkController.current = null
      setIsChecking(false)
    }
  }

  const statusLabel = (value: string) => {
    if (value === 'active') return 'Действует'
    if (value === 'obsolete') return 'Не действует / заменён'
    return 'Не подтверждено'
  }

  return (
    <div className="normatives-workspace">
      <header className="page-heading">
        <div className="eyebrow">МОДУЛЬ 02 / НОРМАТИВНЫЙ КОНТРОЛЬ</div>
        <h1>ГОСТ и НТД<span className="heading-dot">.</span></h1>
        <p className="muted">Извлечение ссылок из документов, проверка по Росстандарту и
          экспертные замечания с исходными цитатами.</p>
      </header>

      {/* СЕКЦИЯ АНАЛИЗА ДОКУМЕНТА */}
      <section className="panel stack normative-analysis">
        <div className="panel-heading">
          <div><div className="eyebrow">01 / ДОКУМЕНТ</div><h2>Анализ нормативных ссылок</h2></div>
          <span className="panel-counter">По 5 нормативов за запрос</span>
        </div>
        
        <div className="normative-chooser">
          <label>Проект
          <select 
            value={selectedProjectId} 
            onChange={e => setSelectedProjectId(e.target.value)} 
            disabled={isExtracting || isChecking}
          >
            <option value="">Выберите проект...</option>
            {projects.map(p => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select></label>
          <label>Документ
          <select 
            value={selectedDocId} 
            onChange={e => setSelectedDocId(e.target.value)} 
            disabled={!selectedProjectId || isExtracting || isChecking}
          >
            <option value="">Выберите документ...</option>
            {documents.map(d => (
              <option key={d.id} value={d.id}>{d.filename}</option>
            ))}
          </select></label>
        </div>

        <div className="normative-actions">
          <button 
            onClick={handleExtract} 
            disabled={isExtracting || isChecking || !selectedDocId} 
            className="secondary"
          >
            {isExtracting ? 'Извлекаем...' : '1. Извлечь нормативные ссылки'}
          </button>
          
          <button 
            onClick={handleCheck} 
            disabled={isChecking || isExtracting || !selectedDocId} 

          >
            {isChecking ? 'Проверяем...' : checkResume.current?.docId === selectedDocId
              ? 'Продолжить проверку' : '2. Проверить актуальность'}
          </button>
        </div>

        {isChecking && (
          <button type="button" className="secondary"
            onClick={() => checkController.current?.abort()}>
            Остановить после текущей порции
          </button>
        )}

        {checkProgress && (
          <div className="normative-progress" role="status" aria-live="polite">
            <div className="normative-progress-top">
              <span>{isChecking ? 'Проверка Росстандарта' :
                checkProgress.checked === checkProgress.total ? 'Все нормативы обработаны' : 'Частичный результат'}</span>
              <strong>{checkProgress.checked} из {checkProgress.total}</strong>
            </div>
            <div className="normative-progress-bar" role="progressbar"
              aria-label="Проверенные нормативы" aria-valuemin={0}
              aria-valuemax={checkProgress.total} aria-valuenow={checkProgress.checked}>
              <span style={{width: (checkProgress.total ?
                (checkProgress.checked / checkProgress.total) * 100 : 100) + '%'}}/>
            </div>
            {!isChecking && checkProgress.checked < checkProgress.total &&
              <p className="normative-note">Проверенная часть сохранена, оставшиеся нормативы можно обработать позже.</p>}
          </div>
        )}

        {report && (
          <div>
            <div className="normative-metrics" aria-label="Сводка проверки">
              <div className="stat-card"><span>ПРОВЕРЕНО</span><strong>{report.total}</strong></div>
              <div className="stat-card"><span>ДЕЙСТВУЕТ</span><strong>{report.active}</strong></div>
              <div className="stat-card"><span>НЕ ДЕЙСТВУЕТ</span><strong>{report.obsolete}</strong></div>
              <div className="stat-card"><span>НЕ ПОДТВЕРЖДЕНО</span><strong>{report.unknown}</strong></div>
            </div>
            <div className="normative-table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Стандарт</th>
                    <th>Тип</th>
                    <th>Статус (Росстандарт)</th>
                    <th>Замена</th>
                    <th>Стр.</th>
                    <th>Источник</th>
                  </tr>
                </thead>
                <tbody>
                  {report.checked.map(item => (
                    <tr key={item.code} className={'normative-row-' + item.status}>
                      <td><strong>{item.code}</strong></td>
                      <td>{item.reference_kind === 'informative' ? 'Справочная' : 'Нормативная'}</td>
                      <td><span className={'normative-status ' + item.status}>
                        {item.registry_status || statusLabel(item.status)}</span></td>
                      <td>{item.replacement || '—'}</td>
                      <td>{item.pages.join(', ') || '—'}</td>
                      <td>
                        {item.source === 'rosstandart' && item.source_url ? (
                          <><a href={item.source_url} target="_blank" rel="noopener noreferrer">
                            Карточка Росстандарта</a>
                            <small style={{display: 'block'}}>{item.checked_at ?
                              new Date(item.checked_at).toLocaleDateString('ru-RU') : 'Дата не указана'}</small></>
                        ) : 'Не подтверждено'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            
            <p className="normative-note">
              Новых замечаний: {report.findings_created}. Замечания с цитатами доступны
              в экспертном отчёте. Локальный статус без ответа Росстандарта не является подтверждением.
            </p>
          </div>
        )}
      </section>

      {/* СЕКЦИЯ УПРАВЛЕНИЯ РЕЕСТРОМ */}
      <section className="panel stack" id="normative-editor">
        <div className="eyebrow">02 / ЛОКАЛЬНЫЙ СПРАВОЧНИК</div>
        <h2>{editId ? '✏️ Редактирование норматива' : '➕ Добавить норматив вручную'}</h2>
        
        <form onSubmit={handleSubmit} className="normative-edit-form">
          <label>Обозначение
            <input value={code} onChange={e => setCode(e.target.value)}
              placeholder="Например, ГОСТ 32144-2013" required disabled={!!editId}/>
          </label>
          <label>Наименование
            <input value={title} onChange={e => setTitle(e.target.value)}
              placeholder="Название документа" required/>
          </label>
          <label>Локальный статус
            <select value={status} onChange={e => setStatus(e.target.value as Normative['status'])}>
              <option value="unknown">Не подтверждено</option>
              <option value="active">Действует (справочно)</option>
              <option value="obsolete">Не действует (справочно)</option>
            </select>
          </label>
          <label>Заменяющий норматив
            <input value={replacement} onChange={e => setReplacement(e.target.value)}
              placeholder="Необязательно"/>
          </label>
          <label className="full-width">Ссылка на карточку
            <input value={sourceUrl} onChange={e => setSourceUrl(e.target.value)}
              type="url" placeholder="https://protect.gost.ru/…"/>
          </label>
          <div className="normative-actions full-width">
            <button type="submit">{editId ? 'Сохранить изменения' : 'Добавить'}</button>
            {editId && <button type="button" className="secondary" onClick={() => {
              setEditId(null)
              setCode('')
              setTitle('')
              setStatus('unknown')
              setReplacement('')
              setSourceUrl('')
            }}>Отмена</button>}
          </div>
        </form>
      </section>
      
      {error && <p className="error" role="alert">{error}</p>}
      
      {/* ТАБЛИЦА РЕЕСТРА */}
      <section className="panel">
        <div className="normative-registry-head">
          <div><div className="eyebrow">03 / РЕЕСТР</div>
            <h2>Локальный реестр</h2>
            <p className="muted">Справочные статусы не считаются проверенными без даты Росстандарта.</p>
          </div>
          <div className="normative-registry-search">
            <label>Поиск
            <input 
              value={search} 
              onChange={e => setSearch(e.target.value)} 
              placeholder="Поиск по коду или названию..." 
            /></label>
            <label>Статус
            <select 
              value={statusFilter} 
              onChange={e => setStatusFilter(e.target.value)} 
              
            >
              <option value="">Все статусы</option>
              <option value="active">Действует</option>
              <option value="obsolete">Устарел</option>
              <option value="unknown">Неизвестно</option>
            </select></label>
          </div>
        </div>
        
        <div className="normative-table-wrap">
        <table>
          <thead>
            <tr>
              <th>Норматив</th>
              <th>Название</th>
              <th>Статус</th>
              <th>Замена</th>
              <th>Официальное подтверждение</th>
              <th>Действия</th>
            </tr>
          </thead>
          <tbody>
            {items.map(i => (
              <tr key={i.id}>
                <td>{i.code}</td>
                <td>{i.title}</td>
                <td><span className={'normative-status ' + i.status}>{statusLabel(i.status)}</span></td>
                <td>{i.replacement ?? '—'}</td>
                <td>
                  {i.checked_at && i.registry_status
                    ? <><strong>{i.registry_status}</strong><small style={{display:'block'}}>
                        Росстандарт · {new Date(i.checked_at).toLocaleDateString('ru-RU')}</small></>
                    : <span className="muted">Только справочная запись</span>}
                </td>
                <td><div className="normative-mini-buttons">
                  <button type="button"
                    onClick={() => startEdit(i)} 
                    className="secondary"
                  >
                    Изменить
                  </button>
                  <button type="button"
                    onClick={() => handleDelete(i.id, i.code)} 
                    className="delete-action"
                  >
                    Удалить
                  </button>
                </div></td>
              </tr>
            ))}
          </tbody>
        </table></div>
        
        {!items.length && <p className="muted">Реестр пуст.</p>}
      </section>
    </div>
  )
}