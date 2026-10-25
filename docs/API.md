# Стабильный контракт v1

Базовый адрес: `/api/v1`. Ошибки: стандарт FastAPI `{ "detail": "..." }`. ID — UUID-строки.

## Документы (участник 1)
- `POST /projects` `{ "name": "..." }` — создание проекта.
- `GET /projects` — список проектов.
- `GET /projects/{id}` — проект.
- `POST /projects/{id}/documents` — multipart `kind=tu|znp|other`, `file=<binary>`; PDF/DOCX.
- `GET /projects/{id}/documents` — файлы проекта.
- `GET /documents/{id}/fragments` — массив `{id,document_id,page,position,text}`. Страницы с 1.

## Реестр (участник 2)
- `GET /normatives` — список нормативов.
- `POST /normatives` — `{code,title,status, replacement?, source_url?}`.
- `GET /normatives/{code}` — норматив по обозначению.
- Статус реестра: `active|obsolete|unknown`. Пустой результат не означает, что ГОСТ утратил силу.
- Проверяющий модуль добавляет свой `/normatives/analyze` после согласования входной схемы.

## Соответствие требований (участник 3)
- `POST /requirements/analyze` — сверка ТУ и ЗнП. Два режима: явные списки `{tu_requirements:[...], znp_clauses:[...]}` (тесты, бенчмарки) или документы проекта `{project_id, tu_document_id?, znp_document_id?, force?}`. Агент выполняет поиск по пунктам, сверку числовых параметров и валидацию цитат; при недоступной LLM работает детерминированный локальный прогон. Результаты сохраняются в `tu_requirements`/`requirement_verifications`, замечания — в общие `findings`. Повторный запуск обновляет результаты по стабильному ключу `(document_id, clause_number, нормализованный текст)`, сохраняет решения инженера и accepted/rejected-замечания; `force=true` сбрасывает их и пересчитывает всё заново. В `trace_info.analysis` возвращаются счётчики `created/updated/preserved_human/preserved_findings/removed/force`. Пустой запрос отклоняется схемой (HTTP 422).
- `GET /requirements/projects/{project_id}` — сохранённые результаты сверки для страницы «ТУ ↔ ЗнП» (объединяет требования и проверки, учитывает `human_override_status`). Каждый элемент содержит происхождение обеих сторон: `tu_fragment_id/tu_page/tu_fragment_text` и `znp_fragment_id/znp_page/znp_fragment_text`. `method` (`llm|deterministic`) и `channels` (`lexical|semantic`) возвращаются в ответе `/analyze`; после перезагрузки они не хранятся.
- `POST /requirements/projects/{project_id}/tu-requirements` — вручную записать подготовленное требование ТУ: `{document_id, clause_number, category, text}`. Документ должен иметь `kind=tu` и принадлежать проекту; только для `draft`.
- `GET /requirements/projects/{project_id}/tu-requirements?document_id=...` — список подготовленных требований, опционально фильтр документа.
- `GET /requirements/projects/{project_id}/verifications` — сохранённые результаты проверки и ручных решений инженера. **Это не запуск сравнения.**
- `POST /requirements/projects/{project_id}/confirm` — ручное решение инженера: `{requirement_id,status:"fulfilled|missing|needs_review",comment?}`. Сохраняет статус в `human_override_status`, не подменяет исходный `status`; если проверки ещё не было, создаёт запись `needs_review` с ручным решением и без искусственного score. Решение инженера также закрывает (`rejected`) или подтверждает (`accepted`) связанное замечание модуля `requirements`; связь берётся из `evidence[].requirement_id`, для старых записей — по префиксу `[<clause_number>]`.
- Модели `TURequirement`, `RequirementVerification` и структуры `TURequirement/ZNPClause/LLMVerificationDecision` перенесены из <https://github.com/Barusss/Sber-Giga-Project/pull/4> (DPazY).
- Семантические совпадения подтверждаются цитатами, а не только числом similarity. При `EMBEDDING_ENABLED=true` и установленном `fastembed` поиск гибридный (`semantic_weight * semantic + (1 - semantic_weight) * lexical`); без модели, при ошибке загрузки или на SQLite остаётся лексический канал, причина видна в `trace_info.embedding.reason`. `missing` в fallback-режиме срабатывает только при отсутствии кандидата выше порога `0.25`; автоматическое `fulfilled` требует ещё и лексической опоры, поэтому чисто семантическое совпадение уходит в `needs_review`. Без LLM semantic-канал консервативен и может дать `needs_review` для отсутствующего пункта, если найден близкий по смыслу кандидат; ложного `fulfilled` при этом не возникает. Финальное решение всегда за инженером.

## Замечания и согласование (участник 1 и оба аналитических модуля)
- `GET /projects/{id}/findings` — замечания.
- `POST /projects/{id}/findings` — создание замечания, пример ниже.
- `PATCH /findings/{id}/review` — `{ "review_status": "accepted" | "rejected" }`.
- `POST /projects/{id}/approve` — утверждение (нельзя при незавершённых замечаниях).
- `GET /projects/{id}/report` — JSON итогового отчёта.

```json
{
  "module":"requirements",
  "type":"missing_requirement",
  "severity":"warning",
  "explanation":"Пункт ТУ не отражён в ЗнП",
  "suggested_action":"Дополнить задание",
  "evidence":[{"document_id":"<uuid>","fragment_id":"<uuid>","page":2,"quote":"Требуется ...","requirement_id":"<uuid>"}]
}
```

`module`: `normatives|requirements`; `review_status`: `pending|accepted|rejected` (управляется инженером).

## Правила
- Не хранить фактический текст только внутри LLM-промптов: ссылаться на `document_id`/`fragment_id`.
- Внешние LLM API не вызываются из браузера; ключи не коммитить.
- Не перезаписывать утверждённые результаты.
- Аналитические модули обязаны подтверждать выводы цитатами; при недоступности LLM используется детерминированный fallback без выдуманных находок.
- До промышленного использования добавить аутентификацию, контроль доступа и фоновые задания.

## OCR, chunks и извлечённые факты (общий слой)

- `GET /documents/{id}/fragments` — **обратно совместимый** маршрут, теперь возвращает несколько фрагментов страницы; дополнительные поля `block_type`, `section_path`, `bbox`, `extraction_method` см. в `docs/DATA_PIPELINE.md`. Старые поля `id/document_id/page/position/text` сохранены.
- `GET /documents/{id}/chunks?limit=100&offset=0` — chunks с `page_start`, `page_end`, `text`, `section_path`, `sources[] = {fragment_id,page,start_offset,end_offset,bbox}`.
- `GET /fact-types` — доступный справочник типовых классов фактов (можно расширять).
- `POST /documents/{id}/facts` — создать подтверждённый цитатами структурированный факт. Использовать в модулях 2/3, **не путать с finding**.
- `GET /documents/{id}/facts?fact_type=power_limit` — получить извлечённые ранее факты.
- `POST /documents/{id}/search` — поиск по всему документу: `{ "query": "мощность", "limit": 10 }`, ответ `[{"chunk": {...}, "score": 0.0164, "channels": ["lexical"]}]`. Пустой ответ не доказывает отсутствие требования.

Пример создания факта (источник обязан реально содержать цитату):

```json
{
  "fact_type": "power_limit",
  "value": {"amount": 150, "unit": "kW"},
  "confidence": 0.92,
  "extraction_version": "normatives-v1",
  "evidence": [
    {"fragment_id": "<uuid фрагмента из этого документа>", "quote": "150 кВт"}
  ]
}
```

### Необязательный векторный канал

- `PUT /documents/{id}/chunks/{chunk_id}/embedding` — `{ "embedding_model":"model-name-v1", "embedding":[384 конечных float] }`.
- `POST /documents/{id}/search` — `{ "query":"...", "embedding_model":"model-name-v1", "embedding":[384 float], "limit":10 }` для гибридной выдачи. Без `query` — только семантическая.

Векторный канал доступен **только с PostgreSQL + pgvector**; в SQLite отдаётся HTTP 501. Оба вектора должны быть созданы **одной** embedding-моделью, ключи внешнего API хранить только на сервере. В API нет автоматического вычисления embeddings: ответственность 2/3 участников.

**Совместимость миграций:** `0002_search_facts` создаёт новые таблицы и индексирует уже загруженные документы. При применении `alembic upgrade head` сохраняются старые `document_id`, `fragment_id` и ссылки на них. Страница DOCX до рендеринга неизвестна: число 1 является технической меткой, а не фактической страницей.

## Миграция участника №3

Следующая после `0003_normative_verifications` миграция — `0004_add_tu_requirements_and_verifications`. Исторический PR #4 участника DPazY использовал номер `0003`, уже занятый в `main`; не накатывайте оригинальную `0003` поверх новой цепочки. При `docker compose up -d --build api` выполнится `alembic upgrade head` без потери имеющихся нормативных фактов и замечаний.

## Общая LLM-интеграция

Настройка серверного DeepSeek-клиента: [docs/DEEPSEEK.md](DEEPSEEK.md). Секрет из корневого `.env` доступен только backend; публичного универсального proxy-эндпоинта для промптов нет.
