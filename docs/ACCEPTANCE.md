# Матрица приёмки AT-01…AT-18 (ТЗ §22)

Статус на этапе 4 (голосовой канал; этап 3 — E2E и нагрузка). Все автотесты лежат в `backend/tests/`, работают на PostgreSQL (нужны
exclusion constraints) и используют mock-адаптеры WhatsApp / Telegram / Voice / LLM. Запуск на отдельной БД:

```powershell
$env:TEST_DATABASE_URL="postgresql+psycopg://crm:crm@localhost:5432/crm_test_e2e"
cd backend; .venv\Scripts\python -m pytest -q -o addopts=""
```

Статусы: **✅ авто** — критерий проверяется автотестом; **🟡 частично** — автотестом проверен mock-путь,
нужна ручная проверка с реальным провайдером; **🖐 вручную** — автоматизировать нельзя, ниже описана процедура.

| ID | Проверка (критерий приёмки) | Доказательство (файл::тест) | Статус | Примечания |
|---|---|---|---|---|
| AT-01 | Два клиента одновременно выбирают последний слот → подтверждена одна бронь, второму предложена альтернатива | `test_booking.py::test_at01_concurrent_last_slot[2]`, `[6]`; `test_booking.py::test_db_exclusion_constraint_is_final_guard`; `test_api_booking.py::test_conflict_409_with_alternatives`; `test_stage3_regressions.py::test_concurrent_same_idempotency_key_returns_one_booking`; нагрузка `scripts/load/slot_race.py` | ✅ авто | Потоки с отдельными сессиями + `EXCLUDE USING gist`. На живом стеке 50 параллельных запросов → 1×201 + 49×409 с альтернативами (docs/LOAD_TEST.md). Найден и исправлен дефект: одинаковый `Idempotency-Key` под конкуренцией давал 409 вместо той же брони |
| AT-02 | Повторный webhook WhatsApp → нет дубля сообщения, лида, брони | `test_whatsapp_webhook.py::test_duplicate_webhook_is_noop`; `test_security_webhooks.py::test_whatsapp_replayed_payload_creates_no_duplicates`; `test_ai_agent.py::test_ai_turn_idempotent`; `test_ai_tools_security.py::test_create_booking_idempotent_per_trigger_message`; нагрузка `scripts/load/webhook_burst.py` | ✅ авто | Всплеск 500 вебхуков (100 повторов, 50 параллельно): сохранено ровно уникальных сообщений, 1 контакт и 1 диалог на клиента |
| AT-03 | AI не знает цену → вопрос админу, цена не выдумана | `test_ai_agent.py::test_unknown_price_escalates_without_inventing`, `::test_discount_question_escalates`; `test_knowledge.py::test_ai_uses_only_published_articles`; E2E `test_e2e_flows.py::test_e2e_unknown_question_escalates_calls_and_becomes_knowledge` | ✅ авто | E2E проверяет и обратный путь: ответ админа → черновик базы знаний → публикация владельцем → AI отвечает сам |
| AT-04 | Нет ACK 5 минут → повторное Telegram-уведомление | `test_escalation.py::test_at04_repeat_after_5_minutes_to_primary_and_backup`; E2E `test_e2e_flows.py::test_e2e_unknown_question_escalates_calls_and_becomes_knowledge` | ✅ авто | Время инжектируется в `run_process_escalations(now=…)`; 4:59 — ничего, 5:00 — повтор основному и резервному |
| AT-05 | Нет ACK 10 минут → разрешённый звонок, статус записан | `test_escalation.py::test_at05_call_after_10_minutes_then_backup_then_cap`, `::test_no_answer_status_pulls_backup_call_forward`, `::test_quiet_hours_skip_calls_but_keep_telegram`, `::test_calls_disabled_by_tenant_settings`; `test_voice_security.py::test_admin_escalation_ivr_in_kazakh_for_kk_admin`, `::test_kk_ivr_forward_phrase`, `::test_twilio_rest_payloads`; E2E (IVR «1» через `/v1/webhooks/voice/mock/gather`) | 🟡 авто (mock voice) | Логика, статусы `Call`, IVR RU/KK (язык — `ui_language` админа) и ограничения проверены с mock-провайдером; формат REST-запросов к Twilio проверен подменой HTTP. Реальный Twilio — ручная проверка по процедуре AT-18 (п. 3) |
| AT-06 | Админ подтвердил на 7-й минуте → звонок отменён, повторов нет | `test_escalation.py::test_at06_ack_at_7th_minute_cancels_call_and_repeats`, `::test_ack_cancels_ringing_call`, `::test_ack_is_idempotent_first_wins`, `::test_ivr_digit_1_acks_task`; `test_telegram_webhook.py::test_take_button_acks_task` | ✅ авто | |
| AT-07 | Мастер ушёл раньше, есть запись → слот закрыт, конфликт на решение | `test_schedule.py::test_at07_early_leave_closes_slots_and_lists_conflicts`; E2E `test_e2e_flows.py::test_e2e_early_leave_approved_in_telegram_conflict_rescheduled`; `test_stage3_regressions.py::test_shift_request_telegram_card_is_rendered`, `::test_reschedule_of_ai_booking_notifies_client_unless_done_by_ai[user]` | ✅ авто | E2E: запрос мастера через API → карточка в Telegram → кнопка «Подтвердить» → задача `schedule_conflict`, бронь не отменена, слоты закрыты → перенос к другому мастеру → клиент уведомлён один раз. Найдены и исправлены 2 дефекта (карточка не рендерилась; клиент не уведомлялся о переносе AI-брони админом) |
| AT-08 | Запись подтверждена, оплаты нет → в бронях есть, оплаченная выручка 0 | `test_payments_reports.py::test_at08_confirmed_booking_without_payment_has_zero_revenue`; E2E `test_e2e_flows.py::test_e2e_kk_whatsapp_booking_to_paid_visit_and_repeat_lead` | ✅ авто | E2E: после визита без оплаты `revenue=0`, `unpaid_completed=1`; после оплаты `revenue=5000` |
| AT-09 | Клиент отменил бронь → слот освобождён, уведомления обновлены, аудит | `test_api_booking.py::test_at09_cancel_frees_slot_notifies_and_audits`; `test_booking.py::test_cancel_frees_slot_and_respects_deadline`; `test_ai_agent.py::test_cancel_own_booking`; `test_outbox_worker.py::test_cancel_event_cancels_reminders_and_alerts` | ✅ авто | |
| AT-10 | Внешний AI/Telegram недоступен → данные не теряются, очередь повторов | `test_ai_agent.py::test_llm_failure_fallback_task_and_data_kept`; `test_outbox_worker.py::test_at10_telegram_down_nothing_lost`, `::test_handler_failure_keeps_event_with_backoff_then_failed`; `test_notifications.py::test_delivery_retries_with_backoff_then_dead`; E2E `test_e2e_flows.py::test_e2e_telegram_outage_retries_with_backoff_and_delivers_once` | ✅ авто | E2E: адаптер Telegram бросает 502 → событие обработано, уведомления ждут с экспоненциальным backoff → восстановление → доставлено ровно по одному разу, повторная обработка события дублей не даёт |
| AT-11 | Мастер открывает чужую организацию → запрет сервером, событие в логе | `test_auth.py::test_cross_tenant_denied_and_logged`; `test_security_isolation.py::test_idor_sweep_foreign_ids_are_404`, `::test_tenant_switch_header_and_forged_tokens`, `::test_staff_role_sees_only_own_calendar`; `test_api_booking.py::test_tenant_isolation_idor`; `test_telegram_webhook.py::test_unknown_chat_is_denied_and_audited` | ✅ авто | Security-набор ведёт инженер безопасности (`test_security_*`) |
| AT-12 | Голосом названа неоднозначная дата → AI уточняет до записи | `test_voice_inbound.py::test_at12_voice_ambiguous_date_and_low_confidence_are_reasked`, `::test_two_failed_recognitions_transfer_to_on_duty_admin_then_callback`; `test_ai_agent.py::test_ambiguous_date_is_clarified`; `test_whatsapp_webhook.py::test_voice_note_is_transcribed_and_answered` | 🟡 авто (Twilio-вебхуки, mock STT) | Телефонный звонок: «на выходных» → уточнение, брони нет; фраза с `Confidence` ниже `settings.voice.low_confidence_threshold` → переспрос, текст не уходит в AI; 2 неудачи подряд → перевод/обратный звонок. С реальным распознаванием Twilio — ручная проверка (AT-18, п. 4) |
| AT-13 | Клиент просит не писать → opt-out, запрещённые рассылки не идут | `test_ai_agent.py::test_opt_out[…]` (RU/KK/EN), `::test_opt_out_detection_negatives`; `test_crm.py::test_opt_out` | ✅ авто | Сервисные сообщения (напоминания) остаются разрешены — `test_notifications.py` |
| AT-14 | Платёж отменён/возвращён → выручка пересчитана | `test_payments_reports.py::test_partial_and_multiple_payments_then_refund_at14`, `::test_cancelled_payment_is_excluded_and_transaction_ref_idempotent`, `::test_payments_api_permissions` | ✅ авто | |
| AT-15 | AI передал диалог человеку → AI не отвечает параллельно | `test_ai_agent.py::test_human_mode_silences_ai`; `test_inbox_api.py::test_takeover_release`; `test_escalation.py::test_sla_skipped_message_when_human_took_over` | ✅ авто | |
| AT-16 | Клиент пишет RU и KZ → язык переключается | `test_ai_agent.py::test_language_switch`, `::test_full_booking_dialog_kk_asks_name`; E2E `test_e2e_flows.py::test_e2e_kk_whatsapp_booking_to_paid_visit_and_repeat_lead` | ✅ авто | |
| AT-17 | Бронь на границе смены → длительность, буферы, закрытие смены | `test_availability.py::test_buffers_must_fit_inside_working_hours`; `test_schedule.py::test_at17_shift_boundary_with_buffers`; `test_booking.py::test_outside_working_hours_rejected` | ✅ авто | |
| AT-18 | Голосовой звонок в реальной сети → слышно RU/KZ, перебивания, перевод человеку | Автоматизируемая часть: `test_voice_inbound.py::test_inbound_ru_booking_with_whatsapp_confirmation`, `::test_inbound_kk_booking_with_whatsapp_confirmation` (TwiML `language="kk-KZ"`, `bargeIn="true"`), `::test_human_request_dials_admin[…]`, `::test_two_failed_recognitions_transfer_to_on_duty_admin_then_callback`; `test_voice_security.py::test_admin_escalation_ivr_in_kazakh_for_kk_admin`; `test_voice_api.py::test_outbound_confirmation_call_flow` | 🟡 авто + 🖐 вручную | Автотестами закрыто: казахские фразы IVR и AI-звонка, распознавание `kk-KZ`, перебивание (`bargeIn`), перевод `<Dial>` и обратный звонок. Слышимость и качество KK-речи в реальной сети — только вручную (процедура ниже). Ограничение: у Twilio нет казахского TTS-голоса — по умолчанию казахский текст читает русский голос `Polly.Tatyana` (`settings.voice.tts_voice_kk`/`tts_language_kk`) |

## Сквозные сценарии (ТЗ §22.1 «E2E: запись от первого сообщения до посещения и оплаты»)

`backend/tests/test_e2e_flows.py` — только публичный HTTP-контур: подписанные вебхуки WhatsApp, callback-вебхуки
Telegram, IVR-вебхук, REST с JWT; фоновые задачи — `app.workers.tasks.run_*` синхронно, время инжектируется.

| Тест | Сценарий |
|---|---|
| `test_e2e_kk_whatsapp_booking_to_paid_visit_and_repeat_lead` | Новый клиент пишет на казахском → AI спрашивает имя, предлагает слот, бронирует только после «Иә» → Telegram обоим админам (один раз) → напоминания запланированы и отправлены в срок → check-in → completed → выручка 0 (AT-08) → оплата → overview: выручка, визиты, воронка → повторное обращение создаёт новый лид `repeat` |
| `test_e2e_unknown_question_escalates_calls_and_becomes_knowledge` | AI не знает ответа → задача админу → T0 Telegram основному → 5 мин повтор основному+резервному → 10 мин звонок → «1» в IVR = ACK → резервного не зовут → решение в CRM с пересылкой клиенту → черновик знаний (AI его не использует) → владелец публикует → AI отвечает сам |
| `test_e2e_early_leave_approved_in_telegram_conflict_rescheduled` | Мастер просит ранний уход → карточка в Telegram с кнопками → админ подтверждает кнопкой → задача-конфликт, брони не отменены, слоты закрыты → админ переносит бронь к другому мастеру → клиенту одно уведомление |
| `test_e2e_telegram_outage_retries_with_backoff_and_delivers_once` | Telegram недоступен (502) → backoff → восстановление → ровно одна доставка каждому админу, без дублей при повторной обработке |

Регрессии найденных дефектов — `backend/tests/test_stage3_regressions.py` (см. также docs/LOAD_TEST.md).

## Ручная процедура AT-18 (и реальные провайдеры для AT-05 / AT-12)

Предусловия: стенд с публичным HTTPS (`PUBLIC_BASE_URL`), `VOICE_PROVIDER=twilio`, заполнены `TWILIO_*`
(или «Интеграции → Звонки»), у основного и резервного админа указаны реальные телефоны и флаги эскалации,
`TELEGRAM_MODE=live` с ботом, `WHATSAPP_MODE=live`. Тихие часы организации — вне времени теста
(или временно `settings.escalation.quiet_hours=[0,0]`). Двое проверяющих: «клиент» и «администратор».

1. **Запуск эскалации.** С телефона «клиента» в WhatsApp спросить то, чего нет в базе знаний
   («Сколько стоит окрашивание?»). Ожидается: клиенту — честный ответ «уточню у администратора», в панели —
   задача, в Telegram основного админа — карточка с кнопками.
2. **5 минут.** Не нажимать «Взять». Через 5 мин ±30 с — повтор основному и резервному.
3. **10 минут (AT-05).** Через 10 мин ±30 с звонит телефон основного админа. Проверить: голос читает текст на
   русском **без персональных данных клиента и текста вопроса**; слышно разборчиво; при нажатии «1» — фраза
   о закреплении задачи и отбой; в панели задача «взята» этим админом, в `calls` статус `answered`, `dtmf=1`.
   Повторить сценарий, нажав «2» — звонок резервному админу; повторить, не отвечая, — через `call_retry_min`
   звонок резервному, после `max_call_attempts` звонков больше нет.
4. **Язык и перебивания.** Нажать «1», не дослушав фразу (перебивание DTMF), — ввод принимается сразу.
   Повторить пп. 3–4 для админа с `ui_language=kk` (профиль команды): фразы IVR звучат по-казахски
   («…бірді басыңыз…»), оценить разборчивость голоса `tts_voice_kk`. Голосовое сообщение «клиента» с неоднозначной
   датой («запишите на выходных») → AI уточняет дату до записи (AT-12, реальный STT).
4a. **AI-звонок клиента (входящий).** Позвонить на номер организации (`Интеграции → Звонки → phone_number`).
   Проверить: приветствие раскрывает AI (и запись разговора, если включена); RU-сценарий «запишите на стрижку завтра
   в 16:00» → AI переспрашивает/подтверждает услугу, дату, время, имя → после «да» бронь в календаре и письменное
   подтверждение в WhatsApp (если клиент пишет в WhatsApp); перебить AI на середине фразы — AI замолкает и слушает
   (barge-in); KK-сценарий («Ертең сағат 16:00-де шаш қиюға жазылғым келеді») — распознаётся `kk-KZ`; дважды
   промолчать/пробормотать или сказать «оператор» / «адаммен сөйлескім келеді» — звонок переводится основному
   админу, при неответе — задача «Обратный звонок» и фраза «мы перезвоним». В панели «Звонки» — транскрипт,
   метрики, ссылки на бронь/задачу.
4b. **Исходящий звонок.** Клиенту с согласием `outbound_calls` нажать «Позвонить» (подтверждение записи) в рабочие
   часы → звонок с раскрытием AI и деталями записи; «да» → «ждём вас». Без согласия / вне часов / сверх лимита —
   понятная ошибка в панели, звонка нет.
5. **Перевод человеку.** После ACK ответить из Telegram (reply на карточку) → ответ доставлен клиенту,
   диалог вернулся AI; в журнале аудита есть `task.ack` (`via=voice`) и `task.resolve`.
6. **Записать результат**: дата, номера (маскированно), длительность дозвона, оценка слышимости RU/KZ,
   ID задач и звонков. Критерий приёмки: все пункты выполнены без ручного вмешательства в БД.
