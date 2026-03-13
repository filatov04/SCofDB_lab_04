"""
LAB 04: Демонстрация идемпотентного повтора запроса.

С заголовком Idempotency-Key:
- Первый вызов: обрабатывается нормально, ответ сохраняется в кэш.
- Повторный вызов с тем же ключом: возвращается КЭШИРОВАННЫЙ ответ.
- Повторное списание НЕ происходит.
- Клиент получает тот же ответ, что и в первый раз.
"""

import os
import pytest
import uuid

from httpx import AsyncClient, ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

_DB_URL = os.getenv("DATABASE_URL", "")


def _pg_available() -> bool:
    try:
        import asyncpg  # noqa
        return _DB_URL.startswith("postgresql")
    except ImportError:
        return False


def _make_engine():
    return create_async_engine(_DB_URL, echo=False)


@pytest.fixture
async def pg_order():
    if not _pg_available():
        pytest.skip("PostgreSQL not available")

    engine = _make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    user_id = uuid.uuid4()
    order_id = uuid.uuid4()

    async with factory() as s:
        await s.execute(
            text("INSERT INTO users (id, email, name, created_at) VALUES (:id, :email, :name, NOW())"),
            {"id": str(user_id), "email": f"idem_{user_id}@example.com", "name": "IdemUser"},
        )
        await s.execute(
            text("INSERT INTO orders (id, user_id, status, total_amount, created_at) VALUES (:id, :uid, 'created', 300, NOW())"),
            {"id": str(order_id), "uid": str(user_id)},
        )
        await s.execute(
            text("INSERT INTO order_status_history (id, order_id, status, changed_at) VALUES (gen_random_uuid(), :oid, 'created', NOW())"),
            {"oid": str(order_id)},
        )
        await s.commit()

    yield order_id, engine, factory, user_id

    async with factory() as s:
        await s.execute(text("DELETE FROM idempotency_keys WHERE idempotency_key LIKE 'lab04-test-%'"))
        await s.execute(text("DELETE FROM order_status_history WHERE order_id = :oid"), {"oid": str(order_id)})
        await s.execute(text("DELETE FROM orders WHERE id = :oid"), {"oid": str(order_id)})
        await s.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": str(user_id)})
        await s.commit()

    await engine.dispose()


@pytest.mark.asyncio
async def test_retry_with_same_key_returns_cached_response(pg_order):
    """
    Повторный запрос с тем же Idempotency-Key возвращает кэшированный ответ.

    Проверяем:
    1. Второй ответ идентичен первому (тот же success, message).
    2. Заголовок X-Idempotency-Replayed=true на втором ответе.
    3. В order_status_history только 1 запись 'paid' (нет двойного списания).
    4. В idempotency_keys есть completed-запись с response_body.
    """
    from app.main import app

    order_id, engine, factory, _ = pg_order
    idem_key = f"lab04-test-{uuid.uuid4()}"
    payload = {"order_id": str(order_id), "mode": "unsafe"}
    headers = {"Idempotency-Key": idem_key}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp1 = await client.post("/api/payments/retry-demo", json=payload, headers=headers)
        resp2 = await client.post("/api/payments/retry-demo", json=payload, headers=headers)

    data1 = resp1.json()
    data2 = resp2.json()

    print("\n" + "=" * 60)
    print("RETRY WITH IDEMPOTENCY KEY")
    print("=" * 60)
    print(f"Idempotency-Key: {idem_key}")
    print(f"1st request: status={resp1.status_code}  replayed={resp1.headers.get('X-Idempotency-Replayed')}  body={data1}")
    print(f"2nd request: status={resp2.status_code}  replayed={resp2.headers.get('X-Idempotency-Replayed')}  body={data2}")

    # First call succeeds normally
    assert resp1.status_code == 200
    assert data1["success"] is True

    # Second call returns cached response
    assert resp2.status_code == 200, f"Expected 200, got {resp2.status_code}"
    assert data2["success"] is True, "Cached response must also be success"
    assert resp2.headers.get("X-Idempotency-Replayed") == "true", \
        "Second response must carry X-Idempotency-Replayed: true"

    # Only one payment recorded in history
    async with factory() as s:
        result = await s.execute(
            text("SELECT COUNT(*) AS cnt FROM order_status_history WHERE order_id = :oid AND status = 'paid'"),
            {"oid": str(order_id)},
        )
        paid_count = result.fetchone().cnt

    print(f"Paid events in history: {paid_count}")
    assert paid_count == 1, f"Expected 1 paid event, got {paid_count} (double payment!)"

    # Idempotency key record exists and is completed
    async with factory() as s:
        result = await s.execute(
            text("SELECT status, status_code, response_body FROM idempotency_keys WHERE idempotency_key = :key"),
            {"key": idem_key},
        )
        idem_row = result.fetchone()

    assert idem_row is not None, "Idempotency key record must exist in DB"
    assert idem_row.status == "completed"
    assert idem_row.status_code == 200
    assert idem_row.response_body is not None

    print(f"Idempotency record: status={idem_row.status}, code={idem_row.status_code}")
    print("=" * 60)


@pytest.mark.asyncio
async def test_same_key_different_payload_returns_conflict(pg_order):
    """
    Один и тот же Idempotency-Key с другим payload → 409 Conflict.

    Предотвращает случайное переиспользование ключа для разных операций.
    """
    from app.main import app

    order_id, engine, factory, _ = pg_order
    idem_key = f"lab04-test-{uuid.uuid4()}"

    payload_1 = {"order_id": str(order_id), "mode": "unsafe"}
    payload_2 = {"order_id": str(uuid.uuid4()), "mode": "unsafe"}  # different order_id!
    headers = {"Idempotency-Key": idem_key}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp1 = await client.post("/api/payments/retry-demo", json=payload_1, headers=headers)
        resp_conflict = await client.post("/api/payments/retry-demo", json=payload_2, headers=headers)

    print("\n" + "=" * 60)
    print("SAME KEY + DIFFERENT PAYLOAD → CONFLICT")
    print("=" * 60)
    print(f"1st request (payload_1): status={resp1.status_code}")
    print(f"2nd request (payload_2): status={resp_conflict.status_code}  body={resp_conflict.json()}")
    print("=" * 60)

    assert resp1.status_code == 200, "First request must succeed"
    assert resp_conflict.status_code == 409, \
        f"Expected 409 Conflict for same key + different payload, got {resp_conflict.status_code}"
