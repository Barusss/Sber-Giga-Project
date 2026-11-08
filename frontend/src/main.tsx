import React from 'react'
import ReactDOM from 'react-dom/client'
import {BrowserRouter, NavLink, Route, Routes, useLocation, Link} from 'react-router-dom'
import {ProjectsPage} from './pages/ProjectsPage'
import {DocumentsPage} from './pages/DocumentsPage'
import {NormativesPage} from './pages/NormativesPage'
import {RequirementsPage} from './pages/RequirementsPage'
import {ReportsPage} from './pages/ReportsPage'
import {DebugPanel} from './debug/DebugPanel'
import './styles.css'
import './theme-bitrix24.css'

type Section = {href: string; title: string; group: string; icon: string}

const sections: Section[] = [
  {href: '/', title: 'Проекты', group: 'Работа', icon: 'projects'},
  {href: '/documents', title: 'Документы', group: 'Работа', icon: 'documents'},
  {href: '/normatives', title: 'ГОСТ и НТД', group: 'Экспертиза', icon: 'normatives'},
  {href: '/requirements', title: 'ТУ и ЗнП', group: 'Экспертиза', icon: 'requirements'},
  {href: '/reports', title: 'Отчёт', group: 'Экспертиза', icon: 'reports'},
]

function NavigationIcon({name}: {name: string}) {
  const common = {fill: 'none', stroke: 'currentColor', strokeWidth: 1.8, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const}
  return <svg width="19" height="19" viewBox="0 0 24 24" aria-hidden="true" {...common}>
    {name === 'projects' && <><rect x="3" y="5" width="18" height="15" rx="2"/><path d="M3 10h18M8 5V3h8v2"/></>}
    {name === 'documents' && <><path d="M6 3h8l5 5v13H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Z"/><path d="M14 3v6h5M8 13h8M8 17h8"/></>}
    {name === 'normatives' && <><path d="M3 5.5c3-1.7 6-1.7 9 0v15c-3-1.7-6-1.7-9 0Zm9 0c3-1.7 6-1.7 9 0v15c-3-1.7-6-1.7-9 0Z"/><path d="M7 9h2m-2 4h2m6-4h2m-2 4h2"/></>}
    {name === 'requirements' && <><path d="M3 7h7M14 7h7M3 17h7m4 0h7"/><path d="m9 4 3 3-3 3m6 4-3 3 3 3"/></>}
    {name === 'reports' && <><rect x="5" y="3" width="14" height="18" rx="2"/><path d="M9 3.5h6M9 10h6M9 14h6M9 18h4"/></>}
  </svg>
}

function App() {
  const location = useLocation()
  const current = sections.find(item => item.href === location.pathname) ?? sections[0]
  let lastGroup = ''

  return <div className="app">
    <a href="#main-content" className="skip-link">Перейти к содержимому</a>
    <aside className="app-sidebar">
      <Link className="brand" to="/" aria-label="Экспертиза, перейти к проектам">
        <span className="brand-mark" aria-hidden="true"><i/><i/><i/><i/></span>
        <span className="brand-copy"><strong>Экспертиза</strong><small>Проектная документация</small></span>
      </Link>
      <div className="sidebar-overline">РАБОЧЕЕ ПРОСТРАНСТВО</div>
      <nav className="app-nav" aria-label="Разделы приложения">
        {sections.map(item => {
          const startGroup = lastGroup !== item.group
          lastGroup = item.group
          return <React.Fragment key={item.href}>
            {startGroup && <span className="sidebar-group">{item.group}</span>}
            <NavLink to={item.href} end={item.href === '/'}
              className={({isActive}) => 'nav-link' + (isActive ? ' active' : '')}>
              <span className="nav-icon"><NavigationIcon name={item.icon}/></span>
              <span className="nav-title">{item.title}</span>
            </NavLink>
          </React.Fragment>
        })}
      </nav>
      <div className="sidebar-bottom">
        <DebugPanel/>
        <div className="sidebar-footer">Инженерная экспертиза</div>
      </div>
    </aside>
    <div className="app-main">
      <header className="app-topbar">
        <div className="app-breadcrumb">
          <span>Рабочее пространство</span><span className="breadcrumb-divider">/</span>
          <strong>{current.title}</strong>
        </div>
        <div className="topbar-detail"><span className="topbar-workspace-label">Система проверки документации</span></div>
      </header>
      <main className="app-content" id="main-content">
        <div className="content-frame">
          <Routes>
            <Route path="/" element={<ProjectsPage/>}/>
            <Route path="/documents" element={<DocumentsPage/>}/>
            <Route path="/normatives" element={<NormativesPage/>}/>
            <Route path="/requirements" element={<RequirementsPage/>}/>
            <Route path="/reports" element={<ReportsPage/>}/>
          </Routes>
        </div>
      </main>
    </div>
  </div>
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode><BrowserRouter><App/></BrowserRouter></React.StrictMode>
)
