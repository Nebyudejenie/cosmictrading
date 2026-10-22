"""Server-side SVG charts: marks wear the series colour by class and text the text colours,
every line of a price chart is named in a legend and at its end, and all text is escaped."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cosmic.web.charts import Guide, bar_chart, line_chart, price_chart

T0 = datetime(2026, 10, 20, 0, tzinfo=UTC)


def test_a_price_chart_names_its_lines_and_escapes_its_text() -> None:
    times = [T0 + timedelta(hours=i) for i in range(5)]
    closes = [100.0, 101.0, 102.0, 101.5, 103.0]
    buy = [None, 101.5, 102.5, 102.5, 102.5]
    svg = str(
        price_chart(
            times,
            closes,
            [Guide(buy, 103.5, "buy", "buys above 103.50", "buys <now>")],
            series="X <b>",
            marker=4,
            marker_text="bought 103",
            tips=[f"hour {i} <" for i in range(5)],
            label="Gold & more",
        )
    )
    assert '<ul class="legend">' in svg and "X &lt;b&gt;" in svg and "<b>" not in svg
    assert "buys &lt;now&gt;" in svg and 'class="key key-buy"' in svg and ">bought</li>" in svg
    assert svg.count('class="hit"') == 5 and "hour 0 &lt;" in svg
    assert svg.count('class="guide buy"') == 1  # the undefined first hour starts the line later
    assert "buys above 103.50" in svg and 'class="marker"' in svg and "bought 103" in svg
    assert 'aria-label="Gold &amp; more"' in svg and 'class="axis-text"' in svg
    assert 'fill="currentColor"' not in svg  # text never wears the series colour


def test_a_result_line_has_its_zero_line_and_its_last_value() -> None:
    times = [T0 + timedelta(days=i) for i in range(3)]
    svg = str(line_chart(times, [-5.0, 8.0, 12.0], baseline=0.0, end_label=True))
    assert svg.count('class="axis"') == 3  # two axes and the zero line
    assert 'class="end-dot"' in svg and ">+12.00</text>" in svg
    assert str(line_chart(times, [-5.0], empty="Too few.")) == '<p class="muted">Too few.</p>'


def test_bars_are_rounded_at_their_end_and_square_at_the_baseline() -> None:
    svg = str(bar_chart([10.0, -4.0], ["won", "lost"]))
    up, down = svg.split('<path class="bar-win" d="')[1], svg.split('<path class="bar-loss" d="')[1]
    assert " 0 0 1 " in up.split('"')[0] and " 0 0 0 " in down.split('"')[0]
    assert "won: +10.00" in svg and "lost: -4.00" in svg
