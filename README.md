# PUBG UC Spark Auto-Checker — плагин для FunPayCardinal

Плагин автоматизирует продажу **PUBG Mobile UC** на FunPay: отслеживает заказы
по нужному лоту, принимает от покупателя его **игровой UID**, начисляет UC из
склада **Spark** (`api.pubgredeemerbot.com`), отвечает покупателю и ведёт учёт с
защитой от повторной обработки.

**Флоу продажи:** покупатель оплачивает → бот **молчит** (никаких сообщений
после оплаты) → покупатель присылает свой **UID** → бот проверяет, что это UID
(цифры, 9–11 знаков) → шлёт запрос на пополнение в Spark (`stock-redeem`) →
сообщает результат.

> Статус: транспорт Spark реализован по OpenAPI. Работает в **mock-режиме**
> (`SPARK_MOCK=1`) — сетевых вызовов нет. Точный разбор ответа завершённого
> job'а подставляется в один файл (`pubg_uc_spark/spark/parser.py`), остальной
> код не меняется.

## Установка в FunPayCardinal

1. Скопируйте **в папку `plugins/` вашего FunPayCardinal**:
   - файл `pubg_uc_spark.py` (точка входа с метаданными и биндингами);
   - пакет `pubg_uc_spark/` (вся логика) — рядом с файлом.
   FPC добавляет `plugins/` в `sys.path`, поэтому `import pubg_uc_spark` находит пакет.
   (Файл-загрузчик на всякий случай добавляет в путь и `plugins/`, и корень FPC.)
2. Создайте `.env` из `.env.example` и заполните параметры.
3. Перезапустите FPC (`python main.py`, или через ваш PM2/systemd — плагин
   работает внутри существующего event loop FPC, свой polling не создаётся).

Плагин полностью изолирован: удаление папки `pubg_uc_spark/` и файла
`plugins/pubg_uc_spark.py` не ломает FPC.

## Архитектура

```
plugins/pubg_uc_spark.py     — точка входа FPC (метаданные + BIND_TO_*)
pubg_uc_spark/
  config.py                  — все параметры (.env), LOTS, тексты сообщений
  plugin.py                  — сборка графа объектов + обработчики событий FPC
  errors.py                  — таксономия ошибок (User / Temporary / Critical)
  funpay/orders.py           — разбор NEW_ORDER, резолв lot_id
  funpay/messenger.py        — отправка + анти-спам (send_once) + notify_admin
  spark/client.py            — SparkChecker.redeem() (job-API или mock)
  spark/parser.py            — Spark ответ → унифицированный статус (ЕДИНСТВЕННОЕ
                               место, знающее формат Spark)
  spark/models.py            — UnifiedStatus, SparkResult
  database/db.py             — SQLite (отдельный файл), схема + индексы
  database/models.py         — датаклассы + FSM (can_transition)
  database/repository.py     — идемпотентные записи, поиск, дедуп
  services/order_service.py  — FSM заказа + сообщения + применение результата
  services/code_service.py   — извлечение/валидация/дедуп UID
  services/retry_service.py  — воркер-поток + retry для временных ошибок
  services/admin_service.py  — админ-операции
  utils/logger.py            — уровни логов + маскирование UID/секретов
  utils/validators.py        — UID_PATTERN, извлечение UID, hash
```

### Поток обработки

```
NEW_ORDER → (дедуп события) → матч лота по описанию → фильтр по LOTS →
идемпотентное создание заказа → статус WAITING_FOR_CODE (МОЛЧА, без сообщения)

NEW_MESSAGE → (дедуп по message_id) → активный заказ покупателя? →
извлечение UID по UID_PATTERN → дедуп → CODE_RECEIVED → CHECKING →
retry_service (в отдельном потоке) → SparkChecker.redeem() → parser →
UnifiedStatus → apply_result: статус заказа + сообщение покупателю + лог
```

### Интеграция со Spark (`api.pubgredeemerbot.com`)

Spark — **асинхронный job-API**. `SparkChecker.redeem(player_id, picks)`:

```
POST /v1/jobs/stock-redeem  {"player_id": uid, "picks": {"60": 1}} → job_id
GET  /v1/jobs/{job_id}?wait=25  (long-poll, ≤60с) → ждём status=done/failed
result.results[0] → parser.parse_job() → UnifiedStatus → бизнес-логика
```

- Авторизация: заголовок `X-API-Key` (ключ из Telegram `@sparkucbot → API`).
- UID: цифры, **9–11** знаков (`UID_PATTERN`).
- `picks = {denomination: order_quantity}` — номинал лота из `LOTS`, количество из заказа FunPay.
- HTTP-маппинг: `429/5xx/сеть` → временная ошибка (retry); `401/403` → критическая; `404` → критическая.

Бизнес-логика зависит только от `UnifiedStatus`
(`VALID / INVALID / ACCOUNT_NOT_FOUND / ALREADY_USED / ERROR / UNKNOWN`).
Замена Spark или изменение формата ответа = правка только `parser.py`.

Формат ответа подтверждён на реальном API (probe):
- успех: `result.results[0].success == true`; имя игрока — `charac_name`;
- ошибки: код в `detail.error` (синхронно, напр. `INVALID_PLAYER_ID`) или в
  `err_code` строки результата (`INVALID_PLAYER_ID`/`PLAYER_NOT_FOUND` →
  ACCOUNT_NOT_FOUND, `OUT_OF_STOCK` → ERROR), иначе `success:false` → INVALID.
Всё это разбирается в `parser.parse_job()` — единственном месте формата Spark.

## Идемпотентность и защита от повторов

- `orders.funpay_order_id` — UNIQUE (заказ создаётся один раз).
- `codes (order_id, code_hash)` — UNIQUE (UID на заказ — один раз). Один и тот
  же UID на разных заказах разрешён (повторный покупатель).
- `processed_events` — таблица уже обработанных событий и уже отправленных
  сообщений (`order:<id>`, `msg:<message_id>`, `sent:<key>`).
- Успешно начисленный UID повторно не отправляется; окончательный негатив —
  тоже (пере-проверка только через админ-команду `/uc_recheck`).

## Состояния заказа (FSM)

`NEW → WAITING_FOR_CODE → CODE_RECEIVED → CHECKING →
VALID | INVALID | ACCOUNT_NOT_FOUND | ALREADY_USED | ERROR | TEMPORARY_ERROR`,
временная ошибка: `CHECKING → TEMPORARY_ERROR → CHECKING (retry)`.
Недопустимые переходы блокируются (`can_transition`), админ может форсировать.

**Неверный/несуществующий UID — в два захода:**
- формат неверный (не 9–11 цифр) → подсказка о формате (1 раз), Spark не вызывается;
- UID есть, но аккаунт не найден (**1-я ошибка**) → *«Ошибка UID! Проверьте…»*, ждём новый UID;
- аккаунт не найден **повторно (2-я ошибка)** → *«Повторная ошибка… Ожидайте ответ продавца!»*,
  заказ → `ERROR`, продавцу уведомление, бот больше **не** принимает UID автоматически
  (`ERROR` исключён из активных заказов покупателя — до действия админа).

## Восстановление после перезапуска

При старте (`Plugin.start`) вызывается `resume_unfinished()`: коды в статусах
`CHECKING`/`TEMPORARY_ERROR` заново ставятся в очередь проверки.

## Админ-команды (Telegram, whitelist `ADMIN_IDS`)

| Команда | Действие |
|---|---|
| `/uc_order <funpay_order_id>` | статус заказа и его коды |
| `/uc_code <code_id>` | статус кода |
| `/uc_history <funpay_order_id>` | история событий заказа |
| `/uc_recheck <code_id>` | повторная проверка (обходит final-негатив) |
| `/uc_cancel <code_id>` | отменить retry (пометить FAILED) |
| `/uc_setstatus <order_id> <STATUS>` | форс-смена статуса заказа |
| `/uc_resend <funpay_order_id>` | вручную попросить у покупателя UID |

## Безопасность

- Секреты только в `.env`, не в коде и не в логах.
- UID/коды в логах маскируются: `1234****8901`.

## Тесты

```bash
pip install -r requirements.txt
python -m pytest -q
```

Покрыты сценарии секции 19 ТЗ: новый заказ (молча), сообщение без UID,
некорректный UID, валидный UID, отказ начисления, ACCOUNT_NOT_FOUND, нет в
наличии, повторный UID, повторный event, timeout/500/недоступность Spark,
критическая ошибка, восстановление после перезапуска, два заказа, один
покупатель с несколькими заказами.

## Что нужно уточнить перед боевым запуском

1. ~~Реальный ответ Spark~~ — снят и зафиксирован в `parser.parse_job()`
   (успех `charac_name`/`success`, ошибки `detail.error`/`err_code`).
2. **Тексты сообщений** покупателю — черновики в `config.py`, нужно утвердить
   (успех и «ошибка UID» уже по вашим формулировкам).
3. **Матч лота по описанию.** FunPay в заказе не отдаёт id оффера, поэтому лот
   узнаётся по описанию: по умолчанию — номинал как отдельное число (`60`, но не
   `660`) + «uc». Проверьте, как выглядит `description` вашего заказа, и при
   необходимости задайте точные `keywords` в `LOTS` (напр. `["pubg", "60 uc"]`).
4. Включить реальный режим: `SPARK_MOCK=0` + `SPARK_API_KEY=<ключ>`
   (endpoint уже настроен: `SPARK_API_URL=https://api.pubgredeemerbot.com`).

Транспорт Spark (job-API `stock-redeem`, `X-API-Key`, UID 9–11 цифр) уже
реализован по OpenAPI-спецификации. Биндинги FPC, сигнатура `send_message`,
классы событий и поля заказа сверены с исходниками sidor0912/FunPayCardinal.
