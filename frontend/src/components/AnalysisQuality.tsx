export type AnalysisTrace = {
  degraded?: boolean
  agent_runs?: Array<{
    requirement_id?: string
    steps?: Array<{tool?: string; reason?: string}>
  }>
}

type Cause = 'key'|'auth'|'balance'|'rate'|'timeout'|'network'|'truncated'|'response'|'server'|'other'

const explanations: Record<Cause,{title:string; action:string}> = {
  key: {
    title:'Ключ DeepSeek API не настроен',
    action:'Администратору нужно указать DEEPSEEK_API_KEY в .env на сервере, перезапустить API и повторить сверку.',
  },
  auth: {
    title:'DeepSeek отклонил ключ доступа (HTTP 401)',
    action:'Проверить ключ DEEPSEEK_API_KEY и настройки доступа к API, затем повторить сверку.',
  },
  balance: {
    title:'Недостаточно средств на счёте DeepSeek (HTTP 402)',
    action:'Проверить баланс учётной записи провайдера и повторить сверку.',
  },
  rate: {
    title:'Превышен лимит запросов DeepSeek (HTTP 429)',
    action:'Проверить лимиты API и повторить сверку позже.',
  },
  timeout: {
    title:'Истекло время ожидания ответа DeepSeek',
    action:'Проверить доступность API и DEEPSEEK_TIMEOUT_SECONDS; при необходимости увеличить таймаут и повторить сверку.',
  },
  network: {
    title:'Сервер не смог подключиться к DeepSeek',
    action:'Проверить сетевое соединение API-контейнера и доступность сервиса DeepSeek, затем повторить сверку.',
  },
  truncated: {
    title:'Ответ модели был обрезан',
    action:'Проверить DEEPSEEK_MAX_TOKENS и повторить сверку после исправления настройки.',
  },
  response: {
    title:'Ответ модели не удалось корректно обработать',
    action:'Проверить формат ответа и логи API. После устранения проблемы повторить сверку.',
  },
  server: {
    title:'API DeepSeek ответил ошибкой',
    action:'Проверить состояние провайдера, сетевые настройки и логи backend; повторить сверку после восстановления.',
  },
  other: {
    title:'ИИ не сформировал завершённое заключение',
    action:'Причина этого запуска не уточняется сохранённой диагностикой. Проверьте логи API и настройки DeepSeek, затем повторите сверку.',
  },
}

function causeFor(tool: string|undefined, reason: string|undefined): Cause|null {
  if (tool !== 'llm_fallback' && tool !== 'invalid_response') return null
  const message = (reason || '').toLowerCase()
  if (message.includes('deepseek_api_key') || message.includes('not configured')) return 'key'
  if (message.includes('http 401')) return 'auth'
  if (message.includes('http 402')) return 'balance'
  if (message.includes('http 429')) return 'rate'
  if (message.includes('timed out') || message.includes('timeout')) return 'timeout'
  if (message.includes('cannot connect')) return 'network'
  if (message.includes('truncated') || message.includes('max_tokens')) return 'truncated'
  if (tool === 'invalid_response' || message.includes('json') || message.includes('malformed')
      || message.includes('empty content') || message.includes('did not finish')) return 'response'
  if (message.includes('http ')) return 'server'
  return 'other'
}

function discoveredCauses(trace: AnalysisTrace|null|undefined): Cause[] {
  const causes = new Set<Cause>()
  for (const run of trace?.agent_runs || []) {
    for (const step of run.steps || []) {
      const cause = causeFor(step.tool, step.reason)
      if (cause) causes.add(cause)
    }
  }
  return [...causes]
}

export function AnalysisFallbackNotice({trace, items}: {
  trace?: AnalysisTrace|null
  items: Array<{method?: string|null}>
}) {
  const fallbackCount = items.filter(item => item.method === 'deterministic').length
  if (!trace?.degraded && fallbackCount === 0) return null
  const modelCount = items.filter(item => item.method === 'llm').length
  const reasons = discoveredCauses(trace)
  const incomplete = modelCount > 0 && fallbackCount > 0
  const title = incomplete
    ? 'Часть требований проверена без завершённого ответа ИИ'
    : 'Сверка выполнена с использованием резервного алгоритма'
  return <aside className="workspace-ai-notice" aria-label="Состояние ИИ-анализа">
    <strong>{title}</strong>
    <p>Проверено ИИ-агентом: {modelCount} из {items.length}.
      Резервным алгоритмом: {fallbackCount}.
      Резервный алгоритм ищет похожие пункты, проверяет доступные параметры и цитаты,
      но не подтверждает смысловое соответствие автоматически.
      Решение по спорным требованиям принимает инженер.</p>
    <details>
      <summary>Почему это произошло и как исправить</summary>
      <p>Модель могла быть недоступна, вернуть неподходящий ответ
        или не завершить проверку за допустимое число шагов.
        Это не означает, что документы потеряны или вся сверка завершилась ошибкой.</p>
      {reasons.length ? <div className="workspace-ai-causes">
        {reasons.map(cause => <div key={cause}>
          <strong>{explanations[cause].title}</strong>
          <p>{explanations[cause].action}</p>
        </div>)}
      </div> : <p>{explanations.other.action}</p>}
      <p>После исправления причины используйте «Повторить проверку».
        Предыдущий результат сохранится в истории.
        Если результат нужен сейчас, откройте цитаты и сохраните решение инженера по каждому требованию.</p>
    </details>
  </aside>
}

function scoreHint(method?:string|null): string {
  if (method === 'llm') return 'Это оценка модели, скорректированная автоматическими проверками. Не является вероятностью правильного заключения.'
  if (method === 'deterministic') return 'Это сходство найденного текста ЗнП с требованием ТУ. Оно не означает подтверждённого выполнения требования.'
  return 'Метод оценки не указан в сохранённом результате; показатель нельзя интерпретировать как уверенность ИИ.'
}

export function AnalysisConfidence({method, score}: {
  method?: string|null
  score?: number|null
}) {
  const isLLM = method === 'llm'
  const isSearch = method === 'deterministic'
  const label = isLLM ? 'Оценка ИИ' : isSearch ? 'Сходство текста' : 'Оценка проверки'
  const valid = typeof score === 'number' && Number.isFinite(score)
  const percent = valid ? Math.round(Math.min(1,Math.max(0,score))*100) : null
  return <div className="workspace-analysis-metrics">
    <span className={'workspace-analysis-method '+(isLLM ? 'llm' : 'local')}>
      {isLLM ? 'ИИ-агент' : isSearch ? 'Поиск и проверка правил' : 'Метод не указан'}
    </span>
    <div className="workspace-score" title={scoreHint(method)}>
      <span>{label}</span>
      <strong>{percent === null ? 'Нет данных' : `${percent}%`}</strong>
      {percent !== null && <div className={'workspace-score-track '+(isLLM ? 'llm':'local')}
        role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={100}
        aria-valuenow={percent}>
        <span style={{width:`${percent}%`}}/>
      </div>}
    </div>
  </div>
}

export function AnalysisScoreExplanation() {
  return <details className="workspace-score-explanation">
    <summary>Как понимать оценку ИИ и сходство текста</summary>
    <p><strong>Оценка ИИ</strong> — значение, возвращённое моделью и ограниченное
      проверками цитат и инженерных параметров. Это не статистическая вероятность
      правильности результата и не основание утверждать требование без проверки.</p>
    <p><strong>Сходство текста</strong> — показатель поиска пунктов ЗнП по требованию ТУ.
      Даже высокий процент не доказывает смыслового соответствия.
      Для итогового решения нужно сверять параметры и исходные цитаты.</p>
  </details>
}
