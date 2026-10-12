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
