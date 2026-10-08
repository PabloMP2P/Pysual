"""Filter and edit a sales grid, then update a chart from the same data.

Run: python examples/dashboard.py --backend window
"""

import argparse
from decimal import Context, Decimal, localcontext

from pysual import (
    App,
    Button,
    ChangeEvent,
    ChartSlice,
    DataGrid,
    DonutChart,
    GridColumn,
    GridEditEvent,
    GridRow,
    Label,
    TextBox,
)


def _revenue(row: GridRow) -> int | float:
    value = row.cells[2]
    return max(0, value) if type(value) is int or type(value) is float else 0


class Dashboard(App):
    def build(self):
        self.title = "Sales dashboard"
        self.width, self.height = 900, 760
        self.layout, self.padding, self.spacing = "stack", 16, 10
        self._source = tuple(
            GridRow(str(i), (name, region, amount, active))
            for i, (name, region, amount, active) in enumerate(
                (
                    ("Acacia", "North", 1200, True),
                    ("Birch", "South", 850, True),
                    ("Cedar", "North", 630, False),
                    ("Dahlia", "West", 1700, True),
                    ("Elm", "South", 950, None),
                    ("Fir", "West", 410, False),
                )
            )
        )
        self.heading = Label(text="Revenue by region", height=28, font_size=20)
        self.query = TextBox(
            placeholder="Filter account or region…",
            height=38,
            tooltip="Filtering uses a prepared tuple; clearing restores all source rows",
        )
        self.grid = DataGrid(
            columns=(
                GridColumn("account", "Account", 200),
                GridColumn("region", "Region", 150),
                GridColumn("revenue", "Revenue", 150, "number", True),
                GridColumn("active", "Active", 120, "bool", True),
            ),
            rows=self._source,
            flex=1,
            min_height=100,
            tooltip="Sort headers; drag a divider to resize; F2 edits Revenue or Active",
        )
        self.add_account = Button(text="Add account", height=38)
        self.chart = DonutChart(
            title="Revenue in the filtered rows", flex=1, min_height=150
        )
        self.status = Label(height=24, font_size=12)
        self.refresh()

    def refresh(self):
        query = self.query.text.strip().casefold()
        rows = tuple(
            row
            for row in self._source
            if not query or query in f"{row.cells[0]} {row.cells[1]}".casefold()
        )
        self.grid.set_data(columns=self.grid.columns, rows=rows)
        totals: dict[str, float] = {}
        maximum = max((_revenue(row) for row in rows), default=0)
        for row in rows:
            # This chart represents nonnegative contributions; grid cells retain
            # the exact scalar values even when an entry is empty or negative.
            region = str(row.cells[1])
            totals[region] = totals.get(region, 0.0) + (
                _revenue(row) / maximum if maximum else 0
            )
        self.chart.slices = tuple(
            ChartSlice(region, region, value)
            for region, value in sorted(totals.items())
        )
        # Slice values are normalized for stable geometry, not revenue totals.
        with localcontext(Context(prec=28)):
            total = sum((Decimal(str(_revenue(row))) for row in rows), Decimal(0))
            self.chart.center_text = format(total, ".4g")
        self.status.text = (
            f"{len(rows)} of {len(self._source)} accounts · "
            "Chart excludes negative revenue; edits stay in memory"
        )

    def query_on_changed(self, event: ChangeEvent[str]):
        self.refresh()

    def grid_on_edited(self, event: GridEditEvent):
        changed = self.grid.selected_row
        if changed is None or changed.key != event.row_key:
            changed = next(row for row in self.grid.rows if row.key == event.row_key)
        self._source = tuple(
            changed if row.key == changed.key else row for row in self._source
        )
        self.refresh()

    def add_account_on_click(self, event):
        if len(self._source) >= 20_000:
            return
        row = GridRow(
            str(len(self._source)),
            (f"Account {len(self._source) + 1}", "North", 0, None),
        )
        self._source = (*self._source, row)
        if not self.query.text.strip():
            self.grid.update_rows((row,))
        self.refresh()


def main():
    # Activate CLI/environment settings only when launching this script.
    from pysual import autoconfig  # noqa: F401

    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Use --backend NAME to choose a backend (default: window). "
            "Use --pysual-help for backend names and theme/scale settings."
        ),
    )
    parser.parse_args()
    Dashboard().run_blocking()


if __name__ == "__main__":
    main()
