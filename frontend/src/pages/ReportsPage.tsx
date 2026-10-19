import {useEffect,useState} from 'react'
import {useSearchParams} from 'react-router-dom'
import {Document,Finding,Project,request,json} from '../api/client'

type Quote={fragment_id:string;page:number;quote:string}
type Review={id:string;review_status:'pending'|'accepted'|'rejected';severity:string;explanation:string;suggested_action:string|null}
type Standard={
 document_id:string;document_name:string;code:string;reference_kind:string
 status:string;registry_status:string|null;title:string|null
 replacement:string|null;source_url:string|null;checked_at:string|null
 attempted_at:string|null;reason:string|null
 section:'requires_attention'|'informative'|'unverified'|'active'
 evidence:Quote[];finding:Review|null
}
type NormativeReport={
 project_id:string;project_name:string;project_state:string
 selected_document_id:string|null
 generated_at:string;total:number;requires_attention:number;informative:number
 unverified:number;active:number;items:Standard[]
}

const date=(value:string|null)=>value?new Date(value).toLocaleString('ru-RU',{dateStyle:'short',timeStyle:'short'}):'—'
const reviewLabel=(value:string)=>{
 if(value==='accepted')return 'Подтверждено экспертом'
 if(value==='rejected')return 'Отклонено экспертом'
 return 'Ожидает решения эксперта'
}
const unknownLabel=(reason:string|null)=>{
 if(reason==='unsupported_or_incomplete_designation')return 'Данный вид обозначения пока не поддерживается реестром ГОСТ'
 if(reason==='official_verification_unavailable')return 'Не удалось подтвердить сведения на официальном сайте'
 if(reason==='exact_official_card_not_found')return 'Точная карточка стандарта не найдена'
 if(reason==='not_yet_verified')return 'После обновления требуется повторная проверка актуальности'
 return 'Статус требует дополнительной проверки'
}
const groups=[
 {key:'requires_attention',title:'Требуют внимания',subtitle:'Недействующие стандарты в нормативных ссылках. Необходима экспертная оценка применимости замены.'},
 {key:'informative',title:'Справочные упоминания',subtitle:'Недействующие стандарты в предисловии или библиографии. Не всегда требуют изменения документа.'},
 {key:'unverified',title:'Не удалось проверить',subtitle:'По этим обозначениям официальный статус не подтверждён. Система не делает вывод об их действии.'},
 {key:'active',title:'Действующие стандарты',subtitle:'Статус подтверждён на дату последней успешной проверки.'},
] as const

export function ReportsPage(){
 const [searchParams,setSearchParams]=useSearchParams()
 const [projects,setProjects]=useState<Project[]>([])
 const [project,setProject]=useState(searchParams.get('project_id')||'')
 const [documentSelection,setDocumentSelection]=useState(searchParams.get('document_id')||'latest')
 const [documents,setDocuments]=useState<Document[]>([])
 const [activeDocumentId,setActiveDocumentId]=useState<string|null>(null)
 const [loading,setLoading]=useState(false)
 const [refreshKey,setRefreshKey]=useState(0)
 const [findings,setFindings]=useState<Finding[]>([])
 const [report,setReport]=useState<NormativeReport|null>(null)
 const [error,setError]=useState('')
 const [busy,setBusy]=useState(false)
 const [state,setState]=useState('draft')

 useEffect(()=>{
  request<Project[]>('/projects').then(setProjects).catch(e=>setError(String(e)))
 },[])

 function changeProject(id:string){
  setProject(id)
  setDocumentSelection('latest')
  setSearchParams(id?{project_id:id,document_id:'latest'}:{})
 }
 function changeDocument(value:string){
  setDocumentSelection(value)
  if(project)setSearchParams({project_id:project,document_id:value})
 }
 function reload(){setRefreshKey(value=>value+1)}

 useEffect(()=>{
  if(!project){
   setReport(null);setFindings([]);setDocuments([]);setActiveDocumentId(null)
   return
  }
  let cancelled=false
  async function load(){
   setLoading(true);setError('');setReport(null)
   try{
    const path=`/projects/${encodeURIComponent(project)}`
    const [nextFindings,nextProject,nextDocuments]=await Promise.all([
     request<Finding[]>(`${path}/findings`),
     request<Project>(path),
     request<Document[]>(`${path}/documents`),
    ])
    const sorted=[...nextDocuments].sort((a,b)=>
     new Date(b.created_at).getTime()-new Date(a.created_at).getTime()||
     b.id.localeCompare(a.id)
    )
    let selectedId:string|null=null
    if(documentSelection==='latest')selectedId=sorted[0]?.id||null
    else if(documentSelection!=='all'){
     if(!sorted.some(item=>item.id===documentSelection))throw new Error('Документ не принадлежит выбранному проекту')
     selectedId=documentSelection
    }
    const url=`${path}/normative-report${selectedId?`?document_id=${encodeURIComponent(selectedId)}`:''}`
    const nextReport=await request<NormativeReport>(url)
    if(cancelled)return
    setDocuments(sorted);setFindings(nextFindings);setState(nextProject.state)
    setActiveDocumentId(selectedId);setReport(nextReport)
   }catch(e){
    if(!cancelled){setError(String(e));setReport(null);setActiveDocumentId(null)}
   }finally{if(!cancelled)setLoading(false)}
  }
  load()
  return ()=>{cancelled=true}
 },[project,documentSelection,refreshKey])

 async function review(item:Review,value:'accepted'|'rejected'){
  if(!project||busy)return
  setBusy(true)
  try{
   await request(`/findings/${item.id}/review`,json('PATCH',{review_status:value}))
   reload()
  }catch(e){setError(String(e))}
  finally{setBusy(false)}
 }
 async function approve(){
  if(!project||busy)return
  setBusy(true)
  try{
   await request(`/projects/${project}/approve`,{method:'POST'})
   reload()
  }catch(e){setError(String(e))}
  finally{setBusy(false)}
 }
 function saveJson(payload:unknown,filename:string){
  const url=URL.createObjectURL(new Blob([JSON.stringify(payload,null,2)],{type:'application/json'}))
  const a=document.createElement('a')
  a.href=url;a.download=filename;a.click();URL.revokeObjectURL(url)
 }
 function downloadExpert(){
  if(!report||loading)return
  saveJson(report,`expert-normatives-${project}-${report.selected_document_id||'all'}.json`)
 }
 async function downloadTechnical(){
  if(!project)return
  try{
   const data=await request(`/projects/${encodeURIComponent(project)}/report`)
   saveJson(data,`technical-findings-${project}-all.json`)
  }catch(e){setError(String(e))}
 }
 const selectionLabel=activeDocumentId?
  `${documents.find(d=>d.id===activeDocumentId)?.filename||'Документ'} · ${activeDocumentId.slice(0,8)}…`:
  'Весь проект'
 
 const otherFindings=findings.filter(f=>
  !(f.module==='normatives'&&f.type==='obsolete_normative')&&
  (!activeDocumentId||f.evidence.some(e=>e.document_id===activeDocumentId))
 )
 const pending=findings.some(f=>f.review_status==='pending')
 return <div className="expert-report">
  <header className="report-header">
   <h1>Экспертный отчёт</h1>
   <p className="muted">Актуальность ГОСТ, исходные цитаты, официальные источники и решения эксперта. Статусы взяты из сохранённых проверок.</p>
  </header>
  <section className="panel stack no-print">
   <label>Проект
    <select value={project} onChange={e=>changeProject(e.target.value)}>
     <option value="">Выберите проект</option>
     {projects.map(item=><option key={item.id} value={item.id}>{item.name}</option>)}
    </select>
   </label>
   {project&&<>
    <label>Отображаемые документы
     <select value={documentSelection} onChange={e=>changeDocument(e.target.value)} disabled={loading}>
      <option value="latest">Последняя загрузка</option>
      <option value="all">Весь проект ({documents.length} документов)</option>
      {documents.map(d=><option key={d.id} value={d.id}>
       {d.filename} · {date(d.created_at)} · {d.id.slice(0,8)}…
      </option>)}
     </select>
    </label>
    <p className="muted">Выбранный документ определяет статистику, печатный отчёт и экспертный JSON. Все загрузки остаются в проекте.</p>
    <div className="horizontal">
     <span>Статус проекта: <b>{state==='approved'?'Утверждён':'В работе'}</b></span>
     <button className="secondary" onClick={reload} disabled={loading}>Обновить отчёт</button>
     <button className="secondary" onClick={downloadExpert} disabled={!report||loading}>Экспертный JSON (выбор)</button>
     <button className="secondary" onClick={downloadTechnical} disabled={loading}>Технический JSON (весь проект)</button>
     <button className="secondary" onClick={()=>window.print()} disabled={!report||loading}>Печать / Сохранить PDF</button>
     <button onClick={approve} disabled={busy||loading||state==='approved'||pending||!report}>Утвердить проект</button>
    </div>
    {pending&&<p className="muted">Утверждение возможно только после рассмотрения всех замечаний проекта, даже если в отчёте выбран один документ.</p>}
   </>}
  </section>
  {error&&<p className="error no-print">{error}</p>}
  {loading&&<p className="muted no-print">Загружаем отчёт…</p>}
  {report&&<>
   <section className="report-cover panel">
    <p className="report-overline">Результаты проверки нормативных ссылок</p>
    <h2>{report.project_name}</h2>
    <p><b>Область отчёта:</b> {selectionLabel}</p>
    <p className="muted">Сводка сформирована {date(report.generated_at)}. Даты конкретных проверок указаны у стандартов.</p>
    <div className="report-metrics">
     <div><strong>{report.total}</strong><span>Обозначений в выбранных документах</span></div>
     <div><strong>{report.requires_attention}</strong><span>Требуют внимания</span></div>
     <div><strong>{report.informative}</strong><span>Справочные замечания</span></div>
     <div><strong>{report.unverified}</strong><span>Не подтверждены</span></div>
     <div><strong>{report.active}</strong><span>Действуют</span></div>
    </div>
   </section>
   {report.items.length===0&&<section className="panel">
    <p>Нормативные ссылки пока не извлечены. Сначала обработайте документ в разделе «ГОСТ и НТД».</p>
   </section>}
   {report.items.some(item=>item.reason==='not_yet_verified')&&<p className="report-warning">
    Для части документов нет сохранённой проверки. Перейдите в «ГОСТ и НТД» и повторно проверьте их ID: результаты предыдущей версии реестра не содержат даты проверки.
   </p>}
   {groups.map(group=>{
    const items=report.items.filter(item=>item.section===group.key)
    return <section key={group.key} className="panel report-section">
     <div className="report-section-heading"><div><h2>{group.title}</h2><p className="muted">{group.subtitle}</p></div><span className="report-count">{items.length}</span></div>
     {items.length===0?<p className="muted">Нет записей в этой категории.</p>:items.map(item=>
      <article key={item.document_id+item.code} className="report-entry">
       <div className="report-entry-heading">
        <div><h3>{item.code}</h3>{item.title&&<p>{item.title}</p>}</div>
        <span className={'report-status status-'+item.status}>{item.registry_status||(item.status==='unknown'?'Не подтверждено':'Действует')}</span>
       </div>
       <div className="report-details">
        <span><b>Документ:</b> {item.document_name}</span>
        <span><b>Страницы PDF:</b> {Array.from(new Set(item.evidence.map(e=>e.page))).sort((a,b)=>a-b).join(', ')||'—'}</span>
        <span><b>Тип упоминания:</b> {item.reference_kind==='informative'?'Справочное':'Нормативное'}</span>
        <span><b>Дата проверки:</b> {date(item.checked_at)}</span>
        {item.attempted_at&&!item.checked_at&&<span><b>Последняя попытка:</b> {date(item.attempted_at)}</span>}
       </div>
       {item.replacement&&<p className="report-replacement"><b>Указанный заменяющий стандарт:</b> {item.replacement}</p>}
       {item.status==='unknown'&&<p className="muted">{unknownLabel(item.reason)}</p>}
       {item.source_url&&<p><a href={item.source_url} target="_blank" rel="noopener noreferrer">Официальная карточка Росстандарта</a></p>}
       <div className="report-evidence">
        <b>Цитаты из документа</b>
        {item.evidence.length===0?<p className="muted">Нет подтверждённых цитат.</p>:item.evidence.map(e=>
         <blockquote key={e.fragment_id+e.quote}>«{e.quote}» <small>PDF, стр. {e.page}</small></blockquote>
        )}
       </div>
       {item.finding&&<div className="report-decision">
        <p><b>Решение эксперта:</b> {reviewLabel(item.finding.review_status)}</p>
        {item.finding.suggested_action&&<p className="muted">{item.finding.suggested_action}</p>}
        {state!=='approved'&&<div className="horizontal no-print">
         <button disabled={busy} onClick={()=>review(item.finding!,'accepted')}>Подтвердить замечание</button>
         <button disabled={busy} className="secondary" onClick={()=>review(item.finding!,'rejected')}>Отклонить замечание</button>
        </div>}
       </div>}
      </article>
     )}
    </section>
   })}
   {otherFindings.length>0&&<section className="panel report-section">
    <h2>Другие замечания проекта</h2>
    {otherFindings.map(item=><article className="report-entry" key={item.id}>
     <h3>{item.type}</h3><p>{item.explanation}</p>
     {item.evidence.map((e,i)=><blockquote key={i}>«{e.quote}» (стр. {e.page??'—'})</blockquote>)}
     <p><b>Решение эксперта:</b> {reviewLabel(item.review_status)}</p>
     {state!=='approved'&&<div className="horizontal no-print">
      <button disabled={busy} onClick={()=>review({id:item.id,review_status:item.review_status,severity:item.severity,explanation:item.explanation,suggested_action:item.suggested_action},'accepted')}>Подтвердить</button>
      <button disabled={busy} className="secondary" onClick={()=>review({id:item.id,review_status:item.review_status,severity:item.severity,explanation:item.explanation,suggested_action:item.suggested_action},'rejected')}>Отклонить</button>
     </div>}
    </article>)}
   </section>}
   <p className="report-footnote">Автоматическая проверка не является юридическим заключением. Наличие заменяющего стандарта не означает автоматической обязанности заменить ссылку: применимость определяет эксперт. Неподтверждённые статусы нельзя трактовать как действующие или отменённые.</p>
  </>}
 </div>
}
