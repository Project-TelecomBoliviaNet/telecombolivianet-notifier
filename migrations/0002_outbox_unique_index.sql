-- FIX-12: índice único parcial para evitar recordatorios duplicados
-- entre instancias concurrentes del ReminderJob.
--
-- La condición WHERE "EstadoFinal" IS NULL limita el índice solo a registros
-- pendientes/en-proceso: una vez que el outbox se marca como ENVIADO o FALLIDO
-- puede volver a crearse un recordatorio para la misma factura en el futuro.
--
-- Este índice habilita ON CONFLICT ("ClienteId", "Tipo", "ReferenciaId")
-- WHERE "EstadoFinal" IS NULL en INSERT_OUTBOX_SAFE de queries.py.
CREATE UNIQUE INDEX IF NOT EXISTS uq_outbox_client_tipo_ref
    ON "NotifOutbox" ("ClienteId", "Tipo", "ReferenciaId")
    WHERE "EstadoFinal" IS NULL;
