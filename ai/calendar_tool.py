import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from langchain_core.tools import tool

from config import settings


def _setting_secret(name: str) -> str | None:
    return getattr(settings, name).get_secret_value() or None


@tool
async def book_dating_event_tool(
    slot_iso_string: str, venue_name: str, girl_name: str
) -> str:
    """Бронирует встречу на 90 минут; время без часового пояса считается московским."""
    try:
        if not all(value.strip() for value in (slot_iso_string, venue_name, girl_name)):
            raise ValueError("Blank event input")
        moscow = ZoneInfo("Europe/Moscow")
        start_time = datetime.fromisoformat(slot_iso_string.strip())
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=moscow)
        else:
            start_time = start_time.astimezone(moscow)
        end_time = (
            start_time.astimezone(timezone.utc) + timedelta(minutes=90)
        ).astimezone(moscow)
    except (ValueError, TypeError):
        return "Ошибка календаря: укажите дату в формате ISO, имя и место встречи."

    event = {
        "summary": f"Свидание: {girl_name.strip()}",
        "description": f"Встреча в заведении: {venue_name.strip()}",
        "start": {"dateTime": start_time.isoformat(), "timeZone": "Europe/Moscow"},
        "end": {"dateTime": end_time.isoformat(), "timeZone": "Europe/Moscow"},
    }

    def create_event() -> dict:
        creds = Credentials(
            token=_setting_secret("google_access_token"),
            refresh_token=_setting_secret("google_refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=_setting_secret("google_client_id"),
            client_secret=_setting_secret("google_client_secret"),
        )
        service = build("calendar", "v3", credentials=creds)
        return service.events().insert(calendarId="primary", body=event).execute()

    try:
        created = await asyncio.to_thread(create_event)
        return f"Забронировано! Ссылка: {created.get('htmlLink')}"
    except Exception:
        return "Ошибка календаря: не удалось забронировать встречу. Попробуйте позже."
