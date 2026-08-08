.DEFAULT_GOAL := help
SHELL := /bin/bash

# Override to build against a specific interpreter:
#   make setup PYTHON=/Library/Frameworks/Python.framework/Versions/3.14/bin/python3
PYTHON ?= python3
VENV := .venv
BIN := $(VENV)/bin

.PHONY: help setup check-tk lint fmt typecheck test test-fast test-browser run clean

help:
	@echo "setup        create $(VENV), install the package and dev extras, fetch Chromium"
	@echo "lint         ruff check + format check"
	@echo "fmt          apply ruff formatting"
	@echo "typecheck    mypy strict over src/"
	@echo "test         full suite, including browser-marked tests"
	@echo "test-fast    skip browser-marked tests (the CI matrix target)"
	@echo "test-browser only browser-marked tests"
	@echo "run          run the CLI: make run ARGS='--toc ... --link ...'"
	@echo "clean        remove the venv and tooling caches"

$(BIN)/python:
	$(PYTHON) -m venv $(VENV)

setup: $(BIN)/python
	$(BIN)/python -m pip install --quiet --upgrade pip
	$(BIN)/python -m pip install --quiet -e ".[dev]"
	$(BIN)/python -m playwright install chromium
	@$(MAKE) --no-print-directory check-tk

# The GUI needs Tk; the CLI does not. This warns rather than fails so a
# headless or CI setup still succeeds.
check-tk:
	@$(BIN)/python -c "import tkinter" 2>/dev/null && \
		echo "tk: ok ($$($(BIN)/python -c 'import tkinter; print(tkinter.TkVersion)'))" || { \
		echo ""; \
		echo "tk: MISSING - the CLI works, the GUI will not."; \
		echo "  Homebrew's python3 ships without _tkinter, and python-tk@3.11 /"; \
		echo "  python-tk@3.12 only help if you also have the matching brew python."; \
		echo "  /usr/bin/python3 has the deprecated Tk 8.5."; \
		echo "  Rebuild against a python.org framework build:"; \
		echo "    make clean"; \
		echo "    make setup PYTHON=/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"; \
		echo ""; }

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

fmt:
	$(BIN)/ruff format .
	$(BIN)/ruff check --fix .

typecheck:
	$(BIN)/mypy

test:
	$(BIN)/python -m pytest

# The CI matrix target. Every job but one runs this, so a matrix job never
# waits on a Chromium download.
test-fast:
	$(BIN)/python -m pytest -m "not browser"

test-browser:
	$(BIN)/python -m pytest -m browser

# make run ARGS='--toc https://... --link a.ch --title h1 --content article'
run:
	$(BIN)/python -m toc_extractor $(ARGS)

clean:
	rm -rf $(VENV) .mypy_cache .ruff_cache .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
