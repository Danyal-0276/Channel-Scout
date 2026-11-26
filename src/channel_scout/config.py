import os
from pathlib import Path

from dotenv import dotenv_values


class ConfigError(Exception):
    """An actionable configuration error that does not include secret values."""


class Settings:
    def __init__(self, root: Path):
        self.root = root
        self.values = dotenv_values(root / ".env", interpolate=False)

    def get(self, name: str, default: str = "") -> str:
        value = os.environ.get(name, self.values.get(name, default))
        return (value if value is not None else default).strip()

    def require(self, name: str) -> str:
        value = self.get(name)
        if not value:
            raise ConfigError(f"Missing setting: {name}")
        return value

    def flag(self, name: str, default: bool = False) -> bool:
        value = self.get(name, str(default)).lower()
        if value not in {"true", "false"}:
            raise ConfigError(f"{name} must be true or false")
        return value == "true"

    def integer(self, name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(self.get(name, str(default)))
        except ValueError:
            value = -1
        if not minimum <= value <= maximum:
            raise ConfigError(f"{name} must be between {minimum} and {maximum}")
        return value

    def postgres(self, maintenance: bool = False) -> dict:
        return {
            "host": self.get("POSTGRES_HOST", "127.0.0.1"),
            "port": self.integer("POSTGRES_PORT", 5432, 1, 65535),
            "dbname": self.get("POSTGRES_DB", "channel_scout"),
            "user": (self.get("POSTGRES_MIGRATION_USER") if maintenance else "") or self.require("POSTGRES_USER"),
            "password": (self.get("POSTGRES_MIGRATION_PASSWORD") if maintenance else "") or self.require("POSTGRES_PASSWORD"),
            "connect_timeout": 5,
            "application_name": "channelscout",
        }
