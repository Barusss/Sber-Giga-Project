# GREF INDEX через SSH (Beget)

Сайт и MySQL работают на Beget. GitHub Actions отправляет команды **по SSH**. HTTPS на техническом домене не нужен для передачи команд.

## Подготовка Beget

1. Загрузить `gref_cli.php` в **существующую** папку `private` сайта, рядом с `config.php`. Запрещать доступ к папке через `private/.htaccess` (`Require all denied`).
2. Через SSH проверить: `php8.2 /ПОЛНЫЙ/ПУТЬ/private/gref_cli.php --check`. Должен вернуться JSON `{"ok":true,"mode":"check"}`.
3. Оставить `api.php` на месте для сайта. Перевыпустить ранее раскрытый API-ключ, если он ещё действителен.

## SSH-ключ и GitHub

В PowerShell на личном ПК создать **отдельную пару ключей** (на вопросы passphrase для Actions нажать Enter):

```powershell
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\gref_actions"
Get-Content "$env:USERPROFILE\.ssh\gref_actions.pub"
```

Добавить **публичный ключ** из .pub в `~/.ssh/authorized_keys` Beget (права `~/.ssh` 700 и `authorized_keys` 600). Проверить вход командой `ssh -i <путь_к_ключу> LOGIN@HOST`.

В репозитории: `Settings → Secrets and variables → Actions`.

Создать Repository **secrets**:
- `GREF_SSH_PRIVATE_KEY_B64`: Base64-представление байтов приватного ключа (получить на Windows: `[Convert]::ToBase64String([IO.File]::ReadAllBytes("$env:USERPROFILE\.ssh\gref_actions")) | Set-Clipboard`). Вставить результат как одну строку в GitHub Secret; он по-прежнему секрет.
- `GREF_SSH_KNOWN_HOSTS`: заранее **проверенная** запись host key (можно получить `ssh-keyscan -p 22 -t ed25519 HOST` и сверить отпечаток у хостера). Не отключать StrictHostKeyChecking.

Создать Repository **variables**:
- `GREF_SSH_HOST`: имя SSH-сервера из техинформации Beget.
- `GREF_SSH_USER`: логин SSH.
- `GREF_SSH_PORT`: `22`.
- `GREF_SSH_SCRIPT_PATH`: реальный полный путь к `private/gref_cli.php`. Узнать через `find "$HOME" -type f -path '*/private/gref_cli.php' -print`.

## Проверка

После попадания workflow в `main` вручную запустить `Actions → GREF INDEX SSH → Run workflow`. Ручной запуск проверяет SSH + PHP + MySQL и ничего не начисляет.

**При отправке команд** ChatGPT создаёт новый JSON в `.gref/commands/`. Action берёт только добавленные файлы. Скрипт на Beget сохраняет `requestId` в MySQL и игнорирует повторение той же команды.

Для начисления `{"type":"rating","member":"mate2","delta":150,"reason":"Премия за PR #4"}`. Руслан: `owner` (фиксировано 1000); Степан: `mate1`; Данил: `mate2`.

**Прежняя команда Данилу +150 не выполнилась**, так как старый workflow остановился ещё на проверке HTTPS. После успешной SSH-проверки потребуется отдельный новый файл-команда.

> Вход в админку с паролем по HTTP небезопасен. SSH защищает только интеграцию GitHub → Beget; для безопасной админки потребуется HTTPS.
