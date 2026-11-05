"""Published informational content; no operational IDs, prices or DB writes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape
from pathlib import Path

ASSETS = Path(__file__).parent / "assets" / "products"
CONTACT_NOTE = "По вопросам препарата напишите Герману лично."


@dataclass(frozen=True)
class ProductCard:
    slug: str
    title: str
    description: str
    composition: str
    photo_sha256: str

    @property
    def photo(self) -> Path:
        return ASSETS / f"{self.slug}.png"

    @property
    def text(self) -> str:
        return (
            f"{self.title}\n\n{self.description}\n\nСостав\n{self.composition}\n\n{CONTACT_NOTE}"
        )

    @property
    def html(self) -> str:
        return (
            '<img src="tg://photo?id=product"/>'
            f"<h2>{escape(self.title)}</h2>"
            f"<p>{escape(self.description)}</p>"
            f"<details><summary>Состав</summary><p>{escape(self.composition)}</p></details>"
            f"<p>{escape(CONTACT_NOTE)}</p>"
        )


PRODUCTS = {
    item["slug"]: ProductCard(**item)
    for item in json.loads((ASSETS / "catalog.json").read_text(encoding="utf-8"))
}
