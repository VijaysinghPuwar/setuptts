"""Voice data model."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Voice:
    short_name: str      # e.g. "en-US-AvaNeural"
    friendly_name: str   # e.g. "Microsoft Ava Online (Natural) - English (United States)"
    locale: str          # e.g. "en-US"
    gender: str          # "Female" | "Male"

    @property
    def display_name(self) -> str:
        """Short label for combobox: 'Ava · Female'"""
        # Extract the persona name from ShortName: "en-US-AvaNeural" → "Ava"
        parts = self.short_name.split("-")
        persona = parts[-1].replace("Neural", "").replace("Multilingual", "")
        return f"{persona} · {self.gender}"

    @property
    def language_tag(self) -> str:
        """First two chars of locale: 'en-US' → 'en'"""
        return self.locale.split("-")[0].lower()

    @classmethod
    def from_edge_dict(cls, d: dict) -> "Voice":
        return cls(
            short_name=d.get("ShortName", ""),
            friendly_name=d.get("FriendlyName", d.get("ShortName", "")),
            locale=d.get("Locale", ""),
            gender=d.get("Gender", ""),
        )
