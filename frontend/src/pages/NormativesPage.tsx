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
  const [status, setStatus] = useState<'active' | 'obsolete' | 'unknown'>('active')
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
    window.scrollTo({ top: 0, behavior: 'smooth' })
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
          <p role="status" aria-live="polite" className="muted">
            {isChecking ? 'Проверяем нормативы' : 'Проверка'}: {checkProgress.checked} из {checkProgress.total}.
            {checkProgress.checked < checkProgress.total ? ' Результат ниже частичный.' : ' Все нормативы обработаны.'}
          </p>
        )}

        {report && (
          <div>
            <p>
              <strong>Результат:</strong> Проверено: {report.total}. 
              Действующих: {report.active}, не действующих: {report.obsolete}, 
              не подтверждено: {report.unknown}. Новых замечаний: {report.findings_created}.
            </p>
            
            <div style={{ overflowX: 'auto' }}>
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
                    <tr 
                      key={item.code} 
                      style={{ 
                        backgroundColor: item.status === 'obsolete' ? '#fff3cd' 
                          : item.status === 'unknown' ? '#f8d7da' 
                          : 'transparent' 
                      }}
                    >
                      <td><strong>{item.code}</strong></td>
                      <td>{item.reference_kind === 'informative' ? 'Справочная' : 'Нормативная'}</td>
                      <td>{item.registry_status || statusLabel(item.status)}</td>
                      <td>{item.replacement || '—'}</td>
                      <td>{item.pages.join(', ') || '—'}</td>
                      <td>
                        {item.source_url ? (
                          <a href={item.source_url} target="_blank" rel="noopener noreferrer">
                            Карточка
                          </a>
                        ) : 'Не найдено'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            
            <p className="muted" style={{ marginTop: '10px' }}>
              Замечания о заменённых стандартах автоматически сохранены в разделе "Отчёты" этого проекта.
            </p>
          </div>
        )}
      </section>

      {/* СЕКЦИЯ УПРАВЛЕНИЯ РЕЕСТРОМ */}
      <section className="panel stack">
        <h2>{editId ? '✏️ Редактирование норматива' : '➕ Добавить норматив вручную'}</h2>
        
        <form onSubmit={handleSubmit} style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px' }}>
          <input 
            value={code} 
            onChange={e => setCode(e.target.value)} 
            placeholder="Обозначение (напр. ГОСТ 32144-2013)" 
            required 
            disabled={!!editId} 
          />
          <input 
            value={title} 
            onChange={e => setTitle(e.target.value)} 
            placeholder="Название" 
            required 
          />
          <select value={status} onChange={e => setStatus(e.target.value as any)}>
            <option value="active">Действует</option>
            <option value="obsolete">Устарел</option>
            <option value="unknown">Неизвестно</option>
          </select>
          <input 
            value={replacement} 
            onChange={e => setReplacement(e.target.value)} 
            placeholder="Замена (код, необязательно)" 
          />
          <input 
            value={sourceUrl} 
            onChange={e => setSourceUrl(e.target.value)} 
            placeholder="Ссылка на источник (необязательно)" 
            style={{ gridColumn: 'span 2' }} 
          />
          <div style={{ gridColumn: 'span 2', display: 'flex', gap: '10px' }}>
            <button type="submit">
              {editId ? 'Сохранить изменения' : 'Добавить'}
            </button>
            {editId && (
              <button 
                type="button" 
                onClick={() => { 
                  setEditId(null)
                  setCode('')
                  setTitle('')
                  setReplacement('')
                  setSourceUrl('')
                }} 
                style={{ backgroundColor: '#6c757d' }}
              >
                Отмена
              </button>
            )}
          </div>
        </form>
      </section>
      
      {error && <p className="error" role="alert">{error}</p>
      
      {/* ТАБЛИЦА РЕЕСТРА */}
      <section className="panel">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '10px' }}>
          <h2 style={{ margin: 0 }}>Локальный реестр</h2>
          <div style={{ display: 'flex', gap: '10px' }}>
            <input 
              value={search} 
              onChange={e => setSearch(e.target.value)} 
              placeholder="Поиск по коду или названию..." 
              style={{ width: '250px' }} 
            />
            <select 
              value={statusFilter} 
              onChange={e => setStatusFilter(e.target.value)} 
              style={{ width: '150px' }}
            >
              <option value="">Все статусы</option>
              <option value="active">Действует</option>
              <option value="obsolete">Устарел</option>
              <option value="unknown">Неизвестно</option>
            </select>
          </div>
        </div>
        
        <table>
          <thead>
            <tr>
              <th>Норматив</th>
              <th>Название</th>
              <th>Статус</th>
              <th>Замена</th>
              <th>Обновлен</th>
              <th>Действия</th>
            </tr>
          </thead>
          <tbody>
            {items.map(i => (
              <tr key={i.id}>
                <td>{i.code}</td>
                <td>{i.title}</td>
                <td>{statusLabel(i.status)}</td>
                <td>{i.replacement ?? '—'}</td>
                <td>
                  <small>
                    {i.updated_at ? new Date(i.updated_at).toLocaleDateString('ru-RU') : '—'}
                  </small>
                </td>
                <td>
                  <button 
                    onClick={() => startEdit(i)} 
                    style={{ padding: '4px 8px', fontSize: '0.8rem', marginRight: '5px' }}
                  >
                    Изменить
                  </button>
                  <button 
                    onClick={() => handleDelete(i.id, i.code)} 
                    style={{ padding: '4px 8px', fontSize: '0.8rem', backgroundColor: '#dc3545' }}
                  >
                    Удалить
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        
        {!items.length && <p className="muted">Реестр пуст.</p>}
      </section>
    </div>
  )
}