import {useEffect, useMemo, useRef, useState} from 'react'
import {API, Document, Fragment, Project, request} from '../api/client'
import {beginDiagnostic} from '../debug/diagnostics'

type UploadEvent = {
  stage: 'received' | 'parsing' | 'parsed' | 'indexing' | 'complete' | 'error'
  message: string
  pages?: number
  fragments?: number
  document?: Document
}

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

async function readUploadResponse(response: Response, onEvent: (event: UploadEvent) => void): Promise<Document> {
  if (!response.ok) {
    let message = `HTTP ${response.status}: ${response.statusText}`
    try {
      const payload = await response.json()
      if (typeof payload.detail === 'string') message = payload.detail
    } catch { /* the server returned a non-JSON error */ }
    throw new Error(message)
  }
  if (!response.body) throw new Error('Браузер не поддерживает потоковую обработку ответа')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let remaining = ''
  let document: Document | null = null

  function parseLine(line: string) {
    if (!line.trim()) return
    const event = JSON.parse(line) as UploadEvent
    onEvent(event)
    if (event.stage === 'error') throw new Error(event.message)
    if (event.stage === 'complete') {
      if (!event.document) throw new Error('Сервер не передал результат загрузки')
      document = event.document
    }
  }

  try {
    while (true) {
      const {done, value} = await reader.read()
      if (done) break
      remaining += decoder.decode(value, {stream: true})
      const lines = remaining.split('\n')
      remaining = lines.pop() ?? ''
      for (const line of lines) parseLine(line)
    }
    remaining += decoder.decode()
    if (remaining.trim()) parseLine(remaining)
  } finally {
    reader.releaseLock()
  }

  if (!document) throw new Error('Соединение завершилось до окончания обработки документа')
  return document
}

export function DocumentsPage() {
  const [projects, setProjects] = useState<Project[]>([])
  const [project, setProject] = useState('')
  const [kind, setKind] = useState<'tu'|'znp'|'other'>('tu')
  const [file, setFile] = useState<File | null>(null)
  const [docs, setDocs] = useState<Document[]>([])
  const [selected, setSelected] = useState<Document | null>(null)
  const [fragments, setFragments] = useState<Fragment[]>([])
  const [busy, setBusy] = useState(false)
  const [reading, setReading] = useState(false)
  const [uploadStep, setUploadStep] = useState('')
  const [error, setError] = useState('')
  const [query, setQuery] = useState('')
  const [pageNumber, setPageNumber] = useState(1)
  const [raw, setRaw] = useState(false)
  const [fontSize, setFontSize] = useState(16)
  const viewRequest = useRef(0)
  const fileInput = useRef<HTMLInputElement>(null)

  useEffect(() => {
    request<Project[]>('/projects').then(setProjects).catch(err => setError(humanError(err)))
  }, [])

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
    viewRequest.current += 1
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
    if (!file || !project || busy) return

    const operation = beginDiagnostic(`OCR: ${file.name}`, 'Отправка файла на сервер')
    setBusy(true)
    setError('')
    setUploadStep('Отправка файла на сервер')

    try {
      const form = new FormData()
      form.append('kind', kind)
      form.append('file', file)
      const response = await fetch(`${API}/projects/${project}/documents/stream`, {
        method: 'POST',
        body: form
      })

      const saved = await readUploadResponse(response, event => {
        setUploadStep(event.message)
        if (event.stage !== 'complete' && event.stage !== 'error') operation.step(event.message)
      })

      operation.success('Документ сохранён и проиндексирован')
      setDocs(items => [...items.filter(item => item.id !== saved.id), saved])
      setFile(null)
      if (fileInput.current) fileInput.current.value = ''
      await viewDocument(saved)
    } catch (err) {
      const message = humanError(err)
      operation.error(message)
      setError(message)
    } finally {
      setBusy(false)
      setUploadStep('')
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
        <h1>Документы и сканы</h1>
        <p className="muted">Загружайте PDF или DOCX. Сканированные страницы распознаются, а обычный текст сохраняется без OCR.</p>
      </header>

      <form onSubmit={upload} className="panel documents-upload">
        <div className="documents-upload-grid">
          <label>Проект
            <select value={project} onChange={event => selectProject(event.target.value)}
              disabled={busy} required>
              <option value="">Выберите проект</option>
              {projects.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
            </select>
          </label>
          <label>Тип документа
            <select value={kind} onChange={event => setKind(event.target.value as 'tu'|'znp'|'other')}
              disabled={busy}>
              <option value="tu">Технические условия (ТУ)</option>
              <option value="znp">Задание на проектирование (ЗнП)</option>
              <option value="other">Другое</option>
            </select>
          </label>
          <label>PDF или DOCX
            <input ref={fileInput} type="file" accept=".pdf,.docx"
              onChange={event => setFile(event.target.files?.[0] ?? null)}
              disabled={busy} required />
          </label>
          <button disabled={busy || !project || !file} type="submit">
            {busy ? 'Обрабатываем...' : 'Загрузить и распознать'}
          </button>
        </div>
        {busy && <div className="documents-upload-progress" role="status" aria-live="polite">
          <span className="documents-spinner" aria-hidden="true" />
          <span><strong>Обработка документа</strong><small>{uploadStep}</small></span>
          <button type="button" className="secondary" onClick={() => window.dispatchEvent(new Event('open-diagnostics'))}>Журнал</button>
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
              <p className="documents-eyebrow">Распознанный документ</p>
              <h2>{selected.filename}</h2>
              <p className="muted">{reading ? 'Получаем текст...' :
                `${pages.length} стр. · ${fragments.length} фрагментов · OCR: ${ocrPages} стр.`}</p>
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
                <nav className="documents-page-nav" aria-label="Страницы документа">
                  <h3>Страницы {searchTerm && <small>Найдено: {matchingPages.length}</small>}</h3>
                  {matchingPages.map(page => (
                    <button key={page.number} type="button"
                      className={currentPage?.number === page.number ? 'active' : ''}
                      onClick={() => setPageNumber(page.number)}
                      aria-current={currentPage?.number === page.number ? 'page' : undefined}>
                      <span>Страница {page.number}</span>
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
                          <h3>Страница {currentPage.number}</h3>
                          <small>{currentPage.hasOcr ? 'Распознано со скана' : 'Текст документа'}</small>
                        </div>
                        <button className="secondary" type="button" onClick={copyPage}>Копировать страницу</button>
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
