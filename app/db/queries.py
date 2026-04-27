"""
Queries SQL del Worker.
El Worker solo accede a: NotifOutbox, NotifConfigs, NotifPlantillas, NotifLogs, Clients, Invoices.
Nunca escribe en tablas de dominio del monolito (Clients, Invoices, Payments…).

NOTA IMPORTANTE — Convención de nombres:
EF Core crea las columnas en PostgreSQL con PascalCase (ej: "Id", "Tipo", "ClienteId").
PostgreSQL es case-sensitive cuando los identificadores están entre comillas dobles.
Por eso todas las referencias a columnas usan comillas dobles con PascalCase exacto.
Se usan aliases AS snake_case para que el código Python siga accediendo con claves
en minúsculas (ej: record["id"], record["tipo"]) sin necesidad de cambios en el worker.
"""

# ── Outbox: tomar lote de pendientes ─────────────────────────────────────────
# BUG B FIX: Este query se usa dentro de _fetch_and_claim_batch() en outbox_worker.py,
# donde se ejecuta en la MISMA transacción que el UPDATE publicado=TRUE.
# FOR UPDATE SKIP LOCKED + transacción = exclusión mutua garantizada entre workers.
#
# BUG COLUMNAS FIX: Las columnas de "NotifOutbox" son PascalCase en PostgreSQL
# (creadas por EF Core). Se usan aliases AS snake_case para compatibilidad con el
# código Python del worker que accede a record["id"], record["tipo"], etc.
FETCH_PENDING_BATCH = """
    SELECT
        o."Id"            AS id,
        o."Tipo"          AS tipo,
        o."ClienteId"     AS cliente_id,
        o."PhoneNumber"   AS phone_number,
        o."Intentos"      AS intentos,
        o."EnviarDesde"   AS enviar_desde,
        o."ContextoJson"  AS contexto_json,
        o."ReferenciaId"  AS referencia_id
    FROM "NotifOutbox" o
    WHERE o."EstadoFinal" IS NULL
      AND o."Publicado" = FALSE
      AND o."EnviarDesde" <= NOW()
      AND (o."ProximoIntento" IS NULL OR o."ProximoIntento" <= NOW())
    ORDER BY o."EnviarDesde" ASC
    LIMIT $1
    FOR UPDATE SKIP LOCKED
"""

# ── Outbox: marcar como tomado (publicado=true) ───────────────────────────────
# BUG B FIX: Ya no se usa como query individual. El MARK ahora ocurre en el mismo
# bloque de transacción que el FETCH en _fetch_and_claim_batch().
# Se conserva para compatibilidad con posibles herramientas de administración manual.
MARK_TAKEN = """
    UPDATE "NotifOutbox"
    SET "Publicado" = TRUE
    WHERE "Id" = $1
"""

# ── Outbox: finalizar con éxito ───────────────────────────────────────────────
FINALIZE_OK = """
    UPDATE "NotifOutbox"
    SET
        "EstadoFinal"  = 'ENVIADO',
        "Publicado"    = TRUE,
        "Intentos"     = $2,
        "ProcesadoAt"  = NOW()
    WHERE "Id" = $1
"""

# ── Outbox: finalizar con fallo definitivo ────────────────────────────────────
FINALIZE_FAILED = """
    UPDATE "NotifOutbox"
    SET
        "EstadoFinal"  = 'FALLIDO',
        "Publicado"    = TRUE,
        "Intentos"     = $2,
        "ProcesadoAt"  = NOW()
    WHERE "Id" = $1
"""

# ── Outbox: devolver al pool con backoff ──────────────────────────────────────
RETRY_WITH_BACKOFF = """
    UPDATE "NotifOutbox"
    SET
        "Publicado"      = FALSE,
        "Intentos"       = $2,
        "ProximoIntento" = NOW() + ($3 || ' seconds')::interval
    WHERE "Id" = $1
"""

# ── Config: obtener config de un tipo ────────────────────────────────────────
# Aliases snake_case para que outbox_worker.py acceda con config["activo"],
# config["inmediato"], config["hora_inicio"], config["hora_fin"].
FETCH_CONFIG = """
    SELECT
        "Tipo"             AS tipo,
        "Activo"           AS activo,
        "DelaySegundos"    AS delay_segundos,
        "HoraInicio"       AS hora_inicio,
        "HoraFin"          AS hora_fin,
        "Inmediato"        AS inmediato,
        "DiasAntes"        AS dias_antes
    FROM "NotifConfigs"
    WHERE "Tipo" = $1
"""

# ── Config: obtener todos los configs activos ─────────────────────────────────
FETCH_ALL_CONFIGS = """
    SELECT
        "Tipo"             AS tipo,
        "Activo"           AS activo,
        "DelaySegundos"    AS delay_segundos,
        "HoraInicio"       AS hora_inicio,
        "HoraFin"          AS hora_fin,
        "Inmediato"        AS inmediato,
        "DiasAntes"        AS dias_antes
    FROM "NotifConfigs"
    ORDER BY "Tipo"
"""

# ── Plantilla: obtener plantilla activa para un tipo ─────────────────────────
FETCH_PLANTILLA = """
    SELECT "Id" AS id, "Texto" AS texto
    FROM "NotifPlantillas"
    WHERE "Tipo" = $1 AND "Activa" = TRUE
    LIMIT 1
"""

# ── Log: insertar resultado ───────────────────────────────────────────────────
INSERT_LOG = """
    INSERT INTO "NotifLogs"
        ("Id", "OutboxId", "ClienteId", "Tipo", "PhoneNumber", "Mensaje",
         "Estado", "IntentoNum", "ErrorDetalle", "RegistradoAt")
    VALUES
        (gen_random_uuid(), $1, $2, $3, $4, $5, $6, $7, $8, NOW())
"""

# ── Recordatorios (ReminderJob): facturas pendientes por vencimiento ──────────
# Aliases snake_case para compatibilidad con reminder_job.py que accede a
# inv["invoice_id"], inv["cliente_id"], inv["phone_number"], inv["nombre"], etc.
#
# CORRECCIÓN (Fix #12): La tabla "Invoices" NO tiene columna "IsDeleted" —
# solo "Clients" la tiene. El filtro soft-delete solo se aplica en Clients.
FETCH_INVOICES_DUE_IN_DAYS = """
    SELECT
        i."Id"          AS invoice_id,
        i."ClientId"    AS cliente_id,
        i."Amount"      AS amount,
        i."DueDate"     AS due_date,
        i."Year"        AS year,
        i."Month"       AS month,
        i."Type"        AS type,
        c."PhoneMain"   AS phone_number,
        c."FullName"    AS nombre,
        p."Name"        AS plan_name
    FROM "Invoices" i
    JOIN "Clients"  c ON c."Id" = i."ClientId"
    LEFT JOIN "Plans" p ON p."Id" = c."PlanId"
    WHERE i."Status" = 'Pendiente'
      AND c."IsDeleted" = FALSE
      AND DATE(i."DueDate" AT TIME ZONE 'America/La_Paz') = $1::date
"""

# ── Recordatorios: contar meses pendientes para un cliente ────────────────────
COUNT_PENDING_MONTHS = """
    SELECT COUNT(*) AS total
    FROM "Invoices"
    WHERE "ClientId" = $1
      AND "Status" IN ('Pendiente', 'Vencida')
      AND "Type" = 'Mensualidad'
"""

# ── Recordatorios: verificar si ya existe un recordatorio para esta factura ───
EXISTS_REMINDER_LOG = """
    SELECT 1
    FROM "NotifLogs" l
    JOIN "NotifOutbox" o ON o."Id" = l."OutboxId"
    WHERE l."ClienteId"    = $1
      AND l."Tipo"         = $2
      AND o."ReferenciaId" = $3
      AND l."Estado" NOT IN ('FALLIDO')
    LIMIT 1
"""

# ── Recordatorios: insertar en outbox ────────────────────────────────────────
INSERT_OUTBOX = """
    INSERT INTO "NotifOutbox"
        ("Id", "Tipo", "ClienteId", "PhoneNumber", "Publicado", "Intentos",
         "EnviarDesde", "EstadoFinal", "CreadoAt", "ContextoJson", "ReferenciaId")
    VALUES
        (gen_random_uuid(), $1, $2, $3, FALSE, 0,
         NOW() + ($4 || ' seconds')::interval, NULL, NOW(), $5::jsonb, $6)
"""

# ── US-04: NotifJobLog — persistencia de última ejecución de jobs ─────────────
# Requiere tabla: CREATE TABLE IF NOT EXISTS "NotifJobLog" (
#   "JobName" TEXT PRIMARY KEY,
#   "LastRun"  TIMESTAMPTZ NOT NULL
# );
FETCH_LAST_JOB_RUN = """
    SELECT "LastRun" AS last_run
    FROM "NotifJobLog"
    WHERE "JobName" = $1
"""

UPSERT_JOB_RUN = """
    INSERT INTO "NotifJobLog" ("JobName", "LastRun")
    VALUES ($1, $2::date)
    ON CONFLICT ("JobName") DO UPDATE SET "LastRun" = EXCLUDED."LastRun"
"""

# ── US-04 / BUG FIX: Distributed lock para ReminderJob ───────────────────────
# pg_try_advisory_lock(key) es atómico en PostgreSQL.
# Si retorna TRUE, este worker adquirió el lock exclusivo.
# Si retorna FALSE, otro worker ya lo tiene — este ciclo se saltea.
# El lock se libera automáticamente al cerrar la conexión.
# Key fija 20250101 identifica al ReminderJob de forma única.
ADVISORY_LOCK_REMINDER   = "SELECT pg_try_advisory_lock(20250101)"
ADVISORY_UNLOCK_REMINDER = "SELECT pg_advisory_unlock(20250101)"

# ── Outbox: operaciones de control de flujo del worker ────────────────────────
# Movidas desde outbox_worker.py para centralizar todo el SQL en un solo lugar.

CLAIM_BATCH = """
    UPDATE "NotifOutbox"
    SET "Publicado" = TRUE
    WHERE "Id" = ANY($1)
"""

RETURN_TO_POOL = """
    UPDATE "NotifOutbox"
    SET "Publicado"      = FALSE,
        "ProximoIntento" = NOW() + ($2 || ' seconds')::interval
    WHERE "Id" = $1
"""

# ── Tipos de recordatorio activos ─────────────────────────────────────────────
# Permite agregar un RECORDATORIO_R4 o similar sin cambiar código Python.
# El ReminderJob usa este query para obtener la lista de tipos en lugar de
# tener REMINDER_TYPES hardcodeado como constante.
FETCH_ACTIVE_REMINDER_TYPES = """
    SELECT DISTINCT "Tipo" AS tipo
    FROM "NotifConfigs"
    WHERE "Tipo" LIKE 'RECORDATORIO_%'
      AND "Activo" = TRUE
    ORDER BY "Tipo"
"""

# ── Health: contar mensajes pendientes en outbox ──────────────────────────────
COUNT_OUTBOX_PENDING = """
    SELECT COUNT(*) FROM "NotifOutbox" WHERE "EstadoFinal" IS NULL
"""
