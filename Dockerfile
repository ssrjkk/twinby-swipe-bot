FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1     PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY ai ./ai
COPY bot ./bot
COPY core ./core
COPY database ./database
COPY filters ./filters
COPY tools ./tools
COPY web ./web
COPY cli.py config.py main.py index.html ./

RUN pip install --no-cache-dir --upgrade pip &&     pip install --no-cache-dir . &&     useradd --create-home --uid 10001 heart3 &&     mkdir -p /app/data &&     chown -R heart3:heart3 /app

USER heart3

EXPOSE 8080 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3     CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/v1/health', timeout=3)" || exit 1

CMD ["python", "main.py"]
