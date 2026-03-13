-- ============================================
-- Схема базы данных маркетплейса (lab_04)
-- ============================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE order_statuses (
    status      VARCHAR(20) PRIMARY KEY,
    description TEXT
);

INSERT INTO order_statuses (status, description) VALUES
    ('created',   'Order has been created'),
    ('paid',      'Order has been paid'),
    ('cancelled', 'Order has been cancelled'),
    ('shipped',   'Order has been shipped'),
    ('completed', 'Order has been completed');

CREATE TABLE users (
    id         UUID         PRIMARY KEY DEFAULT uuid_generate_v4(),
    email      VARCHAR(255) NOT NULL,
    name       VARCHAR(255) NOT NULL DEFAULT '',
    created_at TIMESTAMP    NOT NULL DEFAULT NOW(),

    CONSTRAINT users_email_unique    UNIQUE (email),
    CONSTRAINT users_email_not_empty CHECK  (email <> ''),
    CONSTRAINT users_email_format    CHECK  (email ~ '^[a-zA-Z0-9_.+\-]+@[a-zA-Z0-9\-]+\.[a-zA-Z0-9\-.]+$')
);

CREATE TABLE orders (
    id           UUID          PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id      UUID          NOT NULL,
    status       VARCHAR(20)   NOT NULL DEFAULT 'created',
    total_amount NUMERIC(12,2) NOT NULL DEFAULT 0,
    created_at   TIMESTAMP     NOT NULL DEFAULT NOW(),

    CONSTRAINT orders_user_fk             FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT orders_status_fk           FOREIGN KEY (status)  REFERENCES order_statuses(status),
    CONSTRAINT orders_total_amount_non_neg CHECK (total_amount >= 0)
);

CREATE TABLE order_items (
    id           UUID          PRIMARY KEY DEFAULT uuid_generate_v4(),
    order_id     UUID          NOT NULL,
    product_name VARCHAR(500)  NOT NULL,
    price        NUMERIC(12,2) NOT NULL,
    quantity     INTEGER       NOT NULL,
    subtotal     NUMERIC(12,2) GENERATED ALWAYS AS (price * quantity) STORED,

    CONSTRAINT order_items_order_fk          FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
    CONSTRAINT order_items_price_non_neg     CHECK (price    >= 0),
    CONSTRAINT order_items_quantity_positive CHECK (quantity >  0),
    CONSTRAINT order_items_product_not_empty CHECK (product_name <> '')
);

CREATE TABLE order_status_history (
    id         UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    order_id   UUID        NOT NULL,
    status     VARCHAR(20) NOT NULL,
    changed_at TIMESTAMP   NOT NULL DEFAULT NOW(),

    CONSTRAINT order_status_history_order_fk  FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
    CONSTRAINT order_status_history_status_fk FOREIGN KEY (status)   REFERENCES order_statuses(status)
);

-- ============================================
-- КРИТИЧЕСКИЙ ИНВАРИАНТ: Нельзя оплатить заказ дважды
-- Срабатывает только при ПЕРЕХОДЕ статуса → 'paid'
-- ============================================
CREATE OR REPLACE FUNCTION check_order_not_already_paid()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.status = 'paid' AND OLD.status != 'paid' THEN
        IF EXISTS (
            SELECT 1 FROM order_status_history
            WHERE order_id = NEW.id AND status = 'paid'
        ) THEN
            RAISE EXCEPTION 'Order % has already been paid', NEW.id
                USING ERRCODE = 'unique_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_check_order_not_already_paid
    BEFORE UPDATE ON orders
    FOR EACH ROW
    EXECUTE FUNCTION check_order_not_already_paid();
