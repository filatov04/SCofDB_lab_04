# Отчёт по лабораторной работе №4
## Идемпотентность платежных запросов в FastAPI

**Студент:** Филатов Илья  
**Группа:** БПМ-22-ПО-2  
**Дата:** 14.03.2026

---

## 1. Постановка сценария

### Описание проблемы

В любой распределённой системе возможна ситуация, когда:

1. Клиент отправил запрос на оплату (`POST /api/payments/retry-demo`).
2. Сервер успешно обработал запрос: заказ перешёл в статус `paid`, запись в `order_status_history` создана.
3. **Сеть оборвалась** — клиент не получил HTTP-ответ.
4. Клиент не знает: оплата прошла или нет?
5. Клиент **повторяет тот же запрос**.

### Что происходит без защиты

Без `Idempotency-Key` сервер воспринимает повторный запрос как **новый**:
- Если повтор пришёл после успешной оплаты — сервер возвращает ошибку `"Order already paid"`.
- Клиент получает `success=false` и не понимает: деньги были списаны при первом запросе или нет?
- Результат: **неопределённость на стороне клиента**. В худшем случае — ручное вмешательство службы поддержки.

В ещё более опасном сценарии (без проверки `status='paid'` в `pay_order_unsafe`) **двойная оплата возможна физически** — два параллельных запроса могут оба пройти проверку статуса `created` до коммита друг друга.

---

## 2. Реализация таблицы `idempotency_keys`

### DDL (`backend/migrations/002_idempotency_keys.sql`)

```sql
CREATE TABLE idempotency_keys (
    id               UUID         PRIMARY KEY DEFAULT uuid_generate_v4(),
    idempotency_key  VARCHAR(255) NOT NULL,
    request_method   VARCHAR(16)  NOT NULL,
    request_path     TEXT         NOT NULL,
    request_hash     TEXT         NOT NULL,  -- sha256 от тела запроса
    status           VARCHAR(32)  NOT NULL DEFAULT 'processing',
    status_code      INTEGER,
    response_body    JSONB,
    created_at       TIMESTAMP    NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMP    NOT NULL DEFAULT NOW(),
    expires_at       TIMESTAMP    NOT NULL,

    CONSTRAINT idempotency_status_check
        CHECK (status IN ('processing', 'completed', 'failed')),
    CONSTRAINT idempotency_unique
        UNIQUE (idempotency_key, request_method, request_path)
);

CREATE INDEX idx_idempotency_expires_at ON idempotency_keys (expires_at);
CREATE INDEX idx_idempotency_lookup ON idempotency_keys (idempotency_key, request_method, request_path);
```

### Объяснение колонок

| Колонка | Назначение |
|---------|-----------|
| `idempotency_key` | Уникальный ключ, присланный клиентом в заголовке `Idempotency-Key` |
| `request_method` + `request_path` | Идентифицируют endpoint; один ключ может использоваться только для одного endpoint |
| `request_hash` | SHA-256 от тела запроса; при повторе с другим payload → `409 Conflict` |
| `status` | Жизненный цикл: `processing` → `completed` \| `failed` |
| `status_code` + `response_body` | Кэш HTTP-ответа для повторного возврата |
| `expires_at` | TTL (по умолчанию 24 часа); устаревшие ключи можно удалять |

### Защита от дубликатов

`UNIQUE (idempotency_key, request_method, request_path)` + `INSERT ... ON CONFLICT DO NOTHING` гарантируют, что при конкурентных одинаковых запросах только первый создаёт запись.

---

## 3. Реализация middleware

### Алгоритм (файл `backend/app/middleware/idempotency_middleware.py`)

```
dispatch(request):
  1. Пропустить, если: не POST / не платёжный endpoint / SQLite-режим (тесты).
  2. Считать заголовок Idempotency-Key. Если отсутствует → call_next(request).
  3. Вычислить request_hash = SHA-256(тело запроса).
  4. Открыть сессию к БД:
     a) Найти запись по (idempotency_key, method, path).
     b) Если найдена:
        - hash != request_hash      → вернуть 409 Conflict
        - status == 'completed'     → вернуть кэш + X-Idempotency-Replayed: true
        - status == 'processing'    → продолжить (конкурентный запрос)
     c) Если не найдена:
        → INSERT ... status='processing' ON CONFLICT DO NOTHING
  5. Выполнить downstream request: response = call_next(request).
  6. Прочитать тело response (streaming → bytes).
  7. UPDATE idempotency_keys SET status='completed', status_code=..., response_body=...
  8. Вернуть response клиенту + заголовок X-Idempotency-Replayed: false.
```

### Ключевые решения

- **Буферизация тела запроса**: `raw_body = await request.body()` → `request._receive = _receive` позволяет downstream-хендлеру повторно читать тело.
- **SQLite bypass**: `if _is_sqlite: return await call_next(request)` — тесты на SQLite не затрагивают таблицу `idempotency_keys`.
- **Concurrent safety**: `INSERT ON CONFLICT DO NOTHING` + повторный SELECT устраняют гонку при двух одновременных запросах с одним ключом.

---

## 4. Демонстрация без защиты

### Запуск

```bash
docker compose exec -T backend pytest app/tests/test_retry_without_idempotency.py -v -s
```

### Результат

```
RETRY WITHOUT IDEMPOTENCY KEY
============================================================
1st request: status=200  body={'success': True, 'message': 'Retry demo payment succeeded (unsafe)', ...}
2nd request: status=200  body={'success': False, 'message': 'Order ... is already paid', ...}

PROBLEM: same intention → different responses!
  Client cannot determine if payment was processed.
```

### Вывод

- Первый запрос: `success=True` — оплата прошла.
- Повторный запрос: `success=False` — бизнес-ошибка.
- Клиент получает **разные ответы** на одно и то же намерение.
- **Проблема UX**: клиент не может безопасно интерпретировать ошибку. Деньги уже списаны, но ответ выглядит как отказ.

---

## 5. Демонстрация с Idempotency-Key

### Запуск

```bash
docker compose exec -T backend pytest app/tests/test_retry_with_idempotency_key.py -v -s
```

### Результат

```
RETRY WITH IDEMPOTENCY KEY
============================================================
Idempotency-Key: lab04-test-636ef737-...
1st request: status=200  replayed=false  body={'success': True, ...}
2nd request: status=200  replayed=true   body={'success': True, ...}
Paid events in history: 1
Idempotency record: status=completed, code=200
```

### Проверка

| Критерий | Результат |
|---------|----------|
| Второй ответ пришёл из кэша | ✅ `X-Idempotency-Replayed: true` |
| Повторного списания не было | ✅ 1 запись `paid` в `order_status_history` |
| Запись в `idempotency_keys` | ✅ `status=completed`, `status_code=200`, `response_body` заполнен |

---

## 6. Негативный сценарий

### Тест

```python
# Один ключ + разный payload (другой order_id)
resp_conflict = await client.post("/api/payments/retry-demo",
    json={"order_id": str(uuid.uuid4()), "mode": "unsafe"},
    headers={"Idempotency-Key": same_key}
)
```

### Результат

```
SAME KEY + DIFFERENT PAYLOAD → CONFLICT
============================================================
1st request (payload_1): status=200
2nd request (payload_2): status=409  body={'detail': 'Idempotency key already used with a different payload'}
```

`409 Conflict` возвращается при попытке повторного использования ключа с другим телом запроса. Это предотвращает случайную замену одной операции другой при одном ключе.

---

## 7. Сравнение с решением из ЛР2 (FOR UPDATE)

### Тест сравнения

```bash
docker compose exec -T backend pytest app/tests/test_compare_idempotency_vs_for_update.py -v -s
```

### Результат

```
COMPARISON: FOR UPDATE  vs  IDEMPOTENCY KEY
=================================================================
Scenario A (FOR UPDATE, no idempotency key):
  1st call  → success=True  (Retry demo payment succeeded (for_update))
  Retry     → success=False  (Order ... is already paid)
  Client UX: ERROR on retry — payment state is AMBIGUOUS

Scenario B (unsafe + Idempotency-Key):
  1st call  → success=True  (Retry demo payment succeeded (unsafe))
  Retry     → success=True  replayed=true
  Client UX: Same success — payment state is CLEAR

CONCLUSION:
  FOR UPDATE   → DB-level protection against race conditions.
  Idempotency  → API-level protection against retries after network loss.
  Use BOTH together in production for full payment safety.
```

### Таблица сравнения

| Критерий | FOR UPDATE (ЛР2) | Idempotency-Key (ЛР4) |
|---------|-----------------|----------------------|
| **Цель** | Предотвратить гонку двух параллельных запросов в БД | Сделать повторный запрос безопасным |
| **Уровень защиты** | БД (строка заблокирована `REPEATABLE READ`) | API-контракт (кэш ответа) |
| **Что происходит при повторе** | `OrderAlreadyPaidError` → `success=False` | Кэшированный `success=True` |
| **UX при сетевом сбое** | Клиент получает ошибку, состояние неизвестно | Клиент получает тот же успешный ответ |
| **Защищает от двойного списания** | ✅ Да | ✅ Да (вторичный вызов не доходит до бизнес-логики) |
| **Требует состояния на сервере** | Нет (БД) | Да (таблица `idempotency_keys`) |
| **Срок хранения гарантии** | Вечно (статус в БД) | TTL (по умолчанию 24 ч) |

### Нужно ли использовать оба механизма вместе?

**Да.** Они решают разные проблемы:

- `FOR UPDATE` защищает от того, что **два разных** клиентских запроса (от двух параллельных сессий) одновременно попадут в бизнес-логику оплаты.
- `Idempotency-Key` защищает от того, что **один и тот же** клиентский запрос будет обработан дважды из-за сетевой ошибки или таймаута.

Production-система нуждается в обоих: `Idempotency-Key` не помогает при гонке двух разных пользователей, а `FOR UPDATE` не помогает при сетевом сбое на стороне клиента.

---

## 8. Выводы

1. **Идемпотентность — API-контракт, а не только защита от ошибок.** Клиент вправе ожидать, что повторный запрос с тем же `Idempotency-Key` вернёт тот же ответ. Это снимает обязанность с клиента различать «оплата прошла» и «оплата отклонена».

2. **`FOR UPDATE` и `Idempotency-Key` — взаимодополняющие механизмы.** Первый — защита на уровне БД от race condition; второй — защита на уровне API от дублирующих запросов. В production оба должны работать одновременно.

3. **SHA-256 хэш тела защищает от случайного reuse ключа.** При попытке использовать один ключ для разных операций сервер возвращает `409 Conflict` — это явное указание клиенту, что ключ уже занят.

4. **TTL для ключей идемпотентности критичен.** Бесконечное хранение кэша нерационально. На практике TTL 24 часа покрывает все разумные retry-сценарии, после чего ключ считается устаревшим.

5. **Middleware прозрачна для бизнес-логики.** Endpoint-хендлеры не знают о существовании механизма идемпотентности — это принцип разделения ответственностей. Бизнес-логика (`PaymentService`) остаётся чистой и тестируемой независимо.

---

## Итоги тестирования

| Файл тестов | Результат |
|-------------|-----------|
| `test_domain.py` | ✅ 24 passed |
| `test_concurrent_payment_unsafe.py` | ✅ 2 passed |
| `test_concurrent_payment_safe.py` | ✅ 3 passed |
| `test_retry_without_idempotency.py` | ✅ 1 passed |
| `test_retry_with_idempotency_key.py` | ✅ 2 passed |
| `test_compare_idempotency_vs_for_update.py` | ✅ 1 passed |
| **Итого** | **✅ 37 passed** |
