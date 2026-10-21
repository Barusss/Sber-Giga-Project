import {useEffect, useState, useSyncExternalStore} from 'react'
import {clearDiagnostics, getDiagnostics, subscribeDiagnostics} from './diagnostics'

function formatTime(timestamp: number) {
  return new Date(timestamp).toLocaleTimeString('ru-RU', {hour12: false})
}

export function DebugPanel() {
  const events = useSyncExternalStore(subscribeDiagnostics, getDiagnostics)
  const [open, setOpen] = useState(false)
  const [errorsOnly, setErrorsOnly] = useState(false)
  const [copyMessage, setCopyMessage] = useState('')
  const active = events.filter(event => event.status === 'running').length
  const errors = events.filter(event => event.status === 'error').length
  const visible = errorsOnly ? events.filter(event => event.status === 'error') : events

  useEffect(() => {
    const show = () => setOpen(true)
    window.addEventListener('open-diagnostics', show)
    return () => window.removeEventListener('open-diagnostics', show)
  }, [])

  useEffect(() => {
    if (!open) return
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [open])

  async function copyLog() {
    const text = events.slice().reverse().map(event => {
      const duration = event.updatedAt - event.startedAt
      return `[${formatTime(event.startedAt)}] ${event.status} | ${event.title} | ${event.detail} | ${duration} мс`
    }).join('\n')
    try {
      await navigator.clipboard.writeText(text)
      setCopyMessage('Журнал скопирован')
    } catch {
      setCopyMessage('Не удалось скопировать. Проверьте доступ к буферу обмена.')
    }
  }

  return (
    <>
      <button className="diagnostics-toggle" type="button"
        onClick={() => {setOpen(true); setCopyMessage('')}}
        aria-label="Открыть журнал диагностики">
        <span>Диагностика</span>
        {active > 0 && <span className="diagnostics-count running" title="Выполняются">{active}</span>}
        {errors > 0 && <span className="diagnostics-count failed" title="Ошибки">{errors}</span>}
      </button>
      {open && (
        <div className="diagnostics-backdrop" onMouseDown={event => {
          if (event.target === event.currentTarget) setOpen(false)
        }}>
          <section className="diagnostics-drawer" role="dialog" aria-modal="true" aria-label="Журнал диагностики">
            <header className="diagnostics-header">
              <div>
                <h2>Журнал диагностики</h2>
                <p>Запросы, обработка документов и сообщения об ошибках в этой вкладке.</p>
              </div>
              <button type="button" className="diagnostics-close" onClick={() => setOpen(false)}
                aria-label="Закрыть диагностику">×</button>
            </header>
            <div className="diagnostics-toolbar">
              <label className="diagnostics-check">
                <input type="checkbox" checked={errorsOnly} onChange={event => setErrorsOnly(event.target.checked)} />
                Только ошибки
              </label>
              <div className="diagnostics-buttons">
                <button type="button" className="secondary" onClick={copyLog} disabled={!events.length}>Скопировать</button>
                <button type="button" className="secondary" onClick={clearDiagnostics}
                  disabled={!events.some(event => event.status !== 'running')}>Очистить</button>
              </div>
            </div>
            {copyMessage && <p className="diagnostics-copy-result" role="status">{copyMessage}</p>}
            <div className="diagnostics-list" role="log" aria-label="События приложения">
              {!visible.length && <p className="diagnostics-empty">
                {errorsOnly ? 'Ошибок в журнале пока нет.' : 'Событий пока нет. Начните работу с проектом.'}
              </p>}
              {visible.map(event => (
                <article key={event.id} className={`diagnostics-event ${event.status}`}>
                  <div className="diagnostics-event-heading">
                    <span className="diagnostics-status">
                      {event.status === 'running' ? 'Выполняется' : event.status === 'success' ? 'Готово' : 'Ошибка'}
                    </span>
                    <time dateTime={new Date(event.startedAt).toISOString()}>{formatTime(event.startedAt)}</time>
                  </div>
                  <strong>{event.title}</strong>
                  <p>{event.detail}</p>
                  <small>{event.status === 'running'
                    ? 'Процесс ещё не завершён'
                    : `Время: ${((event.updatedAt - event.startedAt) / 1000).toFixed(1)} с`}</small>
                </article>
              ))}
            </div>
            <footer className="diagnostics-footer">
              Сохраняются только последние 100 операций в памяти вкладки.
              Текст документов, содержимое запросов и ключи API в журнал не записываются.
            </footer>
          </section>
        </div>
      )}
    </>
  )
}
