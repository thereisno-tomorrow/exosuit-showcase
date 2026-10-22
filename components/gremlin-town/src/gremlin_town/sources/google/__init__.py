"""Google Calendar and Gmail readers for one account (D10), and the calendar writer that adds
Exosuit's own calendar entries (N8, 28 Sep 2026). Gmail stays read-only."""

from .auth import GoogleAuthError, GoogleCredential, GoogleTokenProvider
from .calendar import CalendarReader
from .calendar_writer import CalendarEntry, CalendarWriteError, CalendarWriter, EventSlot
from .mail import MailReader
from .settings import GoogleSettings, GoogleSettingsError

__all__ = [
    "CalendarEntry",
    "CalendarReader",
    "CalendarWriteError",
    "CalendarWriter",
    "EventSlot",
    "GoogleAuthError",
    "GoogleCredential",
    "GoogleSettings",
    "GoogleSettingsError",
    "GoogleTokenProvider",
    "MailReader",
]
