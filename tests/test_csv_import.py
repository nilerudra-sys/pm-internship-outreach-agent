from datetime import date

import pytest

from services.discovery.csv_import import import_preview, parse_date, preview_csv, template_csv

TODAY = date(2026, 11, 17)

GOOD = """Company,Website,Title,Job Description,URL,Location,Remote,Source,Date
Acme,acme.io,Product Intern,Own the roadmap,https://acme.io/jobs/1,"Pune, India",WFH,LinkedIn,2026-11-10
Globex,https://globex.com,APM Intern,,https://globex.com/careers/apm,Remote,remote,google,10/11/2026
"""


def test_template_round_trips(conn):
    # The template is stamped with the real current date, so validate it against the real date too.
    preview = preview_csv(conn, template_csv(), date.today())
    assert preview.count("new") == 1
    assert not preview.missing_columns and not preview.unknown_columns


def test_header_aliases_and_values(conn):
    preview = preview_csv(conn, GOOD.encode("utf-8-sig"), TODAY)
    assert preview.count("new") == 2 and preview.count("invalid") == 0
    acme = preview.rows[0].lead
    assert acme.source == "linkedin" and acme.remote_status == "remote" and acme.market == "india"
    assert acme.company_domain == "acme.io"
    assert preview.rows[1].lead.discovered_at == "2026-11-10"


@pytest.mark.parametrize("row, error", [
    (",https://a.com,PM Intern,,,,,,", "company_name is required"),
    ("Acme,,,,https://a.com/x,,,,", "job_title is required"),
    ("Acme,,PM Intern,,,,,,", "provide job_url or website"),
    ("Acme,,PM Intern,,acme.com/jobs,,,,", "job_url must be a full http(s) URL"),
    ("Acme,,PM Intern,,https://a.com/x,,sometimes,,", "remote_status must be"),
    ("Acme,,PM Intern,,https://a.com/x,,,monster,", "platform_source not recognised"),
    ("Acme,,PM Intern,,https://a.com/x,,,,yesterday", "discovery_date not understood"),
    ("Acme,,PM Intern,,https://a.com/x,,,,2027-01-01", "in the future"),
])
def test_invalid_rows(conn, row, error):
    header = "company_name,website,job_title,job_description,job_url,location,remote_status,platform_source,discovery_date\n"
    preview = preview_csv(conn, header + row + "\n", TODAY)
    assert preview.rows[0].status == "invalid"
    assert any(error in e for e in preview.rows[0].errors), preview.rows[0].errors


def test_duplicates_in_file_and_db(conn):
    csv_text = GOOD + "ACME Pvt Ltd,,Product Internship,,https://acme.io/jobs/1?utm_source=x,,,csv,\n"
    first = preview_csv(conn, csv_text, TODAY)
    assert first.rows[2].duplicate_of == "row 1"
    summary = import_preview(conn, first)
    assert (summary.created, summary.merged, summary.skipped_in_file_duplicates) == (2, 0, 1)

    again = preview_csv(conn, GOOD, TODAY)
    assert again.count("duplicate") == 2
    assert all(r.duplicate_of.startswith("opportunity #") for r in again.rows)
    summary = import_preview(conn, again)
    assert (summary.created, summary.merged) == (0, 2)
    assert conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 2


def test_missing_and_unknown_columns(conn):
    preview = preview_csv(conn, "Title,Notes\nPM Intern,hello\n", TODAY)
    assert "company_name" in preview.missing_columns
    assert "job_url or website" in preview.missing_columns
    assert preview.unknown_columns == ["Notes"]


def test_blank_lines_skipped_and_empty_file_rejected(conn):
    preview = preview_csv(conn, GOOD + ",,,,,,,,\n", TODAY)
    assert len(preview.rows) == 2
    with pytest.raises(ValueError):
        preview_csv(conn, "", TODAY)


def test_parse_date_formats():
    assert parse_date("2026-11-05") == "2026-11-05"
    assert parse_date("05/11/2026") == "2026-11-05"
    assert parse_date("5 Nov 2026") == "2026-11-05"
    assert parse_date("Nov 5, 2026") == "2026-11-05"
    assert parse_date("soon") is None
