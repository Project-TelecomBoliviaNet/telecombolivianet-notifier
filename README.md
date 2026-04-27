# TelecomBoliviaNet — Worker de Notificaciones

Proceso Python independiente que consume la cola `notif_outbox` de PostgreSQL
y envía mensajes de WhatsApp vía la API de Meta (Graph API v18.0).

> **Repositorio independiente.** No comparte código con el monolito .NET.
> La única conexión entre ambos es la base de datos PostgreSQL.

---

## Estructura del repositorio

```
telecom-notif-worker/
├── app/
│   ├── db/
│   │   ├── connection.py     # Pool asyncpg a PostgreSQL
│   │   └── queries.py        # Todo el SQL del worker
│   ├── jobs/
│   │   ├── outbox_worker.py  # Polling outbox + envío WhatsApp
│   │   └── reminder_job.py   # Job diario recordatorios R1/R2/R3
│   └── services/
│       ├── whatsapp.py       # Cliente Meta Graph API v18
│       └── template.py       # Sustitución de {{variables}}
├── tests/
│   └── test_template.py
├── main.py
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── README.md
```

---

## Levantar en desarrollo

```bash
cp .env.example .env
docker compose up -d
docker compose logs -f notif_worker
```

Levanta PostgreSQL (puerto 5434), pgAdmin (http://localhost:5052) y el worker.

---

## Conectar con el monolito en desarrollo

El monolito tiene su postgres en el puerto `5433`. Para que compartan BD:

Edita `.env` apuntando al postgres del monolito:
```env
DATABASE_URL=postgresql://telecom_user:telecom_pass_2025@localhost:5433/telecombolivianet2
```

Luego levanta solo el worker:
```bash
docker compose up -d notif_worker
```

---

## Levantar en producción

```bash
# En .env, configurar BD real y credenciales WhatsApp:
DATABASE_URL=postgresql://user:pass@servidor.com:5432/telecombolivianet
WHATSAPP_TOKEN=EAABsbCS1234...
WHATSAPP_PHONE_NUMBER_ID=123456789012345

# Levantar solo el worker (sin postgres local)
docker compose up -d notif_worker
```

---

## Comandos útiles

```bash
docker compose logs -f notif_worker          # logs en tiempo real
docker compose restart notif_worker          # reiniciar (tras cambiar .env)
docker compose down                          # detener todo
docker compose exec postgres psql -U telecom_user -d telecombolivianet
```

---

## Variables de entorno

| Variable | Requerida | Descripción |
|---|---|---|
| `DATABASE_URL` | ✅ | Conexión PostgreSQL |
| `WHATSAPP_TOKEN` | ✅ prod | Token Bearer de Meta |
| `WHATSAPP_PHONE_NUMBER_ID` | ✅ prod | ID del número WhatsApp Business |
| `POLL_INTERVAL_SECONDS` | ❌ | Intervalo polling (default: 30s) |
| `TIMEZONE` | ❌ | Zona horaria (default: America/La_Paz) |
| `LOG_LEVEL` | ❌ | DEBUG/INFO/WARNING/ERROR (default: INFO) |

> Sin `WHATSAPP_TOKEN` el worker corre en **modo stub**: loguea sin enviar.

---

## Migraciones requeridas (US-04)

El ReminderJob ahora persiste su última ejecución en una tabla nueva.
Ejecutar en PostgreSQL antes de desplegar:

```sql
CREATE TABLE IF NOT EXISTS "NotifJobLog" (
    "JobName" TEXT PRIMARY KEY,
    "LastRun" TIMESTAMPTZ NOT NULL
);
```

---

## Ejecución manual del ReminderJob (US-05)

Para recuperar recordatorios perdidos por un reinicio:

```bash
python main.py --reminder-date 2025-06-08
```

El proceso ejecuta el ReminderJob para esa fecha y termina.

---

## Monitoreo (US-06 / US-07)

El worker expone dos endpoints HTTP (por defecto en puerto 8080):

| Endpoint   | Descripción |
|------------|-------------|
| `GET /healthz` | 200 si BD ok, 503 si no |
| `GET /metrics` | Contadores de mensajes enviados, fallidos y pendientes |

Configurable con `HEALTH_PORT` en `.env`.

---

## Variables de entorno completas (US-10)

| Variable | Requerida | Descripción |
|---|---|---|
| `DATABASE_URL` | ✅ siempre | Conexión PostgreSQL |
| `WHATSAPP_TOKEN` | ✅ prod | Token Bearer de Meta |
| `WHATSAPP_PHONE_NUMBER_ID` | ✅ prod | ID del número WhatsApp Business |
| `POLL_INTERVAL_SECONDS` | ❌ | Intervalo polling (default: 30s) |
| `TIMEZONE` | ❌ | Zona horaria (default: America/La_Paz) |
| `LOG_LEVEL` | ❌ | DEBUG/INFO/WARNING/ERROR (default: INFO) |
| `HEALTH_PORT` | ❌ | Puerto de /healthz y /metrics (default: 8080) |
| `WHATSAPP_API_VERSION` | ❌ | Versión Meta API (default: v19.0) |

Sin `WHATSAPP_TOKEN` el worker corre en **modo stub**: loguea sin enviar.

---

## Ejecutar tests

```bash
pytest tests/ -v
```
