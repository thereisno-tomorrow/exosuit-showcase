"""Separately gated Discord surfaces; credentials remain protected hub files."""
from dataclasses import dataclass
from pathlib import Path
import re
from urllib.parse import urlsplit

FLAGS = frozenset({"enabled", "card", "delivery", "ask_diablo", "auto_tidy", "mail_pings", "diablo_home"})
IDS = frozenset({"application_id", "owner_user_id", "guild_id", "morning_channel_id", "home_channel_id"})
PATHS = frozenset({"token_file", "state_file", "emoji_file"})
ALLOWED_KEYS = FLAGS | IDS | PATHS | {"owner_id", "dashboard_url"}


@dataclass(frozen=True, slots=True)
class DiscordSettings:
    enabled: bool = False
    card: bool = False
    delivery: bool = False
    ask_diablo: bool = False
    auto_tidy: bool = False
    mail_pings: bool = False
    diablo_home: bool = False
    owner_id: str = ""
    owner_user_id: str = ""
    guild_id: str = ""
    morning_channel_id: str = ""
    home_channel_id: str = ""
    application_id: str = ""
    token_file: Path | None = None
    state_file: Path | None = None
    emoji_file: Path | None = None
    dashboard_url: str = ""

    def validate(self):
        for key in FLAGS:
            if type(getattr(self, key)) is not bool:
                raise ValueError(f"[discord] {key} must be a boolean")
        for key in IDS:
            value = getattr(self, key)
            if not isinstance(value, str) or (value and not re.fullmatch(r"[0-9]{1,20}", value)):
                raise ValueError(f"[discord] {key} must be a numeric identifier stored as text")
        if not isinstance(self.owner_id, str) or not isinstance(self.dashboard_url, str):
            raise ValueError("[discord] owner_id and dashboard_url must be text")
        if self.dashboard_url:
            parsed = urlsplit(self.dashboard_url)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or parsed.path not in ("", "/", "/app")):
                raise ValueError("[discord] dashboard_url must be the dashboard's HTTPS address")
        if not self.enabled and any(getattr(self, key) for key in FLAGS - {"enabled"}):
            raise ValueError("[discord] feature gates require enabled")
        if self.enabled and not all((self.owner_id.strip(), self.owner_user_id, self.guild_id,
                                     self.application_id, self.token_file, self.state_file)):
            raise ValueError("[discord] enabled requires owner, user, guild, application and protected file paths")
        if self.card and not self.morning_channel_id:
            raise ValueError("[discord] card requires morning_channel_id")
        if (self.delivery or self.ask_diablo or self.auto_tidy) and not self.card:
            raise ValueError("[discord] delivery, ask_diablo and auto_tidy require card")
        if (self.mail_pings or self.diablo_home) and not self.home_channel_id:
            raise ValueError("[discord] mail_pings and diablo_home require home_channel_id")

    @classmethod
    def from_table(cls, table, *, base: Path):
        if table is None:
            return cls()
        if not isinstance(table, dict) or set(table) - ALLOWED_KEYS:
            raise ValueError("[discord] must contain only known settings")
        values = dict(table)
        for key in PATHS:
            if key in values:
                if not isinstance(values[key], str) or not values[key].strip():
                    raise ValueError(f"[discord] {key} must be a file path")
                path = Path(values[key])
                values[key] = (base / path).resolve() if not path.is_absolute() else path.resolve()
        result = cls(**values)
        result.validate()
        return result
