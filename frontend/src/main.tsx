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
import './theme-liquid-glass.css'

const sections = [
  {href: '/', number: '01', title: 'Проекты', category: 'Подготовка'},
  {href: '/documents', number: '02', title: 'Документы', category: 'Подготовка'},
  {href: '/normatives', number: '03', title: 'ГОСТ и НТД', category: 'Анализ'},
  {href: '/requirements', number: '04', title: 'ТУ и ЗнП', category: 'Анализ'},
  {href: '/reports', number: '05', title: 'Отчёт', category: 'Результаты'},
]

function App() {
  const location = useLocation()
  const current = sections.find(item => item.href === location.pathname) ?? sections[0]
  return <div className="app">
    <a href="#main-content" className="skip-link">Перейти к содержимому</a>
    <aside className="app-sidebar">
      <Link className="brand" to="/" aria-label="Проверка документации, на главную">
        <span className="brand-emblem" aria-hidden="true">Т<span>/</span>З</span>
        <span className="brand-copy">
          <strong>Экспертиза</strong>
          <small>Проектная документация</small>
        </span>
      </Link>

      <div className="sidebar-rule" />
      <div className="sidebar-overline">РАБОЧЕЕ ПРОСТРАНСТВО <span> / 01</span></div>
      <nav className="app-nav" aria-label="Разделы приложения">
        {sections.map((item, index) => <React.Fragment key={item.href}>
          {(index === 2 || index === 4) && <span className="sidebar-group">{item.category}</span>}
          <NavLink to={item.href} end={item.href === '/'}
            className={({isActive}) => 'nav-link' + (isActive ? ' active' : '')}>
            <span className="nav-number">{item.number}</span>
            <span className="nav-title">{item.title}</span>
            <span className="nav-arrow" aria-hidden="true">↗</span>
          </NavLink>
        </React.Fragment>)}
      </nav>

      <div className="sidebar-bottom">
        <div className="sidebar-note">
          <div className="sidebar-note-title">От документа к заключению</div>
          <p>Загрузка · Проверка · Экспертное решение</p>
          <span className="sidebar-note-caption">Инструменты анализа ТУ, ЗнП и нормативных ссылок.</span>
        </div>
        <DebugPanel/>
        <div className="sidebar-footer">ИНЖЕНЕРНАЯ ЭКСПЕРТИЗА <span> / 2026</span></div>
      </div>
    </aside>

    <div className="app-main">
      <header className="app-topbar">
        <div className="app-breadcrumb">
          <span>РАБОЧЕЕ ПРОСТРАНСТВО</span>
          <span className="breadcrumb-divider" aria-hidden="true">/</span>
          <strong>{current.title}</strong>
        </div>
        <div className="topbar-detail">
          <span className="topbar-status"><span aria-hidden="true" /> ПРОВЕРКА ДОКУМЕНТАЦИИ</span>
          <span className="topbar-monogram" aria-hidden="true">ТЗ</span>
        </div>
      </header>
      <main className="app-content" id="main-content">
        <div className="content-frame">
          <div className="page-index"><span>{current.number} / 05</span><span>ИНЖЕНЕРНЫЙ АНАЛИЗ</span></div>
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
