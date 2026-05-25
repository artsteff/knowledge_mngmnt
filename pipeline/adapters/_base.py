"""Pluggable source adapter interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class NormalizedItem:
    """A piece of content from any source, ready for scoring."""
    source_id: str          # adapter-unique id (video_id, message_id, gmail msg id, url hash)
    url: str
    title: str
    author: str             # channel, sender, publication, etc.
    date: str               # ISO date the source was published or arrived
    raw_text: str           # transcript or article body, normalized to plain text
    source_type: str        # video | podcast | article | newsletter | forward | note
    source_meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class FetchResult:
    new_items: list[NormalizedItem]
    errors: list[str] = field(default_factory=list)


class SourceAdapter(ABC):
    """Each source (YouTube, Gmail, direct-link, ...) implements this."""

    name: str

    @abstractmethod
    def fetch(self, state: dict) -> FetchResult:
        """Return new items not present in state.

        State shape is per-adapter; callers persist it back to state/<name>.json.
        Adapters MUST update state['seen'] with the IDs they emit so subsequent
        runs skip them.
        """
