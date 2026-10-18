class VibeNovelError(Exception):
    """An actionable input, storage, or integrity error safe to show to users."""


class MissingDocumentError(VibeNovelError):
    """A whitelisted document or a path-specific historical version is absent."""
