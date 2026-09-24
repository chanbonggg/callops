"""Explicit settings: no dotenv loading or implicit production defaults."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

REPOSITORY = Path(__file__).resolve().parents[2]


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    data_root: Path
    host: str
    database: str
    role: str

    @classmethod
    def from_env(cls, env=None, *, migration=False):
        env = os.environ if env is None else env
        key = "CALLOPS_MIGRATION_DATABASE_URL" if migration else "CALLOPS_DATABASE_URL"
        role = "callops_migrator" if migration else "callops_app"
        dsn = env.get(key, "").strip()
        root = env.get("CALLOPS_DATA_ROOT", "").strip()
        if not dsn or not root:
            raise ConfigurationError(f"{key} and CALLOPS_DATA_ROOT are required")
        try:
            url = urlsplit(dsn)
            query = dict(parse_qsl(url.query, strict_parsing=True))
            if (url.scheme not in {"postgres", "postgresql"} or url.username != role
                    or not url.password or not url.hostname or not url.port
                    or not url.path.startswith("/") or len(url.path) <= 1
                    or "/" in url.path[1:] or url.fragment
                    or set(query) != {"sslmode"}
                    or query["sslmode"] not in {"disable", "verify-full"}
                    or (url.hostname not in {"127.0.0.1", "localhost", "::1"}
                        and query["sslmode"] != "verify-full")):
                raise ValueError
            path = Path(root)
            if not path.is_absolute():
                raise ValueError
            path = path.resolve()
            if path == path.parent or path.is_relative_to(REPOSITORY) or REPOSITORY.is_relative_to(path):
                raise ValueError
            if path.exists() and not path.is_dir():
                raise ValueError
        except (ValueError, OSError):
            raise ConfigurationError("Invalid database settings or data root (values withheld)") from None
        return cls(dsn, path, url.hostname, url.path[1:], role)
