"""

ContextEnricher — builds template variable dictionaries from the database.

Mirrors NotifContextBuilderService.BuildAsync() so template variables
never arrive empty at the client.
"""
from datetime import datetime, timezone

import structlog

from app.db import queries as Q
from app.domain.db_types import Pool
from app.domain.records import RawOutboxRow

log = structlog.get_logger(__name__)

# Mirrors NotifContextBuilderService.cs month names
_MONTH_NAMES = [
    "", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]


def build_meses_deuda_detalle(invoices: list[dict]) -> str:
    """
    Formats overdue months for WhatsApp.
    Mirrors NotifContextBuilderService.BuildMesesDeudaDetalle().
    """
    if not invoices:
        return "_(Sin facturas pendientes)_"

    lines = []
    for inv in invoices:
        saldo  = float(inv["amount"]) - float(inv.get("amount_paid") or 0)
        tipo   = inv.get("type", "")
        month  = inv.get("month", 0)
        year   = inv.get("year", 0)

        if tipo == "Instalacion":
            label = "Instalación"
        elif 1 <= month <= 12:
            label = f"{_MONTH_NAMES[month]} {year}"
        else:
            label = str(year)

        due_date = inv["due_date"]
        due_str  = due_date.strftime("%d/%m/%Y") if hasattr(due_date, "strftime") else str(due_date)[:10]
        status   = inv.get("status", "")
        vence    = f" _(vencida {due_str})_" if status == "Vencida" else f" _(vence {due_str})_"
        lines.append(f"• *{label}* - Bs. {saldo:.2f}{vence}")

    return "\n".join(lines)


async def enrich_context_from_db(pool: Pool, row: RawOutboxRow, sys_config: dict) -> dict:
    """
    Builds the full context dict from DB when ContextoJson is missing or sparse
    (< 3 keys). Called only when parse_context() returns an incomplete context.
    """
    async with pool.acquire() as conn:
        client   = await conn.fetchrow(Q.FETCH_CLIENT_CONTEXT, row.cliente_id)
        invoices = await conn.fetch(Q.FETCH_INVOICES_FOR_CONTEXT, row.cliente_id)
        qr_row   = await conn.fetchrow(Q.FETCH_CLIENT_QR, row.cliente_id)

    if not client:
        log.warning("context_enricher.client_not_found", cliente_id=str(row.cliente_id))
        return {}

    invoices_list = [dict(i) for i in invoices]
    deuda_total   = sum(float(i["amount"]) - float(i.get("amount_paid") or 0) for i in invoices_list)
    primera       = invoices_list[0] if invoices_list else None

    dias_mora = 0
    if invoices_list:
        oldest_due = min(i["due_date"] for i in invoices_list)
        if hasattr(oldest_due, "date"):
            oldest_due = oldest_due.replace(tzinfo=timezone.utc)
            dias_mora  = max(0, (datetime.now(timezone.utc) - oldest_due).days)

    nombre_completo = client["nombre_completo"] or ""
    partes   = nombre_completo.split(" ", 1)
    nombre   = partes[0] if partes else nombre_completo
    apellido = partes[1] if len(partes) > 1 else ""
    empresa  = sys_config.get("ISP:NombreEmpresa", "TelecomBoliviaNet")

    qr_enlace = "Contáctenos para obtener su código QR de pago."
    if qr_row and qr_row["image_url"]:
        image_url = qr_row["image_url"]
        if image_url.startswith("http"):
            qr_enlace = image_url
        else:
            base = sys_config.get("ISP:BackendUrl", "").rstrip("/")
            qr_enlace = f"{base}/{image_url.lstrip('/')}" if base else image_url
    elif sys_config.get("ISP:PortalPagoUrl"):
        portal = sys_config["ISP:PortalPagoUrl"].rstrip("/")
        qr_enlace = f"{portal}/pay/{row.cliente_id}"

    if primera:
        tipo  = primera.get("type", "")
        month = primera.get("month", 0)
        year  = primera.get("year", 0)
        if tipo == "Instalacion":
            periodo = "Instalación"
        elif 1 <= month <= 12:
            periodo = f"{_MONTH_NAMES[month]} {year}"
        else:
            periodo = str(year)
        due_date   = primera["due_date"]
        fecha_venc = due_date.strftime("%d/%m/%Y") if hasattr(due_date, "strftime") else str(due_date)[:10]
        monto      = f"{float(primera['amount']) - float(primera.get('amount_paid') or 0):.2f}"
    else:
        periodo    = ""
        fecha_venc = ""
        monto      = "0.00"

    return {
        "nombre":              nombre,
        "apellido":            apellido,
        "nombre_completo":     nombre_completo,
        "deuda":               f"{deuda_total:.2f}",
        "monto":               monto,
        "periodo":             periodo,
        "fecha_vencimiento":   fecha_venc,
        "plan":                client.get("plan_name") or "",
        "zona":                client.get("zona") or "",
        "empresa":             empresa,
        "dias_mora":           str(dias_mora),
        "meses_mora":          str(dias_mora // 30),
        "meses_pendientes":    str(len(invoices_list)),
        "fecha_corte":         sys_config.get("ISP:FechaCorte", ""),
        "num_ticket":          "",
        "tecnico":             "",
        "fecha_visita":        "",
        "meses_deuda_detalle": build_meses_deuda_detalle(invoices_list),
        "qr_enlace":           qr_enlace,
    }
