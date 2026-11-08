import assert from 'node:assert/strict'
import {mkdir} from 'node:fs/promises'
import {spawn} from 'node:child_process'
import {chromium} from 'playwright'

const HOST = 'http://127.0.0.1:4173'
const PROJECT = 'p-visual'
const now = '2026-11-08T12:30:00Z'
const project = {id:PROJECT,name:'Подстанция «Северная»',state:'draft',created_at:now}
const docs = [
  ...Array.from({length:4},(_,i)=>({id:'tu-'+(i+1),project_id:PROJECT,kind:'tu',filename:['ТУ — основное присоединение.pdf','ТУ — резервный ввод.pdf','ТУ — распределительная сеть.pdf','ТУ — дополнительная мощность.pdf'][i],created_at:now,pagination_kind:'physical',normative_extraction_completed:true})),
  ...Array.from({length:5},(_,i)=>({id:'znp-'+(i+1),project_id:PROJECT,kind:'znp',filename:['ЗнП — генеральный проект.docx','ЗнП — электроснабжение.docx','ЗнП — инфраструктура.docx','ЗнП — технологическая часть.docx','ЗнП — расширение.docx'][i],created_at:now,pagination_kind:'logical',normative_extraction_completed:true})),
]
const pairs = [
  [1,1],[1,2],[1,3],[1,4],[1,5],[2,1],[2,2],[2,3],[3,1],[3,3],[3,4],[4,2],
].map(([tu,znp],i)=>{
  const status = i%3===0?'reviewed':i%3===1?'ready':'queued'
  const run_count = status==='queued'?0:status==='ready'?2:1
  return {id:'pair-'+String(i+1).padStart(2,'0'),project_id:PROJECT,tu_document_id:'tu-'+tu,znp_document_id:'znp-'+znp,
    tu_filename:docs[tu-1].filename,znp_filename:docs[3+znp].filename,created_at:now,status,run_count,
    current_run_id:run_count?'run-'+String(i+1).padStart(2,'0'):null,
    latest:run_count?{id:'run-'+String(i+1).padStart(2,'0'),created_at:now,is_current:true,confirmed:status==='reviewed'?3:1,
      stats:{total:3,fulfilled:1,missing:1,needs_review:1}}:null
  }
})
const checkItems = [
  {id:'v1',requirement:{id:'req1',clause_number:'3.2.1',category:'reliability',text:'Предусмотреть резервное питание потребителей первой категории.'},
   status:'missing',automatic_status:'missing',reasoning:'В задании на проектирование не найдено требование к резервному питанию первой категории.',
   matched_clause:null,tu_quote:'резервное питание потребителей первой категории',znp_quote:null,confidence_score:.87,
   is_confirmed_by_human:false,human_override_status:null,human_comment:null},
  {id:'v2',requirement:{id:'req2',clause_number:'3.2.2',category:'power',text:'Предусмотреть максимальную мощность присоединения 150 кВт.'},
   status:'fulfilled',automatic_status:'fulfilled',reasoning:'Указанная мощность совпадает со значением в ТУ.',
   matched_clause:{clause_number:'2.1',text:'Максимальная мощность присоединения составляет 150 кВт.'},
   tu_quote:'150 кВт',znp_quote:'150 кВт',confidence_score:.95,is_confirmed_by_human:true,human_override_status:'fulfilled',human_comment:'Проверено.'},
  {id:'v3',requirement:{id:'req3',clause_number:'3.2.3',category:'equipment',text:'Предусмотреть приборы учета электроэнергии.'},
   status:'needs_review',automatic_status:'needs_review',reasoning:'Найдено частичное описание приборов учета.',
   matched_clause:{clause_number:'2.4',text:'Система учета электроэнергии предусматривает монтаж оборудования.'},
   tu_quote:'приборы учета',znp_quote:'система учета электроэнергии',confidence_score:.58,
   is_confirmed_by_human:false,human_override_status:null,human_comment:null}
]
const result = {checked_requirements:checkItems,stats:{total:3,fulfilled:1,missing:1,needs_review:1},trace_info:null}
const report = {project_id:PROJECT,project_name:project.name,project_state:'draft',selected_document_id:null,
  generated_at:now,total:2,requires_attention:1,informative:0,unverified:1,active:0,items:[]}
const requirementsReport = {available_pairs:pairs.filter(p=>p.current_run_id).map(p=>({
  tu_document_id:p.tu_document_id,znp_document_id:p.znp_document_id,tu_filename:p.tu_filename,znp_filename:p.znp_filename,completed_at:now
})),pair:null,complete:false,stats:null,confirmed:0,items:[]}

function mock(path,method) {
  if (path === '/api/v1/projects') return [project]
  if (path === `/api/v1/projects/${PROJECT}`) return project
  if (path === `/api/v1/projects/${PROJECT}/documents`) return docs
  if (path === `/api/v1/projects/${PROJECT}/comparisons`) return pairs
  if (path === `/api/v1/projects/${PROJECT}/findings`) return []
  if (path.startsWith(`/api/v1/projects/${PROJECT}/expert-report`)) return {normatives:report,requirements:requirementsReport}
  if (path === '/api/v1/normatives') return []
  if (path.startsWith('/api/v1/normatives?')) return []
  if (path.startsWith('/api/v1/projects/'+PROJECT+'/comparisons/')) {
    const match = path.match(/comparisons\/(pair-\d+)/)
    const pair = pairs.find(p=>p.id === match?.[1])
    if (!pair) return {}
    if (path.endsWith('/runs')) return pair.current_run_id ?
      [{id:pair.current_run_id,created_at:now,is_current:true,confirmed:1,stats:result.stats}] : []
    if (path.match(/\/runs\/run-\d+$/)) return {
      run:{id:pair.current_run_id,created_at:now,is_current:true,confirmed:1,stats:result.stats},result
    }
    return pair
  }
  return []
}
const server = spawn(process.execPath,['node_modules/vite/bin/vite.js','preview','--host','127.0.0.1','--port','4173','--strictPort'],{
  cwd:process.cwd(),stdio:['ignore','pipe','pipe']
})
let output=''
server.stdout.on('data',d=>output+=String(d))
server.stderr.on('data',d=>output+=String(d))

async function ready() {
  for(let i=0;i<90;i++) {
    if (server.exitCode!==null) throw new Error('Vite preview stopped: '+output)
    try {const response=await fetch(HOST);if(response.ok)return} catch{}
    await new Promise(resolve=>setTimeout(resolve,400))
  }
  throw new Error('Vite preview unavailable: '+output)
}
await ready()
await mkdir('visual-artifacts',{recursive:true})
const browser = await chromium.launch({headless:true,args:['--no-sandbox']})
const problems = []
const evaluations=[]
const paths = [
  {name:'projects',path:'/'},
  {name:'documents',path:`/projects/${PROJECT}/documents`},
  {name:'prepare',path:`/projects/${PROJECT}/prepare`},
  {name:'comparisons',path:`/projects/${PROJECT}/comparisons`},
  {name:'review',path:`/projects/${PROJECT}/comparisons/pair-02`},
  {name:'report',path:`/projects/${PROJECT}/report`},
  {name:'normatives',path:'/normatives'},
]
async function inspect(viewport,label) {
  const page = await browser.newPage({viewport,deviceScaleFactor:1})
  page.on('pageerror',error=>problems.push(label+': '+error.message))
  await page.route('**/api/v1/**',async route=>{
    if(route.request().method()==='OPTIONS')return route.fulfill({status:204,headers:{
      'access-control-allow-origin':'*','access-control-allow-methods':'GET,POST,PATCH,DELETE,OPTIONS',
      'access-control-allow-headers':'content-type'
    }})
    const url = new URL(route.request().url())
    const body = mock(url.pathname,route.request().method())
    return route.fulfill({status:200,contentType:'application/json',
      headers:{'access-control-allow-origin':'*'},body:JSON.stringify(body)})
  })
  for(const item of paths) {
    await page.goto(HOST+item.path,{waitUntil:'domcontentloaded',timeout:15000})
    await page.locator('main.app-content').waitFor({timeout:15000})
    await page.waitForTimeout(350)
    const observed=await page.evaluate(()=>{
      const container=document.querySelector('.content-frame')
      const active=document.querySelector('nav.workspace-tabs a.workspace-tab.active')
      const rect=container?.getBoundingClientRect()
      const activeCss=active?getComputedStyle(active):null
      return {viewport:innerWidth,containerWidth:rect?.width||0,containerX:rect?.x||0,
        scrollWidth:document.documentElement.scrollWidth,
        activeBackground:activeCss?.backgroundColor||'',
        activeColor:activeCss?.color||'',
        activeRadius:activeCss?.borderRadius||'',
        tabsCount:document.querySelectorAll('nav.workspace-tabs a').length,
        panels:document.querySelectorAll('.panel').length}
    })
    if(observed.containerWidth<Math.max(viewport.width-180,300))problems.push(label+' '+item.name+': narrow content '+JSON.stringify(observed))
    if(observed.scrollWidth>viewport.width+2)problems.push(label+' '+item.name+': horizontal document overflow '+JSON.stringify(observed))
    if(observed.activeBackground!=='') {
      if(observed.activeBackground==='rgb(40, 68, 91)'||observed.activeBackground==='rgb(35, 62, 84)')problems.push(label+' '+item.name+': dark active tab')
      if(observed.activeRadius!=='0px')problems.push(label+' '+item.name+': active tab rounded like a button '+observed.activeRadius)
    }
    if(item.name==='comparisons') {
      const table=page.locator('table.workspace-table')
      if(!(await table.count()))problems.push(label+': comparison table not mounted')
      if(viewport.width>800 && (await table.count()) && (await table.locator('tbody tr').count())!==12)problems.push(label+': expected 12 comparison rows')
    }
    evaluations.push({view:label,page:item.name,...observed})
    console.log('Visual page:',label,item.name,'width:',observed.containerWidth,'overflow:',observed.scrollWidth>viewport.width)
    if(viewport.width>=1024||['comparisons','review','documents'].includes(item.name)) {
      await page.screenshot({path:`visual-artifacts/${label}-${item.name}.png`,fullPage:true,animations:'disabled'})
    }
  }
  await page.close()
}
try {
  await inspect({width:1440,height:900},'desktop')
  await inspect({width:1024,height:768},'tablet')
  await inspect({width:390,height:844},'mobile')
} finally {
  await browser.close()
  server.kill('SIGTERM')
  server.stdout.destroy()
  server.stderr.destroy()
}
console.log('Visual audits:',JSON.stringify(evaluations,null,2))
if(problems.length)throw new Error('Visual audit errors:\n'+problems.join('\n'))
console.log('Visual layout checks passed for all pages and viewports')
