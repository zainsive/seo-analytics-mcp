"""Scope sets. Read-only is the default set, on purpose.

A stranger installing an SEO tool that immediately asks for permission to modify
their Search Console properties will reasonably decline.
"""

from __future__ import annotations

GSC_READ = "https://www.googleapis.com/auth/webmasters.readonly"
GSC_WRITE = "https://www.googleapis.com/auth/webmasters"
GA4_READ = "https://www.googleapis.com/auth/analytics.readonly"

DESCRIPTIONS = {
    GSC_READ: "read Search Console performance and index data",
    GSC_WRITE: "submit sitemaps to Search Console",
    GA4_READ: "read GA4 reports and property lists",
}


def for_read() -> set[str]:
    return {GSC_READ, GA4_READ}


def for_write() -> set[str]:
    """The write set is a superset: Google's webmasters scope covers reads too, but
    both are requested so a downgrade to read-only later stays coherent."""
    return for_read() | {GSC_WRITE}


def requested(enable_writes: bool) -> list[str]:
    """Sorted for a stable consent screen and a stable token file."""
    return sorted(for_write() if enable_writes else for_read())


def satisfies(held: set[str] | list[str] | None, required: set[str]) -> bool:
    held_set = set(held or [])
    if GSC_WRITE in held_set:
        held_set.add(GSC_READ)  # the write scope implies read access
    return required <= held_set


def missing(held: set[str] | list[str] | None, required: set[str]) -> list[str]:
    held_set = set(held or [])
    if GSC_WRITE in held_set:
        held_set.add(GSC_READ)
    return sorted(required - held_set)
