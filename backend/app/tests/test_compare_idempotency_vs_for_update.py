"""
LAB 04: Сравнение подходов — FOR UPDATE (LAB 02) vs Idempotency-Key (LAB 04).

Оба механизма решают РАЗНЫЕ проблемы и дополняют друг друга:

FOR UPDATE (REPEATABLE READ):
  Цель:    Предотвратить ГОНКУ двух ПАРАЛЛЕЛЬНЫХ запросов в БД.
  Защита:  На уровне БД — строка заблокирована до commit первой транзакции.
  Retry:   При повторном вызове — OrderAlreadyPaidError (400/500).
  UX:      Клиент получает ошибку, не знает было ли первое списание.

Idempotency-Key:
  Цель:    Сделать ПОВТОРНЫЙ запрос безопасным (сетевая ошибка → retry).
  Защита:  На уровне API-контракта — кэш ответа по ключу.
  Retry:   При повторном вызове — тот же успешный ответ из кэша.
  UX:      Клиент безопасно получает подтверждение оплаты.

Оба механизма ДОЛЖНЫ применяться вместе в production:
  - FOR UPDATE защищает от конкурентных списаний.
  - Idempotency-Key защищает от неопределённости при сетевых сбоях.
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


async def _make_order(factory) -> tuple[uuid.UUID, uuid.UUID]:
    user_id = uuid.uuid4()
    order_id = uuid.uuid4()
    async with factory() as s:
        await s.execute(
            text("INSERT INTO users (id, email, name, created_at) VALUES (:id, :email, :name, NOW())"),
            {"id": str(user_id), "email": f"cmp_{user_id}@example.com", "name": "Cmp"},
        )
        await s.execute(
            text("INSERT INTO orders (id, user_id, status, total_amount, created_at) VALUES (:id, :uid, 'created', 400, NOW())"),
            {"id": str(order_id), "uid": str(user_id)},
        )
        await s.execute(
            text("INSERT INTO order_status_history (id, order_id, status, changed_at) VALUES (gen_random_uuid(), :oid, 'created', NOW())"),
            {"oid": str(order_id)},
        )
        await s.commit()
    return user_id, order_id


async def _cleanup(factory, user_id, order_id, idem_key=None):
    async with factory() as s:
        if idem_key:
            await s.execute(text("DELETE FROM idempotency_keys WHERE idempotency_key = :k"), {"k": idem_key})
        await s.execute(text("DELETE FROM order_status_history WHERE order_id = :oid"), {"oid": str(order_id)})
        await s.execute(text("DELETE FROM orders WHERE id = :oid"), {"oid": str(order_id)})
        await s.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": str(user_id)})
        await s.commit()


@pytest.mark.asyncio
async def test_compare_for_update_and_idempotency_behaviour():
    """
    Сравнительная демонстрация двух подходов.

    Сценарий A — FOR UPDATE (mode=for_update):
      Первый вызов: success=True.
      Повторный вызов (retry): success=False / "already paid".
      → Клиент видит ошибку, хотя деньги уже списаны.

    Сценарий B — unsafe + Idempotency-Key:
      Первый вызов: success=True.
      Повторный вызов с тем же ключом: success=True (из кэша).
      → Клиент видит тот же успешный ответ.
    """
    if not _pg_available():
        pytest.skip("PostgreSQL not available")

    from app.main import app

    engine = _make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    # ─── Scenario A: FOR UPDATE (no idempotency key) ───────────────────
    u_a, o_a = await _make_order(factory)
    idem_key_a = f"lab04-cmp-a-{uuid.uuid4()}"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r_a1 = await client.post("/api/payments/retry-demo", json={"order_id": str(o_a), "mode": "for_update"})
        r_a2 = await client.post("/api/payments/retry-demo", json={"order_id": str(o_a), "mode": "for_update"})

    # ─── Scenario B: unsafe + Idempotency-Key ──────────────────────────
    u_b, o_b = await _make_order(factory)
    idem_key_b = f"lab04-cmp-b-{uuid.uuid4()}"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r_b1 = await client.post(
            "/api/payments/retry-demo",
            json={"order_id": str(o_b), "mode": "unsafe"},
            headers={"Idempotency-Key": idem_key_b},
        )
        r_b2 = await client.post(
            "/api/payments/retry-demo",
            json={"order_id": str(o_b), "mode": "unsafe"},
            headers={"Idempotency-Key": idem_key_b},
        )

    await _cleanup(factory, u_a, o_a)
    await _cleanup(factory, u_b, o_b, idem_key=idem_key_b)
    await engine.dispose()

    # ─── Assert Scenario A ────────────────────────────────────────────
    da1, da2 = r_a1.json(), r_a2.json()
    assert da1["success"] is True, "Scenario A: first call must succeed"
    assert da2["success"] is False, "Scenario A: retry must return error (already paid)"

    # ─── Assert Scenario B ────────────────────────────────────────────
    db1, db2 = r_b1.json(), r_b2.json()
    assert db1["success"] is True, "Scenario B: first call must succeed"
    assert db2["success"] is True, "Scenario B: retry must return cached success"
    assert r_b2.headers.get("X-Idempotency-Replayed") == "true", \
        "Scenario B: second response must be marked as replayed"

    # ─── Summary ──────────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("COMPARISON: FOR UPDATE  vs  IDEMPOTENCY KEY")
    print("=" * 65)
    print(f"Scenario A (FOR UPDATE, no idempotency key):")
    print(f"  1st call  → success={da1['success']}  ({da1.get('message','')[:60]})")
    print(f"  Retry     → success={da2['success']}  ({da2.get('message','')[:60]})")
    print(f"  Client UX: ERROR on retry — payment state is AMBIGUOUS")
    print()
    print(f"Scenario B (unsafe + Idempotency-Key):")
    print(f"  1st call  → success={db1['success']}  ({db1.get('message','')[:60]})")
    print(f"  Retry     → success={db2['success']}  replayed={r_b2.headers.get('X-Idempotency-Replayed')}")
    print(f"  Client UX: Same success — payment state is CLEAR")
    print()
    print("CONCLUSION:")
    print("  FOR UPDATE   → DB-level protection against race conditions.")
    print("  Idempotency  → API-level protection against retries after network loss.")
    print("  Use BOTH together in production for full payment safety.")
    print("=" * 65)
