import {useEffect, useMemo, useState} from 'react'
import {Link} from 'react-router-dom'
import {Document, json, request} from '../api/client'

export type RunSummary = {
  id: string
  created_at: string
  is_current: boolean
  confirmed: number
  stats: {total?: number; fulfilled?: number; missing?: number; needs_review?: number}
}

export type ComparisonPair = {
  id: string
  project_id: string
  tu_document_id: string
  znp_document_id: string
  tu_filename: string
  znp_filename: string
  created_at: string
  status: 'queued'|'ready'|'reviewed'
  run_count: number
  current_run_id: string | null
  latest: RunSummary | null
}

const statusLabels: Record<ComparisonPair['status'], string> = {
  queued: 'Ожидает запуска',
  ready: 'На рассмотрении',
  reviewed: 'Рассмотрено',
}

export function ComparisonsPage({projectId, approved}: {projectId: string; approved: boolean}) {
  const [documents, setDocuments] = useState<Document[]>([])
  const [pairs, setPairs] = useState<ComparisonPair[]>([])
  const [layout, setLayout] = useState<'list'|'matrix'>('list')
  const [filter, setFilter] = useState<'all'|ComparisonPair['status']>('all')
  const [search, setSearch] = useState('')
  const [selected, setSelected] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState('')
  const [error, setError] = useState('')
  const base = `/projects/${projectId}/comparisons`

  const reload = async () => {
    const [nextDocs, nextPairs] = await Promise.all([
      request<Document[]>(`/projects/${projectId}/documents`),
      request<ComparisonPair[]>(`/projects/${projectId}/comparisons`),
    ])
    setDocuments(nextDocs)
    setPairs(nextPairs)
  }

  useEffect(() => {
    let active = true
    Promise.all([
      request<Document[]>(`/projects/${projectId}/documents`),
      request<ComparisonPair[]>(`/projects/${projectId}/comparisons`),
    ]).then(([docs, records]) => {
      if (active) {setDocuments(docs); setPairs(records)}
    }).catch(e => {if (active) setError(e instanceof Error ? e.message : String(e))})
    return () => {active = false}
  }, [projectId])

  const tuDocs = documents.filter(doc => doc.kind === 'tu')
  const znpDocs = documents.filter(doc => doc.kind === 'znp')
  const existing = new Map(pairs.map(pair => [`${pair.tu_document_id}|${pair.znp_document_id}`, pair]))
  const waiting = pairs.filter(pair => pair.status === 'queued')
  const toReview = pairs.filter(pair => pair.status === 'ready')
  const reviewed = pairs.filter(pair => pair.status === 'reviewed')

  const visible = useMemo(() => pairs.filter(pair => {
    const matchesFilter = filter === 'all' || filter === pair.status
    const matchesSearch = !search.trim() || [pair.tu_filename, pair.znp_filename, pair.id]
      .some(value => value.toLocaleLowerCase('ru').includes(search.trim().toLocaleLowerCase('ru')))
    return matchesFilter && matchesSearch
  }), [pairs, filter, search])

  function toggle(key: string) {
    setSelected(previous => previous.includes(key)
      ? previous.filter(item => item !== key)
      : [...previous, key])
  }

  async function addSelected() {
    if (!selected.length || busy) return
    setBusy(true); setError('')
    try {
      await request(`/projects/${projectId}/comparisons`, json('POST', {
        pairs: selected.map(key => {
          const [tu_document_id, znp_document_id] = key.split('|')
          return {tu_document_id, znp_document_id}
        }),
      }))
      setSelected([])
      setLayout('list')
      setFilter('queued')
      await reload()
    } catch (e) {setError(e instanceof Error ? e.message : String(e))}
    finally {setBusy(false)}
  }

  async function run(ids: string[]) {
    if (!ids.length || busy) return
    setBusy(true); setError('')
    for (let index = 0; index < ids.length; index++) {
      setProgress(`Сверка ${index + 1} из ${ids.length}`)
      try {
        await request(`${base}/${ids[index]}/runs`, {method:'POST'})
        await reload()
      } catch (e) {
        setError(`Ошибка в сверке ${index + 1}: ${e instanceof Error ? e.message : String(e)}`)
        break
      }
    }
    setBusy(false);setProgress('')
  }

  async function remove(pair: ComparisonPair) {
    if (busy || !window.confirm(
      `Удалить сверку «${pair.tu_filename} / ${pair.znp_filename}» и всю её историю запусков? Исходные документы останутся.`
    )) return
    setBusy(true);setError('')
    try {
      await request(`${base}/${pair.id}`, {method:'DELETE'})
      await reload()
    } catch (e) {setError(e instanceof Error ? e.message : String(e))}
    finally {setBusy(false)}
  }

  return <div className="comparisons-workspace">
    <div className="workspace-section-heading">
      <div><h2>Сверки</h2><p>Одна пара документов может проверяться многократно. Результаты запусков сохраняются.</p></div>
      {tuDocs.length > 0 && znpDocs.length > 0 &&
        <button onClick={() => setLayout('matrix')} className="secondary">Создать сверки</button>}
    </div>
    {error && <p className="error" role="alert">{error}</p>}
    {busy && <p role="status" className="workspace-progress">{progress || 'Сохраняем изменения…'}</p>}
    {(!tuDocs.length || !znpDocs.length) &&
      <section className="panel workspace-empty">
        <h3>Для сверки нужны ТУ и ЗнП</h3>
        <p>Загрузите хотя бы один документ каждого типа.</p>
        <Link to={`/projects/${projectId}/documents`} className="workspace-link">Перейти к документам</Link>
      </section>}
    {waiting.length > 0 && !approved && <div className="workspace-next-action">
      <div>
        <strong>{waiting.length} сверок ожидают запуска</strong>
        <p>Запустите все ожидающие проверки или выберите нужную пару в списке.</p>
      </div>
      <button type="button" className="secondary" disabled={busy}
        onClick={() => run(waiting.map(pair => pair.id))}>
        Запустить все
      </button>
    </div>}
    <div className="workspace-summary" aria-label="Сводка сверок">
      <span><strong>{pairs.length}</strong> сверок</span>
      <span><strong>{pairs.reduce((total, pair) => total + pair.run_count, 0)}</strong> запусков</span>
      <span><strong>{waiting.length}</strong> ожидают</span>
      <span><strong>{toReview.length}</strong> на рассмотрении</span>
      <span><strong>{reviewed.length}</strong> рассмотрено</span>
    </div>

    {tuDocs.length > 0 && znpDocs.length > 0 &&
      <section className="panel">
        <div className="workspace-toolbar">
          <div className="workspace-switch">
            <button className={layout === 'list' ? 'active' : ''} onClick={() => setLayout('list')}>Список</button>
            <button className={layout === 'matrix' ? 'active' : ''} onClick={() => setLayout('matrix')}>Матрица</button>
          </div>
          {layout === 'list' ? <div className="workspace-controls">
            <input type="search" aria-label="Поиск сверок" placeholder="Поиск по документам"
              value={search} onChange={e => setSearch(e.target.value)}/>
            <select aria-label="Фильтр сверок" value={filter}
              onChange={e => setFilter(e.target.value as typeof filter)}>
              <option value="all">Все статусы</option>
              <option value="queued">Ожидают запуска</option>
              <option value="ready">На рассмотрении</option>
              <option value="reviewed">Рассмотрено</option>
            </select>
          </div> : null}
        </div>

        {layout === 'list' ? <>
          <div className="workspace-table-scroll"><table className="workspace-table">
            <thead><tr><th>Технические условия</th><th>Задание на проектирование</th><th>Состояние</th><th>Запусков</th><th>Действия</th></tr></thead>
            <tbody>{visible.map(pair => <tr key={pair.id}>
              <td className="workspace-doc-name">{pair.tu_filename}</td>
              <td className="workspace-doc-name">{pair.znp_filename}</td>
              <td><span className={'workspace-status '+pair.status}>{statusLabels[pair.status]}</span></td>
              <td>{pair.run_count}</td>
              <td><div className="workspace-row-actions">
                <Link to={`${base}/${pair.id}`} className="workspace-link">
                  {pair.status === 'queued' ? 'Открыть' : 'Замечания и история'}
                </Link>
                {pair.status === 'queued' && !approved &&
                  <button type="button" className="secondary" disabled={busy} onClick={() => run([pair.id])}>Запустить</button>}
                {!approved && <button type="button" className="workspace-danger" disabled={busy}
                  onClick={() => remove(pair)}>Удалить</button>}
              </div></td>
            </tr>)}</tbody>
          </table></div>
          {!visible.length && <p className="muted">Сверок по выбранному фильтру нет.</p>}

        </> : <>
          <p className="workspace-help">Выберите нужные сочетания документов. Один файл может участвовать в нескольких сверках.</p>
          <div className="workspace-matrix-scroll"><table className="workspace-matrix">
            <thead><tr><th>ТУ / ЗнП</th>{znpDocs.map(doc =>
              <th key={doc.id} title={doc.filename}>{doc.filename}</th>)}</tr></thead>
            <tbody>{tuDocs.map(tu => <tr key={tu.id}>
              <th title={tu.filename}>{tu.filename}</th>
              {znpDocs.map(znp => {
                const key = `${tu.id}|${znp.id}`
                const pair = existing.get(key)
                return <td key={znp.id}>
                  {pair ? <Link to={`${base}/${pair.id}`} className={'workspace-matrix-cell '+pair.status}
                    title={`${tu.filename} / ${znp.filename}: ${statusLabels[pair.status]}`}>
                    {pair.status === 'queued' ? 'Ожидает' : pair.status === 'reviewed' ? 'Рассмотрено' : 'Готово'}
                  </Link> : <label className={'workspace-matrix-cell new '+(selected.includes(key) ? 'selected' : '')}>
                    <input type="checkbox" checked={selected.includes(key)} disabled={busy || approved}
                      onChange={() => toggle(key)} aria-label={`Добавить сверку ${tu.filename} и ${znp.filename}`}/>
                    <span>{selected.includes(key) ? 'Выбрано' : 'Добавить'}</span>
                  </label>}
                </td>
              })}
            </tr>)}</tbody>
          </table></div>
          <div className="workspace-footer">
            <span>Выбрано комбинаций: {selected.length}</span>
            <button disabled={!selected.length || busy || approved} onClick={addSelected}>
              Добавить в очередь
            </button>
          </div>
        </>}
      </section>}
  </div>
}
