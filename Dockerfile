FROM python:3.12-slim

WORKDIR /app

# Dependencias del sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    tzdata \
 && rm -rf /var/lib/apt/lists/*

# Zona horaria Bolivia
ENV TZ=America/La_Paz

# Instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código fuente
COPY . .

# Usuario no root por seguridad
RUN useradd -m -u 1000 worker
USER worker

CMD ["python", "main.py"]
