"""
Time-window utilities for Bolivia timezone.
Extracted from outbox_worker (REFACTOR-04/SRP) — no repository dependencies.
"""
import pytz
from datetime import datetime, time as dt_time, timedelta
from typing import Union

BOLIVIA_TZ = pytz.timezone("America/La_Paz")


def now_bolivia() -> datetime:
    """Current time in Bolivia. Extracted so it can be mocked in tests."""
    return datetime.now(BOLIVIA_TZ)


def is_within_window(hora_inicio, hora_fin) -> bool:
    """True if current Bolivia time falls within [hora_inicio, hora_fin]."""
    now_time = now_bolivia().time().replace(second=0, microsecond=0)
    if hasattr(hora_inicio, "hour"):
        start = hora_inicio.replace(second=0, microsecond=0)
        end   = hora_fin.replace(second=0, microsecond=0)
    else:
        h, m  = str(hora_inicio)[:5].split(":")
        start = dt_time(int(h), int(m))
        h, m  = str(hora_fin)[:5].split(":")
        end   = dt_time(int(h), int(m))
    return start <= now_time <= end


def seconds_until_next_window(hora_inicio) -> int:
    """Seconds until the start of the next window (today or tomorrow)."""
    now = now_bolivia()
    if hasattr(hora_inicio, "hour"):
        h, m = hora_inicio.hour, hora_inicio.minute
    else:
        parts = str(hora_inicio)[:5].split(":")
        h, m  = int(parts[0]), int(parts[1])
    window = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if window <= now:
        window += timedelta(days=1)
    return max(1, int((window - now).total_seconds()))
