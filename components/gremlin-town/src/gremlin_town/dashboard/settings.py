"""All dashboard features start off. No credentials are values in TOML."""
from dataclasses import dataclass
import ipaddress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ALLOWED_KEYS = frozenset({"enabled", "reads", "writes", "ask_diablo", "host", "port", "origin",
                          "rp_id", "owner_id", "state_file", "session_secret_file"})


@dataclass(frozen=True, slots=True)
class DashboardSettings:
    enabled: bool = False
    reads: bool = False
    writes: bool = False
    ask_diablo: bool = False
    host: str = "127.0.0.1"
    port: int = 8794
    origin: str = ""
    rp_id: str = ""
    owner_id: str = ""
    state_file: Path | None = None
    session_secret_file: Path | None = None

    def validate(self) -> None:
        for key in ("enabled", "reads", "writes", "ask_diablo"):
            if type(getattr(self, key)) is not bool:
                raise ValueError(f"[dashboard] {key} must be a boolean")
        if not isinstance(self.host, str) or not self.host.strip():
            raise ValueError("[dashboard] host must be non-empty text")
        try:
            loopback = ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ValueError("[dashboard] host must be a loopback IP address")
        if type(self.port) is not int or not 1024 <= self.port <= 65535:
            raise ValueError("[dashboard] port must be between 1024 and 65535")
        for key in ("origin", "rp_id", "owner_id"):
            if not isinstance(getattr(self, key), str):
                raise ValueError(f"[dashboard] {key} must be text")
        if self.origin:
            parsed = urlsplit(self.origin)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query
                    or parsed.fragment or parsed.username or parsed.password
                    or self.origin != f"https://{parsed.netloc}" or parsed.netloc.lower() != parsed.netloc):
                raise ValueError("[dashboard] origin must be one exact HTTPS origin")
            if self.rp_id and self.rp_id != parsed.hostname:
                raise ValueError("[dashboard] rp_id must equal the origin hostname")
        if self.enabled:
            if not all((self.origin, self.rp_id, self.owner_id.strip(), self.state_file, self.session_secret_file)):
                raise ValueError("[dashboard] enabled requires origin, rp_id, owner_id and both host file paths")
        elif self.reads or self.writes or self.ask_diablo:
            raise ValueError("[dashboard] feature gates require enabled")
        if (self.writes or self.ask_diablo) and not self.reads:
            raise ValueError("[dashboard] writes and ask_diablo require reads")

    @classmethod
    def from_table(cls, table: dict[str, Any] | None, *, base: Path) -> "DashboardSettings":
        if table is None:
            return cls()
        if not isinstance(table, dict) or set(table) - ALLOWED_KEYS:
            raise ValueError("[dashboard] must contain only known settings")
        values = dict(table)
        for key in ("state_file", "session_secret_file"):
            if key in values:
                if not isinstance(values[key], str) or not values[key].strip():
                    raise ValueError(f"[dashboard] {key} must be a file path")
                path = Path(values[key])
                values[key] = (base / path).resolve() if not path.is_absolute() else path.resolve()
        result = cls(**values)
        result.validate()
        return result
