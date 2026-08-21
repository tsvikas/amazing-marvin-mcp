"""Configuration, from environment variables (prefix ``MARVIN_``) or a ``.env`` file.

All credentials come from Marvin's API strategy settings (Strategies → API).
"""

from pathlib import Path

import platformdirs
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_NAME = "amazing-marvin-mcp"


def default_config_dir() -> Path:
    """Return the per-user config directory (holds ``workflow.md``)."""
    return Path(platformdirs.user_config_dir(APP_NAME))


def default_cache_dir() -> Path:
    """Return the per-user cache directory (holds the mirrored database)."""
    return Path(platformdirs.user_cache_dir(APP_NAME))


def _default_workflow_path() -> Path:
    """``workflow/`` directory if the user made one, else ``workflow.md``."""
    directory = default_config_dir() / "workflow"
    return directory if directory.is_dir() else default_config_dir() / "workflow.md"


class Settings(BaseSettings):
    """Runtime settings.

    Reads are served from a local mirror of the CouchDB sync database, so the
    ``sync_*`` credentials are required for anything beyond a connectivity test.
    Writes use Marvin's REST API: ``api_token`` for create/mark-done,
    ``full_access_token`` for editing existing documents.
    """

    # Later files win: a `.env` in the working directory overrides the per-user one.
    model_config = SettingsConfigDict(
        env_prefix="MARVIN_",
        env_file=(str(default_config_dir() / ".env"), ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    api_token: SecretStr | None = Field(default=None, description="apiToken")
    full_access_token: SecretStr | None = Field(
        default=None, description="fullAccessToken; enables update_task"
    )
    sync_server: str | None = Field(default=None, description="syncServer URL")
    sync_database: str | None = Field(default=None, description="syncDatabase name")
    sync_user: SecretStr | None = Field(default=None, description="syncUser")
    sync_password: SecretStr | None = Field(default=None, description="syncPassword")

    workflow_file: Path = Field(
        default_factory=_default_workflow_path,
        description="Markdown (file, or directory of sections) describing how *this* "
        "user works; exposed to the model",
    )
    cache_dir: Path = Field(default_factory=default_cache_dir)

    # Marvin asks for <=1 query / 3s and <=1440 queries / day (REST and CouchDB alike).
    min_request_interval: float = Field(default=3.0, ge=0)
    # How old the mirror may be before a read triggers a `_changes` poll.
    mirror_max_age: float = Field(default=60.0, ge=0)

    @property
    def can_sync(self) -> bool:
        """Whether the CouchDB mirror can be populated."""
        return all(
            (self.sync_server, self.sync_database, self.sync_user, self.sync_password)
        )

    @property
    def can_write(self) -> bool:
        """Whether create/mark-done tools are available."""
        return self.api_token is not None

    @property
    def can_edit(self) -> bool:
        """Whether update tools (``/api/doc/update``) are available."""
        return self.full_access_token is not None
