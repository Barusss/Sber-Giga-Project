# DeepSeek — общая LLM-интеграция

## Настройка

1. В корне репозитория: `cp .env.example .env` (PowerShell: `Copy-Item .env.example .env`).
2. Откройте локальный `.env` и заполните **`DEEPSEEK_API_KEY=sk-...`** реальным ключом из [DeepSeek Platform](https://platform.deepseek.com/api_keys).
3. Запустите `docker compose up --build`. `docker-compose.yml` уже передаёт `.env` в backend через `env_file`.

`.env.example` содержит **пустое** поле для ключа, а `.env` исключён в `.gitignore`. Не вставляйте ключ в frontend, `VITE_*`, код, тесты, issue или PR.

| Переменная | Назначение |
| --- | --- |
| `DEEPSEEK_API_KEY` | Секретный API-ключ (пустой по умолчанию) |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` |
| `DEEPSEEK_MODEL` | `deepseek-flash` (можно сменить на доступную модель) |
| `DEEPSEEK_THINKING` | `disabled` по умолчанию, для JSON-извлечения |
| `DEEPSEEK_TIMEOUT_SECONDS` | Тайм-аут HTTP-запроса (60 секунд) |
| `DEEPSEEK_MAX_TOKENS` | Ограничение вывода (3000 токенов) |

Клиент использует [Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/) и JSON Output. Правильный параметр отключения рассуждений: `{"thinking":{"type":"disabled"}}`. Без `DEEPSEEK_API_KEY` приложение продолжает запускаться; ошибка возникает только при попытке вызвать DeepSeek.

## Использование участниками 2 и 3

В backend-коде:

```python
from app.ai.deepseek import DeepSeekClient, DeepSeekError

ai = DeepSeekClient()
result = ai.complete_json(
    system_prompt="Извлеки технические требования; верни JSON объект с ключом requirements.",
    user_prompt=fragment_text,
)
# Проверяйте структуру результата (Pydantic), наличие цитат и fragment_id.
```

Для обычного ответа используйте `ai.complete(system_prompt=..., user_prompt=...)`. Для тестов передайте `http_client=httpx.Client(transport=httpx.MockTransport(...))`: реальный внешний API не вызывается, ключ не требуется.

**Сейчас подключён только общий серверный клиент.** В `/requirements/analyze` пока HTTP 501; автоматические проверки норматива и извлечение фактов участники реализуют отдельно. Модель не вызывается при загрузке документов, а API-ключ не отправляется браузеру.

DeepSeek Chat API **не заменяет embeddings**. Их генерация для pgvector остаётся отдельной задачей семантического модуля. Для документов с конфиденциальными данными заранее согласуйте возможность отправки текста внешнему API. Не передавайте 100-страничный PDF одним prompt: используйте существующие chunks и ссылки на источники.
