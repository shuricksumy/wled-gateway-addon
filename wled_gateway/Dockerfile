FROM python:3.12-alpine
WORKDIR /app
# Bounded below 4.0: that major release changes APIs this app uses, and an
# unpinned install would pick it up silently on the next rebuild.
RUN pip install --no-cache-dir "aiohttp>=3.9,<4"
COPY app.py .
EXPOSE 8099

LABEL \
    io.hass.name="WLED Gateway" \
    io.hass.description="Single endpoint for WLED live-preview fan-out" \
    io.hass.type="addon"

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8099/', timeout=3)" || exit 1

CMD ["python", "app.py"]
