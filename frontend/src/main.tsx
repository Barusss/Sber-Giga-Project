import React from 'react'
import ReactDOM from 'react-dom/client'
import {BrowserRouter,NavLink,Route,Routes} from 'react-router-dom'
import {ProjectsPage} from './pages/ProjectsPage'
import {DocumentsPage} from './pages/DocumentsPage'
import {NormativesPage} from './pages/NormativesPage'
import {RequirementsPage} from './pages/RequirementsPage'
import {ReportsPage} from './pages/ReportsPage'
import './styles.css'

const links=[['/','Проекты'],['/documents','Документы'],['/normatives','ГОСТ и НТД'],['/requirements','ТУ ↔ ЗнП'],['/reports','Отчёт']]
function App(){return <div className="app"><aside><div className="brand">Sber Giga<br/><span>Проверка документации</span></div><nav>{links.map(([href,label])=><NavLink key={href} to={href} end={href==='/'}>{label}</NavLink>)}</nav><div className="hint">Учебный проект · 3 модуля</div></aside><main><Routes><Route path="/" element={<ProjectsPage/>}/><Route path="/documents" element={<DocumentsPage/>}/><Route path="/normatives" element={<NormativesPage/>}/><Route path="/requirements" element={<RequirementsPage/>}/><Route path="/reports" element={<ReportsPage/>}/></Routes></main></div>}
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><BrowserRouter><App/></BrowserRouter></React.StrictMode>)
