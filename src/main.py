"""FlyRank A9 - The polite scraper.

Pipeline: classify -> fetch -> extract -> normalize -> validate -> store -> report.
Target: Books to Scrape (a public practice sandbox), first 3 catalogue pages only.

Run:
    python src/main.py                 # full run (uses cache when available)
    python src/main.py --robots        # Stage 0: check robots.txt once
    python src/main.py --bad-url       # Stage 5: add one fake URL, prove the run survives
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, ValidationError, field_validator

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "cache"
OUTPUT_DIR = ROOT / "output"

SITE = "https://books.toscrape.com/"
START_URL = SITE + "catalogue/page-1.html"
ROBOTS_URL = SITE + "robots.txt"
MAX_CATALOGUE_PAGES = 3

# TODO: change REPO-NAME to the name of your public GitHub repo.
USER_AGENT = "FlyRankInternshipA9/1.0 (+https://github.com/mantukushali-cmyk/REPO-NAME)"
TIMEOUT_SECONDS = 10
MIN_DELAY_SECONDS = 0.6  # the assignment requires at least 0.5 s between real requests
RETRY_WAIT_SECONDS = 2

# Fails on YOUR machine (nothing listens on port 9), so we never hit the real site for the failure test.
DEFAULT_BAD_URL = "http://127.0.0.1:9/fake-book_0/index.html"


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def cache_path_for(url: str) -> Path:
    """Map a URL to a file inside cache/."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    name = parts[-1] if parts else "index.html"
    if name == "robots.txt":
        return CACHE_DIR / "robots.txt"
    if re.fullmatch(r"page-\d+\.html", name):
        return CACHE_DIR / f"catalogue-{name}"  # cache/catalogue-page-1.html
    slug = parts[-2] if name == "index.html" and len(parts) >= 2 else name
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", slug).removesuffix(".html")
    return CACHE_DIR / "details" / f"{slug}.html"


# --------------------------------------------------------------------------
# Stage 1 + 5: polite fetching (user-agent, timeout, status check, delay, cache, one retry)
# --------------------------------------------------------------------------
class FetchError(Exception):
    """A page could not be fetched. The reason is in the message."""


class Fetcher:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.last_request_at: Optional[float] = None
        self.pages_fetched = 0  # real network downloads
        self.cache_hits = 0

    def get(self, url: str):
        """Return (html_text, fetched_at). Reads the cache first, then the network."""
        path = cache_path_for(url)
        if path.exists():
            self.cache_hits += 1
            fetched_at = iso(datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc))
            print(f"CACHE HIT {url} ({path.stat().st_size} bytes)")
            return path.read_text(encoding="utf-8"), fetched_at

        text = self._download(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")  # only successful (200) pages are cached
        self.pages_fetched += 1
        print(f"FETCH {url} ({path.stat().st_size} bytes)")
        return text, iso(datetime.now(timezone.utc))

    def _wait_turn(self) -> None:
        if self.last_request_at is not None:
            wait = MIN_DELAY_SECONDS - (time.monotonic() - self.last_request_at)
            if wait > 0:
                time.sleep(wait)

    def _download(self, url: str) -> str:
        """Real request. Retries ONCE on timeout / connection problem / 5xx. Never on 404 or 403."""
        for attempt in (1, 2):
            self._wait_turn()
            try:
                response = self.session.get(url, timeout=TIMEOUT_SECONDS)
            except (requests.Timeout, requests.ConnectionError) as exc:
                self.last_request_at = time.monotonic()
                if attempt == 1:
                    print(f"RETRY {url} ({type(exc).__name__})")
                    time.sleep(RETRY_WAIT_SECONDS)
                    continue
                raise FetchError(f"{type(exc).__name__} (after one retry)") from exc

            self.last_request_at = time.monotonic()
            if response.status_code == 200:
                response.encoding = "utf-8"  # otherwise "£" can turn into "Â£"
                return response.text
            if response.status_code >= 500 and attempt == 1:
                print(f"RETRY {url} (HTTP {response.status_code})")
                time.sleep(RETRY_WAIT_SECONDS)
                continue
            raise FetchError(f"HTTP {response.status_code}")  # 404, 403, ... : do not retry
        raise FetchError("unreachable")  # pragma: no cover


# --------------------------------------------------------------------------
# Stage 2: find the book links
# --------------------------------------------------------------------------
def parse_catalogue(html: str, page_url: str):
    """Return (absolute book links, absolute next-page URL or None)."""
    soup = BeautifulSoup(html, "html.parser")
    links = [
        urldefrag(urljoin(page_url, a["href"]))[0]
        for a in soup.select("article.product_pod h3 a[href]")
    ]
    nxt = soup.select_one("li.next a[href]")
    next_url = urljoin(page_url, nxt["href"]) if nxt else None
    return links, next_url


def dedupe_urls(pairs):
    """pairs = [(book_url, source_page), ...]  ->  {book_url: source_page}, first one wins."""
    unique = {}
    for url, source in pairs:
        unique.setdefault(url, source)
    return unique


# --------------------------------------------------------------------------
# Stage 3: extract the raw record (aimed at the product area only)
# --------------------------------------------------------------------------
def text_of(tag) -> Optional[str]:
    if tag is None:
        return None
    text = " ".join(tag.get_text().split())  # collapse extra whitespace/newlines
    return text or None


def parse_detail(html: str, product_url: str, source_page: str, fetched_at: str) -> dict:
    """Return the 8 raw keys. Anything missing on the page is None (never invented)."""
    record = {
        "title": None,
        "product_url": product_url,
        "price_text": None,
        "availability_text": None,
        "rating_text": None,
        "description": None,
        "source_page": source_page,
        "fetched_at": fetched_at,
    }
    soup = BeautifulSoup(html, "html.parser")
    product = soup.select_one("article.product_page")  # ignores "related books", footer, etc.
    if product is None:
        return record

    box = product.select_one("div.product_main")
    if box is not None:
        record["title"] = text_of(box.select_one("h1"))
        record["price_text"] = text_of(box.select_one("p.price_color"))
        record["availability_text"] = text_of(box.select_one("p.availability"))
        rating = box.select_one("p.star-rating")
        if rating is not None:
            words = [c for c in rating.get("class", []) if c != "star-rating"]
            record["rating_text"] = words[0] if words else None

    header = product.select_one("#product_description")
    if header is not None:
        record["description"] = text_of(header.find_next_sibling("p"))
    return record


# --------------------------------------------------------------------------
# Stage 4: normalize + validate
# --------------------------------------------------------------------------
def parse_price(price_text: Optional[str]) -> Optional[float]:
    """'£51.77' -> 51.77.  Returns None if no number can be found."""
    if not price_text:
        return None
    match = re.search(r"\d+(?:\.\d+)?", price_text.replace(",", ""))
    return float(match.group()) if match else None


def normalize(raw: dict) -> dict:
    """Keep the raw text AND add the clean number next to it."""
    clean = dict(raw)
    clean["price_gbp"] = parse_price(raw.get("price_text"))
    return clean


class Book(BaseModel):
    """The shape of a finished record. Description is the only optional field."""

    title: str
    product_url: str
    price_text: str
    price_gbp: float
    availability_text: str
    rating_text: str
    description: Optional[str] = None
    source_page: str
    fetched_at: str

    @field_validator("title", "price_text", "availability_text", "rating_text")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("product_url", "source_page")
    @classmethod
    def must_be_https(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("URL must start with https://")
        return value

    @field_validator("price_gbp")
    @classmethod
    def not_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("price must not be negative")
        return value


def explain(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())


# --------------------------------------------------------------------------
# Stage 0: robots.txt check
# --------------------------------------------------------------------------
def check_robots() -> None:
    fetcher = Fetcher()
    try:
        text, _ = fetcher.get(ROBOTS_URL)
    except FetchError as exc:
        if "404" in str(exc):
            print("RESULT: no robots file found (HTTP 404). A missing file is not permission - it is just missing.")
        else:
            print(f"RESULT: robots.txt could not be fetched: {exc}")
        return
    print("RESULT: robots.txt found (HTTP 200). First lines:")
    for line in text.splitlines()[:15]:
        print("   ", line)


# --------------------------------------------------------------------------
# The whole run (Stages 1-5)
# --------------------------------------------------------------------------
def run(start_url: str = START_URL, bad_url: Optional[str] = None) -> dict:
    started = datetime.now(timezone.utc)
    t0 = time.monotonic()
    fetcher = Fetcher()
    failures = []  # {"url": ..., "reason": ...}

    # Stage 2: follow the catalogue's own "next" link, stop after 3 pages.
    found = []  # (book_url, source_page)
    catalogue_pages = 0
    page_url = start_url
    while page_url and catalogue_pages < MAX_CATALOGUE_PAGES:
        try:
            html, _ = fetcher.get(page_url)
        except FetchError as exc:
            failures.append({"url": page_url, "reason": str(exc)})
            print(f"FAILED {page_url}: {exc}")
            break
        catalogue_pages += 1
        links, next_url = parse_catalogue(html, page_url)
        found.extend((link, page_url) for link in links)
        page_url = next_url

    unique = dedupe_urls(found)
    print(f"catalogue_pages={catalogue_pages} discovered={len(found)} unique_urls={len(unique)}")

    if bad_url:  # Stage 5 test: one deliberately broken URL
        unique.setdefault(bad_url, start_url)

    # Stages 3 + 4: one page at a time, so one bad page cannot kill the run.
    valid = {}  # product_url -> record  (the URL is the identity, so no duplicates)
    errors = []
    detail_pages = 0
    sample_printed = False
    for url, source_page in unique.items():
        try:
            html, fetched_at = fetcher.get(url)
        except FetchError as exc:
            failures.append({"url": url, "reason": str(exc)})
            print(f"FAILED {url}: {exc}")
            continue
        detail_pages += 1

        try:
            raw = parse_detail(html, url, source_page, fetched_at)
            if not sample_printed:
                print("SAMPLE RAW RECORD:")
                print(json.dumps(raw, indent=2, ensure_ascii=False))
                sample_printed = True
            book = Book(**normalize(raw))
        except ValidationError as exc:
            errors.append({"product_url": url, "reason": explain(exc), "raw": raw})
            print(f"INVALID {url}: {explain(exc)}")
            continue
        except Exception as exc:  # a parser bug on one page must not stop the other pages
            failures.append({"url": url, "reason": f"{type(exc).__name__}: {exc}"})
            print(f"FAILED {url}: {type(exc).__name__}: {exc}")
            continue
        valid[book.product_url] = book.model_dump()

    print(f"detail_pages={detail_pages}")

    # Stage 4: store. Files are overwritten each run, so a rerun gives the same records (idempotent).
    write_json(OUTPUT_DIR / "books.json", list(valid.values()))
    write_json(OUTPUT_DIR / "errors.json", errors)

    # Stage 5: the report
    report = {
        "start_time": iso(started),
        "duration_seconds": round(time.monotonic() - t0, 2),
        "catalogue_pages": catalogue_pages,
        "discovered": len(found),
        "unique_urls": len(dedupe_urls(found)),
        "pages_fetched": fetcher.pages_fetched,
        "cache_hits": fetcher.cache_hits,
        "valid_records": len(valid),
        "invalid_records": len(errors),
        "failed_pages": len(failures),
        "failures": failures,
    }
    write_json(OUTPUT_DIR / "run-report.json", report)
    print(
        f"valid_records={report['valid_records']} invalid_records={report['invalid_records']} "
        f"failed_pages={report['failed_pages']} duration={report['duration_seconds']}s"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="FlyRank A9 - The polite scraper")
    parser.add_argument("--robots", action="store_true", help="Stage 0: request robots.txt once and print the result")
    parser.add_argument(
        "--bad-url",
        nargs="?",
        const=DEFAULT_BAD_URL,
        default=None,
        help="Stage 5: add one deliberately broken URL to the list",
    )
    args = parser.parse_args()

    if args.robots:
        check_robots()
    else:
        run(bad_url=args.bad_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())