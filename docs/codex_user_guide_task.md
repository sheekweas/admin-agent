# Task for Codex: end-user guide (RU, with KZ summary) with real screenshots — v1.0

The full stack runs in docker: panel http://localhost:3000, API http://localhost:8000, demo tenant "Barber KZ",
password `demo12345` for owner@demo.kz, admin@demo.kz, barber@demo.kz, analyst@demo.kz. A Claude agent is writing
ops/handover docs in `docs/` (HANDOVER, OPERATIONS, ONBOARDING_BUSINESS, ARCHITECTURE…) — write ONLY into
`docs/user-guide/` and a screenshot script under `frontend/scripts/` (and `frontend/package.json` script entry).
Use sub-agents if available (e.g., one per role). Don't git commit/push, don't stop containers.

1. Write a Playwright script `frontend/scripts/user-guide-screenshots.ts` (runnable via `npm run guide:screenshots`)
   that logs in with each role and captures clean, consistent screenshots (1440×900 desktop, plus a few 390×844
   mobile) of every page and key dialogs into `docs/user-guide/img/` (PNG, compressed, ≤ 300 KB each). Use demo data;
   create any data needed through the UI/API (e.g., simulate a WhatsApp dialog and a call so inbox/calls look real).
   No real personal data, no tokens visible.
2. Write the guide in Russian (`docs/user-guide/README.md` = table of contents + one file per area):
   - `01-start.md` — вход, выбор организации, язык RU/KZ, тема, мобильная версия, роли и что видит каждая роль.
   - `02-dashboard.md` — показатели и графики, как читать (формулы простыми словами: выручка = оплаты − возвраты,
     конверсия, no-show, загрузка мастеров).
   - `03-calendar.md` — запись клиента, альтернативы при занятом времени, перенос (drag-and-drop), отмена, статусы,
     блокировка времени, оплата из записи.
   - `04-clients-leads.md` — карточка клиента, теги, согласия/отказ от рассылок, объединение дублей, воронка лидов.
   - `05-inbox-ai.md` — как работает AI-администратор, индикаторы «AI отвечает / человек отвечает / ждём админа»,
     перехват диалога и возврат AI, внутренние заметки, симулятор для обучения персонала.
   - `06-tasks-escalation.md` — вопросы AI, Telegram-кнопки «Взять/Ответить», таймеры 5/10 минут, звонок
     администратору (нажмите 1 / 2), ответ клиенту и добавление в базу знаний.
   - `07-calls.md` — голосовой AI, звонок клиенту, расшифровки, согласия на звонки и запись.
   - `08-staff.md` — сотрудники, графики, исключения, заявки мастера на ранний уход и решения по конфликтам.
   - `09-services-finance.md` — услуги и цены (RU/KZ), оплаты, предоплата, возвраты, неоплаченные визиты.
   - `10-analytics.md` — отчёты, фильтры, экспорт CSV/XLSX.
   - `11-ai-knowledge.md` — база знаний: черновик → публикация, тестирование ответов AI, настройки AI,
     экстренное отключение AI.
   - `12-integrations-settings.md` — статусы интеграций (без технических секретов: подключение делает техспециалист по
     docs/ONBOARDING_BUSINESS.md), правила записи, таймеры эскалации, команда и роли, журнал аудита.
   - `13-faq.md` — частые вопросы администраторов (клиент пишет, а AI молчит; почему не пришло уведомление; как
     отменить запись без штрафа; что делать если AI ошибся) .
   - `kz-summary.md` — краткая памятка на казахском (главные действия администратора).
   Each page: short, task-oriented steps, screenshots inline with relative paths, callouts for important rules
   (e.g., «AI никогда не подтверждает запись, пока она не создана в системе»). Verify every described behaviour
   against the real UI (click through it); don't document features that don't exist.
3. Check all image links resolve and the markdown renders (no broken relative links). Print a concise summary.
