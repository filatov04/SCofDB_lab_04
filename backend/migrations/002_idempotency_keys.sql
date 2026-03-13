-- ============================================
-- LAB 04: Таблица идемпотентности платёжных запросов
-- ============================================

CREATE TABLE idempotency_keys (
    id               UUID         PRIMARY KEY DEFAULT uuid_generate_v4(),
    idempotency_key  VARCHAR(255) NOT NULL,
    request_method   VARCHAR(16)  NOT NULL,
    request_path     TEXT         NOT NULL,
    -- sha256 от тела запроса — для обнаружения reuse ключа с другим payload
    request_hash     TEXT         NOT NULL,
    -- статус обработки: processing → completed | failed
    status           VARCHAR(32)  NOT NULL DEFAULT 'processing',
    -- кэш ответа
    status_code      INTEGER,
    response_body    JSONB,
    created_at       TIMESTAMP    NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMP    NOT NULL DEFAULT NOW(),
    -- TTL: по истечении ключ считается устаревшим и может быть удалён
    expires_at       TIMESTAMP    NOT NULL,

    CONSTRAINT idempotency_status_check
        CHECK (status IN ('processing', 'completed', 'failed')),
    -- один ключ — один endpoint: защита от дублирующих записей
    CONSTRAINT idempotency_unique
        UNIQUE (idempotency_key, request_method, request_path)
);

-- Индекс для задания cleanup (удаление просроченных ключей)
CREATE INDEX idx_idempotency_expires_at
    ON idempotency_keys (expires_at);

-- Индекс для быстрого lookup по ключу + endpoint
CREATE INDEX idx_idempotency_lookup
    ON idempotency_keys (idempotency_key, request_method, request_path);

-- Триггер автоматического обновления updated_at
CREATE OR REPLACE FUNCTION update_idempotency_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_idempotency_updated_at
    BEFORE UPDATE ON idempotency_keys
    FOR EACH ROW
    EXECUTE FUNCTION update_idempotency_updated_at();
