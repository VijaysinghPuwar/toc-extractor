#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
CLI Chapter Extractor (Playwright, headless-friendly)

Usage example:
  python cli_runner.py \
    --toc "https://example.com/book/toc" \
    --link ".toc a.chapter" \
    --title "h1.title" \
    --content "article.reader" \
    --max 20 \
    --out downloads \
    --dry-run --dump-html --screenshot

If a site needs login, run the GUI app locally to sign in and export a
Playwright storage state JSON, then reuse it here via --storage-state state.json.
"""

import argparse
import os
import re
import sys
import time
import random
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


# ------------------ Utilities ------------------ #

def is_valid_url(u: str) -> bool:
    try:
        p = urlparse(u)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def safe_filename(name: str) -> str:
    name = re.sub(r"[\t\n\r]", " ", name).strip()
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", " ", name)
    return (name or "untitled")[:150]


def clean_text(text: str, remove_links: bool = True, strip_ads: bool = True) -> str:
    if strip_ads:
        for pat in (r"Ads by\s+\w+", r"Sponsored\s+Content"):
            text = re.sub(pat, "", text, flags=re.IGNORECASE)
    if remove_links:
        text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"\n\s+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def sleep_polite(min_s: float, max_s: float):
    delay = max(0.0, random.uniform(min_s, max_s))
    if delay:
        time.sleep(delay)


# ------------------ Main ------------------ #

def main():
    ap = argparse.ArgumentParser(description="Headless TOC extractor using Playwright.")
    ap.add_argument("--toc", required=True, help="TOC URL (must start with http/https)")
    ap.add_argument("--link", required=True, help="CSS selector for chapter links on the TOC page")
    ap.add_argument("--title", required=True, help="CSS selector for title on a chapter page")
    ap.add_argument("--content", required=True, help="CSS selector for content on a chapter page")
    ap.add_argument("--max", type=int, default=20, help="Max chapters to fetch (default: 20)")
    ap.add_argument("--out", default="downloads", help="Output folder (default: downloads)")

    # Behavior toggles
    ap.add_argument("--include-links", action="store_true", help="Include source URL in saved files")
    ap.add_argument("--no-strip-ads", action="store_true", help="Do NOT strip common ad markers from text")
    ap.add_argument("--dry-run", action="store_true", help="List discovered chapter URLs and exit")

    # Debug helpers
    ap.add_argument("--dump-html", action="store_true", help="Save TOC HTML to out/toc.html")
    ap.add_argument("--screenshot", action="store_true", help="Save TOC screenshot to out/toc.png")

    # Browser/context options
    ap.add_argument("--ua", default=None, help="Custom User-Agent string")
    ap.add_argument("--storage-state", default=None, help="Path to Playwright storage state JSON (reuses login)")
    ap.add_argument("--headful", action="store_true", help="Run headed (GUI). Default is headless.")
    ap.add_argument("--timeout", type=int, default=25000, help="Navigation timeout ms (default: 25000)")

    # Politeness & resilience
    ap.add_argument("--min-delay", type=float, default=1.2, help="Min delay between chapters (s)")
    ap.add_argument("--max-delay", type=float, default=2.5, help="Max delay between chapters (s)")
    ap.add_argument("--retries", type=int, default=2, help="Retries per chapter on errors (default: 2)")
    ap.add_argument("--wait-after-load", type=int, default=500, help="Extra settle wait per page (ms)")
    args = ap.parse_args()

    # Validate URL and create out dir
    if not is_valid_url(args.toc):
        sys.exit(f"[ERROR] Invalid --toc URL: {args.toc}")

    os.makedirs(args.out, exist_ok=True)

    # Normalize options
    min_d = max(0.0, args.min_delay)
    max_d = max(min_d, args.max_delay)
    strip_ads = not args.no_strip_ads

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headful)
        context_kwargs = {}
        if args.ua:
            context_kwargs["user_agent"] = args.ua
        if args.storage_state:
            if not os.path.exists(args.storage_state):
                sys.exit(f"[ERROR] storage-state file not found: {args.storage_state}")
            context_kwargs["storage_state"] = args.storage_state

        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        page.set_default_navigation_timeout(args.timeout)

        # ---------- Load TOC ---------- #
        try:
            page.goto(args.toc, wait_until="domcontentloaded")
            if args.wait_after_load:
                page.wait_for_timeout(args.wait_after_load)
        except PWTimeout:
            browser.close()
            sys.exit("[ERROR] Timeout while loading TOC page.")
        except Exception as e:
            browser.close()
            sys.exit(f"[ERROR] Failed to load TOC: {e}")

        # Debug artifacts
        if args.dump_html:
            open(os.path.join(args.out, "toc.html"), "w", encoding="utf-8").write(page.content())
        if args.screenshot:
            page.screenshot(path=os.path.join(args.out, "toc.png"), full_page=True)

        # Collect chapter links
        links = page.eval_on_selector_all(
            args.link,
            "els => els.map(a => a.href || a.getAttribute('href'))"
        ) or []
        links = [l for l in links if l]
        if not links:
            browser.close()
            sys.exit("[ERROR] No links found with the provided --link selector. "
                     "Inspect downloads/toc.html or use --screenshot to refine the selector.")

        base = f"{urlparse(args.toc).scheme}://{urlparse(args.toc).netloc}"
        links = [l if l.startswith("http") else urljoin(base, l) for l in links]
        links = links[: max(1, args.max)]

        if args.dry_run:
            print("[DRY RUN] Will fetch these URLs:")
            for i, u in enumerate(links, 1):
                print(f"{i:03d}  {u}")
            browser.close()
            return

        # ---------- Extract chapters ---------- #
        combined_path = os.path.join(args.out, "combined.txt")
        with open(combined_path, "w", encoding="utf-8") as combo:
            for i, url in enumerate(links, 1):
                attempt = 0
                while True:
                    attempt += 1
                    try:
                        page.goto(url, wait_until="domcontentloaded")
                        if args.wait_after_load:
                            page.wait_for_timeout(args.wait_after_load)

                        # Title & content
                        title = page.eval_on_selector(args.title, "el => el.innerText") or f"chapter_{i}"
                        body = page.eval_on_selector(args.content, "el => el.innerText") or ""
                        title = safe_filename(title)
                        text = clean_text(body, remove_links=not args.include_links, strip_ads=strip_ads)

                        # Write files
                        p = os.path.join(args.out, f"{i:03d} - {title}.txt")
                        with open(p, "w", encoding="utf-8") as f:
                            f.write(title + "\n\n")
                            if args.include_links:
                                f.write(f"Source: {url}\n\n")
                            f.write(text + "\n")

                        combo.write(title + "\n\n")
                        if args.include_links:
                            combo.write(f"Source: {url}\n\n")
                        combo.write(text + "\n\n" + ("-" * 80) + "\n\n")

                        print(f"[OK] {p}")
                        break  # success; move to next link

                    except PWTimeout:
                        msg = f"[WARN] Timeout on {url}"
                    except Exception as e:
                        msg = f"[WARN] Error on {url}: {e}"

                    if attempt <= args.retries:
                        print(msg + f" — retry {attempt}/{args.retries}")
                        sleep_polite(min_d, max_d)
                        continue
                    else:
                        print(msg + " — giving up.")
                        break

                # polite pacing between chapters
                sleep_polite(min_d, max_d)

        browser.close()
        print(f"[DONE] Combined: {combined_path}")


if __name__ == "__main__":
    main()
