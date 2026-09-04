# PUBG UC Spark Auto-Checker — плагин для FunPayCardinal

Плагин автоматизирует обработку заказов **PUBG Mobile UC (код пополнения)** на
FunPay: отслеживает заказы по нужному лоту, принимает код от покупателя,
проверяет его через **Spark** (`api.pubgredeemerbot.com`, HTTP REST), отвечает
покупателю и ведёт учёт кодов с защитой от повторной обработки.

> Статус: **каркас (Вариант B)**. Проверка Spark работает в **mock-режиме** —
> сетевые вызовы отключены. Реальный формат ответа Spark подставляется в один
> файл (`pubg_uc_spark/spark/parser.py`) + метод `_check_http` в `client.py`,
> остальной код не меняется.

## Установка в FunPayCardinal

1. Скопируйте в корень FPC:
   - файл `plugins/pubg_uc_spark.py` (точка входа с метаданными и биндингами);
   - пакет `pubg_uc_spark/` (вся логика).
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
  spark/client.py            — SparkChecker.check_code() (HTTP или mock)
  spark/parser.py            — Spark ответ → унифицированный статус (ЕДИНСТВЕННОЕ
                               место, знающее формат Spark)
  spark/models.py            — UnifiedStatus, SparkResult
  database/db.py             — SQLite (отдельный файл), схема + индексы
  database/models.py         — датаклассы + FSM (can_transition)
  database/repository.py     — идемпотентные записи, поиск, дедуп
  services/order_service.py  — FSM заказа + сообщения + применение результата
  services/code_service.py   — извлечение/валидация/дедуп кода
  services/retry_service.py  — воркер-поток + retry для временных ошибок
  services/admin_service.py  — админ-операции
  utils/logger.py            — уровни логов + маскирование кода/секретов
  utils/validators.py        — CODE_PATTERN, извлечение кода, hash
```

### Поток обработки

```
NEW_ORDER → (дедуп события) → резолв lot_id → фильтр по LOTS →
идемпотентное создание заказа → статус WAITING_FOR_CODE → запрос кода

NEW_MESSAGE → (дедуп по message_id) → активный заказ покупателя? →
извлечение по CODE_PATTERN → дедуп кода → CODE_RECEIVED → CHECKING →
retry_service (в отдельном потоке) → SparkChecker → parser → UnifiedStatus →
apply_result: статус заказа/кода + сообщение покупателю + лог
```

### Интеграция со Spark (`api.pubgredeemerbot.com`)

Spark — **асинхронный job-API**. `SparkChecker.check_code()`:

```
POST /v1/jobs/check-code  {"codes": ["<code>"]}   → job_id
GET  /v1/jobs/{job_id}?wait=25  (long-poll, ≤60с) → ждём status=done/failed
result.results[0] → parser.parse_job() → UnifiedStatus → бизнес-логика
```

- Авторизация: заголовок `X-API-Key` (ключ из Telegram `@sparkucbot → API`).
- Код: ровно **18** символов `[A-Za-z0-9]`, шлётся массивом `codes`.
- HTTP-маппинг: `429/5xx/сеть` → временная ошибка (retry); `401/403` → критическая; `404` → критическая.

Бизнес-логика зависит только от `UnifiedStatus`
(`VALID / INVALID / ACCOUNT_NOT_FOUND / ALREADY_USED / ERROR / UNKNOWN`).
Замена Spark или изменение формата ответа = правка только `parser.py`.

> ⚠️ **Одно место требует реального примера.** В OpenAPI-схеме Spark тело
> завершённого job'а описано пустым объектом, поэтому точные поля per-code
> результата (какое поле = «валиден» / «уже использован» / «аккаунт не найден»)
> пока не подтверждены. `parser.parse_job()` сделан устойчивым: сперва читает
> булев флаг (`valid`/`is_valid`/`redeemable`), иначе матчит по ключевым словам
> в текстовых полях. Пришлите один реальный JSON завершённого job'а — маппинг
> зафиксируется точно, без изменений в остальном коде.

## Идемпотентность и защита от повторов

- `orders.funpay_order_id` — UNIQUE (заказ создаётся один раз).
- `codes (order_id, code_hash)` — UNIQUE (код на заказ — один раз).
- `processed_events` — таблица уже обработанных событий и уже отправленных
  сообщений (`order:<id>`, `msg:<message_id>`, `sent:<key>`).
- Успешно проверенный код повторно не проверяется; окончательно невалидный —
  тоже (только через админ-команду `/uc_recheck`).

## Состояния заказа (FSM)

`NEW → WAITING_FOR_CODE → CODE_RECEIVED → CHECKING →
VALID | INVALID | ACCOUNT_NOT_FOUND | ALREADY_USED | ERROR | TEMPORARY_ERROR`,
временная ошибка: `CHECKING → TEMPORARY_ERROR → CHECKING (retry)`.
Недопустимые переходы блокируются (`can_transition`), админ может форсировать.

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
| `/uc_resend <funpay_order_id>` | повторно отправить запрос кода |

## Безопасность

- Секреты только в `.env`, не в коде и не в логах.
- Коды в логах маскируются: `ABCD****1234`.

## Тесты

```bash
pip install -r requirements.txt
python -m pytest -q
```

Покрыты сценарии секции 19 ТЗ: новый заказ, заказ без кода, валид/невалид,
ACCOUNT_NOT_FOUND, уже использованный код, повторное сообщение, повторный
event, timeout/500/недоступность Spark, критическая ошибка, восстановление
после перезапуска, два заказа, один покупатель с несколькими заказами.

## Что нужно уточнить перед боевым запуском

1. **Реальный JSON завершённого job'а** Spark (`GET /v1/jobs/{id}` после `done`)
   для валидного и для невалидного/использованного кода → зафиксировать маппинг
   в `parser.parse_job()`.
2. **Тексты сообщений** покупателю (сейчас — заглушки в `config.py`).
3. **`resolve_lot_id`** — сверить с версией FunPayAPI в вашем FPC (способ
   получить offer id из заказа).
4. Включить реальную проверку: `SPARK_MOCK=0` + `SPARK_API_KEY=<ключ>`
   (endpoint уже настроен: `SPARK_API_URL=https://api.pubgredeemerbot.com`).

Транспорт Spark (job-API, `X-API-Key`, формат кода 18 символов) уже реализован
по OpenAPI-спецификации.
