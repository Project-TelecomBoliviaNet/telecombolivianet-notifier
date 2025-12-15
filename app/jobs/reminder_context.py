"""
ReminderContext — builds template variable dictionaries for payment reminders.

Functions here are pure context-builders: they query the DB for the client and
invoice data needed to fill reminder template variables.  No scheduling or
persistence state lives here.
"""
from datetime import date
from typing import Optional

import structlog

from app.db import queries as Q
from app.domain.db_types import Connection
from app.jobs.context_enricher import build_meses_deuda_detalle

log = structlog.get_logger(__name__)

_MONTH_NAMES = [
    "", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]


def build_period_label(year: int, month: int, invoice_type: str) -> str:
    """'Mayo 2025' para mensualidades, 'Instalación' para instalaciones."""
    if invoice_type == "Instalacion":
        return "Instalación"
    return f"{_MONTH_NAMES[month]} {year}"


async def _fetch_all_pending_invoices(conn: Connection, cliente_id) -> list[dict]:
    rows = await conn.fetch(Q.FETCH_INVOICES_FOR_CONTEXT, cliente_id)
    return [dict(r) for r in rows]


async def _fetch_qr_enlace(conn: Connection, cliente_id, sys_config: dict) -> str:
    qr = await conn.fetchrow(Q.FETCH_CLIENT_QR, cliente_id)
    if qr and qr["image_url"]:
        image_url = qr["image_url"]
        if image_url.startswith("http"):
            return image_url
        base = sys_config.get("ISP:BackendUrl", "").rstrip("/")
        return f"{base}/{image_url.lstrip('/')}" if base else image_url
    portal = sys_config.get("ISP:PortalPagoUrl", "")
    if portal:
        return f"{portal.rstrip('/')}/pay/{cliente_id}"
    return "Contáctenos para obtener su código QR de pago."


async def build_reminder_context(
    conn: Connection,
    inv: dict,
    target_date: date,
    pending_months: int,
    sys_config: dict,
) -> dict:
    """
    Full context dict ready to substitute template variables for a reminder.
    Includes meses_deuda_detalle and qr_enlace.
    """
    all_invoices    = await _fetch_all_pending_invoices(conn, inv["cliente_id"])
    detalle         = build_meses_deuda_detalle(all_invoices)
    qr_enlace       = await _fetch_qr_enlace(conn, inv["cliente_id"], sys_config)
    empresa         = sys_config.get("ISP:NombreEmpresa", "TelecomBoliviaNet")
    nombre_completo = inv["nombre"] or ""
    partes          = nombre_completo.split(" ", 1)

    return {
        "nombre":              partes[0] if partes else nombre_completo,
        "apellido":            partes[1] if len(partes) > 1 else "",
        "nombre_completo":     nombre_completo,
        "monto":               f"{float(inv['amount']):.2f}",
        "deuda":               f"{sum(float(i['amount']) - float(i.get('amount_paid') or 0) for i in all_invoices):.2f}",
        "fecha_vencimiento":   target_date.strftime("%d/%m/%Y"),
        "periodo":             build_period_label(inv["year"], inv["month"], inv["type"]),
        "meses_pendientes":    str(pending_months or 1),
        "plan":                inv["plan_name"] or "internet",
        "empresa":             empresa,
        "zona":                "",
        "dias_mora":           "0",
        "meses_mora":          "0",
        "fecha_corte":         sys_config.get("ISP:FechaCorte", ""),
        "num_ticket":          "",
        "tecnico":             "",
        "fecha_visita":        "",
        "meses_deuda_detalle": detalle,
        "qr_enlace":           qr_enlace,
    }
