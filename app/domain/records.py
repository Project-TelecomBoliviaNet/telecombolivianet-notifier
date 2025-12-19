"""
Objetos de dominio del worker.

Usar dataclasses inmutables (frozen=True) en lugar de dicts sin tipado
garantiza que campos mal escritos o faltantes se detecten en desarrollo,
no en producción.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class OutboxRecord:
    """
    Registro del outbox completamente preparado para envío.
    Creado por _load_and_render() luego de cargar config, plantilla y renderizar.
    Immutable: una vez construido, sus campos no cambian.

    US-META: si is_template=True, el envío usa send_template() con meta_params
    en lugar de send() con el texto plano.
    """
    outbox_id:           str
    cliente_id:          str
    tipo:                str
    phone:               str
    intentos:            int
    phone_log:           str    # versión enmascarada del teléfono (PII) para logs
    mensaje:             str    # texto renderizado (text) o etiqueta de log (template)
    # US-META: campos para Meta template messages (opcionales)
    is_template:         bool            = False
    meta_template_name:  str | None      = None
    meta_language_code:  str | None      = None
    meta_params:         tuple[str, ...] | None = None  # tuple porque frozen=True requiere hashable


@dataclass(frozen=True)
class RawOutboxRow:
    """
    Fila cruda del outbox tal como viene de la BD, antes de renderizar.
    Separada de OutboxRecord para distinguir estado pre y post renderizado.
    """
    id:            str
    tipo:          str
    cliente_id:    str
    phone_number:  str
    intentos:      int
    contexto_json: str | None

    @classmethod
    def from_db_row(cls, row: dict) -> "RawOutboxRow":
        """Construye desde un dict de asyncpg."""
        return cls(
            id=row["id"],
            tipo=row["tipo"],
            cliente_id=row["cliente_id"],
            phone_number=row["phone_number"],
            intentos=row["intentos"],
            contexto_json=row.get("contexto_json"),
        )
