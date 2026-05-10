"""
Cliente HTTP para la API de WhatsApp Business de Meta (Graph API).

Responsabilidad única: enviar mensajes de texto vía la API de Meta.
La clase WhatsAppClient delega cada sub-tarea a métodos privados enfocados:
  - _build_url()       → construye la URL del endpoint
  - _build_payload()   → construye el cuerpo JSON del mensaje
  - _check_response()  → interpreta la respuesta HTTP (429, errores, éxito)

Principios aplicados:
  SRP — cada método hace exactamente una cosa
  DIP — token/phone_id inyectables por constructor (facilita tests)
  OCP — agregar nuevos tipos de mensaje (imagen, template) no requiere
        modificar la lógica de envío HTTP existente
"""
import os
import re
import structlog
import httpx

from app.services.circuit_breaker import CircuitBreaker, CircuitBreakerError  # noqa: F401

log = structlog.get_logger(__name__)

_GRAPH_BASE = "https://graph.facebook.com/{version}"

# FIX-23: circuito compartido a nivel de módulo — todas las instancias de
# WhatsAppClient comparten el estado del circuito (se abre si Meta cae).
_meta_breaker = CircuitBreaker(fail_max=5, reset_timeout=60, name="meta_api")


class RateLimitError(Exception):
    """Lanzada cuando Meta responde 429. Contiene retry_after en segundos."""

    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__(f"Rate limited by Meta. Retry after {retry_after}s")


class MetaApiUnavailableError(Exception):
    """Lanzada cuando el circuit breaker está abierto (Meta API no disponible)."""


def normalize_phone(phone: str) -> str:
    """
    Normaliza un número de teléfono al formato E.164 con prefijo Bolivia (591).

    Reglas aceptadas:
      - 11 dígitos que comienzan con 591 → se devuelve tal cual
      - 8 dígitos → se prefija con 591
      - Cualquier otro formato → lanza InvalidPhoneError

    Lanza:
      InvalidPhoneError — si el número no puede normalizarse
    """
    from app.domain.exceptions import InvalidPhoneError
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("591") and len(digits) == 11:
        return digits
    if len(digits) == 8:
        return f"591{digits}"
    raise InvalidPhoneError(phone)


class WhatsAppClient:
    """Cliente de larga vida para WhatsApp Business API de Meta (connection pooling)."""

    def __init__(
        self,
        token: str | None = None,
        phone_number_id: str | None = None,
        api_version: str | None = None,
    ) -> None:
        self._token           = token           or os.environ.get("WHATSAPP_TOKEN", "")
        self._phone_number_id = phone_number_id or os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
        self._api_version     = api_version     or os.environ.get("WHATSAPP_API_VERSION", "v19.0")
        self._stub_mode       = not self._token or not self._phone_number_id

        if self._stub_mode:
            log.warning("whatsapp.stub_mode",
                        reason="WHATSAPP_TOKEN or WHATSAPP_PHONE_NUMBER_ID not set")

        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )

    def _build_url(self) -> str:
        """Retorna la URL completa del endpoint de mensajes de Meta."""
        base = _GRAPH_BASE.format(version=self._api_version)
        return f"{base}/{self._phone_number_id}/messages"

    def _build_payload(self, phone: str, message: str) -> dict:
        """Construye el cuerpo JSON para un mensaje de texto libre (sesión 24h)."""
        return {
            "messaging_product": "whatsapp",
            "to":   normalize_phone(phone),
            "type": "text",
            "text": {"body": message},
        }

    def _build_template_payload(
        self, phone: str, template_name: str, language_code: str, params: list[str]
    ) -> dict:
        """
        Construye el cuerpo JSON para un Meta template message aprobado.
        Los parámetros son posicionales: params[0] → {{1}}, params[1] → {{2}}, etc.
        """
        components = []
        if params:
            components.append({
                "type": "body",
                "parameters": [{"type": "text", "text": p} for p in params],
            })
        return {
            "messaging_product": "whatsapp",
            "to":   normalize_phone(phone),
            "type": "template",
            "template": {
                "name":     template_name,
                "language": {"code": language_code},
                "components": components,
            },
        }

    def _check_response(self, response: httpx.Response, phone: str) -> None:
        """Interpreta la respuesta: lanza RateLimitError (429) o HTTPStatusError."""
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 60))
            log.warning("whatsapp.rate_limited", phone=phone,
                        retry_after_seconds=retry_after)
            raise RateLimitError(retry_after)

        if not response.is_success:
            log.error("whatsapp.api_error",
                      status=response.status_code,
                      body=response.text[:200])
            response.raise_for_status()

        log.info("whatsapp.sent", phone=normalize_phone(phone))

    async def _do_send_text(self, phone: str, message: str) -> None:
        response = await self._http.post(
            self._build_url(),
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type":  "application/json",
            },
            json=self._build_payload(phone, message),
        )
        self._check_response(response, phone)

    async def send_text(self, phone: str, message: str) -> None:
        """Envía texto vía circuit breaker. Lanza MetaApiUnavailableError si el circuito está abierto."""
        if self._stub_mode:
            log.info("whatsapp.stub_sent", phone=phone, preview=message[:60])
            return
        try:
            await _meta_breaker.call_async(self._do_send_text, phone, message)
        except CircuitBreakerError as exc:
            log.warning("whatsapp.circuit_open", reason=str(exc))
            raise MetaApiUnavailableError(str(exc)) from exc

    async def _do_send_template(
        self, phone: str, template_name: str, language_code: str, params: list[str]
    ) -> None:
        response = await self._http.post(
            self._build_url(),
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type":  "application/json",
            },
            json=self._build_template_payload(phone, template_name, language_code, params),
        )
        self._check_response(response, phone)

    async def send_template(
        self, phone: str, template_name: str, language_code: str, params: list[str]
    ) -> None:
        """
        Envía un Meta template message aprobado vía circuit breaker.
        Lanza MetaApiUnavailableError si el circuito está abierto.
        """
        if self._stub_mode:
            log.info("whatsapp.stub_template_sent", phone=phone,
                     template=template_name, params_count=len(params))
            return
        try:
            await _meta_breaker.call_async(
                self._do_send_template, phone, template_name, language_code, params
            )
        except CircuitBreakerError as exc:
            log.warning("whatsapp.circuit_open", reason=str(exc))
            raise MetaApiUnavailableError(str(exc)) from exc

    async def send(self, phone: str, message: str) -> None:
        """Alias de send_text() para cumplir el Protocol NotificationChannel."""
        await self.send_text(phone, message)

    async def close(self) -> None:
        """Cierra el cliente HTTP liberando las conexiones del pool."""
        await self._http.aclose()
