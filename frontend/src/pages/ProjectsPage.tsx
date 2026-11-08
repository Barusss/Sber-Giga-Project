import {useEffect, useMemo, useState} from 'react'
import {Link, useNavigate} from 'react-router-dom'
import {Project, json, request} from '../api/client'

export function ProjectsPage() {
  const navigate = useNavigate()
  const [items, setItems] = useState<Project[]>([])
  const [name, setName] = useState('')
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState<'all'|'draft'|'approved'>('all')
  const [showCreate, setShowCreate] = useState(false)
  const [error, setError] = useState('')
  const [creating, setCreating] = useState(false)

  useEffect(() => {
    request<Project[]>('/projects').then(result => {
      setItems(result)
      if (!result.length) setShowCreate(true)
    }).catch(e => setError(e instanceof Error ? e.message : String(e)))
  }, [])

  const filtered = useMemo(() => items.filter(project =>
    (status === 'all' || project.state === status)
    && project.name.toLocaleLowerCase('ru').includes(search.toLocaleLowerCase('ru').trim())
  ), [items, status, search])

  async function create(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (creating || !name.trim()) return
    setCreating(true);setError('')
    try {
      const created = await request<Project>('/projects', json('POST', {name: name.trim()}))
      navigate(`/projects/${created.id}`)
    } catch(e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setCreating(false)
    }
  }

  return <div className="projects-workspace">
    <div className="workspace-section-heading">
      <h1>Проекты</h1>
      <button className="primary-cta" onClick={() => setShowCreate(true)}>Создать проект</button>
    </div>
    {showCreate && <section className="panel workspace-create">
      <div className="workspace-section-heading"><h2>Новый проект</h2>
        <button type="button" className="secondary" onClick={() => setShowCreate(false)}>Отмена</button>
      </div>
      <form onSubmit={create} className="workspace-create-form">
        <label>Название проекта
          <input autoFocus required maxLength={180} value={name} placeholder="Название проекта"
            onChange={e => setName(e.target.value)}/>
        </label>
        <button className="primary-cta" disabled={creating || !name.trim()} type="submit">
          {creating ? 'Создаём…' : 'Создать и открыть'}
        </button>
      </form>
    </section>}
    {error && <p className="error" role="alert">{error}</p>}
    <section className="panel">
      <div className="workspace-toolbar">
        <input type="search" value={search} onChange={e => setSearch(e.target.value)}
          placeholder="Поиск по проектам" aria-label="Поиск по проектам"/>
        <select value={status} aria-label="Статус проекта"
          onChange={e => setStatus(e.target.value as typeof status)}>
          <option value="all">Все проекты</option>
          <option value="draft">В работе</option>
          <option value="approved">Утверждены</option>
        </select>
      </div>
      <div className="workspace-table-scroll"><table className="workspace-table">
        <thead><tr><th>Название</th><th>Статус</th><th>Создан</th><th></th></tr></thead>
        <tbody>{filtered.map(project => <tr key={project.id}>
          <td><Link className="workspace-project-name" to={`/projects/${project.id}`}>{project.name}</Link></td>
          <td><span className={'workspace-state '+(project.state === 'approved' ? 'approved':'')}>
            {project.state === 'approved' ? 'Утверждён':'В работе'}
          </span></td>
          <td>{new Date(project.created_at).toLocaleDateString('ru-RU')}</td>
          <td><Link to={`/projects/${project.id}`} className="workspace-link">Открыть</Link></td>
        </tr>)}</tbody>
      </table></div>
      {!filtered.length && <p className="muted">
        {items.length ? 'Проектов по текущему фильтру нет.' : 'Проектов пока нет. Создайте первый проект.'}
      </p>}
    </section>
  </div>
}
