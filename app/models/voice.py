"""Voice data model."""

from dataclasses import dataclass


def persona_name(short_name: str) -> str:
    """
    Human name for a voice ShortName, keeping the variant visible.

    "en-US-AndrewNeural"             → "Andrew"
    "en-US-AndrewMultilingualNeural" → "Andrew (Multilingual)"

    Stripping "Multilingual" outright made both of those read "Andrew", so the
    picker listed two identical entries that behave differently — the
    multilingual model is chunked and recovered differently on long jobs.
    """
    if short_name and short_name.startswith(("system:", "piper:")):
        from app.services.local_tts import local_display_name
        return local_display_name(short_name)
    persona = short_name.split("-")[-1] if short_name else short_name
    if persona.endswith("Neural"):
        persona = persona[: -len("Neural")]
    multilingual = persona.endswith("Multilingual")
    if multilingual:
        persona = persona[: -len("Multilingual")]
    persona = persona or short_name
    return f"{persona} (Multilingual)" if multilingual else persona


@dataclass(frozen=True)
class Voice:
    short_name: str      # e.g. "en-US-AvaNeural"
    friendly_name: str   # e.g. "Microsoft Ava Online (Natural) - English (United States)"
    locale: str          # e.g. "en-US"
    gender: str          # "Female" | "Male" | "" (unknown, some local voices)
    source: str = "online"   # "online" (Microsoft) | "system" (OS built-in) | "piper" (offline neural)

    @property
    def is_local(self) -> bool:
        """Runs on this computer — works offline, no Microsoft service."""
        return self.source != "online"

    @property
    def source_label(self) -> str:
        return {"system": "Built-in", "piper": "Offline"}.get(self.source, "Online")

    @property
    def persona(self) -> str:
        """'Ava', or 'Ava (Multilingual)' for the multilingual variant."""
        return persona_name(self.short_name)

    @property
    def display_name(self) -> str:
        """Short label for combobox: 'Ava · Female'"""
        return f"{self.persona} · {self.gender}" if self.gender else self.persona

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
            source=d.get("Source", "online") or "online",
        )

    def to_edge_dict(self) -> dict:
        """Inverse of from_edge_dict — used for the on-disk voice list cache."""
        return {
            "ShortName": self.short_name,
            "FriendlyName": self.friendly_name,
            "Locale": self.locale,
            "Gender": self.gender,
            "Source": self.source,
        }
