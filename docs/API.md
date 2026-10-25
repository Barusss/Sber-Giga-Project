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
- `POST /requirements/analyze` — сверка ТУ и ЗнП. Два режима: явные списки `{tu_requirements:[...], znp_clauses:[...]}` (тесты, бенчмарки) или документы проекта `{project_id, tu_document_id?, znp_document_id?, force?}`. Агент выполняет поиск по пунктам, сверку числовых параметров и валидацию цитат; при недоступной LLM работает детерминированный локальный прогон. Результаты сохраняются в `tu_requirements`/`requirement_verifications`, замечания — в общие `findings`. Повторный запуск обновляет результаты по стабильному ключу `(document_id, clause_number, нормализованный текст)` и не создаёт вторые записи. Анализ сериализуется в рамках проекта: в PostgreSQL проект блокируется `SELECT ... FOR UPDATE`, в SQLite действует внутрипроцессная блокировка, поэтому два почти одновременных запроса выполняются по очереди. Если в базе уже остались точные дубли от старых/параллельных прогонов, следующий анализ сливает их в одну запись, сохраняя решение инженера, accepted finding и ручное требование. `force=true` сбрасывает решения инженера и пересчитывает всё заново. В `trace_info.analysis` возвращаются счётчики `created/updated/preserved_human/preserved_findings/removed/force`. Пустой запрос отклоняется схемой (HTTP 422).
- `GET /requirements/projects/{project_id}` — сохранённые результаты сверки для страницы «ТУ ↔ ЗнП» (объединяет требования и проверки, учитывает `human_override_status`). Возвращает одну карточку на стабильный ключ `(document_id, clause_number, нормализованный текст)`: при выборе записи приоритет у подтверждённой человеком (`is_confirmed_by_human`/`human_override_status`), затем у записи с accepted finding, затем у более свежей верификации. `stats` пересчитываются после дедупликации. Требования с другим стабильным ключом (например, из старого документа) не скрываются, если у них есть решение инженера или accepted finding. Каждый элемент содержит происхождение обеих сторон: `tu_fragment_id/tu_page/tu_fragment_text` и `znp_fragment_id/znp_page/znp_fragment_text`. `method` (`llm|deterministic`) и `channels` (`lexical|semantic`) возвращаются в ответе `/analyze`; после перезагрузки они не хранятся.
- Извлечение требований не создаёт карточки из заголовков и оглавления: `Содержание`/`Оглавление`, строки с точечными лидерами и номером страницы, перечни сокращений/терминов и сервисные заголовки (`Приложение`, `Основание`, `Срок действия`, `Примечание`, `Технические условия`, `Задание на проектирование`, `Утверждаю`, `Согласовано`) пропускаются. Заголовок без тела не становится требованием; текст под заголовком получает номер и `section_path` заголовка. Содержательные разделы (`Общие положения`, `Схема выдачи мощности` и т.п.) не отбрасываются.
- `POST /requirements/projects/{project_id}/tu-requirements` — вручную записать подготовленное требование ТУ: `{document_id, clause_number, category, text}`. Документ должен иметь `kind=tu` и принадлежать проекту; только для `draft`.
- `GET /requirements/projects/{project_id}/tu-requirements?document_id=...` — список подготовленных требований, опционально фильтр документа.
- `GET /requirements/projects/{project_id}/verifications` — сохранённые результаты проверки и ручных решений инженера. **Это не запуск сравнения.**
- `POST /requirements/projects/{project_id}/confirm` — ручное решение инженера: `{requirement_id,status:"fulfilled|missing|needs_review",comment?}`. Сохраняет статус в `human_override_status`, не подменяет исходный `status`; если проверки ещё не было, создаёт запись `needs_review` с ручным решением и без искусственного score. Решение инженера также закрывает (`rejected`) или подтверждает (`accepted`) связанное замечание модуля `requirements`; связь берётся из `evidence[].requirement_id`, для старых записей — по префиксу `[<clause_number>]`.
- Модели `TURequirement`, `RequirementVerification` и структуры `TURequirement/ZNPClause/LLMVerificationDecision` перенесены из <https://github.com/Barusss/Sber-Giga-Project/pull/4> (DPazY).
- Семантические совпадения подтверждаются цитатами, а не только числом similarity. При `EMBEDDING_ENABLED=true` и установленном `fastembed` поиск гибридный (`semantic_weight * semantic + (1 - semantic_weight) * lexical`); без модели, при ошибке загрузки или на SQLite остаётся лексический канал, причина видна в `trace_info.embedding.reason`. `missing` в fallback-режиме срабатывает только при отсутствии кандидата выше порога `0.25`; автоматическое `fulfilled` требует ещё и лексической опоры, поэтому чисто семантическое совпадение уходит в `needs_review`. Без LLM semantic-канал консервативен и может дать `needs_review` для отсутствующего пункта, если найден близкий по смыслу кандидат; ложного `fulfilled` при этом не возникает. Финальное решение всегда за инженером.
- Перечень в требовании ТУ (маркированные строки, пункты `1)`/`а)`/`I)`, строки после вводного двоеточия или inline-сегменты через `;`) подтверждается только целиком: `fulfilled` ставится, если в ЗнП найдены все элементы, иначе — `needs_review` с перечнем непокрытых элементов и `confidence_score` не выше `0.65`. Обычный абзац с одной-двумя точками с запятой перечнем не считается. Для элементов с кодом/номером (`ГОСТ`, `СП`, `СНиП`, `ФЗ`, `№`, числа с единицами) требуется совпадение идентификатора, для остальных — консервативное лексическое пересечение.
- Доказательная цитата ЗнП должна быть короткой (не более 600 символов) точной подстрокой одного пункта, а не склеенным списком. Валидатор нормализует ёлочки/кавычки, тире, многоточия и пробелы, поэтому типографские различия не делают цитату выдуманной. В ответе `tool_validate_quotes` возвращаются `tu_quote_chars`, `znp_quote_chars`, `znp_quote_present`, `znp_quote_atomic`; пустая или невалидная цитата не подтверждает `fulfilled`. В fallback цитата выбирается как атомарный фрагмент кандидата, а в LLM-пути невалидная/неатомарная цитата заменяется фактическим фрагментом; выдуманная цитата дополнительно понижает статус до `needs_review`. Проверки видны в trace как `coverage_check`/`evidence_check` (LLM) и шаги `check_list_coverage`/`pick_evidence` (fallback).

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
