from parkrun_monitoring.fetch import parse_chart

CHART_HTML = """
data.addRows([
\t\t\t\t\t[ new Date("2014-03-01"), 2,103,13 ],
[ new Date("2014-03-08"), 2,64,NaN ],
[ new Date("2022-03-05"), NaN,NaN,NaN ],
]);
"""


def test_parse_chart_parses_rows_and_nan():
    stats = parse_chart(CHART_HTML)
    assert [s.week_date for s in stats] == ["2014-03-01", "2014-03-08"]
    assert stats[0].finishers == 103
    assert stats[0].volunteers == 13
    assert stats[1].volunteers is None


def test_parse_chart_skips_all_nan_rows():
    assert all(s.week_date != "2022-03-05" for s in parse_chart(CHART_HTML))
