# Границы трёх модулей

1. **Документы и отчётность**: `backend/app/documents`, `backend/app/reports`, `frontend/src/pages/ProjectsPage.tsx`, `DocumentsPage.tsx`, `ReportsPage.tsx`. Парсинг PDF/DOCX, OCR, фрагменты и chunks, загрузка, согласование и экспорт.
2. **Нормативы**: `backend/app/normatives`, `frontend/src/pages/NormativesPage.tsx`. Реестр ГОСТ/НТД, извлечение ссылок и проверка актуальности. Источник статуса обязателен.
3. **ТУ и ЗнП**: `backend/app/requirements`, `frontend/src/pages/RequirementsPage.tsx`. Извлечение требований, семантический поиск, проверка LLM и цитаты.

**Общий поиск:** `backend/app/search` (контракт `docs/DATA_PIPELINE.md`): первый участник поддерживает хранение/индексацию, второй и третий используют API и реализуют извлечение своих классов фактов и embeddings.

**Общие файлы** `backend/app/core`, `frontend/src/api`, `docs/API.md`, `docker-compose.yml` меняются только после согласования команды. Каждый участник работает в своей feature-ветке и создаёт PR.

Ревизии миграций строго последовательны: `0003` — снимки проверки ГОСТ, `0004` — хранение требований ТУ/ЗнП, перенесённое из PR #4 DPazY. Изменения участника №3 интегрируются отдельным PR из свежего `main`; старый PR #4 не следует повторно объединять без переработки.
