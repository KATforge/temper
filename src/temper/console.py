import sys
from typing import NoReturn

import questionary
import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.theme import Theme

out = Console(
    theme=Theme(
        {
            "accent": "bold #ff7a18",
            "error": "bold #ff3131",
            "muted": "#806a5a",
            "success": "#00d084",
            "warning": "#ffb020",
        }
    )
)


def header(title: str):
    out.print()
    out.print(f"[accent]{title}[/accent]")
    out.print()


def table(headers: list[str], rows: list[list[str]], right: set[int] | None = None):
    value = Table(box=box.ROUNDED, header_style="accent", border_style="muted")
    right = right or set()
    for index, title in enumerate(headers):
        value.add_column(title, justify="right" if index in right else "left")
    for row in rows:
        value.add_row(*row)
    out.print(value)


def success(message: str):
    out.print(f"[success]✓[/success] {message}")


def warning(message: str):
    out.print(f"[warning]{message}[/warning]")


def muted(message: str):
    out.print(f"[muted]{message}[/muted]")


def plain(message: str):
    out.print(message, markup=False, highlight=False)


def error(message: str):
    out.print(Panel(message, title="Error", title_align="left", border_style="error"))


def fatal(message: str) -> NoReturn:
    error(message)
    raise typer.Exit(1)


def confirm(message: str) -> bool:
    if not sys.stdin.isatty():
        fatal(f"Cannot prompt for '{message}' without a terminal; pass --yes")
    return bool(questionary.confirm(message).ask())


def interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except (OSError, ValueError):
        return False


def choose(message: str, values: list[str]) -> str:
    if not sys.stdin.isatty():
        fatal(f"Cannot prompt for '{message}' without a terminal")
    selected = questionary.select(message, choices=values).ask()
    if selected is None:
        raise typer.Exit(0)
    return str(selected)
