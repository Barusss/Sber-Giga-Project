import { useEffect, useState } from 'react'
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

  async function handleCheck(force = false) {
    if (!selectedDocId) return
    setIsChecking(true)
    setError('')
    setReport(null)
    try {
      const suffix = force ? '?refresh=true' : ''
      const result = await request<VerificationReport>(
        `/documents/${selectedDocId}/check-normatives${suffix}`, 
        json('POST', {})
      )
      setReport(result)
      loadNormatives()
    } catch (e: any) {
      setError(e.message || 'Ошибка при проверке')
    } finally {
      setIsChecking(false)
    }
  }

  const statusLabel = (value: string) => {
    if (value === 'active') return 'Действует'
    if (value === 'obsolete') return 'Не действует / заменён'
    return 'Не подтверждено'
  }

  const sourceLabel = (value: string | null) => {
    if (value === 'rosstandart') return 'Росстандарт'
    if (value === 'local') return 'Локальный реестр'
    return 'Не подтверждено'
  }

  return (
    <>
      <h1>Проверка ГОСТ и НТД</h1>
      <p className="muted">
        Извлечение ссылок (DeepSeek) и проверка статусов. Подтверждённые записи
        локального реестра используются без сетевого запроса; при их отсутствии
        выполняется запрос к Росстандарту.
      </p>

      {/* СЕКЦИЯ АНАЛИЗА ДОКУМЕНТА */}
      <section className="panel stack" style={{ marginBottom: '2rem', borderLeft: '4px solid #007bff' }}>
        <h2>🔍 Анализ проектного документа</h2>
        
        <div style={{ display: 'flex', gap: '10px', marginBottom: '10px' }}>
          <select 
            value={selectedProjectId} 
            onChange={e => setSelectedProjectId(e.target.value)} 
            style={{ flex: 1 }}
          >
            <option value="">Выберите проект...</option>
            {projects.map(p => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
          
          <select 
            value={selectedDocId} 
            onChange={e => setSelectedDocId(e.target.value)} 
            disabled={!selectedProjectId} 
            style={{ flex: 1 }}
          >
            <option value="">Выберите документ...</option>
            {documents.map(d => (
              <option key={d.id} value={d.id}>{d.filename}</option>
            ))}
          </select>
        </div>

        <div style={{ display: 'flex', gap: '10px', marginBottom: '10px' }}>
          <button 
            onClick={handleExtract} 
            disabled={isExtracting || !selectedDocId} 
            style={{ backgroundColor: '#17a2b8' }}
          >
            {isExtracting ? 'Извлекаем...' : '1. Извлечь нормативные ссылки'}
          </button>
          
          <button 
            onClick={() => handleCheck()} 
            disabled={isChecking || !selectedDocId} 
            style={{ backgroundColor: '#28a745' }}
          >
            {isChecking ? 'Проверяем...' : '2. Проверить актуальность'}
          </button>

          <button 
            onClick={() => handleCheck(true)} 
            disabled={isChecking || !selectedDocId} 
            style={{ backgroundColor: '#6c757d' }}
            title="Игнорировать локальный кэш и запросить Росстандарт"
          >
            2b. Обновить из Росстандарта
          </button>
        </div>

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
                    <th>Статус</th>
                    <th>Замена</th>
                    <th>Стр.</th>
                    <th>Источник и дата</th>
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
                        <div>{item.source_url ? (
                          <a href={item.source_url} target="_blank" rel="noopener noreferrer">
                            {sourceLabel(item.source)}
                          </a>
                        ) : sourceLabel(item.source)}</div>
                        <small>
                          {item.checked_at
                            ? `Проверено: ${new Date(item.checked_at).toLocaleDateString('ru-RU')}`
                            : 'Дата подтверждения не указана'}
                        </small>
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
      
      {error && <p className="error">{error}</p>}
      
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
              <th>Проверен / изменён</th>
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
                    {i.checked_at
                      ? `Проверен ${new Date(i.checked_at).toLocaleDateString('ru-RU')}`
                      : i.updated_at
                        ? `Изменён ${new Date(i.updated_at).toLocaleDateString('ru-RU')}`
                        : '—'}
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
    </>
  )
}