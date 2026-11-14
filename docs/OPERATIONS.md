# Эксплуатация (production runbook)

Для инженера эксплуатации. Архитектура — [ARCHITECTURE.md](ARCHITECTURE.md), безопасность и ротация секретов —
[SECURITY.md](SECURITY.md), внешние сервисы — [THIRD_PARTY.md](THIRD_PARTY.md).

Все команды — из корня репозитория на сервере. Сокращение:

```bash
alias dc='docker compose --env-file deploy/.env.production -f deploy/docker-compose.prod.yml'
```

## 1. Первая установка

Требования: Linux-сервер с Docker Engine 24+ и плагином compose v2, открытые 80/443, DNS A-записи
`api.<домен>` и `app.<домен>` → IP сервера. Пилот: 4 vCPU / 8 ГБ RAM / 80 ГБ SSD (лимиты контейнеров в compose
в сумме ≈ 7 ГБ).

```bash
git clone <repo> /srv/ai-crm && cd /srv/ai-crm && git checkout v1.0.0
cp deploy/.env.production.example deploy/.env.production && chmod 600 deploy/.env.production
sh deploy/generate-secrets.sh          # Windows: powershell -File deploy/generate-secrets.ps1
# вписать сгенерированные значения, домены, ANTHROPIC_API_KEY, WHATSAPP_APP_SECRET, BACKUP_PASSPHRASE, S3_*
dc config --quiet                      # проверка: нет незаполненных обязательных переменных
dc up -d --build --wait                # migrate -> api/worker/beat -> frontend -> caddy, все healthy
dc ps                                  # migrate: Exited (0), остальные: healthy
curl -fsS https://api.<домен>/health   # {"status":"ok"}
```

- `SECRETS_ENCRYPTION_KEY` и `BACKUP_PASSPHRASE` сохранить **вне сервера** (менеджер паролей): без них
  бэкап не расшифровать и интеграционные токены не прочитать.
- С `ENV=production` API и worker не стартуют с дефолтными/короткими секретами (сообщение
  `Refusing to start with insecure production settings` в `dc logs api`).
- Первая организация: [ONBOARDING_BUSINESS.md](ONBOARDING_BUSINESS.md) §1 (`python -m app.cli create-tenant`).
- `/docs` (Swagger) открыт; если API не должен быть публично документирован — закрыть в `deploy/Caddyfile`
  так же, как `/metrics`.
- Проверено локально (14.11.2026): стек с `API_DOMAIN=api.localhost` (внутренний CA Caddy) — все 9 сервисов
  healthy, HTTPS 200, HTTP→HTTPS 308, `/metrics` снаружи 404, порты наружу только у Caddy
  ([ACCEPTANCE_REPORT.md](ACCEPTANCE_REPORT.md) §2).

## 2. Обновление (deploy / upgrade)

```bash
cd /srv/ai-crm
dc exec backup backup.sh                       # 1. внеплановый бэкап перед обновлением
git fetch --tags && git checkout vX.Y.Z        # 2. новая версия (прочитать CHANGELOG.md)
dc build                                       # 3. сборка образов (сервис продолжает работать)
dc up -d --wait                                # 4. migrate (alembic upgrade head) -> пересоздание api/worker/beat/frontend
dc ps && dc logs migrate | tail -5             # 5. migrate Exited (0), сервисы healthy
```

- Миграции применяются отдельным сервисом `migrate` до пересоздания api/worker/beat; при ошибке миграции
  `up` завершается ошибкой (`dc logs migrate`), зависимые сервисы не пересоздаются — см. откат.
- Простой: секунды на пересоздание api (одна реплика). Вебхуки Meta/Telegram/Twilio повторяются провайдером,
  исходящие события лежат в outbox — ничего не теряется.
- Миграции v0.x → v1.0 аддитивные (`0001_initial`, `0002_voice_calls`); v1.0 новых миграций не добавляет.
- Изменили `PUBLIC_BASE_URL`/домен → `dc build frontend` (URL API встраивается в сборку панели), обновить
  вебхуки в Meta/Twilio, в панели «Интеграции → Telegram → Проверить» (перерегистрация вебхука).
- Изменение только `.env.production` (например kill switch) → `dc up -d` (compose пересоздаёт затронутые сервисы).

## 3. Откат (rollback)

1. **Код без изменений схемы** (обычный случай): `git checkout <предыдущий тег> && dc up -d --build --wait`.
2. **Новая миграция мешает старому коду**: `dc run --rm migrate alembic downgrade <ревизия предыдущей версии>`
   (например `0001_initial` для отката 0002), затем п. 1. Downgrade удаляет добавленные столбцы/таблицы вместе с
   данными в них.
3. **Данные повреждены**: восстановить бэкап, сделанный перед обновлением (§4.3), затем п. 1.

## 4. Резервное копирование и восстановление (ТЗ §20.1, §21: RPO ≤ 24 ч, RTO ≤ 4 ч)

### 4.1 Как устроено (`deploy/backup/`)

| Параметр | Значение |
|---|---|
| Что | `pg_dump -Fc` всей БД (схема + данные), проверка `pg_restore --list`, sha256 |
| Когда | ежедневно в `BACKUP_TIME_UTC` (по умолчанию 21:30 UTC = 02:30 Алматы) + при старте контейнера (`BACKUP_ON_START`) |
| Хранение | том `backups` (или `BACKUP_HOST_DIR`): `daily/` — последние 7, `weekly/` — последние 4 (копия за `BACKUP_WEEKLY_DAY`) |
| Шифрование | AES-256-CBC + PBKDF2 (200k итераций), если задан `BACKUP_PASSPHRASE` → файлы `*.dump.enc` |
| Вне сервера | при заданном `S3_BUCKET`: `aws s3 cp` в `s3://$S3_BUCKET/$S3_PREFIX/daily/` (любой S3-совместимый провайдер через `S3_ENDPOINT_URL`) |
| Контроль | healthcheck контейнера `backup` = unhealthy, если последний успешный бэкап старше `BACKUP_MAX_AGE_HOURS` (26 ч) |

Секреты интеграций в дампе зашифрованы `SECRETS_ENCRYPTION_KEY` — для восстановления нужен и он (хранить отдельно).

**Срок хранения в S3** — lifecycle-правило бакета, например удалять `ai-crm/daily/` старше 35 дней (на дату
внедрения проверить синтаксис у провайдера):

```json
{"Rules": [{"ID": "ai-crm-daily", "Status": "Enabled", "Filter": {"Prefix": "ai-crm/daily/"},
            "Expiration": {"Days": 35}}]}
```

Ключ доступа для бэкапа — только на этот бакет; желательно versioning / object lock от удаления.

**RPO** = 24 ч (ночной дамп). Нужен меньший — запускать `backup.sh` чаще (cron на хосте:
`0 */6 * * * docker compose … exec -T backup backup.sh`) или включить WAL-архивацию (pgBackRest/WAL-G) — это
отдельная настройка, в v1.0 не входит.

### 4.2 Ручные операции

```bash
dc exec backup backup.sh                                  # бэкап сейчас
dc exec backup ls -lt /backups/daily /backups/weekly      # список
dc logs backup | grep backup:                             # журнал (OK/FAILED, размер, длительность)
dc exec backup cat /backups/.last_success                 # время последнего успешного
```

### 4.3 Восстановление (проверенная процедура)

`restore.sh <файл|s3://…> <новая_БД> [--compare <БД>]` никогда не перезаписывает существующую непустую БД:
проверяет sha256 → расшифровывает → `pg_restore --list` → `CREATE DATABASE` → `pg_restore -j4 --exit-on-error`
→ выводит число таблиц/строк; с `--compare` сверяет число строк каждой таблицы с другой БД (код 3 при расхождении).

Восстановление продакшена (сервер жив, данные испорчены):

```bash
dc exec backup ls -lt /backups/daily                                       # 1. выбрать дамп
dc exec backup restore.sh /backups/daily/crm_<stamp>.dump.enc crm_restored # 2. в НОВУЮ БД, сервис ещё работает
dc stop caddy api worker beat                                              # 3. остановить запись (клиенты: 502 от Caddy, провайдеры повторят вебхуки)
dc exec postgres psql -U crm -d postgres \
  -c 'ALTER DATABASE crm RENAME TO crm_broken_<дата>' \
  -c 'ALTER DATABASE crm_restored RENAME TO crm'                           # 4. подмена
dc up -d --wait                                                            # 5. migrate (no-op) + старт
# 6. проверить вход в панель, календарь на сегодня, «Интеграции → События»; crm_broken_* удалить через неделю
```

Сервер потерян: новый сервер по §1 с тем же `.env.production` (из менеджера паролей) → `dc up -d --wait`
(пустая БД со схемой) → `dc exec backup restore.sh s3://<bucket>/ai-crm/daily/crm_<stamp>.dump.enc crm_restored`
→ шаги 3–5 выше → DNS на новый IP. До переключения DNS провайдеры шлют вебхуки на старый адрес (Meta и Twilio
повторяют доставку ограниченное время — проверить по их документации).

**Проверка 14.11.2026** (дамп dev-БД → восстановление в новую БД `crm_restore_drill` → сверка, затем удалена):

| Шаг | Результат |
|---|---|
| `backup.sh` (с шифрованием) | 0,55 с, 568 КБ, 32 таблицы с данными |
| `restore.sh … --compare crm` | контрольная сумма OK, `pg_restore` 4 с, всего 4,5 с; 32 таблицы, **7 142 строки — совпадают с источником по каждой таблице**; `alembic_version = 0002_voice_calls` |
| Источник до/после дампа | без изменений (снимок консистентен) |
| Защиты | непустая целевая БД → отказ (код 1); неверный пароль → отказ до создания БД; изменённый файл → «checksum mismatch» |
| Ротация | 9 запусков подряд → `daily` = 7, `weekly` = 4 |
| Healthcheck | свежий бэкап → 0; бэкап двухдневной давности → 1 (unhealthy) |

**Оценка RTO** (худший случай — потерян сервер): новый сервер с Docker 30–60 мин, `git clone` + `.env` 10 мин,
сборка образов ~5 мин (измерено 4 мин 42 с с нуля), загрузка дампа из S3 и восстановление — минуты
(4,5 с на 7 тыс. строк; при 1 ГБ дампа ориентировочно 5–15 мин, `pg_restore -j4`), DNS 5–60 мин (TTL записи
держать 300 с). Итого ≈ 1–2,5 ч < 4 ч. Повторять учение восстановления ежеквартально и после крупных изменений схемы.

## 5. Секреты

Ротация каждого секрета — [SECURITY.md §3](SECURITY.md#3-rotating-secrets). `.env.production` — `chmod 600`,
владелец root, не в git (`.gitignore`). Доступ к серверу — только по SSH-ключам; отдельные учётные данные для
тестовой и рабочей среды (разные `.env`, разные боты/номера/ключи провайдеров).

## 6. Мониторинг и алерты

**Метрики** — `GET /metrics` на `api:8000` (формат Prometheus). Снаружи Caddy отвечает 404. Внутри: без
`METRICS_TOKEN` — только клиенты из частных сетей; с токеном — заголовок `Authorization: Bearer <METRICS_TOKEN>`.
Пример scrape-конфигурации — `deploy/monitoring/prometheus-scrape.example.yml`.

| Метрика | Смысл |
|---|---|
| `crm_http_requests_total{method,route,status}`, `crm_http_request_duration_seconds` | RED по шаблону маршрута (`/v1/bookings/{booking_id}`), неизвестные пути — `unmatched` |
| `crm_outbox_events{status=pending|failed}`, `crm_outbox_oldest_pending_age_seconds` | очередь событий |
| `crm_notifications{status=pending|failed|dead}` | доставка Telegram/WhatsApp/вебхуков, `dead` = DLQ |
| `crm_admin_tasks{status=open|acked}`, `crm_admin_tasks_sla_overdue` | задачи администраторам |
| `crm_ai_runs_last_hour`, `crm_ai_run_errors_last_hour`, `crm_ai_guard_blocks_last_hour` | AI-ходы, ошибки (клиент получил резервный ответ), блокировки «вы записаны» без брони |
| `crm_escalation_calls_last_hour{status}` | звонки администраторам |
| `crm_db_up`, `crm_ai_globally_disabled` | доступность БД, состояние kill switch |

Метрики агрегированы по платформе (без tenant_id и персональных данных); разбивку по арендаторам даёт панель
(«Аналитика», отчёты) и SQL.

**Алерты** — `deploy/monitoring/prometheus-alerts.yml` (проверено `promtool check rules`: 14 правил):

| Алерт | Условие (старт, подстроить по факту) | Действие |
|---|---|---|
| `CrmApiDown` / `CrmDatabaseUnreachable` | scrape не проходит / `crm_db_up == 0` 2 мин | §7 «API недоступен» |
| `CrmHigh5xxRate` | > 2 % 5xx за 5 мин | логи api по `rid`, последние релизы → откат |
| `CrmSlowApi` | p95 > 2 с 15 мин (ТЗ §21) | нагрузка БД, пул соединений, масштабирование api |
| `CrmWebhookAuthFailures` | 401/403 на `/v1/webhooks/*` | §7 WhatsApp/Telegram |
| `CrmOutboxBacklog` / `CrmOutboxFailedEvents` | старейшее событие > 5 мин / есть failed | §7 «Очередь outbox» |
| `CrmDeadNotifications` / `CrmNotificationRetriesPiling` | новые dead за 30 мин / > 20 в ретраях | §7 «Telegram не доставляет» |
| `CrmAiErrorRate` | ≥ 5 ошибок и > 20 % ходов за час | §7 «AI-провайдер недоступен» |
| `CrmEscalationCallsFailing` | ≥ 3 failed/busy за час | §7 «Звонки эскалации» |
| `CrmAdminTasksOverdue` | задачи после SLA 30 мин | сообщить владельцу бизнеса |
| `CrmAiKillSwitchOn` | kill switch включён > 1 ч | напоминание вернуть AI |
| `HostDiskFillingUp` | < 15 % свободно (node_exporter) | §7 «Заканчивается диск» |

Без Prometheus/node_exporter: минимум — cron на хосте раз в 5 мин
`docker ps --filter health=unhealthy --format '{{.Names}}'` → уведомление, если вывод не пуст (покрывает и
«бэкап старше 26 ч»), плюс внешний HTTP-мониторинг `https://api.<домен>/health` и `https://app.<домен>/login`.

## 7. Типовые инциденты

### API недоступен
`dc ps` → какой сервис не healthy; `dc logs --tail 200 api`. Частые причины: БД недоступна (`dc logs postgres`,
диск), неверный секрет после правки `.env` (`Refusing to start…`), исчерпаны соединения
(`remaining connection slots` — уменьшить реплики/`DB_POOL_SIZE` или поднять `PG_MAX_CONNECTIONS`). Перезапуск:
`dc up -d --wait api`.

### WhatsApp-вебхук не доходит или отклоняется
1. Meta App → WhatsApp → Configuration: URL `https://api.<домен>/v1/webhooks/whatsapp`, подписка на `messages`;
   в разделе вебхуков Meta видны ошибки доставки.
2. `dc logs caddy | grep webhooks/whatsapp` — доходят ли запросы; `dc logs api | grep webhooks/whatsapp` — коды.
   401 = подпись не сошлась: `WHATSAPP_APP_SECRET` не от того приложения (или у арендатора свой `app_secret`).
   Верификация (GET) 403 = `WHATSAPP_VERIFY_TOKEN` не совпадает с введённым в Meta.
3. Сообщения приходят, но ответы не уходят: «Интеграции → События» — `NOT_CONFIGURED` (нет токена/phone_number_id),
   `OUTSIDE_SERVICE_WINDOW` (нет шаблона вне 24 ч), 401 от Graph API — истёк токен (нужен system-user токен).
4. 429 — превышен `RATE_LIMIT_WEBHOOK_PER_MINUTE` с IP Meta: поднять лимит.

### Telegram не доставляет
1. «Интеграции → Telegram → Проверить»: ошибка токена → новый токен у @BotFather; успешная проверка заново
   регистрирует вебхук (нужно после смены `TELEGRAM_WEBHOOK_SECRET` или домена).
2. Конкретному админу не приходит: его chat id в профиле, он не заблокировал бота (`/start` ещё раз).
3. Уведомления `dead` (`crm_notifications{status="dead"}`): причина в `last_error` (403 — бот заблокирован,
   400 chat not found — неверный chat id). Повтор — «Интеграции → События → Повторить».
4. Кнопки не работают: Telegram получает 401 — секрет вебхука устарел → «Проверить».

### Очередь outbox растёт
`crm_outbox_oldest_pending_age_seconds` растёт → не работает beat или worker: `dc ps beat worker`,
`dc logs --tail 100 worker`. Проверка worker: `dc exec worker sh -c 'celery -A app.workers.celery_app inspect ping'`.
Redis: `dc exec redis redis-cli ping` (пароль из `REDISCLI_AUTH`). Перезапуск `dc restart worker beat` —
события обрабатываются идемпотентно, дублей не будет. Failed-события — причина в «Интеграции → События», после
устранения — «Повторить».

### AI-провайдер недоступен
Симптомы: `CrmAiErrorRate`, в `ai_runs.error` ошибки провайдера. Клиенты автоматически получают резервное
сообщение, администратору создаётся задача, диалог переходит человеку (AT-10) — данные не теряются.
Если сбой длительный или AI ведёт себя некорректно (ошибочные ответы, утечка, атака) — **аварийное отключение**:

| Уровень | Как включить | Поведение |
|---|---|---|
| Вся платформа | `AI_GLOBALLY_DISABLED=true` в `deploy/.env.production` → `dc up -d` (пересоздаст api/worker/beat) | модель не вызывается; на каждое новое обращение — резервное сообщение организации + задача «AI не смог ответить» (high) + диалог в `waiting_admin`; входящие звонки сразу переводятся администратору; тест базы знаний — 503; метрика `crm_ai_globally_disabled 1` |
| Одна организация | владелец: «AI и знания → Настройки» (выключить AI) или `PATCH /v1/tenant` `{"settings": {"ai": {"enabled": false}}}` | AI молчит, сообщения ждут администратора в «Чатах» (без автоответа и без задачи); звонки идут администратору |
| Один диалог | «Чаты» → «Перехватить» | AI не отвечает в этом диалоге до «Вернуть AI» (AT-15) |

Обратное включение — `AI_GLOBALLY_DISABLED=false` + `dc up -d`. Диалоги, переведённые к человеку, остаются у
людей до «Вернуть AI» в панели; открытые задачи закрывают администраторы. Сообщить владельцам организаций.

### Звонки эскалации не проходят
`crm_escalation_calls_last_hour{status="failed"}`: в «Звонки» — ошибка провайдера. Проверить в Twilio Console:
баланс, trial-ограничения, Geo permissions для KZ, номер `from_number`; «Интеграции → Звонки → Проверить».
Telegram-эскалация при этом продолжает работать. Временно: `settings.escalation.calls_enabled=false`.

### Сообщение о двойной записи
Двойная запись технически исключена ограничениями `ex_bookings_staff_overlap` / `ex_bookings_resource_overlap`.
При жалобе:
1. Номера броней (`BK-…`) от администратора; `GET /v1/bookings/{id}/history` (аудит: кто, когда, через какой канал).
2. Проверка пересечений в БД (должно вернуть 0 строк):
   ```sql
   SELECT a.number, b.number, a.staff_id, a.occupied_start, a.occupied_end
   FROM bookings a JOIN bookings b ON a.staff_id = b.staff_id AND a.id < b.id
    AND tstzrange(a.occupied_start, a.occupied_end) && tstzrange(b.occupied_start, b.occupied_end)
   WHERE a.status IN ('held','pending','confirmed','checked_in') AND b.status IN ('held','pending','confirmed','checked_in');
   ```
   (`dc exec postgres psql -U crm -d crm`).
3. Частые реальные причины: две брони у **разных** мастеров/филиалов; запись, внесённая мимо системы; клиент
   перепутал часовой пояс филиала; бронь «held» истекла и слот занят другим.
4. Если пересечение найдено — критический дефект: сохранить вывод, `ai_runs` диалога, логи api за период по
   `rid`, передать разработчику (§9).

### Заканчивается диск
`df -h`; `docker system df`. Основные потребители: том `pgdata`, `backups` (7+4 дампа), логи контейнеров
(ограничены 5×20 МБ на сервис), старые образы (`docker image prune -a` после успешного обновления). Рост
`messages`/`ai_runs` — архивирование по дате (план на v1.x).

## 8. Логи и аудит

| Что | Где |
|---|---|
| Логи сервисов | `dc logs [-f] [--since 1h] <service>`; файлы docker json-file с ротацией 5 × 20 МБ на контейнер (`/var/lib/docker/containers/<id>/*-json.log`) |
| API | JSON-строки (`ts, level, logger, msg`), у каждого запроса `rid=<request id>` (он же в заголовке `x-request-id` и в теле ошибки 500); секреты, токены и JWT вырезаются в любом логе |
| Caddy | JSON access log (`dc logs caddy`), IP клиента, хост, статус |
| Аудит действий | таблица `audit_logs`, в панели «Настройки → Журнал аудита» (владелец) |
| AI | таблица `ai_runs` (промпт-версия, статьи, вызовы инструментов, токены, ошибка) по `conversation_id` |
| Доставка | «Интеграции → События» (outbox + уведомления, ретраи, DLQ) |
| Звонки | раздел «Звонки» (статусы, расшифровки до срока хранения) |

Для централизованного сбора (Loki/ELK) — драйвер логов docker или promtail по файлам json-file; редактирование
секретов уже выполнено приложением.

## 9. Поддержка и контакты

| Роль | Контакт |
|---|---|
| Дежурный инженер эксплуатации | `<заполнить: имя, телефон, Telegram>` |
| Разработчик (дефекты, двойная запись, утечки) | `<заполнить>` |
| Ответственный за договоры Meta / Twilio / Anthropic / хостинг | `<заполнить>` |
| Юрист / ответственный за персональные данные | `<заполнить>` |

Журнал инцидентов (ТЗ §20.1): дата, обнаружение, влияние (организации, клиенты), действия, первопричина,
меры — вести в общем документе заказчика.
