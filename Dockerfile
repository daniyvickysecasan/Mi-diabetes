# Imagen única para la API y el recolector (cambia el comando en docker-compose).
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY comun ./comun
COPY api ./api
COPY servicios ./servicios
COPY herramientas ./herramientas
COPY pwa ./pwa
RUN useradd --system --uid 10001 app
USER app
EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
