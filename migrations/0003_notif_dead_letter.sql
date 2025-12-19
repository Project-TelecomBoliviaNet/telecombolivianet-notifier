-- FIX-24: Dead Letter Queue para mensajes permanentemente fallidos.
-- Los registros movidos aquí ya no se reintentan; sirven para análisis y alertas.
CREATE TABLE IF NOT EXISTS "NotifDeadLetter" (
    "Id"              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    "OriginalOutboxId" UUID        NOT NULL,
    "Tipo"            VARCHAR(100),
    "PhoneNumber"     VARCHAR(20),
    "ErrorType"       VARCHAR(50),  -- INVALID_PHONE | TEMPLATE_ERROR | RATE_LIMIT | CIRCUIT_OPEN | UNKNOWN
    "ErrorMessage"    TEXT,
    "ContextoJson"    JSONB,
    "FailedAt"        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dlq_error_type ON "NotifDeadLetter" ("ErrorType", "FailedAt");
