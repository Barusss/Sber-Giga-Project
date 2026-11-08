import React from 'react'
import ReactDOM from 'react-dom/client'
import {BrowserRouter, NavLink, Route, Routes, Link} from 'react-router-dom'
import {ProjectsPage} from './pages/ProjectsPage'
import {ProjectWorkspacePage} from './pages/ProjectWorkspacePage'
import {DocumentsPage} from './pages/DocumentsPage'
import {NormativesPage} from './pages/NormativesPage'
import {RequirementsPage} from './pages/RequirementsPage'
import {ReportsPage} from './pages/ReportsPage'
import {DebugPanel} from './debug/DebugPanel'
import './styles.css'
import './theme-bitrix24.css'
import './theme-workspace.css'
import './project-workspace.css'

function App() {
  return <div className="app">
    <a href="#main-content" className="skip-link">Перейти к содержимому</a>
    <header className="app-topbar">
      <div className="app-topbar-inner">
        <Link className="brand" to="/" aria-label="Экспертиза — к проектам">
          <span className="brand-name">Экспертиза</span>
        </Link>
        <nav className="app-nav" aria-label="Основная навигация">
          <NavLink to="/" end className={({isActive}) => 'nav-link'+(isActive?' active':'')}>
            Проекты
          </NavLink>
          <NavLink to="/normatives" className={({isActive}) => 'nav-link'+(isActive?' active':'')}>
            Реестр НТД
          </NavLink>
        </nav>
        <div className="app-topbar-actions"><DebugPanel/></div>
      </div>
    </header>
    <main className="app-content" id="main-content">
      <div className="content-frame">
        <Routes>
          <Route path="/" element={<ProjectsPage/>}/>
          <Route path="/projects/:projectId/*" element={<ProjectWorkspacePage/>}/>
          <Route path="/normatives" element={<NormativesPage/>}/>
          {/* Existing direct links stay available for compatibility. */}
          <Route path="/documents" element={<DocumentsPage/>}/>
          <Route path="/requirements" element={<RequirementsPage/>}/>
          <Route path="/reports" element={<ReportsPage/>}/>
          <Route path="*" element={<ProjectsPage/>}/>
        </Routes>
      </div>
    </main>
  </div>
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode><BrowserRouter><App/></BrowserRouter></React.StrictMode>
)
