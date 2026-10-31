"""Runs the Alembic migrations shipped inside the package, so the container image can migrate."""

from alembic import command
from alembic.config import Config


def upgrade_to_head(database_url: str) -> None:
    """Bring the schema up to date. Blocking: call it outside a running event loop."""
    config = Config()
    config.set_main_option("script_location", "llm_gateway.storage:migrations")
    # Passed as an attribute, not a main option: ini values treat "%" in passwords as interpolation.
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")
