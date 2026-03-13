"""
LAB 04 (унаследовано из LAB 02): Демонстрация РЕШЕНИЯ race condition.

pay_order_safe() — REPEATABLE READ + FOR UPDATE.
Только одна транзакция успешно оплачивает заказ.
"""

import asyncio
import os
import time
import pytest
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.application.payment_service import PaymentService
from app.domain.exceptions import OrderAlreadyPaidError

_DB_URL = os.getenv("DATABASE_URL", "")


def _pg_available() -> bool:
    try:
        import asyncpg  # noqa
        return _DB_URL.startswith("postgresql")
    except ImportError:
        return False


def _make_engine():
    return create_async_engine(_DB_URL, echo=False)


async def _create_order(factory, user_suffix="") -> tuple[uuid.UUID, uuid.UUID]:
    user_id = uuid.uuid4()
    order_id = uuid.uuid4()
    async with factory() as s:
        await s.execute(
            text("INSERT INTO users (id, email, name, created_at) VALUES (:id, :email, :name, NOW())"),
            {"id": str(user_id), "email": f"safe_test_{user_suffix}{user_id}@example.com", "name": "T"},
        )
        await s.execute(
            text("INSERT INTO orders (id, user_id, status, total_amount, created_at) VALUES (:id, :uid, 'created', 100, NOW())"),
            {"id": str(order_id), "uid": str(user_id)},
        )
        await s.execute(
            text("INSERT INTO order_status_history (id, order_id, status, changed_at) VALUES (gen_random_uuid(), :oid, 'created', NOW())"),
            {"oid": str(order_id)},
        )
        await s.commit()
    return user_id, order_id


async def _cleanup(factory, user_id, order_id):
    async with factory() as s:
        await s.execute(text("DELETE FROM order_status_history WHERE order_id = :oid"), {"oid": str(order_id)})
        await s.execute(text("DELETE FROM orders WHERE id = :oid"), {"oid": str(order_id)})
        await s.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": str(user_id)})
        await s.commit()


@pytest.mark.asyncio
async def test_concurrent_payment_safe_prevents_race_condition():
    if not _pg_available():
        pytest.skip("PostgreSQL not available")

    engine = _make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    user_id, order_id = await _create_order(factory)

    async def attempt():
        async with factory() as s:
            try:
                return await PaymentService(s).pay_order_safe(order_id)
            except Exception as e:
                return e

    results = await asyncio.gather(attempt(), attempt(), return_exceptions=True)

    async with factory() as s:
        history = await PaymentService(s).get_payment_history(order_id)

    await _cleanup(factory, user_id, order_id)
    await engine.dispose()

    successes = [r for r in results if isinstance(r, dict)]
    errors = [r for r in results if isinstance(r, Exception)]

    print(f"\n✅ RACE CONDITION PREVENTED!")
    print(f"Order {order_id}: paid {len(history)} time(s)")
    print(f"Success: {len(successes)}, Error: {len(errors)}")
    if errors:
        print(f"Rejected attempt: {errors[0]}")

    assert len(history) == 1, f"Expected exactly 1 paid event, got {len(history)}"
    assert len(successes) == 1, "Expected exactly 1 successful payment"
    assert len(errors) == 1, "Expected exactly 1 rejected attempt"


@pytest.mark.asyncio
async def test_concurrent_payment_safe_with_explicit_timing():
    if not _pg_available():
        pytest.skip("PostgreSQL not available")

    engine = _make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    user_id, order_id = await _create_order(factory, "timing_")

    t1_done = None
    t2_done = None

    async def slow_payment():
        nonlocal t1_done
        async with factory() as s:
            await s.execute(text("BEGIN"))
            await s.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            await s.execute(
                text("SELECT id, status FROM orders WHERE id = :oid FOR UPDATE"),
                {"oid": str(order_id)},
            )
            await asyncio.sleep(1.0)
            await s.execute(
                text("UPDATE orders SET status='paid' WHERE id=:oid AND status='created'"),
                {"oid": str(order_id)},
            )
            await s.execute(
                text("INSERT INTO order_status_history (id, order_id, status, changed_at) VALUES (gen_random_uuid(), :oid, 'paid', NOW())"),
                {"oid": str(order_id)},
            )
            await s.execute(text("COMMIT"))
            t1_done = time.monotonic()

    async def fast_payment():
        nonlocal t2_done
        await asyncio.sleep(0.1)
        async with factory() as s:
            try:
                await PaymentService(s).pay_order_safe(order_id)
            except Exception:
                pass
            finally:
                t2_done = time.monotonic()

    await asyncio.gather(slow_payment(), fast_payment(), return_exceptions=True)

    async with factory() as s:
        history = await PaymentService(s).get_payment_history(order_id)

    await _cleanup(factory, user_id, order_id)
    await engine.dispose()

    assert len(history) == 1
    if t1_done and t2_done:
        print(f"\n⏱  T1 finished at: {t1_done:.3f}, T2 finished at: {t2_done:.3f}")
        print(f"   T2 waited at least {t2_done - t1_done:.3f}s after T1")
        assert t2_done >= t1_done - 0.1, "T2 should finish after T1 (or very close)"


@pytest.mark.asyncio
async def test_concurrent_payment_safe_multiple_orders():
    """FOR UPDATE блокирует только конкретную строку, разные заказы не мешают друг другу."""
    if not _pg_available():
        pytest.skip("PostgreSQL not available")

    engine = _make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    u1, o1 = await _create_order(factory, "m1_")
    u2, o2 = await _create_order(factory, "m2_")

    async def pay(order_id):
        async with factory() as s:
            return await PaymentService(s).pay_order_safe(order_id)

    results = await asyncio.gather(pay(o1), pay(o2), return_exceptions=True)

    async with factory() as s:
        h1 = await PaymentService(s).get_payment_history(o1)
        h2 = await PaymentService(s).get_payment_history(o2)

    await _cleanup(factory, u1, o1)
    await _cleanup(factory, u2, o2)
    await engine.dispose()

    print(f"\n✅ Two different orders paid independently:")
    print(f"  Order 1: {len(h1)} paid event(s)")
    print(f"  Order 2: {len(h2)} paid event(s)")

    assert len(h1) == 1
    assert len(h2) == 1
    errors = [r for r in results if isinstance(r, Exception)]
    assert len(errors) == 0, f"Unexpected errors: {errors}"
