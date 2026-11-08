import {useEffect, useState} from 'react'
import {Link, NavLink, Navigate, Route, Routes, useLocation, useParams} from 'react-router-dom'
import {Document, Project, request} from '../api/client'
import {DocumentsPage} from './DocumentsPage'
import {NormativesPage} from './NormativesPage'
import {ReportsPage} from './ReportsPage'
import {ComparisonsPage, ComparisonPair} from './ComparisonsPage'
import {ComparisonReviewPage} from './ComparisonReviewPage'
import {OperationsPanel, OperationsProvider} from '../operations/OperationsPanel'

const tabs = [
  {key: 'documents', title: 'Документы'},
  {key: 'prepare', title: 'Подготовка'},
  {key: 'comparisons', title: 'Сверки'},
  {key: 'report', title: 'Отчёт'},
]

type Overview = {documents: Document[]; pairs: ComparisonPair[]}

function WorkspaceStart({projectId}: {projectId: string}) {
  const [overview, setOverview] = useState<Overview | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true
    Promise.all([
      request<Document[]>(`/projects/${projectId}/documents`),
      request<ComparisonPair[]>(`/projects/${projectId}/comparisons`),
    ]).then(([documents, pairs]) => {if (active) setOverview({documents, pairs})})
      .catch(e => {if (active) setError(String(e))})
    return () => {active = false}
  }, [projectId])

  if (error) return <div className="panel error" role="alert">{error}</div>
  if (!overview) return <p className="muted">Загружаем проект…</p>
  const {documents, pairs} = overview
  const hasTu = documents.some(doc => doc.kind === 'tu')
  const hasZnp = documents.some(doc => doc.kind === 'znp')
  if (!hasTu || !hasZnp) return <Navigate to="documents" replace/>
  if (!pairs.length || pairs.some(pair => pair.status !== 'reviewed')) {
    return <Navigate to="comparisons" replace/>
  }
  return <Navigate to="report" replace/>
}

export function ProjectWorkspacePage() {
  const {projectId} = useParams<{projectId: string}>()
  const location = useLocation()
  const [project, setProject] = useState<Project | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    if (!projectId) return
    let active = true
    setProject(null)
    request<Project>(`/projects/${projectId}`)
      .then(item => {if (active) setProject(item)})
      .catch(e => {if (active) setError(e instanceof Error ? e.message : String(e))})
    return () => {active = false}
  }, [projectId])

  if (!projectId) return <Navigate to="/" replace/>
  if (error) return <p className="error" role="alert">{error}</p>
  if (!project) return <p className="muted">Загружаем проект…</p>

  const base = `/projects/${projectId}`
  const insideReview = location.pathname.includes('/comparisons/')
  return <OperationsProvider projectId={projectId}><div className="workspace">
    <div className="workspace-breadcrumb">
      <Link to="/">Проекты</Link><span aria-hidden="true">/</span><span>{project.name}</span>
    </div>
    <header className="workspace-heading">
      <h1>{project.name}</h1>
      <span className={'workspace-state ' + (project.state === 'approved' ? 'approved' : '')}>
        {project.state === 'approved' ? 'Утверждён' : 'В работе'}
      </span>
    </header>
    <nav className="workspace-tabs" aria-label="Работа над проектом">
      {tabs.map(tab => <NavLink key={tab.key} to={`${base}/${tab.key}`}
        className={({isActive}) => 'workspace-tab' + (isActive ? ' active' : '')}>
        {tab.title}
      </NavLink>)}
    </nav>
    <OperationsPanel/>
    <div className={'workspace-body' + (insideReview ? ' workspace-review' : '')}>
      <Routes>
        <Route index element={<WorkspaceStart projectId={projectId}/>}/>
        <Route path="documents" element={<DocumentsPage projectId={projectId}/>}/>
        <Route path="prepare" element={<NormativesPage projectId={projectId}/>}/>
        <Route path="comparisons" element={<ComparisonsPage projectId={projectId} approved={project.state === 'approved'}/>}/>
        <Route path="comparisons/:pairId" element={<ComparisonReviewPage projectId={projectId} approved={project.state === 'approved'}/>}/>
        <Route path="report" element={<ReportsPage projectId={projectId}/>}/>
        <Route path="*" element={<Navigate to={base} replace/>}/>
      </Routes>
    </div>
  </div></OperationsProvider>
}
