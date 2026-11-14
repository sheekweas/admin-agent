# AI CRM для сервисного бизнеса — v1.0.0

Мультиарендная SaaS-платформа для барбершопов, салонов красоты, стоматологий и других сервисных бизнесов:
WhatsApp AI-агент записывает клиентов по реальному расписанию, CRM ведёт клиентов и лиды, администраторы получают
Telegram-уведомления с эскалацией (повтор через 5 минут, звонок через 10), AI отвечает и на телефонные звонки,
владелец видит выручку и аналитику. Реализация ТЗ `AI_CRM_WhatsApp_Technical_Specification.pdf`
(текст — [docs/SPEC_ru.txt](docs/SPEC_ru.txt)). **Передача заказчику — [docs/HANDOVER.md](docs/HANDOVER.md).**

| Часть | Технологии |
|---|---|
| `backend/` | Python 3.12, FastAPI, SQLAlchemy 2, PostgreSQL 16, Alembic, Celery + Redis |
| `frontend/` | Next.js 15, TypeScript, Tailwind, TanStack Query — панель RU/KZ |
| AI | Claude (`claude-opus-5-5`) через сменяемый адаптер + детерминированный `MockLLM` для разработки |
| `deploy/` | production: Caddy (HTTPS), compose, бэкапы, алерты Prometheus |

Ключевые гарантии: нет двойных записей (блокировка + `EXCLUDE USING gist` + idempotency), сообщения клиенту
только после COMMIT и только по результату инструментов, изоляция арендаторов, transactional outbox с ретраями и
DLQ, выручка = оплаты − возвраты. Подробно — [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Быстрый старт (разработка, всё в mock-режиме)

```bash
cp .env.example .env
docker compose up -d --build
docker compose exec api python -m app.seed      # демо-организация «Barber KZ»
```

Панель http://localhost:3000, Swagger http://localhost:8000/docs. Демо-входы (пароль `demo12345`):
`owner@demo.kz`, `admin@demo.kz`, `backup@demo.kz`, `barber@demo.kz`, `analyst@demo.kz`. В «Чатах» и «Звонках»
есть симуляторы WhatsApp-сообщения и звонка — весь сценарий проходит без Meta/Twilio.

## Production (кратко)

```bash
cp deploy/.env.production.example deploy/.env.production && chmod 600 deploy/.env.production
sh deploy/generate-secrets.sh            # вписать значения, домены, ключи провайдеров
docker compose --env-file deploy/.env.production -f deploy/docker-compose.prod.yml up -d --build --wait
docker compose --env-file deploy/.env.production -f deploy/docker-compose.prod.yml exec api \
  python -m app.cli create-tenant --name "Моя организация" --owner-email owner@example.kz
```

Нужны DNS-записи `api.<домен>` и `app.<домен>` на сервер и открытые порты 80/443. Обновление, откат, бэкапы,
мониторинг, аварийное отключение AI (`AI_GLOBALLY_DISABLED=true`) — [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Разработка и тесты

```bash
docker compose up -d postgres redis
cd backend && python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"   # Linux/macOS: .venv/bin/pip
pytest && ruff check app tests          # PostgreSQL обязателен; БД crm_test создаётся автоматически
cd ../frontend && npm ci && npm test && npm run e2e   # e2e — против запущенного стека
```

CI (`.github/workflows/ci.yml`): ruff + pytest с покрытием, lint/typecheck/unit/build фронтенда, Playwright на docker.

## Документация

| Документ | Содержание |
|---|---|
| [docs/HANDOVER.md](docs/HANDOVER.md) | что передано (ТЗ §23.1) и где |
| [docs/user-guide/](docs/user-guide/README.md) | руководство пользователя RU + памятка KZ |
| [docs/ONBOARDING_BUSINESS.md](docs/ONBOARDING_BUSINESS.md) | подключение бизнеса: организация, WhatsApp, Telegram, Twilio, Zapier, go-live |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | runbook: деплой, откат, бэкапы (RPO/RTO), алерты, инциденты, логи |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | компоненты, поток данных, модель данных, инварианты, масштабирование |
| [docs/API.md](docs/API.md), [docs/CONTRACTS.md](docs/CONTRACTS.md) | REST API и внутренние контракты (полная схема — `/openapi.json`) |
| [docs/SECURITY.md](docs/SECURITY.md) | модель угроз, меры ТЗ §20, ротация секретов, чек-лист продакшена |
| [docs/THIRD_PARTY.md](docs/THIRD_PARTY.md) | внешние сервисы, тарифы, ответственные, юридический чек-лист |
| [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md), [docs/ACCEPTANCE_REPORT.md](docs/ACCEPTANCE_REPORT.md), [docs/LOAD_TEST.md](docs/LOAD_TEST.md) | приёмка AT-01…AT-18, итоговый протокол, нагрузка |
| [CHANGELOG.md](CHANGELOG.md) | история версий |
| `frontend/README.md` | структура админ-панели |

## Статус по этапам ТЗ

| Этап | Статус |
|---|---|
| P0 MVP: WhatsApp текст, AI + база знаний, CRM, календарь, брони/отмены, лиды, Telegram, ручной перехват, аналитика | ✅ |
| P1: сложные графики, филиалы, подтверждение админом, платежи, отчёты, Zapier, голосовые WhatsApp | ✅ (STT — интерфейс + mock, провайдер не выбран) |
| P2: звонок администратору при эскалации, AI-звонки клиентам (входящие и исходящие) | ✅ Twilio (пошаговый Gather) + mock; WhatsApp Calling — заглушка |
| Фаза 6: продакшен, бэкапы, мониторинг, runbook, протокол приёмки | ✅ v1.0.0 |
| P3: тарифы SaaS, white-label, BI | заложено в модель данных |

Ограничения и то, что требует решения заказчика, — [docs/ACCEPTANCE_REPORT.md](docs/ACCEPTANCE_REPORT.md).
