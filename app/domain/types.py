"""
Tipos de dominio del worker.

TimeValue: asyncpg puede retornar la hora de las columnas TIME de PostgreSQL
como datetime.time (cuando el driver la parsea) o como str "HH:MM:SS"
(en ciertas configuraciones). Se define un Union para tipar correctamente
las funciones que reciben este valor sin perder información.
"""
from datetime import time
from typing import Union

# Tipo para columnas TIME de PostgreSQL devueltas por asyncpg.
# En la práctica siempre es datetime.time, pero se documenta el Union
# para reflejar que la función acepta ambas formas.
TimeValue = Union[time, str]
