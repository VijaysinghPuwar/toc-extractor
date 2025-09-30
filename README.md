# TOC Extractor (Playwright + Tkinter)

A desktop GUI that launches a **headful Playwright** browser so you can log in / solve site challenges, then extracts chapter pages from a **Table of Contents (TOC)** using **your CSS selectors**. It saves per-chapter text files and builds a `combined.txt`.

> Built for respectful use: no site defaults, no bypasses, optional polite delays, persistent **local** profile, and an explicit **“I’m Ready”** step before extraction.

---

## Why this exists

Scraping tutorials often hard-code selectors for a single site and break quickly. This tool flips that: **you supply the selectors**, so it works across many sites you’re permitted to access, without embedding site-specific logic.

---

## Features

* Bring-your-own **CSS selectors** (links, title, content)
* **Headful** flow to handle login/captcha manually
* **Polite pacing** with min/max randomized delays
* Cleans text (optional: strip “Ads by …” lines, remove raw URLs)
* Saves `001 - <Title>.txt`, `002 - …`, plus a **merged** `combined.txt`
* Uses a **persistent local profile** (cookies/session), never committed to git

---

## Requirements

* **Python** 3.10+
* **Playwright** 1.44+
* **Tkinter** (bundled with standard Python builds)

---

## Install

```bash
# 1) Create & activate a venv
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# 2) Install dependencies
pip install -r requirements.txt

# 3) Install Playwright browser engines
python -m playwright install
```

`requirements.txt`

```
playwright>=1.44
```

---

## Run (GUI)

```bash
python toc_playwright.py
```

---

## Quick Start

1. Paste the **TOC URL** (must be `http(s)`).
2. Enter **CSS selectors**:

   * **Chapter links** → selects all chapter anchors on the TOC page
   * **Title** → chapter title element on each chapter page
   * **Content** → main text container on each chapter page
3. Click **Launch Browser & Open TOC** and complete any login/captcha in the opened Chromium window.
4. Click **I’m Ready**, then **Start Extraction**.
5. Find results in the **Output folder** (default `downloads/`) and `combined.txt`.

**Selector tips**

* Use DevTools → Inspect → right-click → “Copy selector,” then simplify.
* Start generic, then narrow:
  `ol.toc a`, `a.chapter-item`, `article h1`, `div#chapter-content`, etc.
* Relative links (e.g., `/chapter/12`) are auto-resolved against the TOC’s domain.

---

## Output

* **Per-chapter** files: `downloads/001 - <Title>.txt`, `002 - …`
  Each file contains the title and cleaned text; optionally adds the page URL.
* **Merged** file: `downloads/combined.txt` with separators between chapters.

**Cleaning options**

* **Strip common ad markers** (e.g., “Ads by …”, “Sponsored Content”)
* **Remove raw links** (unless you enable “Include page URL”)

---

## UI Options

* Max chapters: **20 / 25 / 50 / 100**
* Delay range (seconds): randomized per page for polite pacing
* Include page URL in saved files
* Strip common ad markers
* Choose output folder

---

## .gitignore (prevent huge pushes)

```gitignore
# Python
.venv/
__pycache__/
*.pyc
*.log

# Playwright / app artifacts
playwright/.cache/
playwright-report/
.pw_profile/
_pw_profile/
pw_output/
downloads/

# Editors / OS
.vscode/
.DS_Store
```

**Accidentally committed big files?** Purge history and force-push:

```bash
python -m pip install --user git-filter-repo
git filter-repo --force \
  --invert-paths \
  --path .venv/ \
  --path playwright/.cache/ \
  --path .pw_profile/ \
  --path _pw_profile/ \
  --path pw_output/ \
  --path downloads/
git push origin main --force
```

---

## FAQ

**No links found**
Make sure the TOC is fully visible (scroll if infinite). Test with a very broad selector (e.g., `a`) to ensure you’re on the right page, then refine.

**Empty content**
Your **content selector** must target the readable container, not `body`. Test in DevTools:
`document.querySelector('<selector>').innerText`

**Captcha every page**
This project **does not** bypass protections. Keep the session authenticated; extraction may be partial if the site blocks automation.

---

## Legal & Ethics

Use only on content **you own** or **have permission** to access. Respect each site’s **Terms of Service**, **robots.txt**, and rate limits. This project implements **no** automated bypasses and requires explicit user confirmation before proceeding.

---

## Roadmap

* Optional CLI with JSON config for selectors
* Retry/backoff for flaky pages
* Markdown/EPUB exporters

---

## Contributing

PRs welcome! Keep changes focused, document behavior, and do **not** commit large binaries or site-specific bypass logic.

****
