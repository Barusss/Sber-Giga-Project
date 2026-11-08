import {useEffect, useMemo, useRef, useState} from 'react'
import {Document, Fragment, Project, request} from '../api/client'
import {useOptionalOperations} from '../operations/OperationsPanel'

type PageGroup = {
  number: number
  blocks: Fragment[]
  hasOcr: boolean
  searchable: string
}

const kindLabels = {tu: 'Технические условия', znp: 'Задание на проектирование', other: 'Другой документ'}

function readableText(fragment: Fragment, raw: boolean) {
  const text = fragment.text.replace(/\r\n?/g, '\n').trim()
  if (raw || fragment.extraction_method !== 'ocr') return text
  // OCR supplies physical line wraps, not actual paragraph boundaries.
  // Compact only for display; original text remains unchanged in the database.
  return text.split(/\n\s*\n/).map(paragraph =>
    paragraph.split('\n').map(line => line.trim()).filter(Boolean).join(' ')
  ).join('\n\n')
}

function Highlight({text, query}: {text: string; query: string}) {
  const needle = query.trim().toLocaleLowerCase('ru-RU')
  if (!needle) return <>{text}</>
  const lower = text.toLocaleLowerCase('ru-RU')
  const chunks: React.ReactNode[] = []
  let cursor = 0
  let index = lower.indexOf(needle)
  while (index !== -1) {
    chunks.push(text.slice(cursor, index))
    chunks.push(<mark key={index}>{text.slice(index, index + needle.length)}</mark>)
    cursor = index + needle.length
    index = lower.indexOf(needle, cursor)
  }
  chunks.push(text.slice(cursor))
  return <>{chunks}</>
}

function humanError(error: unknown) {
  return error instanceof Error ? error.message : String(error)
}

export function DocumentsPage({projectId}: {projectId?: string} = {}) {
  const operationContext = useOptionalOperations()
  const operations = operationContext?.operations ?? []
  const processedJobs = useRef(new Set<string>())
  const [projects, setProjects] = useState<Project[]>([])
  const [project, setProject] = useState(projectId ?? '')
  const [kind, setKind] = useState<'tu'|'znp'|'other'>('tu')
  const [file, setFile] = useState<File | null>(null)
  const [docs, setDocs] = useState<Document[]>([])
  const [selected, setSelected] = useState<Document | null>(null)
  const [fragments, setFragments] = useState<Fragment[]>([])
  const [busy, setBusy] = useState(false)
  const [reading, setReading] = useState(false)
  const [error, setError] = useState('')
  const [query, setQuery] = useState('')
  const [pageNumber, setPageNumber] = useState(1)
  const [raw, setRaw] = useState(false)
  const [fontSize, setFontSize] = useState(16)
  const viewRequest = useRef(0)
  const fileInput = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!projectId) request<Project[]>('/projects').then(setProjects).catch(err => setError(humanError(err)))
  }, [projectId])

  useEffect(() => {
    if (projectId && project !== projectId) selectProject(projectId)
  }, [projectId])

  useEffect(() => {
    let cancelled = false
    if (!project) {
      setDocs([])
      return
    }
    request<Document[]>(`/projects/${project}/documents`)
      .then(result => { if (!cancelled) setDocs(result) })
      .catch(err => { if (!cancelled) setError(humanError(err)) })
    return () => { cancelled = true }
  }, [project])

  useEffect(() => {
    if (!project) return
    const finished = operations.filter(job =>
      job.kind === 'upload' && job.project_id === project && job.status === 'completed')
    if (!finished.some(job => !processedJobs.current.has(job.id))) return
    finished.forEach(job => processedJobs.current.add(job.id))
    request<Document[]>(`/projects/${project}/documents`)
      .then(setDocs)
      .catch(err => setError(humanError(err)))
  }, [operations, project])

  const uploadRunning = operations.some(job =>
    job.kind === 'upload' && job.project_id === project &&
    (job.status === 'queued' || job.status === 'running'))

  const pages = useMemo<PageGroup[]>(() => {
    const byPage = new Map<number, Fragment[]>()
    for (const fragment of fragments) {
      const blocks = byPage.get(fragment.page) ?? []
      blocks.push(fragment)
      byPage.set(fragment.page, blocks)
    }
    return [...byPage.entries()].sort((a, b) => a[0] - b[0]).map(([number, blocks]) => {
      blocks.sort((a, b) => a.position - b.position)
      return {
        number,
        blocks,
        hasOcr: blocks.some(block => block.extraction_method === 'ocr'),
        searchable: blocks.map(block => readableText(block, raw)).join('\n')
      }
    })
  }, [fragments, raw])

  const searchTerm = query.trim().toLocaleLowerCase('ru-RU')
  const matchingPages = useMemo(() => pages.filter(page =>
    !searchTerm || page.searchable.toLocaleLowerCase('ru-RU').includes(searchTerm)
  ), [pages, searchTerm])
  const currentPage = matchingPages.find(page => page.number === pageNumber) ?? matchingPages[0]
  const ocrPages = pages.filter(page => page.hasOcr).length

  function selectProject(id: string) {
    // Invalidate the previous document read before switching projects.
    // Its finally-block must not leave the new project stuck in "reading".
    viewRequest.current += 1
    setReading(false)
    setDocs([])
    setProject(id)
    setSelected(null)
    setFragments([])
    setQuery('')
    setError('')
    setPageNumber(1)
  }

  async function viewDocument(document: Document) {
    const requestId = ++viewRequest.current
    setSelected(document)
    setFragments([])
    setQuery('')
    setPageNumber(1)
    setReading(true)
    setError('')
    try {
      const result = await request<Fragment[]>(`/documents/${document.id}/fragments`)
      if (requestId !== viewRequest.current) return
      setFragments(result)
    } catch (err) {
      if (requestId === viewRequest.current) setError(humanError(err))
    } finally {
      if (requestId === viewRequest.current) setReading(false)
    }
  }

  async function upload(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!file || !project || busy || uploadRunning) return
    setBusy(true)
    setError('')
    try {
      const form = new FormData()
      form.append('kind', kind)
      form.append('file', file)
      if (operationContext) await operationContext.upload(form)
      else await request(`/projects/${project}/operations/upload`, {method:'POST', body:form})
      setFile(null)
      if (fileInput.current) fileInput.current.value = ''
    } catch (err) {
      setError(humanError(err))
    } finally {
      setBusy(false)
    }
  }

  async function copyPage() {
    if (!currentPage) return
    const original = currentPage.blocks.map(block => block.text).join('\n\n')
    try {
      await navigator.clipboard.writeText(original)
    } catch {
      setError('Не удалось скопировать текст. Разрешите доступ к буферу обмена.')
    }
  }

  return (
    <div className="documents-workspace">
      <header className="documents-heading">
        <h1>Документы</h1>
        <p className="muted">Загружайте PDF или DOCX. Сканированные страницы распознаются, а обычный текст сохраняется без OCR.</p>
      </header>

      <form onSubmit={upload} className="panel documents-upload">
        <div className="documents-upload-grid">
          {!projectId && <label>Проект
            <select value={project} onChange={event => selectProject(event.target.value)}
              disabled={busy || uploadRunning} required>
              <option value="">Выберите проект</option>
              {projects.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
            </select>
          </label>}
          <label>Тип документа
            <select value={kind} onChange={event => setKind(event.target.value as 'tu'|'znp'|'other')}
              disabled={busy || uploadRunning}>
              <option value="tu">Технические условия (ТУ)</option>
              <option value="znp">Задание на проектирование (ЗнП)</option>
              <option value="other">Другое</option>
            </select>
          </label>
          <label>PDF или DOCX
            <input ref={fileInput} type="file" accept=".pdf,.docx"
              onChange={event => setFile(event.target.files?.[0] ?? null)}
              disabled={busy || uploadRunning} required />
          </label>
          <button disabled={busy || uploadRunning || !project || !file} type="submit">
            {busy ? 'Передаём файл…' : uploadRunning ? 'Идёт обработка' : 'Загрузить и распознать'}
          </button>
        </div>
        {busy && <div className="workspace-progress operation-uploading" role="status">
          <span className="operation-spinner" aria-hidden="true"/>
          <span>Файл передаётся на сервер. После передачи распознавание продолжится независимо от страницы.</span>
        </div>}
      </form>

      {error && <p className="error" role="alert">{error}</p>}

      <section className="panel documents-files">
        <div className="documents-section-title">
          <h2>Файлы проекта</h2>
          <span className="documents-muted-count">{docs.length}</span>
        </div>
        {!project && <p className="muted">Выберите проект, чтобы увидеть документы.</p>}
        {project && !docs.length && <p className="muted">В этом проекте пока нет документов.</p>}
        {docs.map(document => (
          <div key={document.id} className={`documents-file ${selected?.id === document.id ? 'is-selected' : ''}`}>
            <div className="documents-file-info">
              <strong>{document.filename}</strong>
              <small>{kindLabels[document.kind]} · {new Date(document.created_at).toLocaleString('ru-RU')}</small>
            </div>
            <button type="button" className={selected?.id === document.id ? '' : 'secondary'}
              onClick={() => viewDocument(document)} disabled={reading && selected?.id === document.id}>
              {reading && selected?.id === document.id ? 'Открываем...' : 'Читать текст'}
            </button>
          </div>
        ))}
      </section>

      {selected && (
        <section className="panel documents-viewer">
          <div className="documents-viewer-head">
            <div>
              <h2>{selected.filename}</h2>
              <p className="muted">{reading ? 'Получаем текст...' : selected.pagination_kind === 'physical' ?
                `${pages.length} стр. · ${fragments.length} фрагментов · OCR: ${ocrPages} стр.` :
                `Физические страницы не определены · ${fragments.length} фрагментов`}</p>
            </div>
            <button type="button" className="secondary" onClick={() => { setSelected(null); setFragments([]); viewRequest.current += 1 }}>
              Закрыть
            </button>
          </div>

          {!reading && pages.length > 0 && (
            <>
              <div className="documents-reader-tools">
                <label className="documents-search-label">
                  <span>Поиск по документу</span>
                  <input type="search" value={query} onChange={event => setQuery(event.target.value)}
                    placeholder="Найти слово, ГОСТ, номер..." aria-label="Поиск по распознанному тексту" />
                </label>
                <label className="documents-mode-label">
                  <input type="checkbox" checked={raw} onChange={event => setRaw(event.target.checked)} />
                  Исходные переносы строк
                </label>
                <div className="documents-text-size" aria-label="Размер текста">
                  <button type="button" className="secondary" disabled={fontSize <= 13}
                    onClick={() => setFontSize(size => Math.max(13, size - 1))} aria-label="Уменьшить текст">A−</button>
                  <button type="button" className="secondary" disabled={fontSize >= 22}
                    onClick={() => setFontSize(size => Math.min(22, size + 1))} aria-label="Увеличить текст">A+</button>
                </div>
              </div>

              <div className="documents-reader">
                <nav className="documents-page-nav" aria-label={selected.pagination_kind === 'physical' ? 'Страницы документа' : 'Текст документа'}>
                  <h3>{selected.pagination_kind === 'physical' ? 'Страницы' : 'Текст документа'} {searchTerm && <small>Найдено: {matchingPages.length}</small>}</h3>
                  {matchingPages.map(page => (
                    <button key={page.number} type="button"
                      className={currentPage?.number === page.number ? 'active' : ''}
                      onClick={() => setPageNumber(page.number)}
                      aria-current={currentPage?.number === page.number ? 'page' : undefined}>
                      <span>{selected.pagination_kind === 'physical' ? `Страница ${page.number}` : 'Текст документа'}</span>
                      {page.hasOcr && <small>OCR</small>}
                    </button>
                  ))}
                  {!matchingPages.length && <p className="muted">Совпадений не найдено.</p>}
                </nav>

                <div className="documents-reader-content">
                  {currentPage ? (
                    <>
                      <div className="documents-page-header">
                        <div>
                          <h3>{selected.pagination_kind === 'physical' ? `Страница ${currentPage.number}` : 'Текст документа'}</h3>
                          <small>{currentPage.hasOcr ? 'Распознано со скана' : 'Текст документа'}</small>
                        </div>
                        <button className="secondary" type="button" onClick={copyPage}>{selected.pagination_kind === 'physical' ? 'Копировать страницу' : 'Копировать текст'}</button>
                      </div>
                      <article className="documents-paper" style={{fontSize: `${fontSize}px`}}>
                        {currentPage.blocks.map(block => (
                          <div className="documents-text-block" key={block.id}>
                            <Highlight text={readableText(block, raw)} query={query} />
                          </div>
                        ))}
                      </article>
                      <p className="documents-reader-note">Отображение OCR сглаживает переносы строк. Оригинал можно посмотреть переключателем выше. Номера нормативов сверяйте со сканом.</p>
                    </>
                  ) : <p className="documents-empty-search">На страницах документа ничего не найдено. Измените запрос.</p>}
                </div>
              </div>
            </>
          )}
          {!reading && !pages.length && <p className="muted">Текстовых фрагментов нет. Возможно, страница пустая или распознавание не дало результата.</p>}
        </section>
      )}
    </div>
  )
}
