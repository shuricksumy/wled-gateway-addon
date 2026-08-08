FROM python:3.12-alpine
WORKDIR /app
RUN pip install --no-cache-dir aiohttp
COPY app.py .
EXPOSE 8099

LABEL \
    io.hass.name="WLED Gateway" \
    io.hass.description="Single endpoint for WLED live-preview fan-out" \
    io.hass.type="addon"

CMD ["python", "app.py"]
