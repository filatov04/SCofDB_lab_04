"""Idempotency middleware — LAB 04."""

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Callable

from fastapi import Request, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware

# Payment endpoints that require idempotency protection
_IDEMPOTENCY_PATHS = {
    "/api/payments/pay",
    "/api/payments/retry-demo",
}


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """
    Middleware идемпотентности POST-запросов оплаты.

    Алгоритм:
    1. Пропускать без изменений: не-POST и не-платёжные endpoints.
    2. Если заголовок Idempotency-Key отсутствует — пропустить.
    3. Считать sha256 от тела запроса (request_hash).
    4. Открыть сессию к БД:
       а) Если запись с этим ключом существует:
          - hash совпадает + status=completed → вернуть кэш + X-Idempotency-Replayed: true
          - hash другой → 409 Conflict
          - status=processing → пропустить (конкурентный запрос)
       б) Если записи нет — INSERT с status=processing (ON CONFLICT — другой поток вставил первым).
    5. Выполнить downstream запрос (call_next).
    6. Прочитать response body, обновить запись в БД: status=completed.
    7. Вернуть response клиенту.

    При SQLite (тестовая среда) — middleware пропускает всю логику.
    """

    def __init__(self, app, ttl_seconds: int = 24 * 60 * 60):
        super().__init__(app)
        self.ttl_seconds = ttl_seconds

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        from app.infrastructure.db import _is_sqlite, engine

        # Skip for non-payment requests or in SQLite test mode
        if _is_sqlite or request.method != "POST" or request.url.path not in _IDEMPOTENCY_PATHS:
            return await call_next(request)

        idem_key = request.headers.get("Idempotency-Key")
        if not idem_key:
            return await call_next(request)

        # Read and buffer body so it can be replayed to the downstream handler
        raw_body = await request.body()
        request_hash = self.build_request_hash(raw_body)

        method = request.method
        path = request.url.path
        expires_at = datetime.utcnow() + timedelta(seconds=self.ttl_seconds)

        # --- Check / insert idempotency record ---
        async with AsyncSession(engine) as session:
            result = await session.execute(
                text("""
                    SELECT status, status_code, response_body, request_hash
                    FROM idempotency_keys
                    WHERE idempotency_key = :key
                      AND request_method  = :method
                      AND request_path    = :path
                """),
                {"key": idem_key, "method": method, "path": path},
            )
            existing = result.fetchone()

            if existing:
                if existing.request_hash != request_hash:
                    return Response(
                        content=json.dumps({
                            "detail": "Idempotency key already used with a different payload"
                        }),
                        status_code=409,
                        media_type="application/json",
                        headers={"X-Idempotency-Replayed": "false"},
                    )
                if existing.status == "completed":
                    cached_body = existing.response_body
                    if isinstance(cached_body, dict):
                        cached_body = json.dumps(cached_body, ensure_ascii=False)
                    elif cached_body is None:
                        cached_body = "{}"
                    return Response(
                        content=cached_body,
                        status_code=existing.status_code or 200,
                        media_type="application/json",
                        headers={"X-Idempotency-Replayed": "true"},
                    )
                # status == 'processing': concurrent request — fall through
            else:
                # Try to insert a new processing record
                try:
                    await session.execute(
                        text("""
                            INSERT INTO idempotency_keys
                                (id, idempotency_key, request_method, request_path,
                                 request_hash, status, created_at, updated_at, expires_at)
                            VALUES
                                (:id, :key, :method, :path,
                                 :hash, 'processing', NOW(), NOW(), :expires_at)
                            ON CONFLICT (idempotency_key, request_method, request_path)
                            DO NOTHING
                        """),
                        {
                            "id":         str(uuid.uuid4()),
                            "key":        idem_key,
                            "method":     method,
                            "path":       path,
                            "hash":       request_hash,
                            "expires_at": expires_at,
                        },
                    )
                    await session.commit()
                except Exception:
                    await session.rollback()

        # --- Execute the actual request ---
        # Rebuild the request so the downstream handler can read the body
        async def _receive():
            return {"type": "http.request", "body": raw_body, "more_body": False}

        request._receive = _receive  # type: ignore[attr-defined]

        response = await call_next(request)

        # Read response body (streaming → bytes)
        response_chunks: list[bytes] = []
        async for chunk in response.body_iterator:
            response_chunks.append(chunk)
        response_body_bytes = b"".join(response_chunks)

        # Save response to idempotency_keys
        try:
            response_json = json.loads(response_body_bytes)
        except Exception:
            response_json = {"raw": response_body_bytes.decode("utf-8", errors="replace")}

        async with AsyncSession(engine) as session:
            try:
                await session.execute(
                    text("""
                        UPDATE idempotency_keys
                        SET status       = 'completed',
                            status_code  = :code,
                            response_body = :body,
                            updated_at   = NOW()
                        WHERE idempotency_key = :key
                          AND request_method  = :method
                          AND request_path    = :path
                    """),
                    {
                        "code":   response.status_code,
                        "body":   json.dumps(response_json, ensure_ascii=False),
                        "key":    idem_key,
                        "method": method,
                        "path":   path,
                    },
                )
                await session.commit()
            except Exception:
                await session.rollback()

        return Response(
            content=response_body_bytes,
            status_code=response.status_code,
            headers={
                **dict(response.headers),
                "X-Idempotency-Replayed": "false",
            },
            media_type=response.media_type,
        )

    @staticmethod
    def build_request_hash(raw_body: bytes) -> str:
        """Стабильный sha256-хэш тела запроса."""
        return hashlib.sha256(raw_body).hexdigest()

    @staticmethod
    def encode_response_payload(body_obj) -> str:
        return json.dumps(body_obj, ensure_ascii=False)
