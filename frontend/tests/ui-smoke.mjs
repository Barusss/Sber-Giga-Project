// Real Chromium rendering checks against the Vite app with deterministic API data.
// The test deliberately exercises the same navigation and classes as production.
import assert from 'node:assert/strict'
import {mkdir} from 'node:fs/promises'
import {chromium} from 'playwright'
import {createServer} from 'vite'

const root = 'http://127.0.0.1:4173'
const output = new URL('../ui-artifacts/', import.meta.url).pathname
const project = {id:'prj-1',name:'Реконструкция подстанции «Северная»',state:'draft',created_at:'2026-11-07T09:10:00Z'}
const documents = [
  ...Array.from({length:4},(_,i)=>({
    id:`tu-${i+1}`,project_id:project.id,kind:'tu',
    filename:`ТУ — объект ${i+1}.pdf`,created_at:'2026-11-07T09:10:00Z',
    pagination_kind:'physical',normative_extraction_completed:true,
  })),
  ...Array.from({length:5},(_,i)=>({
    id:`znp-${i+1}`,project_id:project.id,kind:'znp',
    filename:`Задание на проектирование ${i+1}.pdf`,
    created_at:'2026-11-07T09:10:00Z',
    pagination_kind:'physical',normative_extraction_completed:true,
  })),
]
const pairKeys = documents.filter(d=>d.kind==='tu')
  .flatMap(tu=>documents.filter(d=>d.kind==='znp')
    .map(znp=>[tu,znp])).slice(0,12)
const pairs = pairKeys.map(([tu,znp],i)=>({
  id:`cmp-${i+1}`,project_id:project.id,
  tu_document_id:tu.id,znp_document_id:znp.id,
  tu_filename:tu.filename,znp_filename:znp.filename,
  status:i===0?'queued':i===1?'ready':i===2?'reviewed':'queued',
  run_count:i===0?0:i===1?2:1,
  current_run_id:i===0?null:'run-2',
  latest:i===0?null:{id:'run-2',created_at:'2026-11-08T08:35:00Z',is_current:true,confirmed:i===2?3:1,
    stats:{total:3,fulfilled:1,missing:1,needs_review:1}},
  created_at:'2026-11-07T09:10:00Z',
}))
const requirements = [
  {
    id:'ver-1',
    requirement:{id:'req-1',clause_number:'3.2.1',category:'reliability',
      text:'Предусмотреть резервное электроснабжение потребителей первой категории.'},
    status:'missing',automatic_status:'missing',
    reasoning:'В задании на проектирование не найдено требования к резервному питанию.',
    tu_quote:'Предусмотреть резервное электроснабжение потребителей первой категории.',
    znp_quote:null,matched_clause:null,
    is_confirmed_by_human:false,human_override_status:null,human_comment:null,
    confidence_score:0.89,
  },{
    id:'ver-2',
    requirement:{id:'req-2',clause_number:'3.2.2',category:'power',
      text:'Максимальная разрешённая мощность — 150 кВт.'},
    status:'fulfilled',automatic_status:'fulfilled',
    reasoning:'В ЗнП найдено соответствующее значение мощности.',
    tu_quote:'Максимальная разрешённая мощность — 150 кВт.',
    znp_quote:'Предусмотрена расчётная мощность 150 кВт.',
    matched_clause:{clause_number:'4.1.1',text:'Предусмотрена расчётная мощность 150 кВт.'},
    is_confirmed_by_human:true,human_override_status:'fulfilled',
    human_comment:'Проверено',confidence_score:0.95,
  },{
    id:'ver-3',
    requirement:{id:'req-3',clause_number:'3.2.3',category:'metering',
      text:'Установить счётчик электроэнергии класса точности 0.5S.'},
    status:'needs_review',automatic_status:'needs_review',
    reasoning:'Требуется ручная проверка параметров счётчика.',
    tu_quote:'Установить счётчик электроэнергии класса точности 0.5S.',
    znp_quote:'Приборы учёта уточняются в рабочей документации.',
    matched_clause:{clause_number:'5.1',text:'Приборы учёта уточняются в рабочей документации.'},
    is_confirmed_by_human:false,human_override_status:null,human_comment:null,
    confidence_score:0.6,
  },
]
const run = {id:'run-2',created_at:'2026-11-08T08:35:00Z',is_current:true,confirmed:1,
  stats:{total:3,fulfilled:1,missing:1,needs_review:1}}
const result = {checked_requirements:requirements,
  stats:{total:3,fulfilled:1,missing:1,needs_review:1},trace_info:null}
const history = [run,{...run,id:'run-1',is_current:false,confirmed:0,
  created_at:'2026-11-07T10:35:00Z'}]
const normativesReport = {project_id:project.id,project_name:project.name,project_state:'draft',
  selected_document_id:'tu-1',generated_at:'2026-11-08T08:35:00Z',
  total:0,requires_attention:0,informative:0,unverified:0,active:0,items:[]}
const requirementsReport = {available_pairs:[],
  pair:null,complete:false,stats:null,confirmed:0,items:[]}

function respond(method,url){
  const u = new URL(url)
  const path = u.pathname.replace(/^\/api\/v1/,'')
  if (path==='/projects' && method==='GET') return [project]
  if (path==='/projects' && method==='POST') return {...project,id:'prj-new',name:'Новый тестовый проект'}
  if (/^\/projects\/[^/]+$/.test(path)) return {...project,id:path.split('/')[2]}
  if (/^\/projects\/[^/]+\/documents$/.test(path)) return path.includes('prj-new')?[]:documents
  if (/^\/projects\/[^/]+\/comparisons$/.test(path)) return path.includes('prj-new')?[]:pairs
  if (path.endsWith('/comparisons/cmp-2')) return pairs[1]
  if (path.endsWith('/comparisons/cmp-2/runs')) return history
  if (path.endsWith('/comparisons/cmp-2/runs/run-2')) return {run,result}
  if (path.endsWith('/comparisons/cmp-2/runs/run-1')) return {run:history[1],result}
  if (path==='/normatives') return []
  if (/^\/projects\/[^/]+\/findings$/.test(path)) return []
  if (/^\/projects\/[^/]+\/expert-report$/.test(path)) return {
    normatives:normativesReport,requirements:requirementsReport,
  }
  if (/^\/documents\/[^/]+\/fragments$/.test(path)) return [
    {id:'frag-1',document_id:path.split('/')[2],page:1,position:0,
      text:'1.1. Требования к оборудованию и безопасности.',extraction_method:'text',block_type:'paragraph'},
  ]
  throw new Error(`No API fixture for ${method} ${path}`)
}

async function screenshot(page,name,print=false) {
  const data = await page.screenshot({fullPage:true,path:`${output}/${name}.png`})
  if (print && process.env.UI_SCREENSHOT_LOG==='1') {
    const b64 = data.toString('base64')
    for (let i=0,index=0;i<b64.length;i+=6000,index++) {
      console.log(`UI_SHOT_CHUNK:${name}:${index}:${b64.slice(i,i+6000)}`)
    }
  }
}

async function checkNoOverflow(page,width,name) {
  const result = await page.evaluate(() => ({
    viewport:window.innerWidth,
    documentWidth:document.documentElement.scrollWidth,
    mainWidth:document.querySelector('main.app-content')?.getBoundingClientRect().width||0,
    mainRight:document.querySelector('main.app-content')?.getBoundingClientRect().right||0,
  }))
  assert(result.documentWidth <= width+2,
    `${name} at ${width}: horizontal page overflow ${JSON.stringify(result)}`)
  assert(result.mainWidth >= width-3,
    `${name} at ${width}: main content narrower than viewport ${JSON.stringify(result)}`)
}

async function verifyTabs(page,active) {
  const tabs = await page.locator('.workspace-tabs > .workspace-tab').evaluateAll(anchors =>
    anchors.map(a=>{
      const css=getComputedStyle(a)
      return {
        title:a.textContent?.trim(),active:a.classList.contains('active'),
        background:css.backgroundColor,radius:css.borderTopLeftRadius,
        color:css.color,bottom:css.borderBottomColor,
        box:a.getBoundingClientRect().toJSON(),
      }
    }))
  assert.equal(tabs.length,4,'Four project stages visible')
  assert.equal(tabs.filter(tab=>tab.active).length,1,'Exactly one active project tab')
  assert.equal(tabs.find(tab=>tab.active)?.title,active)
  for(const tab of tabs){
    assert(['rgba(0, 0, 0, 0)','transparent'].includes(tab.background),
      `${active}: unexpected tab background ${JSON.stringify(tab)}`)
    assert.equal(tab.radius,'0px',`${active}: unexpected pill corner`)
  }
}

async function review(page,width,errors) {
  await page.setViewportSize({width,height:900})
  const routes=[
    ['/',null,'projects'],
    ['/projects/prj-1/documents','Документы','documents'],
    ['/projects/prj-1/prepare','Подготовка','prepare'],
    ['/projects/prj-1/comparisons','Сверки','comparisons'],
    ['/projects/prj-1/comparisons/cmp-2?run=run-2','Сверки','review'],
    ['/projects/prj-1/report','Отчёт','report'],
  ]
  for(const [route,tab,name] of routes) {
    await page.goto(root+route,{waitUntil:'networkidle'})
    await page.locator('main.app-content').waitFor()
    if(tab) await verifyTabs(page,tab)
    await checkNoOverflow(page,width,name)
    if(width===1440) await screenshot(page,`${name}-desktop`,true)
    if(width===390 && (name==='comparisons'||name==='review'||name==='documents')) {
      await screenshot(page,`${name}-mobile`,name==='comparisons')
    }
  }
  await page.goto(root+'/projects/prj-1/comparisons',{waitUntil:'networkidle'})
  await page.getByRole('link',{name:'Подготовка'}).hover()
  await verifyTabs(page,'Сверки')
  const tabBackground = await page.getByRole('link',{name:'Подготовка'}).evaluate(
    element=>getComputedStyle(element).backgroundColor)
  assert(['transparent','rgba(0, 0, 0, 0)'].includes(tabBackground),
    'Hovering inactive project tab must not produce a filled button')
  const primaryButton = await page.getByRole('button',{name:'Создать сверки'}).evaluate(element=>({
    background:getComputedStyle(element).backgroundColor,
    color:getComputedStyle(element).color,
  }))
  assert.equal(primaryButton.background,'rgb(8, 126, 171)')
  assert.equal(primaryButton.color,'rgb(255, 255, 255)')
  await page.getByRole('button',{name:'Матрица'}).click()
  assert(await page.locator('.workspace-matrix tbody tr').count()===4,
    'Matrix contains four TU document rows')
  assert(await page.locator('.workspace-matrix thead th').count()===6,
    'Matrix contains five ZnP columns')
  await checkNoOverflow(page,width,'matrix')
  if(width===1440)await screenshot(page,'matrix-desktop',true)
  if(width===390)await screenshot(page,'matrix-mobile')
  await page.goto(root+'/projects/prj-1/comparisons/cmp-2?run=run-2',{waitUntil:'networkidle'})
  assert.equal(await page.locator('.workspace-requirement').count(),3,'Remarks show all three requirements')
  await page.getByRole('tab',{name:/История запусков/}).click()
  assert.equal(await page.locator('.workspace-run-row').count(),2,'History shows two runs')
  await page.locator('.workspace-run-select').first().click()
  assert.equal(await page.locator('.workspace-requirement').count(),3,'Selecting a run returns to remarks')
  assert.equal(errors.length,0,`Browser exceptions: ${errors.join(' | ')}`)
}

async function main(){
  await mkdir(output,{recursive:true})
  const vite=await createServer({server:{host:'127.0.0.1',port:4173,strictPort:true}})
  await vite.listen()
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']})
  try{
    for(const width of [390,768,1440,1920]){
      const context=await browser.newContext({viewport:{width,height:900}})
      const page=await context.newPage()
      const errors=[]
      page.on('pageerror',error=>errors.push(error.message))
      await page.route('**/api/v1/**',async route=>{
        try{
          const data=respond(route.request().method(),route.request().url())
          await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(data)})
        }catch(error){
          errors.push(error.message)
          await route.fulfill({status:500,contentType:'application/json',
            body:JSON.stringify({detail:error.message})})
        }
      })
      await review(page,width,errors)
      await context.close()
      console.log(`UI_PASS viewport=${width} routes=6 matrix=4x5`)
    }
    console.log('UI_PASS all pages, responsive widths and active tab styling')
  }finally{
    await browser.close()
    await vite.close()
  }
}

main().catch(error=>{console.error(error);process.exitCode=1})
