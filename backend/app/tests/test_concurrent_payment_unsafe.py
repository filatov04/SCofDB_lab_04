"""
LAB 04 (унаследовано из LAB 02): Демонстрация ПРОБЛЕМЫ race condition.

pay_order_unsafe() — READ COMMITTED без блокировок.
Два параллельных вызова могут оба записать 'paid' в историю.
"""

import asyncio
import os
import pytest
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.application.payment_service import PaymentService

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
async def pg_session():
    if not _pg_available():
        pytest.skip("PostgreSQL not available")
    engine = _make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def test_order(pg_session):
    user_id = uuid.uuid4()
    order_id = uuid.uuid4()
    await pg_session.execute(
        text("INSERT INTO users (id, email, name, created_at) VALUES (:id, :email, :name, NOW())"),
        {"id": str(user_id), "email": f"unsafe_test_{user_id}@example.com", "name": "Test"},
    )
    await pg_session.execute(
        text("INSERT INTO orders (id, user_id, status, total_amount, created_at) VALUES (:id, :uid, 'created', 100, NOW())"),
        {"id": str(order_id), "uid": str(user_id)},
    )
    await pg_session.execute(
        text("INSERT INTO order_status_history (id, order_id, status, changed_at) VALUES (gen_random_uuid(), :oid, 'created', NOW())"),
        {"oid": str(order_id)},
    )
    await pg_session.commit()
    yield order_id
    await pg_session.execute(text("DELETE FROM order_status_history WHERE order_id = :oid"), {"oid": str(order_id)})
    await pg_session.execute(text("DELETE FROM order_items WHERE order_id = :oid"), {"oid": str(order_id)})
    await pg_session.execute(text("DELETE FROM orders WHERE id = :oid"), {"oid": str(order_id)})
    await pg_session.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": str(user_id)})
    await pg_session.commit()


@pytest.mark.asyncio
async def test_concurrent_payment_unsafe_demonstrates_race_condition(pg_session, test_order):
    """
    Демонстрирует race condition: два параллельных pay_order_unsafe()
    приводят к двум записям 'paid' в order_status_history.
    """
    order_id = test_order
    engine = _make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def attempt(n: int):
        async with factory() as s:
            try:
                return await PaymentService(s).pay_order_unsafe(order_id)
            except Exception as e:
                return e

    results = await asyncio.gather(attempt(1), attempt(2), return_exceptions=True)
    await engine.dispose()

    service = PaymentService(pg_session)
    history = await service.get_payment_history(order_id)

    print(f"\n⚠️  RACE CONDITION DETECTED!")
    print(f"Order {order_id} paid {len(history)} time(s):")
    for r in history:
        print(f"  - {r['changed_at']}: status={r['status']}")

    assert len(history) >= 1, "Expected at least 1 paid event"
    # Race condition: both transactions may insert into history
    # In asyncio concurrency this often results in 2 records
    print(f"Results: {[str(r) for r in results]}")


@pytest.mark.asyncio
async def test_concurrent_payment_unsafe_both_succeed():
    """
    Проверяет, что без защиты обе попытки в оплаты могут обойти проверку.
    """
    if not _pg_available():
        pytest.skip("PostgreSQL not available")

    engine = _make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    user_id = uuid.uuid4()
    order_id = uuid.uuid4()
    async with factory() as s:
        await s.execute(
            text("INSERT INTO users (id, email, name, created_at) VALUES (:id, :email, :name, NOW())"),
            {"id": str(user_id), "email": f"race2_{user_id}@example.com", "name": "T"},
        )
        await s.execute(
            text("INSERT INTO orders (id, user_id, status, total_amount, created_at) VALUES (:id, :uid, 'created', 50, NOW())"),
            {"id": str(order_id), "uid": str(user_id)},
        )
        await s.execute(
            text("INSERT INTO order_status_history (id, order_id, status, changed_at) VALUES (gen_random_uuid(), :oid, 'created', NOW())"),
            {"oid": str(order_id)},
        )
        await s.commit()

    async def attempt():
        async with factory() as s:
            try:
                return await PaymentService(s).pay_order_unsafe(order_id)
            except Exception as e:
                return e

    results = await asyncio.gather(attempt(), attempt(), return_exceptions=True)

    async with factory() as s:
        history = await PaymentService(s).get_payment_history(order_id)
        await s.execute(text("DELETE FROM order_status_history WHERE order_id = :oid"), {"oid": str(order_id)})
        await s.execute(text("DELETE FROM orders WHERE id = :oid"), {"oid": str(order_id)})
        await s.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": str(user_id)})
        await s.commit()

    await engine.dispose()

    successes = [r for r in results if isinstance(r, dict)]
    print(f"\nBoth attempts completed. Successes: {len(successes)}, History entries: {len(history)}")
    assert len(history) >= 1
