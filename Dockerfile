FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 app \
    && mkdir /app/data && chown app:app /app/data
COPY agents/ agents/
COPY ai/ ai/
COPY bot/ bot/
COPY core/ core/
COPY database/ database/
COPY device/ device/
COPY filters/ filters/
COPY web/ web/
COPY config.py main.py ./
USER app
CMD ["python", "main.py"]
