"""
FIX-24: Dead Letter Queue — mueve mensajes permanentemente fallidos a una tabla
de análisis para que no contaminen el outbox principal ni se reintentan.
"""
import structlog
from app.domain.db_types import Connection

log = structlog.get_logger(__name__)

_INSERT_DEAD_LETTER = """
    INSERT INTO "NotifDeadLetter"
        ("OriginalOutboxId", "Tipo", "PhoneNumber", "ErrorType", "ErrorMessage", "ContextoJson")
    VALUES ($1, $2, $3, $4, $5, $6::jsonb)
"""

_MARK_DLQ = """
    UPDATE "NotifOutbox"
    SET "EstadoFinal" = 'FALLIDO_DLQ'
    WHERE "Id" = $1
"""


def classify_error(error: str) -> str:
    """Clasifica el error para facilitar el análisis en la DLQ."""
    e = error.lower()
    if "invalid_phone" in e or "does not exist" in e or "invalidphone" in e:
        return "INVALID_PHONE"
    if "template" in e or "variable" in e or "plantilla" in e:
        return "TEMPLATE_ERROR"
    if "rate limit" in e or "429" in e or "ratelimit" in e:
        return "RATE_LIMIT"
    if "circuit" in e or "unavailable" in e:
        return "CIRCUIT_OPEN"
    return "UNKNOWN"


async def move_to_dlq(conn: Connection, outbox_id: str, tipo: str,
                      phone: str, error: str, contexto_json: str) -> None:
    """Mueve un registro al DLQ y lo marca como FALLIDO_DLQ en NotifOutbox."""
    error_type = classify_error(error)
    await conn.execute(
        _INSERT_DEAD_LETTER,
        outbox_id, tipo, phone, error_type, error[:500], contexto_json or "{}",
    )
    await conn.execute(_MARK_DLQ, outbox_id)
    log.error("worker.moved_to_dlq",
              tipo=tipo, error_type=error_type,
              outbox_id=outbox_id, phone=phone)
