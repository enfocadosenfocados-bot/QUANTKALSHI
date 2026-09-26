# QUANT KALSHI - imagen de la app (Python 3.12: el codigo usa datetime.UTC, 3.11+)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencias primero: si no cambian, esta capa se reutiliza.
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

# Usuario sin privilegios. docker-compose.yml lo sobreescribe con el UID/GID del
# usuario dueno del checkout para que el estado en JSON no quede como root.
RUN useradd --create-home --uid 10001 quant \
    && mkdir -p /app/logs \
    && chown -R quant:quant /app
USER quant

# Publica en todas las interfaces dentro del contenedor; el mapeo de puertos del
# host decide si es accesible (por defecto 127.0.0.1:8000 -> tunel SSH).
EXPOSE 8000

# /api/health responde mientras el motor de datos esta vivo.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=8)"

CMD ["python", "main.py"]
