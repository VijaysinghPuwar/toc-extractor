# TOC Extractor

Extracts chapter text from a table-of-contents page using CSS selectors **you**
supply. Nothing is hard-coded per site: you give it the TOC URL and three
selectors — chapter links, title, content — and it writes one file per chapter
plus a merged one. Scraping tutorials usually hard-code selectors for a single
site and break the week it redesigns; inverting that is the entire point.

Two front ends over one engine: a command line tool, and a Tk window whose
whole reason to exist is the step where you sign in or solve a challenge
yourself before anything is fetched.

## Requirements

- Python 3.11–3.14
- Playwright, the only runtime dependency
- Tk, for the GUI only — see [macOS notes](#macos-notes)

## Quickstart

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m playwright install chromium

python -m toc_extractor --profile profiles/example.toml --toc https://example.com/toc --dry-run
python -m toc_extractor --gui
```

`make setup` does the four setup steps and then tells you whether Tk is usable.

`--dry-run` lists the chapter URLs it found and writes nothing — the right first
command against any new site, because it tells you whether your link selector is
right before a single chapter is fetched.

## Permissions and ethics

Use this only on content you own or are permitted to access. Respect each site's
Terms of Service, its `robots.txt`, and its rate limits.

This is enforced in code, not asserted in a paragraph:

- **robots.txt is checked before fetching**, and a `Disallow` is a hard refusal.
  There is no `--ignore-robots` flag, deliberately: a flag that turns the check
  off gets copied between runs until the check means nothing. The only way past
  it is a real signed-in session established by hand in the GUI, and every rule
  that gets overridden is named, shown where it cannot be scrolled past, and
  recorded in the manifest.
- **`Crawl-delay` is honoured** and can only ever slow the tool down. A site
  asking to be hit faster than you configured does not get to.
- **The per-host delay survives concurrency.** Requests to one host stay spaced
  by the configured interval no matter how many workers run — see
  [Architecture](#architecture) for why that is not automatic.
- **No protection is bypassed.** No captcha solving, no fingerprint spoofing
  beyond a configurable User-Agent, no stealth plugins, no proxy rotation.
- **URLs found in a page are untrusted.** Non-HTTP schemes and hosts resolving
  to loopback, link-local, or private ranges are refused, across redirects too.

## Selector profiles

A profile is how you point this at a particular site. It is a file you own, not
code in this repository — which is what keeps the tool general.

```toml
[selectors]
link = "ol.toc a"          # every chapter anchor on the TOC page
title = "h1.title"         # the title element on a chapter page
content = "article.reader" # the readable container, not `body`

[options]
min_delay = 1.5
max_delay = 3.0
concurrency = 2
max = 25
formats = ["text", "jsonl"]
include_links = false
```

```bash
python -m toc_extractor --profile my-site.toml --toc https://example.com/toc
```

Flags override the profile, so it is a starting point rather than a file you
edit to run one different command. An unknown key refuses the whole profile and
lists the valid ones — a silently ignored typo is a profile that does not do
what it says.

`profiles/example.toml` is written against `tests/fixtures/`, so it runs as-is
and describes no real site.

### Deriving selectors for a site you have rights to use

1. `--dump-html --screenshot --dry-run` writes `toc.html` and `toc.png` to the
   output folder and fetches no chapters. If the screenshot shows a login wall,
   you need the GUI rather than better selectors.
2. Find the chapter links in `toc.html`. Start broad — `a` — and narrow until
   only chapters match. `--dry-run` prints exactly what your selector found.
3. Open one chapter page and check the content selector in DevTools:
   `document.querySelector('<selector>').innerText`. If that returns the whole
   page, you have selected `body` by another name.
4. Put the three in a profile and run without `--dry-run`.

## Output

```
downloads/
  001 - Chapter One.txt
  002 - Chapter Two.txt
  combined.txt
  manifest.jsonl
```

`--format` is repeatable: `text` (the default), `markdown`, and `jsonl`. The
JSONL manifest carries per-chapter URL, final URL after redirects, fetch time,
byte count, content hash, the number of URLs stripped from the text, and any
robots override — plus a summary line with every rejected link and why.

There is no EPUB exporter. It would need author, language, cover, and spine
order, none of which a selector-driven scraper has, so it could only invent
them. `--format markdown` then `pandoc book.md -o book.epub` does the same job
honestly.

URLs are stripped from body text by default, as in v1. That behaviour is
unchanged; what is new is that the count removed is reported, so the loss is
visible rather than silent. `--include-links` keeps them.

## Resume

Interrupt a run and start it again — resume is the default, keyed on URL. There
is no `--resume` flag, because a flag whose only job is to request the default
is one people forget, and the cost of forgetting is a few hundred redundant
requests to somebody else's server. `--force` discards saved progress.

A table of contents that grew is the normal case for a serial, so growth at
either end resumes and fetches only what is new. Removals or reordering refuse
with specifics, because chapter numbers would stop lining up with files already
written. If new chapters were added to the *start*, numbering reflects fetch
order rather than TOC order, and the tool says so rather than letting you find
out in the output folder.

## Command line reference

<!-- cli-reference: generated, do not edit by hand -->

```
usage: toc-extractor [-h] [--version] [--gui] [--profile PROFILE] --toc TOC
                     [--link LINK] [--title TITLE] [--content CONTENT]
                     [--max MAX] [--out OUT] [--include-links]
                     [--format FORMAT] [--no-strip-ads] [--dry-run] [--force]
                     [--dump-html] [--screenshot] [-v] [-q] [--ua UA]
                     [--storage-state STORAGE_STATE] [--headful]
                     [--timeout TIMEOUT] [--min-delay MIN_DELAY]
                     [--max-delay MAX_DELAY] [--retries RETRIES]
                     [--wait-after-load WAIT_AFTER_LOAD]
                     [--concurrency CONCURRENCY] [--allow-private-hosts]

Extract chapter text from a table-of-contents page using CSS selectors you supply. Use only on content you own or are permitted to access.

options:
  -h, --help            show this help message and exit
  --version             show program's version number and exit
  --gui                 Open the graphical front end instead of running from
                        the command line

source and selectors:
  --profile PROFILE     TOML file holding the three selectors and optional
                        defaults. Flags you pass explicitly override it. See
                        profiles/example.toml.
  --toc TOC             TOC URL (must start with http/https)
  --link LINK           CSS selector for chapter links on the TOC page
  --title TITLE         CSS selector for title on a chapter page
  --content CONTENT     CSS selector for content on a chapter page

output:
  --max MAX             Max chapters to fetch (default: 20)
  --out OUT             Output folder (default: downloads)
  --include-links       Include source URL in saved files
  --format FORMAT       Output format, repeatable (default: text). Choices:
                        jsonl, markdown, text. For EPUB, export markdown and
                        run: pandoc book.md -o book.epub
  --no-strip-ads        Do NOT strip common ad markers from text

behaviour:
  --dry-run             List discovered chapter URLs and exit
  --force               Discard any saved progress and start over (resume is
                        the default)

debugging:
  --dump-html           Save TOC HTML to out/toc.html
  --screenshot          Save TOC screenshot to out/toc.png
  -v, --verbose         Show debug output
  -q, --quiet           Show warnings and errors only

browser:
  --ua UA               Custom User-Agent string
  --storage-state STORAGE_STATE
                        Path to Playwright storage state JSON (reuses login)
  --headful             Run headed (GUI). Default is headless.
  --timeout TIMEOUT     Navigation timeout ms (default: 25000)

politeness:
  --min-delay MIN_DELAY
                        Min delay between chapters (s)
  --max-delay MAX_DELAY
                        Max delay between chapters (s)
  --retries RETRIES     Retries per chapter on errors (default: 2)
  --wait-after-load WAIT_AFTER_LOAD
                        Extra settle wait per page (ms)
  --concurrency CONCURRENCY
                        Chapters fetched at once (default: 3). The per-host
                        delay still applies.
  --allow-private-hosts
                        Permit hosts resolving to loopback or private ranges
                        (for local testing)
```

<!-- /cli-reference -->

## macOS notes

**Tk.** The GUI needs it and Homebrew's `python3` does not ship it. Installing
`python-tk@3.11` or `python-tk@3.12` only helps if you also have that exact
Homebrew interpreter, and `/usr/bin/python3` carries the deprecated Tk 8.5.
Build the environment against a python.org framework build:

```bash
make clean
make setup PYTHON=/Library/Frameworks/Python.framework/Versions/3.14/bin/python3
```

`make setup` reports which case you are in, and the GUI prints the same advice
instead of an ImportError traceback. The CLI does not need Tk.

**Certificates.** A python.org build ships without root certificates until you
run `Install Certificates.command` from its install folder. Until you do, every
HTTPS request fails verification — including the one that reads `robots.txt`.

**First Chromium launch.** `playwright install chromium` downloads a browser
Gatekeeper has not seen. The first launch is slow while macOS verifies it. Not a
hang.

**Filenames.** APFS is case-insensitive, so `Chapter One.txt` and
`chapter one.txt` are the same file; colliding titles get a numeric suffix and a
log line rather than silently overwriting. Names are normalised to NFC, capped
at 255 *bytes* rather than characters, and leading dots are stripped so a
chapter titled `.Prologue` does not vanish from Finder.

## Breaking change from v1

Filenames containing runs of forbidden characters differ from what the v1 GUI
produced. `Chapter//One` was `Chapter__One` and is now `Chapter_One`.

The two v1 scripts had drifted — the GUI replaced each forbidden character, the
CLI collapsed each run — and v2 adopts the CLI behaviour. Titles that are URLs
or Windows paths are the realistic cases: `https://example.com/chapter/1` was
`https___example.com_chapter_1` and is now `https_example.com_chapter_1`.

Chapter text is unchanged. A golden fixture captured from the tagged v1 pins it.

## Architecture

```
TOC page ─▶ parser ─▶ politeness ─▶ fetcher ─▶ exporters ─▶ files
            vets       robots +      bounded    text
            links      rate limit    workers    markdown
                                                jsonl
```

`PageSource` is the seam between the fetch loop and Playwright. Most tests run
against a dict-backed stub with no browser at all; only browser-marked tests
open Chromium.

Five things below are non-obvious, and each is here because it was established
by measurement rather than reasoning. Together they are most of why the code
looks more complicated than a reader would expect.

**The rate limiter is acquired inside the concurrency semaphore, not before it.**
Acquiring first would let every pending worker queue on the limiter regardless
of the concurrency ceiling, and the ceiling would stop bounding anything. A test
runs five workers against one host and asserts the observed spacing holds.

**Redirects are followed by hand.** Playwright's `route` handler fires once per
navigation, not once per redirect hop — Chromium follows redirects internally.
`route.fetch` plus `fulfill` does not re-enter the handler, and request events
see every hop but cannot block one. So the loop lives in the handler: fetch with
`max_redirects=0`, validate the target, repeat, abort on the first disallowed
hop. A consequence is that `page.url` becomes wrong — the body is fulfilled at
the originally requested URL — so the final URL is tracked in the handler.

**One browser page per worker.** Two concurrent `goto()` calls on one page abort
each other with `net::ERR_ABORTED`. This passed 447 tests and failed on the
first run against a live server, because a dict-backed stub has no notion of a
busy resource. The stub now models exclusivity, so a caller has to state how
many pages it believes it has.

**"I could not check" is not "there are no rules."** RFC 9309 makes a 404
`robots.txt` mean no restrictions, and treating *every* fetch failure the same
way looked equally reasonable. It was not: a TLS trust problem on the local
machine silently marked every site unrestricted, with no output at all. The
safe-looking default was unsafe because it conflated the absence of rules with
the inability to read them. Non-404 failures now warn loudly and say when the
fault is local.

**The robots escape hatch is keyed on evidence, not on a click.** After the
GUI's human gate, a signed-in session may proceed past a `Disallow` — a site
routinely disallows exactly the paths a login unlocks. The obvious wiring was to
key that on the confirm button, which would have made pressing Ready without
signing in an override, turning the escape hatch into the default path. It is
keyed on the session actually carrying cookies instead, and an anonymous run
still takes a hard refusal.

### One invariant, four enforcement points

Every link is accounted for: `raw == kept + rejected + truncated`. That is
checked in a `LinkCollection` constructor, at the `PageSource` error boundary,
in `run()` before it returns, and again where output is merged. Four checks for
one rule looks like defensive layering. It is not — each was added after a real
bug in which content disappeared without a trace:

| What vanished | How |
|---|---|
| SVG-anchor chapters | `SVGAnimatedString` arrives in Python as `{}` — truthy in JavaScript, falsy in Python, so a `if link` filter dropped it |
| A whole run | a stdlib `TimeoutError` escaped the retry vocabulary and killed the task group |
| One chapter, silently | `asyncio.TaskGroup` absorbs a child's `CancelledError` and discards the task with it |
| Everything a resume did not refetch | the merged file was rebuilt from only the chapters that run fetched |

The last one is the reason for the fourth check. Every earlier check passed
while it happened: the links really were all accounted for. The loss was
downstream of every check that existed.

## Known limitations

- **DNS rebinding is not closed.** The guard resolves a host and checks the
  addresses; Chromium then resolves it again independently. A name answering
  differently between those lookups can reach an address the guard rejected.
  Closing it needs connection-level control the browser does not expose.
- **Subresources are screened but not proxied.** A disallowed image or script
  request is aborted, but only navigations are inspected hop by hop.
- **An unreachable `robots.txt` still permits everything**, per RFC 9309. It is
  now loud about it, but the default is permissive.
- **`robots.txt` parsing follows RFC 9309 precedence** via the standard library.
  Non-standard extensions beyond `Crawl-delay` are ignored.
- **The GUI holds a persistent browser profile.** A previous run whose browser
  is still alive locks it, and the next launch fails until that process exits.

## Development

```bash
make setup       # venv, install, Chromium, Tk check
make deps        # venv and install only, no browser
make lint        # ruff check + format check
make typecheck   # mypy, strict, over src/
make test        # full suite, browser tests included
make test-fast   # skips browser tests; what CI's matrix runs
make run ARGS='--toc ... --link ...'
make gui
```

mypy runs strict over `src/` with no unexplained ignores. CI covers Python
3.11–3.14 on Linux, one macOS job, and browser tests on a single job with
Chromium cached on the build Playwright resolves.

After changing a flag, regenerate the reference above:

```bash
./.venv/bin/python scripts_gen_readme.py
```

To see the rewrite: `git diff v1.0.0..v2.0.0` — 670 lines across three
free-standing scripts becoming a tested package.

## License

MIT. See [LICENSE](LICENSE).
