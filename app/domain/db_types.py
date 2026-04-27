"""
Type aliases para objetos de asyncpg.

asyncpg.Pool y asyncpg.Connection son los tipos concretos, pero importarlos
directamente desde asyncpg en cada módulo crea un acoplamiento de importación
innecesario. Se definen aquí como aliases que se pueden cambiar fácilmente
si se migra a otro driver de BD.
"""
import asyncpg

# Pool de conexiones asyncpg
Pool = asyncpg.Pool

# Conexión individual asyncpg (dentro de un acquire())
Connection = asyncpg.Connection
