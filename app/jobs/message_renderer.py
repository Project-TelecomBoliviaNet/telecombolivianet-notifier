"""
MessageRenderer — turns a RawOutboxRow into a sendable OutboxRecord.

Responsibilities:
  - Validate notif_config (active, within time window).
  - Fetch system config once per batch.
  - Load and render the template (Meta HSM or free-text fallback).
  - Enrich context from DB when ContextoJson is sparse.

Does NOT send the message — that responsibility belongs to outbox_worker._send_and_finalize.
"""
import json
from typing import Optional

import structlog

from app.db import queries as Q
from app.domain.db_types import Pool, Connection
from app.domain.records import OutboxRecord, RawOutboxRow
from app.services.template import render, parse_context
from app.utils.pii import mask_phone
from app.jobs._time_window import is_within_window
from app.jobs.outbox_batch_fetcher import (
    finalize_type_disabled,
    finalize_no_template,
    return_to_pool_until_window,
)
from app.jobs.context_enricher import enrich_context_from_db

log = structlog.get_logger(__name__)


async def fetch_sys_config(pool: Pool) -> dict:
    """Loads system config (empresa, portal, backend URL) in a single query."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(Q.FETCH_SYSTEM_CONFIG_BULK)
    return {r["key"]: r["value"] for r in rows}


async def _validate_config(
    conn: Connection, row: RawOutboxRow
) -> Optional[dict]:
    """Loads config and checks active + time window. Returns None to discard."""
    config = await conn.fetchrow(Q.FETCH_CONFIG, row.tipo)
    if not config or not config["activo"]:
        log.warning("worker.type_disabled", tipo=row.tipo, outbox_id=row.id)
        await finalize_type_disabled(conn, row)
        return None
    if not config["inmediato"] and not is_within_window(
        config["hora_inicio"], config["hora_fin"]
    ):
        await return_to_pool_until_window(conn, row, config["hora_inicio"])
        return None
    return config


async def load_and_render(
    pool: Pool, row: RawOutboxRow, sys_config: dict
) -> Optional[OutboxRecord]:
    """
    Pipeline: enrich context → validate config → fetch template → build OutboxRecord.
    Returns None if the record was discarded (inactive type, outside window, no template).

    If the template has MetaTemplateName and HsmStatus='Aprobada', returns
    an OutboxRecord with is_template=True and positional params for Meta API.
    Otherwise renders free-text (24h session — only valid within active window).
    """
    contexto = parse_context(row.contexto_json or "{}")
    if len(contexto) < 3:
        contexto = await enrich_context_from_db(pool, row, sys_config)

    async with pool.acquire() as conn:
        config = await _validate_config(conn, row)
        if config is None:
            return None

        plantilla = await conn.fetchrow(Q.FETCH_PLANTILLA, row.tipo)
        if not plantilla:
            log.error("worker.no_template", tipo=row.tipo, outbox_id=row.id)
            await finalize_no_template(conn, row)
            return None

    meta_name = plantilla["meta_template_name"]
    hsm_ok    = (plantilla["hsm_status"] or "").lower() == "aprobada"

    if meta_name and hsm_ok:
        param_order = json.loads(plantilla["meta_param_order"] or "[]")
        params      = tuple(str(contexto.get(name, "")) for name in param_order)
        empty_params = [param_order[i] for i, v in enumerate(params) if not v]
        if empty_params:
            log.error(
                "worker.template_empty_params",
                tipo=row.tipo, outbox_id=row.id,
                meta_template_name=meta_name,
                empty_vars=empty_params,
                context_keys=list(contexto.keys()),
                msg="Empty params detected — Meta would reject the send. "
                    "Verify that MetaParamOrder matches ContextoJson keys.",
            )
            async with pool.acquire() as conn:
                await finalize_no_template(conn, row)
            return None
        return OutboxRecord(
            outbox_id=row.id,
            cliente_id=row.cliente_id,
            tipo=row.tipo,
            phone=row.phone_number,
            intentos=row.intentos,
            phone_log=mask_phone(row.phone_number),
            mensaje=f"[Template: {meta_name}]",
            is_template=True,
            meta_template_name=meta_name,
            meta_language_code=plantilla["meta_language_code"] or "es",
            meta_params=params,
        )

    if meta_name and not hsm_ok:
        log.warning("worker.template_not_approved", tipo=row.tipo,
                    outbox_id=row.id, meta_template_name=meta_name,
                    hsm_status=plantilla["hsm_status"])

    mensaje, missing = render(plantilla["texto"], contexto)
    if missing:
        log.warning("worker.template_missing_vars", tipo=row.tipo,
                    outbox_id=row.id, missing_vars=missing)

    return OutboxRecord(
        outbox_id=row.id,
        cliente_id=row.cliente_id,
        tipo=row.tipo,
        phone=row.phone_number,
        intentos=row.intentos,
        phone_log=mask_phone(row.phone_number),
        mensaje=mensaje,
    )
