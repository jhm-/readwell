# Readwell

A self-hosted news reader backend. It turns websites, RSS feeds, government publications and email newsletters into clean Atom feeds, then uses an LLM to filter them down to the topics you care about. You read the results in any ordinary RSS reader.

> **Status:** pre-alpha. Phase 1 (feeds, discovery, dedupe, `all.atom`, `health.atom`) works; filtering comes in Phase 3.

## What it does

- **Follows any source.** That includes regular RSS/Atom feeds, sites with no feed (point and click to define one), numbered official documents such as orders-in-council, periodical PDFs such as gazettes, and newsletters that only arrive by email (including paid Substack posts).
- **Gets the whole article.** If a feed only carries a snippet, Readwell fetches the page and extracts the full text. PDFs are converted to text, with OCR for scans.
- **Removes duplicates.** The same story syndicated across several outlets appears once, with every source listed.
- **Filters by topic with an LLM.** You describe your topics in plain language. Each item is scored against them and lands only in the feeds where it scores high enough. Every item carries a one-line reason ("Why it's here: …").
- **Learns from you.** 👍/👎 links in each item become examples that sharpen the filter.
- **Stays within a budget.** A cheap keyword prefilter runs first and every LLM call is logged. A hard daily spending cap stops calls once it's reached, and anything over the cap waits for a later pass instead of being dropped.
- **Sends articles to PDF.** A link in every item renders it as a clean PDF into a folder of your choice (for an e-ink tablet, for example).

## How it works

```
 sources ──► fetch ──► normalize ──► dedupe ──► full text ──► keyword prefilter ──► LLM screen ──► Atom feeds ──► your RSS reader
 (RSS, listings, OICs,          (canonical URLs,   (trafilatura,                      (batched,     (per topic, combined
  gazettes, email)               near-dup titles)   PDF/OCR)                           cached)       views, all, rejected)
```

Readwell runs as two processes on a small Linux server: a **worker**, which fetches and processes, and a **web** process, which serves the feeds and a small management UI. State is kept in SQLite. Sources, topics and views are YAML files, so they're easy to edit by hand and keep under version control.

## Source kinds

| Kind | For | New item when |
|---|---|---|
| `feed` | Sites with RSS/Atom (found automatically from any page URL) | A new feed entry |
| `listing` (stream) | News sites with no feed: a recipe picks items from the page | A link not seen before |
| `listing` (inventory) | Catalogue pages such as asset-sale listings | A listing appears (and, optionally, disappears) |
| `numbered` | Sequentially numbered documents at predictable URLs | The next number exists |
| `documents` | Index pages or periodical PDFs (e.g. gazettes), split into individual instruments | A new document or instrument |
| `email` | An IMAP mailbox of newsletters and alerts | A new message |

**Substack:** free posts come in through the publication's `/feed`. Paid posts arrive in full by email, and Readwell merges the two copies so the full version wins.

## Filtering

Each source chooses how it's screened:

| `filter.mode` | Keyword prefilter | LLM | Use for |
|---|---|---|---|
| `topics` *(default)* | ✓ | ✓ | General news |
| `llm` | – | ✓ | Low-volume, high-value sources (official orders, regulators) |
| `keywords` | ✓ | – | High-volume, low-value sources |
| `none` | – | – | General-interest reading you want unfiltered |

Sources can also limit which topics they're screened against, override thresholds, add their own keywords, mute terms, or cap items per day. **Views** combine topics and sources into a single feed, for example "every topic match, plus everything tagged `general-interest`".

## Output

| Feed | Contents |
|---|---|
| `/feeds/topic/<id>.atom` | Items matching one topic |
| `/feeds/view/<id>.atom` | A combined view |
| `/feeds/all.atom` | Everything, de-duplicated and unfiltered |
| `/feeds/rejected.atom` | What the filter turned down, for auditing |
| `/feeds/health.atom` | Broken recipes, failing sources, budget events |
| `/opml` | All of the above, for one-click import into a reader |

Any RSS reader works, for example Fluent Reader or RSS Guard on Windows, or NetNewsWire or Reeder on Apple devices.

## Paywalls

Readwell uses full text from the publisher's feed where it exists, extracts the text from the page where the publisher serves it, and can use your own cookies for sites you subscribe to. Anything still paywalled is tagged 🔒 with a link to an existing archive.today snapshot. Readwell doesn't impersonate browsers or search-engine crawlers, and it doesn't scrape archive services.

## Optional: daily editor pass

Once a day, Readwell can export borderline and over-budget items to a shared folder. An external agent, such as a scheduled Claude task, reviews them there and writes back its decisions and a short brief, which Readwell publishes as a `daily-brief` feed. If the pass doesn't run, nothing breaks.

## Configuration

Your configuration is private and lives outside this repo. The path is set by `READWELL_CONFIG_DIR`. Examples are in [`examples/`](examples):

```
config.yaml          # server, fetching, dedupe, retention (every key optional)
sources/*.yaml       # sources: one per file, a list, or {defaults, sources}
topics.yaml          # topic profiles: description, include, exclude, keywords
views.yaml           # combined output feeds
```

Secrets (API keys, the mailbox password, cookie files) go in an environment file readable only by the service user, never in YAML.

## Install (Arch Linux)

```bash
git clone https://github.com/jhm-/readwell.git ~/readwell-src
sudo ~/readwell-src/deploy/install.sh     # re-run any time to update
readwell check                            # find every source's feed
```

The script creates a `readwell` system user, installs into `/opt/readwell` with a uv-managed Python, keeps state in `/var/lib/readwell`, writes `/etc/readwell/env`, and enables `readwell-web`, `readwell-worker` and a nightly backup timer. It never runs `pacman` itself. If the config share isn't mounted, it prints the fstab line to add.

## Requirements

- Linux with systemd
- Python 3.13+, managed by `uv`, so system Python upgrades can't break it
- A Gemini API key (other LLM backends are pluggable)
- Optional: Docker, for the headless browser used by JavaScript-heavy sites, and `ocrmypdf` for scanned PDFs

## Roadmap

1. **Skeleton:** feeds, auto-discovery, dedupe, `all.atom`, `health.atom`
2. **Full text and documents:** extraction, paywall tagging, numbered and gazette sources, listings, email/Substack
3. **Screening:** topics, per-source filters, views, feedback, budget, send-to-PDF
4. **Daily editor pass**
5. **Recipe Builder:** point-and-click feeds for sites without RSS, plus a send-to-PDF bookmarklet

## License

[MIT](LICENSE)
