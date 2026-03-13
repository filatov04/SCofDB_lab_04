"""Реализация репозиториев с использованием SQLAlchemy."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional, List

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.user import User
from app.domain.order import Order, OrderItem, OrderStatus, OrderStatusChange


class UserRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def save(self, user: User) -> None:
        await self.session.execute(
            text("""
                INSERT INTO users (id, email, name, created_at)
                VALUES (:id, :email, :name, :created_at)
                ON CONFLICT (id) DO UPDATE
                    SET email = EXCLUDED.email,
                        name  = EXCLUDED.name
            """),
            {
                "id":         str(user.id),
                "email":      user.email,
                "name":       user.name,
                "created_at": user.created_at,
            },
        )
        await self.session.commit()

    async def find_by_id(self, user_id: uuid.UUID) -> Optional[User]:
        result = await self.session.execute(
            text("SELECT id, email, name, created_at FROM users WHERE id = :id"),
            {"id": str(user_id)},
        )
        row = result.fetchone()
        return self._row_to_user(row) if row else None

    async def find_by_email(self, email: str) -> Optional[User]:
        result = await self.session.execute(
            text("SELECT id, email, name, created_at FROM users WHERE email = :email"),
            {"email": email},
        )
        row = result.fetchone()
        return self._row_to_user(row) if row else None

    async def find_all(self) -> List[User]:
        result = await self.session.execute(
            text("SELECT id, email, name, created_at FROM users ORDER BY created_at")
        )
        return [self._row_to_user(row) for row in result.fetchall()]

    @staticmethod
    def _row_to_user(row) -> User:
        user = object.__new__(User)
        user.id = uuid.UUID(str(row.id))
        user.email = row.email
        user.name = row.name
        user.created_at = _ensure_datetime(row.created_at)
        return user


class OrderRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def save(self, order: Order) -> None:
        await self.session.execute(
            text("""
                INSERT INTO orders (id, user_id, status, total_amount, created_at)
                VALUES (:id, :user_id, :status, :total_amount, :created_at)
                ON CONFLICT (id) DO UPDATE
                    SET status       = EXCLUDED.status,
                        total_amount = EXCLUDED.total_amount
            """),
            {
                "id":           str(order.id),
                "user_id":      str(order.user_id),
                "status":       order.status.value,
                "total_amount": float(order.total_amount),
                "created_at":   order.created_at,
            },
        )

        for item in order.items:
            await self.session.execute(
                text("""
                    INSERT INTO order_items (id, order_id, product_name, price, quantity)
                    VALUES (:id, :order_id, :product_name, :price, :quantity)
                    ON CONFLICT (id) DO NOTHING
                """),
                {
                    "id":           str(item.id),
                    "order_id":     str(order.id),
                    "product_name": item.product_name,
                    "price":        float(item.price),
                    "quantity":     item.quantity,
                },
            )

        for change in order.status_history:
            await self.session.execute(
                text("""
                    INSERT INTO order_status_history (id, order_id, status, changed_at)
                    VALUES (:id, :order_id, :status, :changed_at)
                    ON CONFLICT (id) DO NOTHING
                """),
                {
                    "id":         str(change.id),
                    "order_id":   str(order.id),
                    "status":     change.status.value,
                    "changed_at": change.changed_at,
                },
            )

        await self.session.commit()

    async def find_by_id(self, order_id: uuid.UUID) -> Optional[Order]:
        result = await self.session.execute(
            text("""
                SELECT id, user_id, status, total_amount, created_at
                FROM orders WHERE id = :id
            """),
            {"id": str(order_id)},
        )
        row = result.fetchone()
        if not row:
            return None
        order = self._row_to_order(row)
        await self._load_items(order)
        await self._load_history(order)
        return order

    async def find_by_user(self, user_id: uuid.UUID) -> List[Order]:
        result = await self.session.execute(
            text("""
                SELECT id, user_id, status, total_amount, created_at
                FROM orders WHERE user_id = :user_id ORDER BY created_at DESC
            """),
            {"user_id": str(user_id)},
        )
        orders = [self._row_to_order(row) for row in result.fetchall()]
        for order in orders:
            await self._load_items(order)
            await self._load_history(order)
        return orders

    async def find_all(self) -> List[Order]:
        result = await self.session.execute(
            text("""
                SELECT id, user_id, status, total_amount, created_at
                FROM orders ORDER BY created_at DESC
            """)
        )
        orders = [self._row_to_order(row) for row in result.fetchall()]
        for order in orders:
            await self._load_items(order)
            await self._load_history(order)
        return orders

    async def _load_items(self, order: Order) -> None:
        result = await self.session.execute(
            text("SELECT id, order_id, product_name, price, quantity FROM order_items WHERE order_id = :oid"),
            {"oid": str(order.id)},
        )
        order.items = [self._row_to_item(r) for r in result.fetchall()]

    async def _load_history(self, order: Order) -> None:
        result = await self.session.execute(
            text("SELECT id, order_id, status, changed_at FROM order_status_history WHERE order_id = :oid ORDER BY changed_at"),
            {"oid": str(order.id)},
        )
        order.status_history = [self._row_to_history(r) for r in result.fetchall()]

    @staticmethod
    def _row_to_order(row) -> Order:
        order = object.__new__(Order)
        order.id = uuid.UUID(str(row.id))
        order.user_id = uuid.UUID(str(row.user_id))
        order.status = OrderStatus(row.status)
        order.total_amount = Decimal(str(row.total_amount))
        order.created_at = _ensure_datetime(row.created_at)
        order.items = []
        order.status_history = []
        return order

    @staticmethod
    def _row_to_item(row) -> OrderItem:
        item = object.__new__(OrderItem)
        item.id = uuid.UUID(str(row.id))
        item.order_id = uuid.UUID(str(row.order_id))
        item.product_name = row.product_name
        item.price = Decimal(str(row.price))
        item.quantity = int(row.quantity)
        return item

    @staticmethod
    def _row_to_history(row) -> OrderStatusChange:
        change = object.__new__(OrderStatusChange)
        change.id = uuid.UUID(str(row.id))
        change.order_id = uuid.UUID(str(row.order_id))
        change.status = OrderStatus(row.status)
        change.changed_at = _ensure_datetime(row.changed_at)
        return change


def _ensure_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
    return datetime.utcnow()
