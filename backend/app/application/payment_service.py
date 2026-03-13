"""Сервис для демонстрации конкурентных оплат.

1. pay_order_unsafe() — READ COMMITTED без блокировок (уязвим к race condition)
2. pay_order_safe()   — REPEATABLE READ + FOR UPDATE (безопасная реализация)
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import OrderAlreadyPaidError, OrderNotFoundError


class PaymentService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def pay_order_unsafe(self, order_id: uuid.UUID) -> dict:
        """Небезопасная оплата (READ COMMITTED, без блокировок).

        Уязвима к race condition: два параллельных вызова могут оба пройти
        проверку статуса до того, как любой из них выполнит UPDATE.
        Результат — две записи 'paid' в order_status_history.
        """
        await self.session.execute(text("BEGIN"))

        result = await self.session.execute(
            text("SELECT id, status FROM orders WHERE id = :oid"),
            {"oid": str(order_id)},
        )
        row = result.fetchone()
        if not row:
            await self.session.execute(text("ROLLBACK"))
            raise OrderNotFoundError(order_id)

        if row.status == "paid":
            await self.session.execute(text("ROLLBACK"))
            raise OrderAlreadyPaidError(order_id)

        await self.session.execute(
            text("UPDATE orders SET status = 'paid' WHERE id = :oid AND status = 'created'"),
            {"oid": str(order_id)},
        )
        await self.session.execute(
            text("""
                INSERT INTO order_status_history (id, order_id, status, changed_at)
                VALUES (gen_random_uuid(), :oid, 'paid', NOW())
            """),
            {"oid": str(order_id)},
        )
        await self.session.execute(text("COMMIT"))
        return {"order_id": str(order_id), "status": "paid", "method": "unsafe"}

    async def pay_order_safe(self, order_id: uuid.UUID) -> dict:
        """Безопасная оплата (REPEATABLE READ + FOR UPDATE).

        FOR UPDATE блокирует строку до конца транзакции.
        Второй конкурентный запрос ждёт снятия блокировки и видит
        уже обновлённый статус 'paid', после чего выбрасывает исключение.
        """
        await self.session.execute(text("BEGIN"))
        await self.session.execute(
            text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        )

        result = await self.session.execute(
            text("SELECT id, status FROM orders WHERE id = :oid FOR UPDATE"),
            {"oid": str(order_id)},
        )
        row = result.fetchone()
        if not row:
            await self.session.execute(text("ROLLBACK"))
            raise OrderNotFoundError(order_id)

        if row.status == "paid":
            await self.session.execute(text("ROLLBACK"))
            raise OrderAlreadyPaidError(order_id)

        await self.session.execute(
            text("UPDATE orders SET status = 'paid' WHERE id = :oid AND status = 'created'"),
            {"oid": str(order_id)},
        )
        await self.session.execute(
            text("""
                INSERT INTO order_status_history (id, order_id, status, changed_at)
                VALUES (gen_random_uuid(), :oid, 'paid', NOW())
            """),
            {"oid": str(order_id)},
        )
        await self.session.execute(text("COMMIT"))
        return {"order_id": str(order_id), "status": "paid", "method": "safe"}

    async def get_payment_history(self, order_id: uuid.UUID) -> list[dict]:
        """Список записей об оплате из истории статусов."""
        result = await self.session.execute(
            text("""
                SELECT id, order_id, status, changed_at
                FROM order_status_history
                WHERE order_id = :oid AND status = 'paid'
                ORDER BY changed_at
            """),
            {"oid": str(order_id)},
        )
        return [
            {
                "id":         str(r.id),
                "order_id":   str(r.order_id),
                "status":     r.status,
                "changed_at": str(r.changed_at),
            }
            for r in result.fetchall()
        ]
