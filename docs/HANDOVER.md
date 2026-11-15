# Передача проекта заказчику — AI CRM v1.0.0 (ТЗ §23.1)

Дата: 14.11.2026. Версия: `1.0.0` ([CHANGELOG.md](../CHANGELOG.md)). Итоги проверки — [ACCEPTANCE_REPORT.md](ACCEPTANCE_REPORT.md).

## Что передаётся (по пунктам ТЗ §23.1)

| # | Требование ТЗ §23.1 | Где | Примечание |
|---|---|---|---|
| 1 | Исходный код и репозитории | git-репозиторий, ветка `main`, теги `v0.1.0`…`v0.4.0` (тег `v1.0.0` ставит заказчик после приёмки) | `backend/` (FastAPI, Celery), `frontend/` (Next.js), `deploy/`, `scripts/load/` |
| | Docker-конфигурация | dev: [`docker-compose.yml`](../docker-compose.yml); prod: [`deploy/docker-compose.prod.yml`](../deploy/docker-compose.prod.yml), [`deploy/Caddyfile`](../deploy/Caddyfile), [`deploy/.env.production.example`](../deploy/.env.production.example) | HTTPS (Caddy), БД/Redis без внешних портов, healthchecks, лимиты, ротация логов |
| | Схема БД и миграции | [`backend/app/models.py`](../backend/app/models.py), [`backend/alembic/versions/`](../backend/alembic/versions/) (`0001_initial`, `0002_voice_calls`, `0003_auth_sessions`); обзор — [ARCHITECTURE.md §3](ARCHITECTURE.md#3-модель-данных-обзор) | миграции применяет сервис `migrate` при каждом деплое |
| 2 | OpenAPI / Swagger | `https://api.<домен>/docs`, `/openapi.json`; контракт — [API.md](API.md), внутренние контракты — [CONTRACTS.md](CONTRACTS.md) | ответы типизированы, контракт защищён snapshot-тестом |
| | Вебхуки, события, интеграции, ошибки | входящие: WhatsApp `/v1/webhooks/whatsapp`, Telegram `/v1/webhooks/telegram/{tenant_id}`, Twilio `/v1/webhooks/voice/twilio/*` ([API.md](API.md), [ONBOARDING_BUSINESS.md](ONBOARDING_BUSINESS.md)); исходящие события (17 типов: `booking.*`, `lead.*`, `payment.*`, `admin_task.*`, `shift_request.*`, `staff.schedule_changed`, `message.delivery_failed`) с подписью `X-CRM-Signature` — [SECURITY.md §2](SECURITY.md); формат ошибок `{"detail":{"code","message"}}` — [API.md](API.md) | |
| 3 | Тестовая и рабочая среда с разделением прав и секретов | dev/тест: `docker-compose.yml` + `.env` (mock-провайдеры); prod: отдельный `deploy/.env.production` (не в git), `ENV=production` блокирует дефолтные секреты; роли owner/admin/staff/analyst + доступ по филиалам; оператор платформы — только CLI на сервере | Доступы к серверам и аккаунтам провайдеров создаёт заказчик: см. [OPERATIONS.md §5](OPERATIONS.md#5-секреты) |
| 4 | Инструкция по подключению бизнеса, WhatsApp, Telegram, телефонии, базы знаний | [ONBOARDING_BUSINESS.md](ONBOARDING_BUSINESS.md); работа в панели — [руководство пользователя](user-guide/README.md) (13 разделов RU + [памятка KZ](user-guide/kz-summary.md), со скриншотами) | `python -m app.cli create-tenant` |
| 5 | Тест-кейсы, результаты нагрузочных и интеграционных проверок | [ACCEPTANCE.md](ACCEPTANCE.md) (AT-01…AT-18 → тесты), [ACCEPTANCE_REPORT.md](ACCEPTANCE_REPORT.md) (результаты 14.11.2026), [LOAD_TEST.md](LOAD_TEST.md), сырые результаты `scripts/load/results/` | 355 backend-тестов, 42 Playwright, 39 unit-тестов фронтенда |
| 6 | Бэкапы, восстановление, логи, аварийное отключение AI, поддержка | [OPERATIONS.md](OPERATIONS.md): §4 бэкапы/восстановление (проверено), §6 мониторинг и алерты, §7 инциденты и kill switch, §8 логи, §9 контакты | `deploy/backup/`, `deploy/monitoring/` |
| 7 | Сторонние сервисы, тарифы, расходы, ответственные | [THIRD_PARTY.md](THIRD_PARTY.md) | цены — по официальным ссылкам на дату запуска |

## Документация

| Документ | Для кого |
|---|---|
| [README.md](../README.md) | обзор, быстрый старт dev и prod |
| [user-guide/](user-guide/README.md) | владелец, администратор, мастер, аналитик |
| [ONBOARDING_BUSINESS.md](ONBOARDING_BUSINESS.md) | оператор платформы, владелец бизнеса |
| [OPERATIONS.md](OPERATIONS.md) | эксплуатация (runbook) |
| [ARCHITECTURE.md](ARCHITECTURE.md) | разработчики, архитектор |
| [API.md](API.md), [CONTRACTS.md](CONTRACTS.md), `/docs` | интеграторы, разработчики |
| [SECURITY.md](SECURITY.md) | безопасность, ротация секретов, чек-лист продакшена |
| [THIRD_PARTY.md](THIRD_PARTY.md) | заказчик, финансы, юрист |
| [ACCEPTANCE.md](ACCEPTANCE.md), [ACCEPTANCE_REPORT.md](ACCEPTANCE_REPORT.md), [LOAD_TEST.md](LOAD_TEST.md) | приёмка |
| [SPEC_ru.txt](SPEC_ru.txt) | исходное ТЗ |

## Разграничение доступа (кратко)

| Уровень | Кто | Как |
|---|---|---|
| Сервер, `.env.production`, БД, бэкапы | инженер эксплуатации заказчика | SSH-ключи, `chmod 600`; ключи `SECRETS_ENCRYPTION_KEY`/`BACKUP_PASSPHRASE` — в менеджере паролей вне сервера |
| Организации (создание) | оператор платформы | `python -m app.cli create-tenant` на сервере (аудит `tenant.create`) |
| Организация | owner → admin → staff → analyst | RBAC на сервере, доступ по филиалам, изоляция арендаторов (AT-11) |
| Провайдеры (Meta, Telegram, Twilio, Anthropic, S3) | владелец аккаунта у заказчика | токены вводятся в «Интеграции» (write-only, шифруются) или в `.env.production` |

## Что остаётся за заказчиком

Открытые ограничения и решения — [ACCEPTANCE_REPORT.md §6–7](ACCEPTANCE_REPORT.md#6-открытые-ограничения): живые
проверки с реальными WhatsApp/Telegram/Twilio (AT-18), выбор STT-провайдера, качество казахского TTS, WhatsApp
Calling, вопросы ТЗ §25 и юридические проверки ([THIRD_PARTY.md §5](THIRD_PARTY.md)).
