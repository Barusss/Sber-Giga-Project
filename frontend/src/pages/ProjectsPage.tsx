import {useEffect, useState} from 'react'
import {Project, json, request} from '../api/client'

export function ProjectsPage() {
  const [items, setItems] = useState<Project[]>([])
  const [name, setName] = useState('')
  const [error, setError] = useState('')
  const [creating, setCreating] = useState(false)

  const reload = () => request<Project[]>('/projects').then(setItems).catch(
    (e: unknown) => setError(e instanceof Error ? e.message : String(e))
  )
  useEffect(() => {reload()}, [])

  async function create(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault()
    if (creating) return
    setCreating(true)
    setError('')
    try {
      await request('/projects', json('POST', {name: name.trim()}))
      setName('')
      await reload()
    } catch(e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setCreating(false)
    }
  }

  return <div className="projects-workspace">
    <header className="page-heading">
      <h1>Проекты</h1>
    </header>

    <section className="panel project-create">
      <div className="panel-heading"><h2>Создать проект</h2></div>
      <form onSubmit={create} className="project-create-form">
        <label htmlFor="new-project-name">Название проекта
          <input id="new-project-name" value={name} onChange={e => setName(e.target.value)}
            placeholder="Название проекта" required maxLength={180}/>
        </label>
        <button type="submit" disabled={creating || !name.trim()}>
          {creating ? 'Создание…' : 'Создать проект'}
        </button>
      </form>
    </section>

    {error && <p className="error" role="alert">{error}</p>}

    <section className="panel project-list">
      <div className="panel-heading">
        <h2>Список проектов</h2>
        <span className="panel-counter">{items.length}</span>
      </div>
      {!items.length ? <div className="empty-state">
        <h3>Проектов пока нет</h3><p>Создайте проект через форму выше.</p>
      </div> : <div className="table-scroll">
        <table><thead><tr><th>Название</th><th>Идентификатор</th><th>Состояние</th></tr></thead>
          <tbody>{items.map(item => <tr key={item.id}>
            <td className="project-name">{item.name}</td>
            <td><code>{item.id}</code></td>
            <td><span className={'project-status ' + (item.state === 'draft' ? 'is-draft' : 'is-approved')}>
              {item.state === 'draft' ? 'В работе' : 'Утверждён'}
            </span></td>
          </tr>)}</tbody>
        </table>
      </div>}
    </section>
  </div>
}
