"""Database connection and session management."""

import os
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    async_sessionmaker,
    AsyncSession,
    AsyncEngine,
)
from sqlalchemy.pool import StaticPool

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@db:5432/marketplace",
)

_is_sqlite = DATABASE_URL.startswith("sqlite")

if _is_sqlite:
    engine: AsyncEngine = create_async_engine(
        DATABASE_URL,
        echo=False,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
else:
    engine = create_async_engine(DATABASE_URL, echo=False)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

_sqlite_tables_ready = False


async def create_tables_if_sqlite() -> None:
    global _sqlite_tables_ready
    if not _is_sqlite or _sqlite_tables_ready:
        return
    _sqlite_tables_ready = True
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP NOT NULL
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'created',
                total_amount REAL NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS order_items (
                id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL,
                product_name TEXT NOT NULL,
                price REAL NOT NULL,
                quantity INTEGER NOT NULL,
                subtotal REAL GENERATED ALWAYS AS (price * quantity) STORED,
                FOREIGN KEY (order_id) REFERENCES orders(id)
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS order_status_history (
                id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL,
                status TEXT NOT NULL,
                changed_at TIMESTAMP NOT NULL,
                FOREIGN KEY (order_id) REFERENCES orders(id)
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS idempotency_keys (
                id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL,
                request_method TEXT NOT NULL,
                request_path TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'processing',
                status_code INTEGER,
                response_body TEXT,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                UNIQUE (idempotency_key, request_method, request_path)
            )
        """))


async def get_db() -> AsyncSession:
    await create_tables_if_sqlite()
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
