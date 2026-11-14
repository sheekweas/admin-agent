# AI CRM для сервисного бизнеса

Мультиарендная SaaS-платформа для барбершопов, салонов красоты, стоматологий и других сервисных бизнесов:
WhatsApp AI-агент записывает клиентов по реальному расписанию, CRM ведёт клиентов и лиды, администраторы получают
Telegram-уведомления с эскалацией (повтор через 5 минут, звонок через 10), владелец видит выручку и аналитику.

Реализация технического задания `AI_CRM_WhatsApp_Technical_Specification.pdf` (текст — `docs/SPEC_ru.txt`).

## Состав

| Часть | Технологии | Что делает |
|---|---|---|
| `backend/` | Python 3.12, FastAPI, SQLAlchemy 2, PostgreSQL 16, Alembic | REST API, booking engine, CRM, AI-оркестратор, вебхуки |
| `backend/app/workers` | Celery + Redis (beat) | outbox, напоминания, эскалации 5/10 мин, удержания слотов, сводки |
| `frontend/` | Next.js 15, TypeScript, Tailwind, TanStack Query, Recharts | Админ-панель RU/KZ: дашборд, календарь, CRM, inbox, финансы… |
| AI | Claude (`claude-opus-5-5`) через сменяемый адаптер + детерминированный `MockLLM` | Ответы RU/KZ, вызовы проверяемых backend-инструментов |

Ключевые гарантии из ТЗ:
- **Нет двойных записей**: блокировка строки сотрудника + PostgreSQL `EXCLUDE USING gist` по интервалу времени
  (сотрудник и ресурс) + idempotency key. Проверено конкурентным тестом (AT-01).
- **AI не бронирует «на словах»**: бронь создаёт только `create_booking`; сообщение клиенту уходит только после
  COMMIT транзакции; фраза «вы записаны» без успешного вызова инструмента блокируется.
- **Изоляция арендаторов**: `tenant_id` берётся только из токена; чужие id неотличимы от несуществующих (404);
  попытки доступа к чужой организации логируются (AT-11).
- **Ничего не теряется**: transactional outbox → worker с ретраями/backoff/DLQ; повторный вебхук не создаёт дублей.
- **Выручка = подтверждённые оплаты − возвраты**, а не число записей (AT-08, AT-14).

## Быстрый старт (Docker)

```bash
cp .env.example .env            # по умолчанию все внешние сервисы в mock-режиме
docker compose up -d --build
docker compose exec api python -m app.seed   # демо-организация «Barber KZ»
```

- Админ-панель: http://localhost:3000
- API и Swagger: http://localhost:8000/docs
- Демо-входы (пароль `demo12345`): `owner@demo.kz` (владелец), `admin@demo.kz` (администратор, основной для
  эскалаций), `backup@demo.kz` (резервный), `barber@demo.kz` (мастер), `analyst@demo.kz` (аналитик).

В mock-режиме в разделе «Чаты» есть кнопка «Симулировать сообщение WhatsApp» — так можно пройти весь сценарий
записи без подключения Meta.

## Подключение реальных сервисов

| Сервис | Что указать |
|---|---|
| Claude | `.env`: `LLM_PROVIDER=anthropic`, `ANTHROPIC_API_KEY=…` (модель `ANTHROPIC_MODEL`, по умолчанию `claude-opus-5-5`) |
| WhatsApp Cloud API | `.env`: `WHATSAPP_MODE=live`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`; в панели «Интеграции → WhatsApp»: `phone_number_id`, access token, шаблон для сообщений вне 24-часового окна. Вебхук Meta: `https://<домен>/v1/webhooks/whatsapp` |
| Telegram | `.env`: `TELEGRAM_MODE=live`, `TELEGRAM_WEBHOOK_SECRET`, публичный `PUBLIC_BASE_URL`; в панели — токен бота. Каждый администратор пишет боту `/start`, получает chat id и вносит его в профиль команды |
| Звонки (Twilio) | `.env`: `VOICE_PROVIDER=twilio`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` (или в панели). Основному/резервному админу указать телефон и флаг эскалации |
| Zapier / внешние системы | Панель «Интеграции → Вебхуки»: URL и события; запросы подписаны `X-CRM-Signature` (HMAC-SHA256), персональные данные не передаются без флага `include_pii` |

## Разработка

```bash
docker compose up -d postgres redis
cd backend && python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"   # Linux/macOS: .venv/bin/pip
pytest                       # нужен PostgreSQL (exclusion constraints); БД crm_test создаётся автоматически
ruff check app tests
uvicorn app.main:app --reload
celery -A app.workers.celery_app worker -l INFO   # и beat
cd ../frontend && npm ci && npm run dev
```

CI (`.github/workflows/ci.yml`): ruff + pytest с покрытием на PostgreSQL, lint + typecheck + тесты + сборка фронтенда.

## Документация

- `docs/API.md` — контракт REST API (полная схема — `/openapi.json`)
- `docs/CONTRACTS.md` — внутренние контракты сервисов
- `docs/SPEC_ru.txt` — исходное ТЗ
- `frontend/README.md` — структура админ-панели

## Статус по этапам ТЗ

| Этап | Статус |
|---|---|
| P0 MVP: WhatsApp текст, AI + база знаний, CRM, календарь, брони/отмены, лиды, Telegram, ручной перехват, аналитика | ✅ |
| P1: сложные графики, филиалы, подтверждение админом, платежи, отчёты, Zapier, голосовые WhatsApp (STT-адаптер) | ✅ (STT — интерфейс + mock) |
| P2: звонок администратору при эскалации (Twilio, IVR «нажмите 1») | ✅ адаптер; AI-звонки клиентам — следующий этап |
| P3: тарифы SaaS, white-label, BI | заложено в модель данных |
