export type AnalysisTrace = {
  atomization?: {
    mode: string
    clauses_total: number
    model_calls: number
    clauses_split: number
    atoms_total: number
    fallback_clauses: number
    fallback_reasons?: Record<string, number>
    degraded: boolean
  }
  degraded?: boolean
  ai_evaluated_requirements?: number
  proposed_links?: number
  validated_links?: number
  rejected_links?: Record<string, number>
  targeted_limit_reached?: boolean
  stopped_due_to_invalid_evidence?: boolean
  llm_usage?: {
    model_calls_total: number
    model_calls_limit: number
    attempted_by_phase: Record<string, number>
    responses_by_phase: Record<string, number>
    errors_by_phase: Record<string, number>
    blocked_by_limit: Record<string, number>
    limit_reached: boolean
  }
  znp_atom_results?: Array<{
    atom_id: string
    clause_number: string
    text: string
    page?: number|null
    model_checked: boolean
    candidates_count: number
    links: Array<{
      tu_requirement_id: string
      tu_clause_number: string
      tu_text: string
      relation: string
      confidence?: number|null
      search_score?: number|null
      tu_quote: string
      znp_quote: string
      reasoning?: string
    }>
  }>
  matching_limit_reached?: boolean
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
  const modelCount = items.filter(item => item.method === 'llm').length
  const noValidatedLinks = trace?.validated_links === 0 && (trace.proposed_links ?? 0) > 0
  if (!trace?.degraded && fallbackCount === 0 && !noValidatedLinks) return null
  const reasons = discoveredCauses(trace)
  const title = noValidatedLinks
    ? 'DeepSeek предложил связи, но ни одна не прошла проверку цитат'
    : trace?.degraded
      ? 'ИИ-анализ выполнен не полностью'
      : 'Часть требований не проходила анализ ИИ'
  return <aside className="workspace-ai-notice" aria-label="Состояние ИИ-анализа">
    <strong>{title}</strong>
    <p>ИИ рассматривал: {modelCount} из {items.length} требований.
      Без завершённой смысловой проверки: {fallbackCount}.
      Участие ИИ не означает, что требование выполнено: для этого
      необходимы проверяемые доказательства. Спорные случаи рассматривает инженер.</p>
    {trace?.llm_usage && <details>
      <summary>Расход запросов и проверка доказательств</summary>
      <p>Выполнено запросов к DeepSeek: {trace.llm_usage.model_calls_total} из лимита {trace.llm_usage.model_calls_limit}.
        Запросы по этапам: {Object.entries(trace.llm_usage.attempted_by_phase).map(
          ([name, count]) => `${name}: ${count}`
        ).join('; ') || 'нет'}.</p>
      <p>Ответов от DeepSeek: {Object.values(trace.llm_usage.responses_by_phase).reduce((a, b) => a + b, 0)}.
        Связей предложено: {trace.proposed_links ?? 0}.
        Подтверждено исходными цитатами: {trace.validated_links ?? 0}.</p>
      {trace.rejected_links && Object.keys(trace.rejected_links).length > 0 &&
        <p>Причины отклонения: {Object.entries(trace.rejected_links).map(
          ([reason, count]) => `${reason}: ${count}`
        ).join('; ')}.</p>}
      {(trace.llm_usage.limit_reached || trace.targeted_limit_reached) &&
        <p>Лимит запросов достигнут: оставшиеся условия требуют проверки.
          Число запросов не является суммой затрат в денежном выражении.</p>}
    </details>}
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

function scoreHint(method?:string|null, scope: 'link'|'requirement' = 'requirement'): string {
  if (scope === 'link' && method === 'llm') return 'Оценка конкретной связи ЗнП → ТУ, возвращённая DeepSeek. Цитаты проверены на принадлежность исходным документам, но оценка не является статистической вероятностью достоверности. Статус связи может быть частичным даже при высокой оценке.'
  if (method === 'llm') return 'Оценка ИИ для подтверждённого требования ТУ. Если итоговая проверка условий не завершена, числовой оценки нет. Процент не является статистической вероятностью.'
  if (method === 'deterministic') return 'Показатель текстового поиска, не оценка DeepSeek. Он не означает подтверждённого выполнения требования.'
  return 'Метод оценки не указан; показатель нельзя интерпретировать как уверенность ИИ.'
}

export function AnalysisConfidence({method, score, scope = 'requirement'}: {
  method?: string|null
  score?: number|null
  scope?: 'link'|'requirement'
}) {
  const isLLM = method === 'llm'
  const isSearch = method === 'deterministic'
  const label = isLLM
    ? scope === 'link' ? 'Оценка связи ИИ' : 'Оценка ИИ'
    : isSearch ? 'Сходство текста' : 'Оценка проверки'
  const valid = typeof score === 'number' && Number.isFinite(score)
  const percent = valid ? Math.round(Math.min(1,Math.max(0,score))*100) : null
  return <div className="workspace-analysis-metrics">
    <span className={'workspace-analysis-method '+(isLLM ? 'llm' : 'local')}>
      {isLLM ? 'ИИ-агент' : isSearch ? 'Поиск и проверка правил' : 'Метод не указан'}
    </span>
    <div className="workspace-score" title={scoreHint(method, scope)}>
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
    <p><strong>Оценка связи ИИ (ЗнП → ТУ)</strong> — число, которое DeepSeek вернул
      для конкретной пары положений. Сервер отдельно проверяет цитаты, объекты и
      параметры; поэтому даже высокая оценка может сопровождаться статусом
      «частичное соответствие». Это не вероятность правильности.</p>
    <p><strong>Оценка ИИ (ТУ)</strong> — итоговый показатель подтверждённого требования.
      Если проверка покрытия не завершена, вместо ложных 0% показываем отсутствие
      данных. Оценка одной связи и итог по всему требованию — разные показатели.</p>
    <p><strong>Сходство текста</strong> — показатель поиска пунктов ЗнП по требованию ТУ.
      Даже высокий процент не доказывает смыслового соответствия.
      Для итогового решения нужно сверять параметры и исходные цитаты.</p>
  </details>
}
