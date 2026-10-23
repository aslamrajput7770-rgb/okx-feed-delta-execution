# Thin wrappers so a new session does not have to rediscover the entry points.
# Every target is safe to run repeatedly. Nothing here sends an order except
# `live`, which is deliberately the one target that is not a default.

PY := ./.venv/bin/python
PAPER_MINUTES ?= 5
PAPER_ORDER_SIZE ?= 0.001

.PHONY: help setup test dry preflight paper paper-tg telegram-check live feed diagnose clean

help:
	@echo "make setup      install dependencies and run the tests"
	@echo "make test       offline test suite"
	@echo "make dry        offline dry run, synthetic tape, no socket, no order"
	@echo "make preflight  read-only venue check (live host, no orders)"
	@echo "make paper      live feed, reports the order, sends nothing  [PAPER_MINUTES=5]"
	@echo "make paper-tg   live feed, signals to Telegram, sends nothing"
	@echo "make telegram-check  prove the bot can reach the chat"
	@echo "make live       REAL ORDERS - needs DELTA_ORDER_SIZE set"
	@echo "make feed       raw OKX feed capture, no execution leg"
	@echo "make clean      remove the virtualenv and caches"

setup:
	./bootstrap.sh

test:
	$(PY) -m pytest tests -q

dry:
	$(PY) live_feed.py --dry-run --order-size $(PAPER_ORDER_SIZE)

preflight:
	. deploy/load_env.sh; \
	$(PY) preflight_delta.py --live

paper:
	./deploy/run_paper.sh $(PAPER_MINUTES)

paper-tg:
	./deploy/run_paper_telegram.sh $(PAPER_MINUTES)

telegram-check:
	$(PY) telegram_check.py

# Not a default target, and the runner itself refuses without DELTA_ORDER_SIZE.
live:
	@echo "This places real orders on Delta India." >&2
	./deploy/run_live.sh $(PAPER_MINUTES)

feed:
	$(PY) live_feed.py --minutes $(PAPER_MINUTES) --record capture.txt

diagnose:
	$(PY) diagnose_okx.py capture.txt

clean:
	rm -rf .venv .pytest_cache __pycache__ */__pycache__ */*/__pycache__
