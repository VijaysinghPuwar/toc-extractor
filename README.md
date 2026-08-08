# TOC Extractor

Extracts chapter text from a table-of-contents page using CSS selectors **you**
supply. Nothing is hard-coded per site: you give it the TOC URL and three
selectors — chapter links, title, content — and it writes one `.txt` per chapter
plus a merged `combined.txt`. Scraping tutorials usually hard-code selectors for
one site and break the week it redesigns; inverting that is the entire point.

## Status

The v1 scripts are gone; both front ends are now the package. CI and the full
README rewrite are still outstanding.

To see the rewrite: `git diff v1.0.0..main` — 669 lines across three
free-standing scripts becoming a tested package.

## Requirements

- Python 3.11–3.14
- Playwright (the only runtime dependency)
- Tk, for the GUI only — see [macOS notes](#macos-notes)

## Quickstart

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m playwright install chromium

python -m toc_extractor --help    # command line
python -m toc_extractor --gui     # graphical front end
```

`make setup` does all four steps and then tells you whether Tk is usable.

## Permissions and ethics

Use this only on content you own or are permitted to access. Respect each
site's Terms of Service, its `robots.txt`, and its rate limits.

This is enforced in code, not asserted in a paragraph:

- **robots.txt is checked before fetching**, and a `Disallow` is a hard refusal
  for an anonymous run. There is no `--ignore-robots` flag, deliberately: a flag
  that turns the check off gets copied between runs until the check means
  nothing. The only way past it is a real human logging in through the GUI's
  gate, and that decision is recorded with the matched rule and its line number.
- **`Crawl-delay` is honoured**, and can only ever slow the tool down. A site
  asking to be hit faster than you configured does not get to.
- **The per-host delay survives concurrency.** Requests to one host are spaced by
  the configured interval no matter how many workers are running — see
  [Architecture](#architecture) for why that is not automatic.
- **No protection is bypassed.** No captcha solving, no fingerprint spoofing
  beyond a configurable User-Agent, no stealth plugins, no proxy rotation.
- **URLs from a page are treated as untrusted.** Non-HTTP schemes and hosts
  resolving to loopback, link-local, or private ranges are refused, including
  across redirects.

## Selectors

Three, all yours:

| Selector | Selects |
|---|---|
| Chapter links | every chapter anchor on the TOC page |
| Title | the title element on a chapter page |
| Content | the readable container on a chapter page |

Find them with DevTools → Inspect → Copy selector, then simplify. Start broad
(`ol.toc a`) and narrow until only chapters match. Test a content selector with
`document.querySelector('<selector>').innerText` before using it — if that
returns the whole page, you have selected `body` by another name.

Relative links resolve against the page's own `<base>`, in the page.

## Output

```
downloads/
  001 - Chapter One.txt
  002 - Chapter Two.txt
  combined.txt
```

URLs are stripped from body text by default, as in v1. That behaviour is
unchanged; what is new is that the count of removed URLs is reported, so the
loss is visible rather than silent. Pass `--include-links` to keep them.

## macOS notes

**Tk.** The GUI needs it and Homebrew's `python3` does not ship it. Installing
`python-tk@3.11` or `python-tk@3.12` only helps if you also have the matching
Homebrew interpreter, and `/usr/bin/python3` has the deprecated Tk 8.5. Build
the venv against a python.org framework build:

```bash
make clean
make setup PYTHON=/Library/Frameworks/Python.framework/Versions/3.14/bin/python3
```

`make setup` reports which case you are in. The CLI does not need Tk.

**First Chromium launch.** `playwright install chromium` downloads a browser
Gatekeeper has not seen. The first launch is slow while macOS verifies it. This
is not a hang.

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
TOC page ─▶ parser ─▶ politeness ─▶ fetcher ─▶ sink ─▶ files
            vets       robots +      bounded    text
            links      rate limit    workers
```

Three details are non-obvious enough to be worth stating, because each was
established by measurement and each makes the code look more complicated than a
reader would expect.

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

**Every link is accounted for, enforced in three places.** `raw == kept +
rejected + truncated` is a constructor assertion, the fetch loop translates
foreign exceptions at the `PageSource` boundary, and the run asserts its own
accounting before returning. That is not defensive layering: each was added
after a real bug in which a chapter disappeared without a trace — SVG anchors
arriving as a value that was truthy in JavaScript and falsy in Python, a stdlib
`TimeoutError` escaping the retry vocabulary, and `asyncio.TaskGroup` absorbing
a cancellation and discarding the task with it.

## Known limitations

- **DNS rebinding is not closed.** The guard resolves a host and checks the
  addresses; Chromium then resolves it again independently. A name that answers
  differently between those two lookups can reach an address the guard rejected.
  Closing this needs connection-level control the browser does not expose.
- **Subresources are screened but not proxied.** A disallowed image or script
  request is aborted, but only navigations are inspected hop by hop.
- **`robots.txt` parsing follows RFC 9309 precedence** via the standard library.
  Non-standard extensions beyond `Crawl-delay` are ignored.
- **An unreachable robots.txt is treated as permitting everything**, per RFC
  9309. That includes the case where the failure is local — a TLS trust problem
  on your machine looks the same as a site with no robots.txt. It is now warned
  about loudly rather than assumed.
- **No EPUB export.** Markdown output plus `pandoc chapter.md -o chapter.epub`
  covers it without this project inventing metadata it does not have.

## Development

```bash
make setup       # venv, install, Chromium, Tk check
make lint        # ruff check + format check
make typecheck   # mypy, strict, over src/
make test        # full suite, browser tests included
make test-fast   # skips browser tests
```

389 tests, 11 of which need a real browser. mypy runs strict over `src/` with no
unexplained ignores. The two v1 scripts are excluded from linting because they
are scheduled for deletion, not repair.

## License

MIT. See [LICENSE](LICENSE).
