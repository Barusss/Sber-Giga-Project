import {useEffect,useState} from 'react'
import {Normative,json,request} from '../api/client'

type CheckedStandard={
 code:string
 reference_kind:string
 status:string
 registry_status:string|null
 replacement:string|null
 source_url:string|null
 checked_at:string|null
 pages:number[]
 reason:string|null
}
type VerificationReport={
 document_id:string
 total:number
 active:number
 obsolete:number
 unknown:number
 findings_created:number
 findings_already_existing:number
 checked:CheckedStandard[]
}

export function NormativesPage(){
 const [items,setItems]=useState<Normative[]>([])
 const [code,setCode]=useState(''),[title,setTitle]=useState(''),[status,setStatus]=useState('active'),[replacement,setReplacement]=useState(''),[error,setError]=useState('')
 const [documentId,setDocumentId]=useState(''),[checking,setChecking]=useState(false),[report,setReport]=useState<VerificationReport|null>(null)
 const reload=()=>request<Normative[]>('/normatives').then(setItems).catch(e=>setError(String(e)))
 useEffect(()=>{reload()},[])
 async function create(e:React.FormEvent){
  e.preventDefault()
  try{
   setError('')
   await request('/normatives',json('POST',{code,title,status,replacement:replacement||null}))
   setCode('');setTitle('');setReplacement('');reload()
  }catch(e){setError(String(e))}
 }
 async function check(e:React.FormEvent){
  e.preventDefault()
  if(!documentId.trim())return
  setChecking(true);setError('');setReport(null)
  try{
   const result=await request<VerificationReport>(`/documents/${encodeURIComponent(documentId.trim())}/check-normatives`,json('POST',{}))
   setReport(result)
   await reload()
  }catch(e){setError(String(e))}
  finally{setChecking(false)}
 }
 const statusLabel=(value:string)=>{
  if(value==='active')return 'Действует'
  if(value==='obsolete')return 'Не действует / заменён'
  return 'Не подтверждено'
 }
 return <><h1>Проверка ГОСТ и НТД</h1>
  <p className="muted">Автоматическое извлечение ссылок и проверка ГОСТ по официальному порталу Росстандарта. Если статус не удалось получить, он остаётся неподтверждённым.</p>
  <section className="panel stack">
   <h2>Проверить документ</h2>
   <p className="muted">Сначала извлеките нормативные факты из документа. Затем введите его ID, чтобы проверить обозначения в реестре.</p>
   <form onSubmit={check} className="stack">
    <input value={documentId} onChange={e=>setDocumentId(e.target.value)} placeholder="ID загруженного документа" required/>
    <button disabled={checking}>{checking?'Проверяем официальный реестр…':'Проверить актуальность'}</button>
   </form>
   {report&&<><p>Проверено: {report.total}. Действующих: {report.active}, не действующих: {report.obsolete}, не подтверждено: {report.unknown}. Новых замечаний: {report.findings_created}.</p>
    <div style={{overflowX:'auto'}}><table><thead><tr><th>Стандарт</th><th>Тип ссылки</th><th>Статус</th><th>Замена</th><th>Страницы PDF</th><th>Источник</th></tr></thead>
    <tbody>{report.checked.map(item=><tr key={item.code}>
     <td>{item.code}</td>
     <td>{item.reference_kind==='informative'?'Справочная':'Нормативная'}</td>
     <td>{item.registry_status||statusLabel(item.status)}</td>
     <td>{item.replacement||'—'}</td>
     <td>{item.pages.join(', ')||'—'}</td>
     <td>{item.source_url?<a href={item.source_url} target="_blank" rel="noopener noreferrer">Росстандарт</a>:'Не подтверждено'}</td>
    </tr>)}</tbody></table></div>
    <p className="muted">Замечания о заменённых или отменённых стандартах сохранены в отчёте проекта. Наличие замены не означает автоматической обязанности её применить.</p>
   </>}
  </section>
  <form onSubmit={create} className="panel stack"><h2>Добавить норматив вручную</h2>
   <input value={code} onChange={e=>setCode(e.target.value)} placeholder="Обозначение" required/>
   <input value={title} onChange={e=>setTitle(e.target.value)} placeholder="Название" required/>
   <select value={status} onChange={e=>setStatus(e.target.value)}><option value="active">Действует</option><option value="obsolete">Устарел</option><option value="unknown">Неизвестно</option></select>
   <input value={replacement} onChange={e=>setReplacement(e.target.value)} placeholder="Замена (необязательно)"/><button>Добавить</button>
  </form>
  {error&&<p className="error">{error}</p>}
  <section className="panel"><h2>Локальный реестр</h2>
   <p className="muted">Это сохранённые сведения, а не проверка в реальном времени. Для актуального статуса запускайте проверку документа.</p>
   <table><thead><tr><th>Норматив</th><th>Название</th><th>Статус</th><th>Замена</th><th>Источник</th></tr></thead>
   <tbody>{items.map(i=><tr key={i.id}><td>{i.code}</td><td>{i.title}</td><td>{statusLabel(i.status)}</td><td>{i.replacement??'—'}</td><td>{i.source_url?<a href={i.source_url} target="_blank" rel="noopener noreferrer">Карточка</a>:'—'}</td></tr>)}</tbody></table>
   {!items.length&&<p className="muted">Реестр пока пуст.</p>}
  </section>
 </>
}
