from prometheus_client import Counter, Gauge, Histogram, start_http_server

SWIPES_TOTAL = Counter("heart3_swipes_total", "Total processed profiles", labelnames=["decision"])
MATCHES_TOTAL = Counter("heart3_matches_total", "Total mutual matches")
HANDOFFS_TOTAL = Counter("heart3_chat_handoffs_total", "Total chat handoffs")
API_ERRORS_TOTAL = Counter("heart3_api_errors_total", "API errors by status code", labelnames=["status_code"])
CV_INFERENCE_SECONDS = Histogram("heart3_cv_inference_seconds", "Vision/face inference duration")
WORKER_ACTIVE = Gauge("heart3_worker_active", "Worker status (1=running, 0=stopped)")


def start_metrics_server(port: int = 8000) -> None:
    start_http_server(port)
