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
| Звонки (Twilio) | `.env`: `VOICE_PROVIDER=twilio`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` (или в панели). Основному/резервному админу указать телефон и флаг эскалации. AI-звонки клиентам — см. «Голосовой канал» ниже |
| Zapier / внешние системы | Панель «Интеграции → Вебхуки»: URL и события; запросы подписаны `X-CRM-Signature` (HMAC-SHA256), персональные данные не передаются без флага `include_pii` |

### Голосовой канал (AI-звонки, ТЗ §13)

1. Купить в Twilio номер с поддержкой Voice (наличие казахстанских номеров уточняется у провайдера, ТЗ §27).
2. «Интеграции → Звонки» (`PUT /v1/integrations/voice`): `config.phone_number` — этот номер в E.164 (по нему входящий
   звонок находит организацию), секреты `account_sid`, `auth_token`, `from_number`. Без `auth_token` все
   Twilio-вебхуки отклоняются (fail closed, проверка `X-Twilio-Signature`).
3. В Twilio Console у номера: *A call comes in* → Webhook `POST https://<домен>/v1/webhooks/voice/twilio/inbound`;
   *Call status changes* → `POST https://<домен>/v1/webhooks/voice/twilio/status`. `PUBLIC_BASE_URL` должен
   совпадать с публичным адресом (по нему считается подпись). Остальные адреса (`turn`, `dial`, `answer`,
   `recording`) backend передаёт Twilio сам.
4. Настройки организации `settings.voice` (панель «Настройки» или `PATCH /v1/tenant`): запись разговоров
   (`recording_enabled`, по умолчанию выкл.; включение добавляет предупреждение в приветствие, клиенты с
   `consent_flags.call_recording=false` не записываются, исходящие — только при `call_recording=true`), срок хранения
   расшифровок/записей `transcript_retention_days` (очистка — задача beat `ops.voice_retention`, раз в час), лимит
   исходящих `max_outbound_per_day`, окно исходящих `outbound_hours`, порог уверенности распознавания
   `low_confidence_threshold`, голоса `tts_voice_ru` / `tts_voice_kk`.
5. Перевод на человека звонит основному админу эскалации (его телефон в профиле команды) вне тихих часов
   (`settings.escalation.quiet_hours`); иначе/при неответе создаётся задача «Обратный звонок».
6. Исходящие звонки клиентам — только при `consent_flags.outbound_calls=true` и без opt-out.

Ограничения: диалог пошаговый (`<Gather input="speech">` + `<Say>`, перебивание `bargeIn`), а не потоковый —
задержка ответа 1–3 с (метрика `first_response_ms`, отчёт `GET /v1/reports/voice`). **У Twilio нет казахского
TTS-голоса**: по умолчанию казахские фразы читает русский голос `Polly.Tatyana` (`tts_language_kk=ru-RU`) — понятно,
но с акцентом; при появлении голоса у провайдера достаточно сменить `tts_voice_kk`/`tts_language_kk`. Распознавание
KK — `kk-KZ` (`stt_language_kk`; проверьте поддержку в аккаунте Twilio). WhatsApp Business Calling API не
реализован (нужны одобрение Meta и медиашлюз BSP): класс-заглушка `WhatsAppCallingProvider` за флагом
`WHATSAPP_CALLING_ENABLED` честно отвечает «не настроено». В mock-режиме звонок можно смоделировать:
`POST /v1/calls/simulate` (кнопка «Симулировать звонок» в разделе «Звонки»).

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

- `docs/API.md` — контракт REST API, включая раздел «Voice» (полная схема — `/openapi.json`)
- `docs/CONTRACTS.md` — внутренние контракты сервисов
- `docs/SECURITY.md` — модель угроз, меры защиты (ТЗ §20), ротация секретов, чек-лист продакшена
- `docs/SPEC_ru.txt` — исходное ТЗ
- `frontend/README.md` — структура админ-панели

## Статус по этапам ТЗ

| Этап | Статус |
|---|---|
| P0 MVP: WhatsApp текст, AI + база знаний, CRM, календарь, брони/отмены, лиды, Telegram, ручной перехват, аналитика | ✅ |
| P1: сложные графики, филиалы, подтверждение админом, платежи, отчёты, Zapier, голосовые WhatsApp (STT-адаптер) | ✅ (STT — интерфейс + mock) |
| P2: звонок администратору при эскалации (Twilio, IVR «нажмите 1», RU/KK); AI-звонки клиентам: входящие (запись, перевод на человека, обратный звонок) и исходящие (подтверждение, напоминание, перенос) | ✅ Twilio (пошаговый Gather) + mock; WhatsApp Calling — заглушка |
| P3: тарифы SaaS, white-label, BI | заложено в модель данных |
