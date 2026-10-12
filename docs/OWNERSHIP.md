# Границы трёх модулей

1. **Документы и отчётность**: `backend/app/documents`, `backend/app/reports`, `frontend/src/pages/ProjectsPage.tsx`, `DocumentsPage.tsx`, `ReportsPage.tsx`. Парсинг PDF/DOCX, OCR, загрузка, согласование и экспорт.
2. **Нормативы**: `backend/app/normatives`, `frontend/src/pages/NormativesPage.tsx`. Реестр ГОСТ/НТД, извлечение ссылок и проверка актуальности. Источник статуса обязателен.
3. **ТУ и ЗнП**: `backend/app/requirements`, `frontend/src/pages/RequirementsPage.tsx`. Извлечение требований, семантический поиск, проверка LLM и цитаты.

**Общие файлы** `backend/app/core`, `frontend/src/api`, `docs/API.md`, `docker-compose.yml` меняются только после согласования команды. Каждый участник работает в своей feature-ветке и создаёт PR.
