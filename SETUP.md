# Simple News: developer setup

This is the technical setup and build-pipeline reference. If you're just
looking to understand the site as a visitor, see [README.md](README.md)
instead.

## How it works

- `scripts/fetch_news.py` reads `scripts/sources.json` for its config and
  pulls from three sources, writing `data/news.json`:
  - **Reddit** - r/LocalLLaMA top posts (day + week), via the public
    `.json` endpoint. Many cloud/CI IPs (including GitHub Actions runners)
    get blocked from this endpoint. If that happens, set the repo secrets
    `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET` (free, create a
    "script" type app at https://www.reddit.com/prefs/apps) and the script
    will use authenticated OAuth instead, which is far more reliable.
    Reddit has also introduced a "Responsible Builder Policy" gate on new
    app creation; if you hit that, the site still runs fine on Hacker News
    and release feeds alone while you sort out access.
  - **Hacker News** - the official Algolia API (`hn.algolia.com`), searched
    across a set of local-LLM-related queries. No key needed, no scraping.
  - **Release feeds** - official GitHub `releases.atom` feeds for
    llama.cpp, ollama, vLLM, text-generation-webui, koboldcpp, mlx-lm, and
    sglang, plus the Hugging Face blog RSS feed. Capped at 3 most recent
    entries per feed so a chatty per-commit release cadence (llama.cpp)
    doesn't flood a section.

  A DuckDuckGo HTML scrape was tried as a fourth source and dropped: it
  returned ~0 usable results in practice (DDG's anti-bot challenge blocks
  most automated requests) and scraping a search engine's results page
  is out of step with its terms of service. Not worth the risk for a
  source that wasn't contributing anything.
- Items are deduplicated by URL, categorized into **model / security /
  harness** via keyword matching (see `keywords` in `scripts/sources.json`),
  and ranked with an HN-style gravity formula (`score / (age_hours + 2)^1.8`)
  so fast-rising items surface over stale high-scorers.
- `scripts/evaluate_truthiness.py` optionally labels each item with a small
  local open-weight model for a "truthiness" signal, a direct green/yellow/red
  categorical pick rather than a 0-100 score (small models don't produce
  meaningfully calibrated continuous scores, see
  [methodology.html](methodology.html) for the full, unhidden methodology:
  exact model, exact prompt, exact reasoning).
- `.github/workflows/update-news.yml` runs both scripts every 6 hours (and
  on manual dispatch), commits `data/news.json`, the day's archive
  snapshot, and `feed.xml` if they changed, and files a GitHub issue if a
  run comes back with zero items across the board.
- `index.html` / `assets/app.js` fetch that JSON client-side and render
  the three columns, with a client-side filter box per column and a
  read/unread dim tracked only in the visitor's own `localStorage` (never
  sent anywhere). No build step, no server, pure static site.
- `archive.html` browses the last 90 days of daily snapshots
  (`data/archive/*.json`); `feed.xml` is a combined RSS 2.0 feed of the
  current items for anyone who wants to subscribe instead of checking back.
- The bar chart at the bottom of the home page reads `data/history.json`
  (item counts per section per day, last 30 days), built by
  `write_history()` in `fetch_news.py` from the same archive snapshots.
  It's plain CSS divs sized by inline `height: %`, no charting library.
  Clicking a bar opens that day in the archive.
- `robots.txt` and `sitemap.xml` point at the real Pages URL
  (`https://twinkites.github.io/simple-llm-news/`). If you ever move this
  to a different repo name or a custom domain, update `SITE_URL` in
  `scripts/fetch_news.py` and the URLs in both files to match.
- `disclaimer.html` and `privacy.html` are static pages, no data pipeline
  involved. All three fonts (`assets/fonts/*.woff2`) are self-hosted, not
  loaded from Google's CDN, so the privacy page's claims hold up: nothing
  third-party loads on page view. If you ever change fonts, re-fetch the
  new `.woff2` files and update the `@font-face` rules at the top of
  `assets/style.css` rather than adding a CDN `<link>` back in.

## Setup

1. Push this repo to GitHub.
2. In **Settings -> Pages**, set source to "Deploy from a branch",
   branch `main`, folder `/ (root)`.
3. (Optional but recommended) Add repo secrets `REDDIT_CLIENT_ID` and
   `REDDIT_CLIENT_SECRET` from a free Reddit script app, so the Reddit
   feed works reliably from GitHub's runners.
4. The `update-news` workflow will start running on its schedule. You can
   also trigger it manually from the Actions tab (`workflow_dispatch`).

## Local development

```
python3 scripts/fetch_news.py         # regenerate data/news.json, data/archive/*, feed.xml
python3 scripts/evaluate_truthiness.py  # optional: score truthiness (downloads a ~1GB model on first run)
python3 -m http.server 8000           # serve the site at http://localhost:8000
```

`evaluate_truthiness.py` requires `pip install llama-cpp-python huggingface_hub`.
It's best-effort: if the model can't load, it logs a warning and leaves
`data/news.json` untouched rather than failing the pipeline.

## Customizing

- Keyword lists, HN queries, and RSS feed URLs all live in
  `scripts/sources.json`. Categorization is first-match-wins in the order
  security -> harness -> model, so an item mentioning both "CVE" and
  "llama.cpp" lands in security.
- The truthiness model repo/filename are also in `scripts/sources.json`
  under `truthiness_model`.

---

&copy; Twin Kites LLC
