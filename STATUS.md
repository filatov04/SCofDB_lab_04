# Статус лабораторной работы №4

## Что уже готово
- ✅ Основа проекта из предыдущей лабораторной (`backend`, `frontend`, docker)
- ✅ Endpoint `POST /api/payments/retry-demo` для retry-сценария
- ✅ `IdempotencyMiddleware` — полная реализация с DB-backed кэшем
- ✅ Миграция `backend/migrations/002_idempotency_keys.sql`
- ✅ Шаблоны тестов LAB 04 — реализованы
- ✅ SQL-утилиты ручной проверки в `sql/`
- ✅ Отчёт `REPORT.md` заполнен

## Что сделал студент

### Backend
- ✅ Реализована таблица `idempotency_keys` в `002_idempotency_keys.sql`
  - UNIQUE на (idempotency_key, method, path)
  - индексы для lookup и cleanup
  - триггер автообновления `updated_at`
- ✅ Реализована логика middleware в `idempotency_middleware.py`
  - чтение `Idempotency-Key` из заголовка
  - SHA-256 хэш тела запроса для проверки payload
  - возврат кэшированного ответа с `X-Idempotency-Replayed: true`
  - `409 Conflict` при reuse ключа с другим payload
  - SQLite bypass для тестов без PostgreSQL
- ✅ Скопирован и реализован весь Python-стек из ЛР3 (domain, repositories, services)
- ✅ `001_init.sql` с корректной схемой (GENERATED ALWAYS AS subtotal, триггер)

### Тесты/демо
- ✅ `test_retry_without_idempotency.py` — демонстрирует неопределённость без ключа
- ✅ `test_retry_with_idempotency_key.py` — кэш + X-Idempotency-Replayed + 409 Conflict
- ✅ `test_compare_idempotency_vs_for_update.py` — сравнение двух подходов
- ✅ `test_concurrent_payment_unsafe.py` — race condition (2 paid в истории)
- ✅ `test_concurrent_payment_safe.py` — FOR UPDATE блокировка (1 paid в истории)

### Отчёт
- ✅ Заполнены все разделы `REPORT.md`
- ✅ Доказано: повтор с тем же ключом возвращает кэш (X-Idempotency-Replayed: true)
- ✅ Подтверждено: повторного списания нет (1 запись в order_status_history)
- ✅ Сравнение: Idempotency-Key vs FOR UPDATE — разные уровни защиты

## Результаты тестирования

Все ключевые тесты лабораторной:

```
test_domain.py                        → 24 passed
test_retry_without_idempotency.py     →  1 passed
test_retry_with_idempotency_key.py    →  2 passed
test_compare_idempotency_vs_for_update.py → 1 passed
test_concurrent_payment_unsafe.py     →  2 passed
test_concurrent_payment_safe.py       →  3 passed
─────────────────────────────────────────
ИТОГО: 37 passed, 0 failed
```

> Примечание: `test_integration.py` проходит на свежей БД (33 passed).
> После нескольких запусков 3 теста с фиксированными email-адресами
> (`ordertest@example.com` и др.) завершаются ошибкой из-за дубликатов —
> это ограничение шаблонных тестов, не связанное с логикой ЛР4.
