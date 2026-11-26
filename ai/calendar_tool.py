import logging
import os
from datetime import datetime, timedelta, timezone

try:
    from langchain_core.tools import tool
except ImportError:
    def tool(fn):  # type: ignore[misc]
        return fn

logger = logging.getLogger(__name__)


def _get_calendar_service():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=os.getenv("GOOGLE_ACCESS_TOKEN"),
        refresh_token=os.getenv("GOOGLE_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    )
    return build("calendar", "v3", credentials=creds)


async def list_busy_slots(days_ahead: int = 3) -> str:
    """Возвращает занятые слоты в Google Календаре на ближайшие N дней."""
    if not os.getenv("GOOGLE_ACCESS_TOKEN") and not os.getenv("GOOGLE_REFRESH_TOKEN"):
        return "Google Calendar не настроен (задайте GOOGLE_ACCESS_TOKEN / GOOGLE_REFRESH_TOKEN в .env)."
    try:
        service = _get_calendar_service()
        now = datetime.now(timezone.utc)
        end = now + timedelta(days=days_ahead)
        events = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=now.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
            .get("items", [])
        )
        if not events:
            return f"На ближайшие {days_ahead} дн. календарь полностью свободен."
        lines = [
            f"- {e['start'].get('dateTime', e['start'].get('date'))} — {e.get('summary', 'Занято')}"
            for e in events
        ]
        return "Занятые слоты:\n" + "\n".join(lines)
    except Exception as exc:
        logger.exception("Google Calendar: список занятости не получен")
        return "Не удалось прочитать календарь. Проверьте настройки Google Calendar."


async def book_dating_event(slot_iso_string: str, venue_name: str, girl_name: str) -> str:
    """Бронирует встречу в Google Календаре."""
    if not os.getenv("GOOGLE_ACCESS_TOKEN") and not os.getenv("GOOGLE_REFRESH_TOKEN"):
        return "Google Calendar не настроен (задайте GOOGLE_ACCESS_TOKEN / GOOGLE_REFRESH_TOKEN в .env)."
    try:
        service = _get_calendar_service()
        start_time = datetime.fromisoformat(slot_iso_string)
        end_time = start_time + timedelta(hours=1, minutes=30)
        event = {
            "summary": f"Свидание: {girl_name}",
            "description": f"Встреча в заведении: {venue_name}. Создано <3.",
            "start": {"dateTime": start_time.isoformat(), "timeZone": "Europe/Moscow"},
            "end": {"dateTime": end_time.isoformat(), "timeZone": "Europe/Moscow"},
            "reminders": {"useDefault": True},
        }
        created = service.events().insert(calendarId="primary", body=event).execute()
        return f"Забронировано! Ссылка: {created.get('htmlLink')}"
    except Exception as exc:
        logger.exception("Google Calendar: бронь не создана")
        return "Не удалось создать встречу. Проверьте настройки Google Calendar."


@tool
async def list_busy_slots_tool(days_ahead: int = 3) -> str:
    """Возвращает занятые слоты в Google Календаре на ближайшие N дней для подбора времени свидания."""
    return await list_busy_slots(days_ahead=days_ahead)


@tool
async def book_dating_event_tool(slot_iso_string: str, venue_name: str, girl_name: str) -> str:
    """Бронирует встречу в Google Календаре."""
    return await book_dating_event(slot_iso_string=slot_iso_string, venue_name=venue_name, girl_name=girl_name)
