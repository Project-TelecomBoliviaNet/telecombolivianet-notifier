"""
Utilidades para enmascarar datos personales (PII) en logs.

BUG FIX: los números de teléfono de clientes se logueaban en texto plano.
Enmascara los datos preservando suficiente info para debugging.

Ejemplo:
    mask_phone("59176543210") → "591****210"
    mask_phone("76543210")    → "****3210"
"""


def mask_phone(phone: str) -> str:
    """Enmascara número dejando prefijo de país y últimos 3 dígitos."""
    if not phone:
        return "***"
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) >= 11:
        return f"{digits[:3]}****{digits[-3:]}"
    if len(digits) >= 4:
        return f"****{digits[-3:]}"
    return "***"
