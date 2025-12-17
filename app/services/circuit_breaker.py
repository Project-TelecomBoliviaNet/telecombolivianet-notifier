"""
FIX-23: Circuit Breaker simple para Meta API.
Implementación sin dependencias externas que cumple el contrato del plan.

Estados:
  CLOSED   → operación normal, se cuentan fallos
  OPEN     → falla rápido (sin llamar a Meta) hasta que reset_timeout expire
  HALF-OPEN → después del timeout, deja pasar un intento de prueba
"""
import time
import asyncio
from typing import Callable, Awaitable, TypeVar

T = TypeVar("T")


class CircuitBreakerError(Exception):
    """Lanzada cuando el circuito está abierto."""


class CircuitBreaker:
    """
    Implementación thread-safe de circuit breaker para corrutinas asyncio.

    Args:
        fail_max:       fallos consecutivos para abrir el circuito.
        reset_timeout:  segundos de espera antes de intentar de nuevo.
        name:           nombre del circuito (para logging).
    """

    def __init__(self, fail_max: int = 5, reset_timeout: int = 60, name: str = "circuit") -> None:
        self.fail_max      = fail_max
        self.reset_timeout = reset_timeout
        self.name          = name
        self._fail_count   = 0
        self._opened_at: float | None = None
        self._lock         = asyncio.Lock()

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        return (time.monotonic() - self._opened_at) < self.reset_timeout

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "CLOSED"
        if self.is_open:
            return "OPEN"
        return "HALF-OPEN"

    async def call_async(self, fn: Callable[..., Awaitable[T]], *args, **kwargs) -> T:
        async with self._lock:
            if self.is_open:
                raise CircuitBreakerError(
                    f"[{self.name}] Circuito abierto. "
                    f"Reintentando en {self.reset_timeout}s"
                )
            # HALF-OPEN: reset counters to give one trial
            if self.state == "HALF-OPEN":
                self._fail_count = 0
                self._opened_at  = None

        try:
            result = await fn(*args, **kwargs)
            async with self._lock:
                self._fail_count = 0
                self._opened_at  = None
            return result
        except Exception:
            async with self._lock:
                self._fail_count += 1
                if self._fail_count >= self.fail_max:
                    self._opened_at = time.monotonic()
            raise
