"""Tests run offline: they use small HTML fixtures, never the real website."""

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import main  # noqa: E402

PAGE = "https://books.toscrape.com/catalogue/page-1.html"
RAW_KEYS = {
    "title", "product_url", "price_text", "availability_text",
    "rating_text", "description", "source_page", "fetched_at",
}


# ---------- fixtures (tiny fake pages) ----------
def catalogue_html(hrefs, next_href=None):
    items = "".join(
        f'<li><article class="product_pod"><h3><a href="{h}" title="t">t</a></h3></article></li>'
        for h in hrefs
    )
    nxt = f'<ul class="pager"><li class="next"><a href="{next_href}">next</a></li></ul>' if next_href else ""
    return f'<ol class="row">{items}</ol>{nxt}'


def detail_html(title="A Light in the Attic", price="£51.77", description="A fun book.", availability=None):
    price_tag = f'<p class="price_color">{price}</p>' if price is not None else ""
    desc = (
        f'<div id="product_description" class="sub-header"><h2>Product Description</h2></div><p>{description}</p>'
        if description is not None else ""
    )
    availability = availability or '<i class="icon-ok"></i>\n   In stock (22 available)\n  '
    return f"""
    <article class="product_page">
      <div class="product_main">
        <h1>{title}</h1>
        {price_tag}
        <p class="instock availability">{availability}</p>
        <p class="star-rating Three"></p>
      </div>
      {desc}
    </article>
    <section class="related"><p class="price_color">£99.99</p></section>
    """


# ---------- the five required unit tests ----------
def test_price_normalization():
    assert main.parse_price("£51.77") == 51.77
    assert main.parse_price("£1,234.50") == 1234.5
    assert main.parse_price("") is None
    assert main.parse_price("free") is None
    assert main.parse_price(None) is None


def test_relative_urls_become_absolute():
    html = catalogue_html(["../book-a_1/index.html", "book-b_2/index.html"], next_href="page-3.html")
    links, next_url = main.parse_catalogue(html, "https://books.toscrape.com/catalogue/page-2.html")
    assert links == [
        "https://books.toscrape.com/book-a_1/index.html",
        "https://books.toscrape.com/catalogue/book-b_2/index.html",
    ]
    assert next_url == "https://books.toscrape.com/catalogue/page-3.html"


def test_missing_description_is_null_not_invented():
    raw = main.parse_detail(detail_html(description=None), "https://books.toscrape.com/x/index.html", PAGE, "2026-10-03T00:00:00Z")
    assert set(raw) == RAW_KEYS  # all eight keys are still there
    assert raw["description"] is None
    assert raw["price_text"] == "£51.77"  # not the "related books" price
    assert raw["availability_text"] == "In stock (22 available)"  # extra whitespace removed
    assert raw["rating_text"] == "Three"
    main.Book(**main.normalize(raw))  # still a valid record


def test_duplicate_urls_count_once():
    pairs = [("https://a/1", "p1"), ("https://a/2", "p1"), ("https://a/1", "p2")]
    unique = main.dedupe_urls(pairs)
    assert list(unique) == ["https://a/1", "https://a/2"]
    assert unique["https://a/1"] == "p1"  # first source page wins


def test_malformed_page_fails_validation_with_a_reason():
    raw = main.parse_detail(detail_html(price=None), "https://books.toscrape.com/x/index.html", PAGE, "2026-10-03T00:00:00Z")
    assert raw["price_text"] is None
    with pytest.raises(ValidationError) as info:
        main.Book(**main.normalize(raw))
    assert "price_text" in main.explain(info.value)


# ---------- extra checks ----------
def test_http_url_is_rejected():
    raw = main.parse_detail(detail_html(), "http://books.toscrape.com/x/index.html", PAGE, "2026-10-03T00:00:00Z")
    with pytest.raises(ValidationError):
        main.Book(**main.normalize(raw))


def test_cache_file_names():
    assert main.cache_path_for(PAGE).name == "catalogue-page-1.html"
    path = main.cache_path_for("https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html")
    assert path.parent.name == "details" and path.name == "a-light-in-the-attic_1000.html"


# ---------- whole pipeline, offline (fake website in memory) ----------
@pytest.fixture
def fake_site(monkeypatch, tmp_path):
    site = {}
    for page in (1, 2, 3):
        hrefs = [f"book-{page}-{i}_{page}{i}/index.html" for i in range(20)]
        site[f"https://books.toscrape.com/catalogue/page-{page}.html"] = catalogue_html(hrefs, f"page-{page + 1}.html")
        for h in hrefs:
            site[f"https://books.toscrape.com/catalogue/{h}"] = detail_html(title=h)

    def fake_download(self, url):
        if url in site:
            return site[url]
        raise main.FetchError("HTTP 404")

    monkeypatch.setattr(main.Fetcher, "_download", fake_download)
    monkeypatch.setattr(main, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path / "output")
    return tmp_path


def test_full_run_is_idempotent_and_survives_a_bad_page(fake_site):
    out = fake_site / "output"

    first = main.run()
    books_1 = json.loads((out / "books.json").read_text(encoding="utf-8"))
    assert (first["catalogue_pages"], first["discovered"], first["unique_urls"]) == (3, 60, 60)
    assert first["valid_records"] == 60 and len(books_1) == 60
    assert first["pages_fetched"] == 63 and first["cache_hits"] == 0
    assert all(isinstance(b["price_gbp"], float) and b["product_url"].startswith("https://") for b in books_1)

    second = main.run()  # rerun: same 60, mostly cache
    books_2 = json.loads((out / "books.json").read_text(encoding="utf-8"))
    assert books_2 == books_1 and len(books_2) == 60
    assert second["pages_fetched"] == 0 and second["cache_hits"] == 63

    third = main.run(bad_url="https://books.toscrape.com/catalogue/fake_0/index.html")
    books_3 = json.loads((out / "books.json").read_text(encoding="utf-8"))
    assert third["failed_pages"] == 1 and third["valid_records"] == 60 and len(books_3) == 60
    report = json.loads((out / "run-report.json").read_text(encoding="utf-8"))
    assert report["failed_pages"] == 1