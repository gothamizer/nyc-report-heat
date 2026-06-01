from nyc_report_heat.urltools import detect_format, filename_from_url, normalize_url


def test_normalize_url_drops_tracking_and_normalizes_host() -> None:
    url = "http://www1.nyc.gov/assets/report.pdf?utm_source=x&keep=1"
    assert normalize_url(url) == "https://www.nyc.gov/assets/report.pdf?keep=1"


def test_detect_format_handles_html_pages_and_pdfs() -> None:
    assert detect_format("https://www.nyc.gov/assets/report.pdf") == "pdf"
    assert detect_format("https://rules.cityofnewyork.us/rule/something") == "html"


def test_filename_from_url() -> None:
    assert filename_from_url("https://example.com/path/report.pdf?x=1") == "report.pdf"
