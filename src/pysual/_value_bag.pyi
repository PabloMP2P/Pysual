from collections.abc import Mapping
from typing import Literal, overload

class _ValueBag(dict[str, object]):
    @overload
    def __getitem__(
        self,
        key: Literal[
            "left",
            "top",
            "min_width",
            "min_height",
            "margin",
            "flex",
            "padding",
            "spacing",
            "scroll_x",
            "scroll_y",
        ],
    ) -> float: ...
    @overload
    def __getitem__(
        self, key: Literal["width", "height", "max_width", "max_height"]
    ) -> float | None: ...
    @overload
    def __getitem__(
        self,
        key: Literal[
            "enabled",
            "visible",
            "cache_paint",
            "cache_measure",
            "focusable",
        ],
    ) -> bool: ...
    @overload
    def __getitem__(self, key: Literal["anchor"]) -> str: ...
    @overload
    def __getitem__(
        self,
        key: Literal[
            "grid_row",
            "grid_col",
            "grid_row_span",
            "grid_col_span",
            "tab_index",
            "columns",
        ],
    ) -> int: ...
    @overload
    def __getitem__(
        self, key: Literal["dock"]
    ) -> Literal["left", "top", "right", "bottom", "fill"]: ...
    @overload
    def __getitem__(
        self, key: Literal["layout"]
    ) -> Literal["absolute", "stack", "grid", "flow", "dock"]: ...
    @overload
    def __getitem__(
        self, key: Literal["direction"]
    ) -> Literal["vertical", "horizontal"]: ...
    @overload
    def __getitem__(
        self, key: Literal["column_tracks", "row_tracks"]
    ) -> tuple[float | str, ...]: ...
    @overload
    def __getitem__(self, key: str) -> object: ...

def _replaced_bag(current: Mapping[str, object], updates: Mapping[str, object]) -> _ValueBag: ...
