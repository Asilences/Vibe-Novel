"""Read-only source extractors. Imported content remains untrusted reference material."""

from .text import EXTRACTOR_VERSION, ParsedText, parse_text_source

__all__ = ["EXTRACTOR_VERSION", "ParsedText", "parse_text_source"]
