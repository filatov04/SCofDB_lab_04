"""
LAB 04: Демонстрация проблемы retry БЕЗ идемпотентности.

Сценарий:
1) Клиент отправил запрос на оплату.
2) Сеть оборвалась — клиент не получил ответ.
3) Клиент повторил тот же запрос БЕЗ Idempotency-Key.

Результат без защиты:
- Первый вызов: оплата прошла успешно (success=True).
- Повторный вызов: ошибка "already paid" (success=False).
- Клиент получает РАЗНЫЕ ответы на ОДНО и ТО ЖЕ намерение.
- Клиент не может понять: деньги списались или нет?
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
    """Создать заказ в PostgreSQL, вернуть order_id, убрать после теста."""
    if not _pg_available():
        pytest.skip("PostgreSQL not available")

    engine = _make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    user_id = uuid.uuid4()
    order_id = uuid.uuid4()

    async with factory() as s:
        await s.execute(
            text("INSERT INTO users (id, email, name, created_at) VALUES (:id, :email, :name, NOW())"),
            {"id": str(user_id), "email": f"nokey_{user_id}@example.com", "name": "NoKey"},
        )
        await s.execute(
            text("INSERT INTO orders (id, user_id, status, total_amount, created_at) VALUES (:id, :uid, 'created', 200, NOW())"),
            {"id": str(order_id), "uid": str(user_id)},
        )
        await s.execute(
            text("INSERT INTO order_status_history (id, order_id, status, changed_at) VALUES (gen_random_uuid(), :oid, 'created', NOW())"),
            {"oid": str(order_id)},
        )
        await s.commit()

    yield order_id

    async with factory() as s:
        await s.execute(text("DELETE FROM order_status_history WHERE order_id = :oid"), {"oid": str(order_id)})
        await s.execute(text("DELETE FROM orders WHERE id = :oid"), {"oid": str(order_id)})
        await s.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": str(user_id)})
        await s.commit()

    await engine.dispose()


@pytest.mark.asyncio
async def test_retry_without_idempotency_can_double_pay(pg_order):
    """
    Без Idempotency-Key повторный запрос возвращает ДРУГОЙ ответ.

    Первый вызов: success=True (оплата прошла).
    Повторный вызов: success=False ("already paid").

    Проблема UX: клиент не может безопасно интерпретировать ошибку повтора —
    он уже не знает, были ли деньги списаны при первом запросе.
    """
    from app.main import app

    order_id = pg_order
    payload = {"order_id": str(order_id), "mode": "unsafe"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp1 = await client.post("/api/payments/retry-demo", json=payload)
        resp2 = await client.post("/api/payments/retry-demo", json=payload)

    data1 = resp1.json()
    data2 = resp2.json()

    print("\n" + "=" * 60)
    print("RETRY WITHOUT IDEMPOTENCY KEY")
    print("=" * 60)
    print(f"1st request: status={resp1.status_code}  body={data1}")
    print(f"2nd request: status={resp2.status_code}  body={data2}")
    print()
    print("PROBLEM: same intention → different responses!")
    print("  Client cannot determine if payment was processed.")
    print("=" * 60)

    # First call succeeds
    assert resp1.status_code == 200
    assert data1["success"] is True, "First payment must succeed"

    # Second call reports error (order already paid)
    assert data2["success"] is False, "Second call must report order-already-paid error"

    # Responses differ — this IS the problem
    assert data1 != data2, "Without idempotency, retry produces different response"
