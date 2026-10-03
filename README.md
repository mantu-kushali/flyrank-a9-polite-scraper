# The polite scraper (FlyRank · Backend Track · W5 · A9)

A small scraping pipeline: **fetch → extract → normalize → validate → store → report**.
It downloads the first 3 catalogue pages of Books to Scrape, visits all 60 book pages, and saves clean, checked JSON.
It skips a broken page without crashing and ends every run with a report.

## Target classification

- **Site:** https://books.toscrape.com/ (part of toscrape.com)
- **Why it is allowed:** toscrape.com says on its own page that it is a sandbox built for people to practise scraping. That is my permission. This is the only kind of site this project touches.
- **How much:** the first 3 catalogue pages only, which gives 60 book pages.
- **What I collect:** title, product URL, price, availability, star rating, description, plus where and when each record was fetched.
- **robots.txt result:** `TODO: run python src/main.py --robots and paste what it printed here.`
  (If the site has no robots file, write "no robots file found". A missing file is not permission, it is just a missing file.)

I will not reuse this code on another site without checking its rules and terms first.

## Lane

Python lane: Python 3.10+, Requests, Beautiful Soup, Pydantic, built-in `json`.

## Run it (about 2 minutes)

```bash
git clone <this-repo-url>
cd <repo>/scraper
python -m venv .venv
# Windows:  .venv\Scripts\activate        macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt

python src/main.py
```

Output appears in `output/`: `books.json`, `errors.json`, `run-report.json`.

Other commands:

```bash
python src/main.py --robots     # Stage 0: request robots.txt once
python src/main.py --bad-url    # Stage 5: add one fake URL to prove the run survives it
python -m pytest                # unit tests (offline)
```

The first run prints `FETCH` for each page. A second run prints `CACHE HIT` and gives the same 60 records.
Delete the `cache/` folder to download fresh copies.

## Record schema

| Field | Type | Notes |
|---|---|---|
| `title` | text | required |
| `product_url` | text | absolute `https://` URL, the record's identity (canonical URL) |
| `price_text` | text | raw, e.g. `"£51.77"` |
| `price_gbp` | number | clean value, e.g. `51.77` |
| `availability_text` | text | e.g. `"In stock (22 available)"` |
| `rating_text` | text | e.g. `"Three"` |
| `description` | text or `null` | the only optional field |
| `source_page` | text | catalogue page where the book was found (provenance) |
| `fetched_at` | text | UTC time the page was fetched (provenance) |

The schema is a Pydantic model (`Book` in `src/main.py`). A record that fails goes to `output/errors.json` with the reason, and never into `books.json`.

## Politeness rules

- **User-agent:** `FlyRankInternshipA9/1.0 (+link to this repo)`, so a site owner can see who I am.
- **Delay:** at least 0.6 s between real requests. Cached pages need no delay.
- **Timeout:** 10 s, then the request gives up.
- **Status check:** only `200` counts as a page. Anything else is a failed fetch.
- **Cache:** every page is saved in `cache/` and reused on the next run. `cache/` is not committed.
- **Retries:** one retry after a timeout, connection problem or 5xx. Never for 404 or 403.

## Proof: one real run report

```json
TODO: paste your real output/run-report.json here
```

## Why no browser?

The data is already in the HTML the server sends, so a browser would only add cost.

## One honest limitation

The selectors depend on this site's current HTML (`article.product_page`, `div.product_main`, ...). If the site changes its layout, the scraper will report invalid records instead of silently storing wrong data, but it will need new selectors.

## Ethics note

`TODO: rewrite this in your own words.` Draft: Use an official API when one exists. Never bypass logins, paywalls or blocks. Collect only what you need, go slowly, and say who you are.

## Project layout

```
scraper/
├── src/main.py          the whole pipeline
├── tests/test_scraper.py  unit tests + offline full-run test
├── cache/               saved HTML (git-ignored)
├── output/              books.json, errors.json, run-report.json
├── requirements.txt
└── README.md
```
