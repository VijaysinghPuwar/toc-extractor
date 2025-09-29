# -*- coding: utf-8 -*-
"""
TOC Chapter Extractor (Generic) — Public Share Version
=====================================================

A GUI tool that opens a Playwright-controlled browser, lets the user
manually solve any site challenges/login, then extracts chapter pages
listed on a Table-of-Contents (TOC) page using **user-provided CSS selectors**.

Key design choices for public sharing:
- **No site defaults**: You provide the TOC URL and CSS selectors.
- **Respectful usage**: Adds configurable delays between requests.
- **No link embedding by default**: Toggle to include per-page URLs.
- **Local-only state**: Persistent profile lives in a hidden local folder.
- **No auto-bypass of protections**: You must click "I'm Ready" to proceed after pages load.

Tested with: Python 3.10+, Playwright 1.44+, Tkinter (standard library)

Disclaimer: Use only on content you own or have permission to access. Follow the
website's Terms of Service and your local laws. Check robots.txt where applicable.
"""

import os
import re
import time
import random
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

APP_TITLE = "TOC Chapter Extractor (Playwright Browser)"
PROFILE_DIR = os.path.abspath("./.pw_profile")  # persistent profile for cookies/session (untracked)
DEFAULT_OUTPUT_DIR = os.path.abspath("./downloads")
ALLOWED_COUNTS = [20, 25, 50, 100]
DEFAULT_COUNT = 20
DEFAULT_MIN_DELAY = 1.2
DEFAULT_MAX_DELAY = 2.5

# -------------------------- Helpers --------------------------

def safe_filename(name: str) -> str:
    name = re.sub(r"[\t\n\r]", " ", name).strip()
    name = re.sub(r"[\\/:*?\"<>|]", "_", name)
    name = re.sub(r"\s+", " ", name)
    return name[:150] if name else "untitled"


def clean_text(text: str, remove_links: bool = True, strip_ads: bool = True) -> str:
    if strip_ads:
        # Common ad markers you may extend as needed
        ad_patterns = [
            r"Ads by\s+\w+",   # generic: "Ads by PubRev" etc.
            r"Sponsored\s+Content",
        ]
        for pat in ad_patterns:
            text = re.sub(pat, "", text, flags=re.IGNORECASE)

    if remove_links:
        # Remove raw URLs
        text = re.sub(r"https?://\S+", "", text)

    # Normalize whitespace
    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"\n\s+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    return text


def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)


# -------------------------- GUI App --------------------------

class ExtractorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("880x720")
        self.root.minsize(820, 640)

        # State variables
        self.toc_url_var = tk.StringVar()
        self.link_selector_var = tk.StringVar()
        self.title_selector_var = tk.StringVar()
        self.content_selector_var = tk.StringVar()

        self.max_count_var = tk.IntVar(value=DEFAULT_COUNT)
        self.output_dir_var = tk.StringVar(value=DEFAULT_OUTPUT_DIR)

        self.include_links_var = tk.BooleanVar(value=False)
        self.strip_ads_var = tk.BooleanVar(value=True)

        self.min_delay_var = tk.DoubleVar(value=DEFAULT_MIN_DELAY)
        self.max_delay_var = tk.DoubleVar(value=DEFAULT_MAX_DELAY)

        self.is_running = False
        self.ready_to_extract = threading.Event()
        self.thread = None

        self._build_gui()

    def _build_gui(self):
        pad = {"padx": 8, "pady": 6}

        frm_top = ttk.LabelFrame(self.root, text="Source & Selectors")
        frm_top.pack(fill="x", **pad)

        # TOC URL
        ttk.Label(frm_top, text="TOC URL:").grid(row=0, column=0, sticky="e", **pad)
        ttk.Entry(frm_top, textvariable=self.toc_url_var, width=80).grid(row=0, column=1, columnspan=3, sticky="we", **pad)

        # CSS selectors
        ttk.Label(frm_top, text="Chapter link CSS selector:").grid(row=1, column=0, sticky="e", **pad)
        ttk.Entry(frm_top, textvariable=self.link_selector_var, width=60).grid(row=1, column=1, sticky="we", **pad)

        ttk.Label(frm_top, text="Title CSS selector:").grid(row=1, column=2, sticky="e", **pad)
        ttk.Entry(frm_top, textvariable=self.title_selector_var, width=30).grid(row=1, column=3, sticky="we", **pad)

        ttk.Label(frm_top, text="Content CSS selector:").grid(row=2, column=0, sticky="e", **pad)
        ttk.Entry(frm_top, textvariable=self.content_selector_var, width=60).grid(row=2, column=1, sticky="we", **pad)

        # Options
        frm_opts = ttk.LabelFrame(self.root, text="Options")
        frm_opts.pack(fill="x", **pad)

        ttk.Label(frm_opts, text="Max chapters:").grid(row=0, column=0, sticky="e", **pad)
        ttk.Combobox(frm_opts, textvariable=self.max_count_var, values=ALLOWED_COUNTS, width=10, state="readonly").grid(row=0, column=1, sticky="w", **pad)

        ttk.Label(frm_opts, text="Output folder:").grid(row=0, column=2, sticky="e", **pad)
        ttk.Entry(frm_opts, textvariable=self.output_dir_var, width=40).grid(row=0, column=3, sticky="we", **pad)
        ttk.Button(frm_opts, text="Browse…", command=self.choose_output_dir).grid(row=0, column=4, sticky="w", **pad)

        ttk.Checkbutton(frm_opts, text="Include page URL in saved files", variable=self.include_links_var).grid(row=1, column=0, columnspan=2, sticky="w", **pad)
        ttk.Checkbutton(frm_opts, text="Strip common ad markers", variable=self.strip_ads_var).grid(row=1, column=2, columnspan=2, sticky="w", **pad)

        ttk.Label(frm_opts, text="Delay (seconds) min → max:").grid(row=2, column=0, sticky="e", **pad)
        ttk.Entry(frm_opts, textvariable=self.min_delay_var, width=10).grid(row=2, column=1, sticky="w", **pad)
        ttk.Entry(frm_opts, textvariable=self.max_delay_var, width=10).grid(row=2, column=2, sticky="w", **pad)

        # Controls
        frm_ctrl = ttk.LabelFrame(self.root, text="Controls")
        frm_ctrl.pack(fill="x", **pad)

        self.btn_launch = ttk.Button(frm_ctrl, text="Launch Browser & Open TOC", command=self.launch_browser)
        self.btn_launch.grid(row=0, column=0, **pad)

        self.btn_ready = ttk.Button(frm_ctrl, text="I'm Ready (continue extraction)", command=self.signal_ready, state="disabled")
        self.btn_ready.grid(row=0, column=1, **pad)

        self.btn_start = ttk.Button(frm_ctrl, text="Start Extraction", command=self.start_extraction, state="disabled")
        self.btn_start.grid(row=0, column=2, **pad)

        self.btn_stop = ttk.Button(frm_ctrl, text="Stop", command=self.stop_extraction, state="disabled")
        self.btn_stop.grid(row=0, column=3, **pad)

        # Log output
        frm_log = ttk.LabelFrame(self.root, text="Log")
        frm_log.pack(fill="both", expand=True, **pad)

        self.txt_log = tk.Text(frm_log, wrap="word", height=20)
        self.txt_log.pack(fill="both", expand=True)

        self._log_info("Welcome! Provide the TOC URL and CSS selectors, then click 'Launch Browser & Open TOC'.")
        self._log_info("After the page loads, handle any login/captcha if required, then click 'I'm Ready'.")

    # --------------- UI callbacks ---------------

    def choose_output_dir(self):
        path = filedialog.askdirectory(initialdir=self.output_dir_var.get() or os.getcwd())
        if path:
            self.output_dir_var.set(path)

    def launch_browser(self):
        if self.is_running:
            messagebox.showwarning("Busy", "An extraction is currently running.")
            return
        if not self._validate_basic_inputs(launch_only=True):
            return

        self.btn_launch.config(state="disabled")
        self.btn_ready.config(state="disabled")
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="disabled")

        def _launch():
            try:
                self._log_info("Launching Playwright (headful) with persistent profile…")
                ensure_dir(PROFILE_DIR)
                self.pw = sync_playwright().start()
                self.browser = self.pw.chromium.launch_persistent_context(
                    user_data_dir=PROFILE_DIR,
                    headless=False,
                    args=["--start-maximized"],
                )
                self.page = self.browser.new_page()

                toc_url = self.toc_url_var.get().strip()
                self._log_info(f"Opening TOC: {toc_url}")
                self.page.goto(toc_url, wait_until="domcontentloaded")
                self._log_info("If needed, log in or solve challenges in the browser window.")
                self._log_info("Then click 'I'm Ready' to allow extraction.")

                self.btn_ready.config(state="normal")
                self.btn_start.config(state="disabled")
                self.btn_stop.config(state="disabled")

            except Exception as e:
                self._log_error(f"Failed to launch browser: {e}")
                self._teardown_browser()
                self.btn_launch.config(state="normal")

        threading.Thread(target=_launch, daemon=True).start()

    def signal_ready(self):
        # User confirms the TOC is visible and selectors are appropriate
        if not hasattr(self, "page"):
            messagebox.showerror("No Browser", "Launch the browser first.")
            return
        self.ready_to_extract.set()
        self._log_info("Ready acknowledged. You can now click 'Start Extraction'.")
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="normal")
        self.btn_ready.config(state="disabled")

    def start_extraction(self):
        if self.is_running:
            messagebox.showwarning("Busy", "Extraction already in progress.")
            return
        if not self._validate_basic_inputs():
            return
        if not hasattr(self, "page"):
            messagebox.showerror("No Browser", "Please launch the browser first.")
            return

        self.is_running = True
        self.btn_launch.config(state="disabled")
        self.btn_start.config(state="disabled")
        self.btn_ready.config(state="disabled")
        self.btn_stop.config(state="normal")

        self.thread = threading.Thread(target=self._run_extraction, daemon=True)
        self.thread.start()

    def stop_extraction(self):
        if not self.is_running:
            return
        self.is_running = False
        self._log_info("Stop requested. Finishing current step…")

    # --------------- Core extraction ---------------

    def _run_extraction(self):
        try:
            # Wait until the user confirmed they're ready (after any login/captcha)
            if not self.ready_to_extract.is_set():
                self._log_info("Waiting for 'I'm Ready'…")
                self.ready_to_extract.wait(timeout=600)  # 10 minutes
                if not self.ready_to_extract.is_set():
                    self._log_error("Timed out waiting for readiness. Aborting.")
                    return

            link_selector = self.link_selector_var.get().strip()
            title_selector = self.title_selector_var.get().strip()
            content_selector = self.content_selector_var.get().strip()

            # Collect chapter links from TOC
            self._log_info(f"Collecting chapter links with selector: {link_selector}")
            links = self.page.eval_on_selector_all(link_selector, "elements => elements.map(a => a.href || a.getAttribute('href'))")
            links = [l for l in links if l]

            if not links:
                self._log_error("No links found with the given selector. Check your CSS selector and that the TOC is visible.")
                return

            # Normalize relative URLs
            toc_url = self.toc_url_var.get().strip()
            base = f"{urlparse(toc_url).scheme}://{urlparse(toc_url).netloc}"
            links = [l if l.startswith("http") else urljoin(base, l) for l in links]

            max_count = int(self.max_count_var.get() or DEFAULT_COUNT)
            links = links[:max_count]
            self._log_info(f"Found {len(links)} chapter links. Starting extraction…")

            out_dir = self.output_dir_var.get().strip() or DEFAULT_OUTPUT_DIR
            ensure_dir(out_dir)
            combined_path = os.path.join(out_dir, "combined.txt")
            combined_fp = open(combined_path, "w", encoding="utf-8")

            include_links = bool(self.include_links_var.get())
            strip_ads = bool(self.strip_ads_var.get())
            min_d = float(self.min_delay_var.get())
            max_d = float(self.max_delay_var.get())
            if min_d < 0: min_d = 0
            if max_d < min_d: max_d = min_d

            for idx, url in enumerate(links, start=1):
                if not self.is_running:
                    self._log_info("Extraction stopped by user.")
                    break

                self._log_info(f"[{idx}/{len(links)}] Opening: {url}")
                try:
                    self.page.goto(url, wait_until="domcontentloaded")
                    self.page.wait_for_timeout(500)  # small settle

                    # Extract title and content
                    title = self.page.eval_on_selector(title_selector, "el => el.innerText")
                    body = self.page.eval_on_selector(content_selector, "el => el.innerText")

                    if not body:
                        raise RuntimeError("Content not found with provided selector.")

                    title = safe_filename(title or f"chapter_{idx}")
                    cleaned = clean_text(body, remove_links=not include_links, strip_ads=strip_ads)

                    # Write per-chapter file
                    chapter_path = os.path.join(out_dir, f"{idx:03d} - {title}.txt")
                    with open(chapter_path, "w", encoding="utf-8") as cfp:
                        cfp.write(title + "\n\n")
                        if include_links:
                            cfp.write(f"Source: {url}\n\n")
                        cfp.write(cleaned + "\n")

                    # Append to combined
                    combined_fp.write(title + "\n\n")
                    if include_links:
                        combined_fp.write(f"Source: {url}\n\n")
                    combined_fp.write(cleaned + "\n\n" + ("-"*80) + "\n\n")

                    self._log_info(f"Saved: {chapter_path}")

                    # Respectful pacing
                    delay = random.uniform(min_d, max_d)
                    self._log_info(f"Sleeping {delay:.2f}s…")
                    self.page.wait_for_timeout(int(delay * 1000))

                except PWTimeout:
                    self._log_error("Timeout while loading or selecting content. Skipping.")
                except Exception as e:
                    self._log_error(f"Error on {url}: {e}. Skipping.")

            combined_fp.close()
            self._log_info(f"Combined file written to: {combined_path}")

        finally:
            self.is_running = False
            self.btn_launch.config(state="normal")
            self.btn_start.config(state="disabled")
            self.btn_ready.config(state="disabled")
            self.btn_stop.config(state="disabled")
            self._teardown_browser()

    # --------------- Validation & Teardown ---------------

    def _validate_basic_inputs(self, launch_only=False) -> bool:
        toc = self.toc_url_var.get().strip()
        if not toc or not toc.startswith("http"):
            messagebox.showerror("Invalid TOC URL", "Please provide a valid http(s) URL.")
            return False

        if not launch_only:
            missing = []
            if not self.link_selector_var.get().strip():
                missing.append("Chapter link CSS selector")
            if not self.title_selector_var.get().strip():
                missing.append("Title CSS selector")
            if not self.content_selector_var.get().strip():
                missing.append("Content CSS selector")
            if missing:
                messagebox.showerror("Missing selectors", "Please provide: " + ", ".join(missing))
                return False

        out_dir = self.output_dir_var.get().strip() or DEFAULT_OUTPUT_DIR
        try:
            ensure_dir(out_dir)
        except Exception as e:
            messagebox.showerror("Output Error", f"Cannot create/access output folder: {e}")
            return False
        return True

    def _teardown_browser(self):
        try:
            if hasattr(self, "page") and self.page:
                self.page.close()
        except Exception:
            pass
        try:
            if hasattr(self, "browser") and self.browser:
                self.browser.close()
        except Exception:
            pass
        try:
            if hasattr(self, "pw") and self.pw:
                self.pw.stop()
        except Exception:
            pass

    # --------------- Logging ---------------

    def _log(self, msg: str, tag: str = "info"):
        self.txt_log.insert("end", f"[{tag.upper()}] {msg}\n")
        self.txt_log.see("end")
        self.root.update_idletasks()

    def _log_info(self, msg: str):
        self._log(msg, tag="info")

    def _log_error(self, msg: str):
        self._log(msg, tag="error")


# -------------------------- Main --------------------------

def main():
    # Ensure default output dir exists
    ensure_dir(DEFAULT_OUTPUT_DIR)

    root = tk.Tk()
    style = ttk.Style()
    try:
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass

    app = ExtractorApp(root)

    # Quick tip panel
    tip = (
        "Usage Tips:\n"
        "1) Paste TOC URL and provide CSS selectors for links, title, and content.\n"
        "2) Click 'Launch Browser & Open TOC', solve any challenges/login in the opened browser.\n"
        "3) Click 'I'm Ready', then 'Start Extraction'.\n"
        "4) Adjust delay to be kind to the site.\n"
        "5) By default, output does NOT include page URLs. Enable if you need provenance."
    )
    app._log_info(tip)

    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()


if __name__ == "__main__":
    main()
