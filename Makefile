SHELL := /bin/bash
PYTHON ?= python3
RUN_JSON ?= /tmp/podcast_run.json

.PHONY: help setup run chat sms email html open all discord unlock commit clean

help:
	@echo "Podcast Queue Report — available commands:"
	@echo ""
	@echo "  make setup             Copy .env.example -> .env (first run only)"
	@echo "  make run               Query the Podcasts DB, write $(RUN_JSON)"
	@echo "  make chat              Print the chat summary"
	@echo "  make sms               Print the SMS text"
	@echo "  make email             Print the email SUBJECT + body"
	@echo "  make html              Regenerate reports/podcast_report.html"
	@echo "  make open              Regenerate the HTML report and open it in your browser"
	@echo "  make all               Run once, then chat + sms + email + html"
	@echo "  make discord           Post the chat summary to Discord (needs DISCORD_WEBHOOK_URL in .env)"
	@echo "  make unlock            Clear stale git lock files (see scripts/git_unlock.py)"
	@echo "  make commit MSG='...'  Unlock, stage everything, and commit"
	@echo "  make clean             Remove the scratch $(RUN_JSON) file"

setup:
	@if [ -f .env ]; then \
		echo ".env already exists — leaving it alone."; \
	else \
		cp .env.example .env; \
		echo "Created .env — edit it with your own values before running anything else."; \
	fi
	@if [ -z "$$(git config user.email 2>/dev/null)" ]; then \
		echo "Reminder: also run 'git config user.email you@example.com' and 'git config user.name \"Your Name\"' in this repo before committing."; \
	fi

run:
	$(PYTHON) podcast_summary.py > $(RUN_JSON)

chat: run
	$(PYTHON) render_report.py $(RUN_JSON) chat

sms: run
	$(PYTHON) render_report.py $(RUN_JSON) sms

email: run
	$(PYTHON) render_report.py $(RUN_JSON) email

html: run
	@mkdir -p reports
	$(PYTHON) render_report.py $(RUN_JSON) html > reports/podcast_report.html
	@echo "Wrote reports/podcast_report.html"

open: html
	open reports/podcast_report.html

all: chat sms email html

discord: run
	$(PYTHON) render_report.py $(RUN_JSON) discord | $(PYTHON) scripts/post_discord.py

unlock:
	$(PYTHON) scripts/git_unlock.py

commit: unlock
	@if [ -z "$(MSG)" ]; then \
		echo "Usage: make commit MSG='your message'"; \
		exit 1; \
	fi
	git add -A
	@$(PYTHON) scripts/git_unlock.py > /dev/null
	git commit -m "$(MSG)"

clean:
	@rm -f $(RUN_JSON) 2>/dev/null || true
