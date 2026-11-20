# DeepSeek / ТУ ↔ ЗнП: воспроизводимая техническая диагностика

**Назначение:** получить проверяемое объяснение, почему DeepSeek не завершает
полную сверку **конкретной пары документов**, а не скрывать сбой дополнительными
retry или поднимать лимиты наугад.

## Что уже подтверждено из кода (а что ещё нет)

- В исходной конфигурации `DEEPSEEK_MAX_TOKENS=3000`.
- При `finish_reason=length` HTTP-запрос **мог завершиться 200 OK**, однако
  ответ обрывается на ограничении генерации и вызывается
  `DeepSeekResponseError`. Это не отказ API принимать запрос.
- `responses=121` — число **успешных** ответов из разных фаз сверки на момент
  ошибки, **не** количество failed targeted-вызовов.
- Режим 10×10 в интерфейсе **отключает targeted**. Он не может воспроизвести
  ошибку полного обратного поиска.
- Обратный поиск уже использует адаптивное дробление, но при остаточной ошибке
  информация о фактических токенах, размерах JSON и причинах провала раньше
  отсутствовала.
- Не доказано, что причиной **всех** сбоев является `max_tokens`.
  Требуется полный прогон на реальных ТУ/ЗнП с журналированием.

## Что добавлено

1. `app/ai/diagnostics.py` — JSONL-трасса и контролируемый приватный захват
   original request/response без API-ключей.
2. `app/ai/deepseek.py` — подробности каждого HTTP-попытки и ответа:
   фазу, request ID, модель, thinking, `max_tokens`, размеры, latency,
   `finish_reason`, входные/выходные/общие токены, JSON parse error.
3. `app/requirements/llm_tracking.py` — счётчики и лимиты на каждой фазе.
4. `app/requirements/znp_first.py` — кандидаты, решения matching,
   результаты проверки цитат, targeted split, coverage и conflict audit.
5. `app/requirements/semantic_atomization.py` — каждое пропущенное положение
   и причина пропуска; момент перехода в fallback.
6. `app/ai/embeddings.py` — активация/сбой локального поиска.
7. `app/requirements/router.py` — контекст реальной серверной сверки.
8. `scripts/diagnose_deepseek_pair.py` — автономная реплика *полного*
   конвейера **двух PDF/DOCX** без изменения базы и предыдущего результата.
9. `scripts/replay_deepseek_call.py` — точное воспроизведение **одного**
   исходного запроса с контролируемым изменением `max_tokens` или числа
   targeted-кандидатов.

## Безопасность и расходы

- **Ни один скрипт не принимает API-ключ аргументом.** Он читается только
  приложением из окружения/`.env`.
- По умолчанию `events.jsonl` содержит только размеры, счётчики, хэши и
  фиксированные коды ошибок. Тексты документов и prompts **не** сохраняются.
- Флаг `--capture-payloads` сохраняет тексты ТУ/ЗнП, полные запросы и ответы
  в `private/`. Эти файлы **конфиденциальны**. Не отправлять в GitHub,
  обычные чаты, публичные issue или CI artifacts; хранить локально.
- `--probe` вызывает DeepSeek **один** раз отдельно, чтобы проверить модель
  и JSON mode. Полная сверка затем вызывает модель многократно и стоит денег.
- `--dry-run` выполняет парсинг без единого вызова DeepSeek.
- `--execute` в replay — ровно **одна новая платная попытка**.
  Повторять только осознанно; даже обрезанные ответы могут тарифицироваться.
- Не запускать диагностику на рабочем сервере под реальной нагрузкой без
  понимания последствий.

## Точная инструкция Codex: реальный прогон двух файлов на Windows + Docker

**Выполнять в корне проекта.** Файлы должны быть именно парой, на которой
возникает ошибка, а не сгенерированными примерами. Расширения .pdf/.docx.

```powershell
git switch dev
git pull origin dev

New-Item -ItemType Directory -Force .\diagnostic-inputs, .\diagnostics | Out-Null
Copy-Item 'D:\path\real_TU.docx' .\diagnostic-inputs\tu.docx
Copy-Item 'D:\path\real_ZNP.pdf' .\diagnostic-inputs\znp.pdf

docker compose build api
```

**1. Проверить парсинг, OCR и число требований (без DeepSeek):**

```powershell
docker compose run --rm --no-deps `
  -v "${PWD}/diagnostic-inputs:/inputs:ro" `
  -v "${PWD}/diagnostics:/diagnostics" `
  api python -m scripts.diagnose_deepseek_pair `
  --tu /inputs/tu.docx --znp /inputs/znp.pdf `
  --out /diagnostics --dry-run
```

**2. Запустить ПОЛНУЮ сверку (а не preview 10×10) на тех же двух файлах:**

```powershell
docker compose run --rm --no-deps `
  -v "${PWD}/diagnostic-inputs:/inputs:ro" `
  -v "${PWD}/diagnostics:/diagnostics" `
  api python -m scripts.diagnose_deepseek_pair `
  --tu /inputs/tu.docx --znp /inputs/znp.pdf `
  --out /diagnostics --probe --capture-payloads
```

Команда может завершиться с exit code **2** (результат неполный) или
с ненулевым кодом при исключении. Это само по себе является диагностическим
результатом. Содержимое появляется в
`diagnostics/<run-id>/events.jsonl`, `report.json`, `private/`.

**3. Найти конкретный первый проблемный HTTP-запрос:**

```powershell
$run = Get-ChildItem .\diagnostics -Directory |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1
$events = Get-Content "$($run.FullName)\events.jsonl" |
  ForEach-Object { $_ | ConvertFrom-Json }
$events | Where-Object {
  $_.event -in @(
    'provider.response.rejected', 'provider.json.invalid',
    'provider.network.error', 'provider.http.error', 'ledger.limit',
    'atomization.model.error', 'targeted.batch.truncated'
  )
} | Format-Table timestamp,phase,event,call_id,code,error_type -AutoSize
```

**4. Сверить идентификатор `call_id` со всей историей именно этого вызова:**

```powershell
$bad = $events | Where-Object { $_.event -eq 'provider.response.rejected' } |
  Select-Object -First 1
$events | Where-Object { $_.call_id -eq $bad.call_id } |
  Format-List timestamp,phase,event,status_code,request_bytes,
    prompt_tokens,completion_tokens,total_tokens,max_tokens,finish_reason,
    elapsed_ms,request_id,response_bytes,content_chars
```

Оригинальный payload: `private/<call_id>-request.json` и
`private/<call_id>-response.json`. В случае network/timeout ответного файла
может не быть.

**5. Воспроизвести только этот запрос без повторной полной сверки:**

```powershell
docker compose run --rm --no-deps `
  -v "${PWD}/diagnostics:/diagnostics" `
  api python -m scripts.replay_deepseek_call `
  --request /diagnostics/<run-id>/private/<call_id>-request.json `
  --out /diagnostics
```

Первый запуск replay без `--execute` — **сухая проверка** структуры запроса.

**6. Контролируемая серия экспериментов, строго по одному платному запросу:**

```powershell
# Воспроизвести исходное число токенов:
... python -m scripts.replay_deepseek_call --request /diagnostics/<run-id>/private/<call_id>-request.json --out /diagnostics --execute --capture-payloads

# ТОТ ЖЕ запрос, но увеличенный лимит ответа:
... python -m scripts.replay_deepseek_call --request /diagnostics/<run-id>/private/<call_id>-request.json --out /diagnostics --max-tokens 6000 --execute --capture-payloads

# ТОТ ЖЕ запрос targeted, но только первые 3 кандидата:
... python -m scripts.replay_deepseek_call --request /diagnostics/<run-id>/private/<call_id>-request.json --out /diagnostics --trim-candidates 3 --execute --capture-payloads
```

В трёх командах `...` означает: повторить **тот же** префикс
`docker compose run --rm --no-deps -v "${PWD}/diagnostics:/diagnostics" api`,
не вводить троеточие в PowerShell буквально.

## Методика установления причины — что считать доказательством

| Наблюдение в JSONL | Что это доказывает | Следующий шаг |
|---|---|---|
| HTTP 200, `finish_reason=length`, `completion_tokens≈max_tokens` | Обрыв по бюджету вывода, а не отказ API принять запрос | Replay того же payload при 3000 и 6000 токенах |
| С большим `max_tokens` ответ полный, при прежнем обрезан | Причина данного сбоя — недостаточный бюджет вывода для данного payload | Профильные лимиты токенов/уменьшение JSON |
| При равном `max_tokens` уменьшение candidates спасает ответ | Избыточный размер конкретного targeted-пакета | Вывести безопасный лимит размера пакета |
| HTTP 401 / 402 / 429 | Авторизация / баланс / rate limit провайдера | Исправить ключ, баланс или скорость |
| `ReadTimeout`, `ReadError` без HTTP-ответа | Транспорт или таймаут на стороне запроса; доставка могла состояться | Проверить сеть/время/провайдера, **не** слепо повторять |
| `finish_reason=stop`, но `provider.json.invalid` | Ответ завершён, но нарушен JSON-синтаксис | Смотреть приватный ответ, формат, prompts |
| `atomization.model.error`, `provider_unavailable` | Деградация начинается ещё до matching | Сначала чинить атомизацию |
| `ledger.limit` | **Внутренний** лимит приложения, а не обязательно DeepSeek | Анализировать расход по фазам, не подменять rate limit |
| `embedding.disabled` | Семантический канал отключился; retrieval перешёл на lexical | Разобрать fastembed/model, отдельно от HTTP DeepSeek |
| `targeted.batch.truncated` > 0, `targeted.adaptive.end` без unchecked | Ошибка выхода восстановлена дроблением, семантическая полнота не доказана | Сравнить затраты, валидацию цитат и coverage |

Для **каждой** ошибки Codex должен составить цепочку:
`atom/requirement → retrieval candidates → ledger phase/index →
provider call_id → HTTP → finish_reason → usage → JSON parse →
валидация цитат/параметров → итог требования`.

## Отдельно: диагностика обычного интерфейса

Если автономный прогон успешен, но UI-прогон падает, включить в корневом
`.env` (НЕ коммитить):

```dotenv
DEEPSEEK_TRACE_DIR=/data/diagnostics
DEEPSEEK_TRACE_CAPTURE_PAYLOADS=1
```

В `docker-compose.yml` каталог `./diagnostics` монтируется в
`/data/diagnostics`. Пересоздать контейнер API:

```powershell
docker compose up -d --build api
```

Запустить **полную** сверку в интерфейсе и изучить события из отдельной
папки `diagnostics/<run-id>/`. По окончании удалить две переменные из
`.env` и пересоздать API, чтобы не хранить новые документы постоянно.

## Задание Codex после прогона

1. Не редактировать бизнес-логику до появления воспроизводимого примера.
2. Сопоставить попытки **одного и того же payload** при неизменной модели
   и изменяемых `max_tokens`, размере batch и thinking mode (последний —
   только отдельно, с явной пометкой, что это уже другой эксперимент).
3. Оценить число кандидатов, рост `user_chars`, `request_bytes`,
   `prompt_tokens`, `completion_tokens`, `finish_reason` по фазам.
4. Проверить, где именно начинается деградация: atomization, matching,
   coverage, targeted или contradictions.
5. Проверить, почему `model_failure_kind` и `degraded` приводят к
   сохранению/отмене результата, нет ли ложноположительных `fulfilled`.
6. Выдать **отчёт с фактами**, минимум:
   - количество и тип исходных блоков/требований/атомов;
   - запросов/ошибок/токенов по каждому этапу;
   - **первый** сбой: конкретный `call_id`, статус, usage, finish_reason,
     входные размеры и ожидаемый выход;
   - детерминированное воспроизведение и результат 1–2 контрольных replay;
   - доказанная причина, условия воспроизведения, альтернативные гипотезы;
   - минимальный исправляющий PR и регрессионный тест на этом сценарии.
7. Не сообщать «исправлено», пока новая полная реальная сверка **этих же
   двух файлов** не завершилась и не прошла проверка сохранения/валидности.

**Важно:** автономный скрипт воспроизводит те же функции парсинга,
атомизации, LLM, retrieval, coverage и targeted, но **не** обращается
к PostgreSQL и не сохраняет результаты в приложение. Если потребуется
проверить очередь/транзакции/состояние БД — включить trace интерфейса.
