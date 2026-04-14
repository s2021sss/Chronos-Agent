# Quick Start — Chronos Agent

Пошаговая инструкция по запуску Chronos Agent с нуля.

**Требуется:** Docker + Docker Compose, Python 3.11+, ngrok (для локальной разработки).

---

## Шаг 1. Google Cloud Project

### 1.1 Создать проект

1. Перейти на [console.cloud.google.com](https://console.cloud.google.com/)
2. Нажать **Select a project** → **New Project**
3. Ввести название (например, `chronos-agent`) → **Create**

### 1.2 Включить API

В поиске (вверху страницы) найти и включить оба API:

- **Google Calendar API** → Enable
- **Tasks API** → Enable

Путь: **APIs & Services → Library** → поиск → Enable.

### 1.3 Настроить OAuth Consent Screen

1. **APIs & Services → OAuth consent screen**
2. User Type: **External** → Create
3. Заполнить:
   - App name: `Chronos Agent`
   - User support email: ваш email
   - Developer contact: ваш email
4. **Scopes**: добавить доступы к Google Calendar и Google Tasks
5. **Test users**: обязательно добавить Google-аккаунт, с которого будете логиниться в боте

Пока приложение не прошло официальную проверку Google, оно работает в режиме **Testing**.
В этом режиме OAuth доступен только email-адресам из списка **Test users**.
Если нужную почту не добавить, Google покажет ошибку:

```text
Доступ заблокирован: приложение "<ваш-ngrok-домен>" не прошло проверку Google
Ошибка 403: access_denied
```

Для локальной разработки добавьте свою почту, например `почта@gmail.com`, в:
**APIs & Services → OAuth consent screen → Audience / Test users**.

### 1.4 Создать OAuth 2.0 Credentials

1. **APIs & Services → Credentials → Create Credentials → OAuth client ID**
2. Application type: **Web application**
3. Name: `Chronos Agent Web Client`
4. **Authorized redirect URIs** — добавить:
   - `https://<ваш-ngrok-домен>/auth/google/callback`
   - (например: `https://abcd1234.ngrok-free.app/auth/google/callback`)
5. **Create** → скопировать **Client ID** и **Client Secret**

> После получения ngrok URL (Шаг 4) вернитесь сюда и добавьте точный redirect URI.

---

## Шаг 2. Telegram Bot

### 2.1 Создать бота

1. Открыть [@BotFather](https://t.me/BotFather) в Telegram
2. Отправить `/newbot`
3. Ввести имя бота (например, `Chronos Agent`)
4. Ввести username (должен заканчиваться на `bot`, например `chronos_mybot`)
5. Скопировать **Bot Token** (формат: `123456789:ABCdef...`)

### 2.2 Настроить команды (опционально)

Отправить BotFather:
```
/setcommands
```
Выбрать бота и вставить:
```
start - Начать настройку или показать приветствие
cancel - Отменить текущее действие
help - Справка и примеры запросов
status - Список активных задач
timezone - Установить часовой пояс (пример: /timezone Europe/Moscow)
```

---

## Шаг 3. Mistral API Key

1. Перейти на [console.mistral.ai](https://console.mistral.ai/)
2. Создать аккаунт или войти
3. **API Keys → Create new key**
4. Скопировать ключ (показывается один раз)

---

## Шаг 4. ngrok (локальная разработка)

ngrok создаёт публичный HTTPS-туннель к вашему локальному серверу. Это нужно для webhook'ов Telegram и Google OAuth callback.

### 4.1 Установить ngrok

```bash
# macOS
brew install ngrok/ngrok/ngrok

# Linux / Windows: скачать с https://ngrok.com/download
```

### 4.2 Зарегистрироваться и получить authtoken

1. Зарегистрироваться на [ngrok.com](https://ngrok.com/)
2. **Your Authtoken** → скопировать
3. `ngrok config add-authtoken <YOUR_TOKEN>`

### 4.3 Запустить туннель

```bash
ngrok http 8000
```

Вы увидите URL вида: `https://abcd1234.ngrok-free.app`

> В бесплатном плане ngrok URL меняется при каждом запуске.
> После получения нового URL нужно обновить redirect URI в Google Cloud Console (шаг 1.4) и переменную `TELEGRAM_WEBHOOK_URL` в `.env`.

---

## Шаг 5. Langfuse (observability)

Langfuse поставляется в `docker-compose.yml` и запускается автоматически.

После первого запуска:
1. Открыть `http://localhost:3000`
2. Войти: `admin@admin.com` / `admin12345678` (из `.env.example`)
3. Перейти в **Settings → API Keys → Create new key**
4. Скопировать `Public Key` и `Secret Key`

---

## Шаг 6. Генерация секретных ключей

### ENCRYPTION_KEY (Fernet, для шифрования OAuth токенов)

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### OAUTH_STATE_SECRET (HMAC, для защиты CSRF в OAuth flow)

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

---

## Шаг 7. Настройка .env

```bash
cp .env.example .env
```

Заполнить `.env` всеми значениями:

```dotenv
# Telegram
TELEGRAM_BOT_TOKEN=123456789:ABCdef...
TELEGRAM_WEBHOOK_URL=https://abcd1234.ngrok-free.app/webhook/telegram

# Google OAuth
GOOGLE_CLIENT_ID=123456789-xxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-xxx
GOOGLE_REDIRECT_URI=https://abcd1234.ngrok-free.app/auth/google/callback
OAUTH_STATE_SECRET=<hex из шага 6>

# Шифрование
ENCRYPTION_KEY=<Fernet key из шага 6>

# LLM
MISTRAL_API_KEY=<ключ из шага 3>
MISTRAL_MODEL=mistral-large-latest
MISTRAL_BASE_URL=https://api.mistral.ai/v1

# PostgreSQL (значения по умолчанию для docker-compose)
POSTGRES_URL=postgresql+asyncpg://chronos:chronos@postgres:5432/chronos

# Langfuse (заполнить после запуска docker-compose, шаг 5)
LANGFUSE_PUBLIC_KEY=pk-lf-placeholder
LANGFUSE_SECRET_KEY=sk-lf-placeholder
LANGFUSE_HOST=http://langfuse:3000
```

---

## Шаг 8. Запуск

### 8.1 Собрать и запустить

```bash
docker-compose up --build
```

При первом запуске Docker скачает образы и соберёт контейнер (~5-10 минут).

### 8.2 Применить миграции базы данных (только первый раз)

В отдельном терминале:

```bash
docker-compose run --rm agent alembic upgrade head
```

### 8.3 Проверить здоровье сервиса

```bash
curl http://localhost:8000/health
# Ожидаемый ответ: {"status":"ok","postgres":"ok","mistral":"ok"}
```

### 8.4 Зарегистрировать Telegram webhook

Webhook регистрируется автоматически при старте агента через `TELEGRAM_WEBHOOK_URL`.

Проверить статус webhook:
```bash
curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"
```

---

## Шаг 9. Онбординг пользователя

1. Открыть бота в Telegram → `/start`
2. Бот отправит ссылку Google OAuth → перейти по ней
3. Авторизоваться в Google → разрешить доступ к Calendar и Tasks
4. После редиректа бот попросит указать часовой пояс:
   ```
   /timezone Europe/Moscow
   ```
5. Готово! Можно отправлять запросы.

**Примеры запросов:**
- «Встреча с командой завтра в 14:00 на час»
- «Напомни сдать отчёт в пятницу»
- «Перенеси стендап на завтра в 9 утра»

---

## Просмотр логов

```bash
# Логи агента в реальном времени
docker-compose logs -f agent

# Только ошибки
docker-compose logs agent | grep '"level":"ERROR"'
```

---

## Разработка и тесты

```bash
# Линт + форматирование
ruff check --fix .
ruff format .

# ReAct eval (тест выбора инструментов агентом)
# --delay 2.0 — пауза между кейсами для избежания rate limit Mistral API
python tests/evals/run_nlu_eval.py --delay 2.0 --out tests/evals/results/results.json

# Rate limit stress test
python tests/evals/run_ratelimit_stress.py --delay 2.0 --out tests/evals/results/results_asr.json

# Создать новую миграцию БД
alembic revision --autogenerate -m "description"
```
