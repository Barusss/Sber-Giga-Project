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

  const inProgress = items.filter(item => item.state === 'draft').length
  const approved = items.length - inProgress

  return <div className="projects-workspace">
    <header className="page-heading">
      <div className="eyebrow">ПАНЕЛЬ УПРАВЛЕНИЯ / ПРОЕКТЫ</div>
      <h1>Рабочие проекты<span className="heading-dot">.</span></h1>
      <p className="muted">Создайте проект, загрузите технические условия и задание на проектирование.
        Результаты проверки будут доступны в соответствующих разделах.</p>
    </header>

    <section className="projects-stats" aria-label="Статистика проектов">
      <div className="stat-card"><span>ВСЕГО ПРОЕКТОВ</span><strong>{items.length}</strong><small>Записей в системе</small></div>
      <div className="stat-card"><span>В РАБОТЕ</span><strong>{inProgress}</strong><small>Доступны для изменений</small></div>
      <div className="stat-card"><span>УТВЕРЖДЕНО</span><strong>{approved}</strong><small>Завершённые решения</small></div>
    </section>

    <section className="panel project-create">
      <div className="panel-heading">
        <div><span className="eyebrow">01 / НОВАЯ ПРОВЕРКА</span><h2>Создать проект</h2></div>
        <span className="panel-ornament" aria-hidden="true">✳</span>
      </div>
      <form onSubmit={create} className="project-create-form">
        <label htmlFor="new-project-name">Название проекта
          <input id="new-project-name" value={name} onChange={e => setName(e.target.value)}
            placeholder="Например, реконструкция электроснабжения" required maxLength={180}/>
        </label>
        <button type="submit" disabled={creating || !name.trim()}>
          {creating ? 'Создание…' : 'Создать проект'} <span aria-hidden="true">↗</span>
        </button>
      </form>
    </section>

    {error && <p className="error" role="alert">{error}</p>}

    <section className="panel project-list">
      <div className="panel-heading">
        <div><span className="eyebrow">02 / РЕЕСТР</span><h2>Список проектов</h2></div>
        <span className="panel-counter">{items.length} записей</span>
      </div>
      {!items.length ? <div className="empty-state"><span aria-hidden="true">＋</span>
        <h3>Список пока пуст</h3><p>Создайте первый проект через форму выше.</p>
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
