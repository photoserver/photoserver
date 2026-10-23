import logging

import typer

from photoserver.config import get_settings
from photoserver.logging_setup import configure_logging

app = typer.Typer(no_args_is_help=True, help="Photo server administration.")
logger = logging.getLogger(__name__)


@app.callback()
def main() -> None:
    """Runs before every command."""
    configure_logging(get_settings().log_level)


@app.command()
def info() -> None:
    """Show the effective configuration."""
    settings = get_settings()
    for name, value in settings.model_dump().items():
        typer.echo(f"{name:20} {value}")
    logger.debug("Configuration loaded")
