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
- `POST /requirements/analyze` зарезервирован, пока возвращает HTTP 501.
- Предлагаемый вход: `{project_id, tu_document_id, znp_document_id}`.
- Семантические совпадения должны подтверждаться цитатами, а не только числом similarity.

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
  "evidence":[{"document_id":"<uuid>","fragment_id":"<uuid>","page":2,"quote":"Требуется ..."}]
}
```

`module`: `normatives|requirements`; `review_status`: `pending|accepted|rejected` (управляется инженером).

## Правила
- Не хранить фактический текст только внутри LLM-промптов: ссылаться на `document_id`/`fragment_id`.
- Внешние LLM API не вызываются из браузера; ключи не коммитить.
- Не перезаписывать утверждённые результаты.
- Анализ в MVP добавляется отдельными сервисами; до реализации вернуть 501, не создавать фиктивных находок.
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
