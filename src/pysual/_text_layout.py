"""Bounded, opt-in reuse of host-specific measurements and fitted captions."""

from collections import OrderedDict


TextLayout = tuple[float, float] | str


class TextLayoutCache:
    """Owned explicitly by built-in hosts, never attached to arbitrary hosts.

    Entries are valid only for one metrics environment. The byte allowance is
    conservative: four bytes per retained character plus per-entry overhead.
    It covers both source text and fitted results, independently of entry count.
    """

    MAX_ENTRIES = 8192
    MAX_BYTES = 16 * 1024 * 1024
    MAX_TEXT = 8192

    def __init__(self) -> None:
        self._entries: OrderedDict[tuple[object, ...], tuple[TextLayout, int]] = OrderedDict()
        self._bytes = 0
        self._environment: tuple[object, ...] | None = None

    def clear(self) -> None:
        self._entries.clear()
        self._bytes = 0
        self._environment = None

    def prepare(self, environment: tuple[object, ...]) -> None:
        if environment != self._environment:
            self.clear()
            self._environment = environment

    def get(self, key: tuple[object, ...]) -> TextLayout | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._entries.move_to_end(key)
        return entry[0]

    def store(self, key: tuple[object, ...], value: TextLayout) -> None:
        cost = 256
        for part in key:
            if isinstance(part, str):
                length = len(part)
                if length > self.MAX_TEXT:
                    return
                cost += 4 * length
        if isinstance(value, str):
            length = len(value)
            if length > self.MAX_TEXT:
                return
            cost += 4 * length
        if cost > self.MAX_BYTES:
            return
        previous = self._entries.pop(key, None)
        if previous is not None:
            self._bytes -= previous[1]
        while self._entries and (
            len(self._entries) >= self.MAX_ENTRIES
            or self._bytes + cost > self.MAX_BYTES
        ):
            _, (_, released) = self._entries.popitem(last=False)
            self._bytes -= released
        self._entries[key] = (value, cost)
        self._bytes += cost
