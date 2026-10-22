"""Literal, line-based work capture with an explicit dependency choice."""
from .models import CardError


def capture_parts(message, order="independent"):
    if order not in {"ordered", "independent"}:
        raise CardError("Choose ordered or independent work. Nothing changed.")
    titles = [line.strip() for line in message.splitlines() if line.strip()]
    if not 1 <= len(titles) <= 20 or any(len(title) > 256 for title in titles):
        raise CardError("Enter up to 20 items, one per line, with each title under 257 characters.")
    return [{"title":title, **({"then":True} if order == "ordered" and number else {})}
            for number,title in enumerate(titles)]
