import {useEffect,useState} from 'react'
import {Project,json,request} from '../api/client'
export function ProjectsPage(){
 const [items,setItems]=useState<Project[]>([]),[name,setName]=useState(''),[error,setError]=useState('')
 const reload=()=>request<Project[]>('/projects').then(setItems).catch(e=>setError(e.message))
 useEffect(()=>{reload()},[])
 async function create(e:React.FormEvent){e.preventDefault();try{await request('/projects',json('POST',{name}));setName('');reload()}catch(e){setError(String(e))}}
 return <><h1>Проекты</h1><p className="muted">Создайте проект, затем загрузите технические условия и задание на проектирование.</p><form onSubmit={create} className="panel horizontal"><input value={name} onChange={e=>setName(e.target.value)} placeholder="Название проверки" required/><button>Создать проект</button></form>{error&&<p className="error">{error}</p>}<section className="panel"><h2>Список проектов</h2>{!items.length?<p className="muted">Пока нет проектов</p>:<table><thead><tr><th>Название</th><th>ID</th><th>Статус</th></tr></thead><tbody>{items.map(i=><tr key={i.id}><td>{i.name}</td><td><code>{i.id}</code></td><td>{i.state==='draft'?'В работе':'Утверждён'}</td></tr>)}</tbody></table>}</section></>
}
