# wallexbot: mock / shadow only. Live trading does not exist in this build.
# Works from a fresh clone: `make venv test demo`.

PYTHON   ?= python3.12
VENV     ?= .venv
PY       := $(VENV)/bin/python
RUN_DIR  ?= data/run
DB       ?= $(RUN_DIR)/bot.db
BALANCE  ?= 1000
# wall-clock seconds between shadow ticks; at most max_data_age_s / 2 (15), else the bot exits 2
INTERVAL ?= 12
IMAGE    ?= wallexbot:shadow
PROFILE  ?= strict
# e.g. HEALTH_ARGS=--allow-killswitch: report an engaged kill switch as a warning instead of failing
HEALTH_ARGS ?=

.PHONY: help venv test demo backtest shadow guardian docker-build docker-up docker-down healthcheck clean

help:
	@echo "targets: venv test demo backtest shadow guardian docker-build docker-up docker-down healthcheck clean"

$(PY):
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install -e '.[dev]'

venv: $(PY)

test: $(PY)
	$(PY) -m pytest -q

# Offline: scripted mock round trip, then three ticks of the standalone guardian DEMO on its own
# throwaway database and mock broker. No network, no keys.
demo: $(PY)
	@mkdir -p $(RUN_DIR)
	$(PY) -m wallexbot demo --db $(RUN_DIR)/demo.db
	$(PY) -m wallexbot guardian --db $(RUN_DIR)/demo_guardian.db --mode mock --demo-db --ticks 3 --interval 1

# Exploratory grid on train+validation of local 1m history (needs data/history, see docs/BACKTEST.md).
backtest: $(PY)
	@test -d data/history || { echo "data/history not found: download 1m history first (docs/BACKTEST.md)"; exit 2; }
	$(PY) -m wallexbot backtest

# Local shadow run: public GETs to api.wallex.ir only, simulated fills, no orders sent. Ctrl-C stops it.
# The guardian runs in-process. PROFILE=demo selects the EXERCISE profile (no edge claimed).
shadow: $(PY)
	@mkdir -p $(RUN_DIR)
	$(PY) -m wallexbot bot --db $(DB) --mode shadow --balance $(BALANCE) --interval-s $(INTERVAL) --profile $(PROFILE)

# Standalone guardian DEMO only (own mock broker, own throwaway DB). It refuses a bot's database.
guardian: $(PY)
	@mkdir -p $(RUN_DIR)
	$(PY) -m wallexbot guardian --db $(RUN_DIR)/demo_guardian.db --mode mock --demo-db --ticks 3 --interval 1

docker-build:
	docker build $(if $(BASE_IMAGE),--build-arg BASE_IMAGE=$(BASE_IMAGE)) -t $(IMAGE) .

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down

# Exit 1 if a heartbeat is old, guardian alerts are recent, or the kill switch is engaged.
healthcheck: $(PY)
	$(PY) deploy/healthcheck.py --db $(DB) $(HEALTH_ARGS)

clean:
	rm -rf .pytest_cache build *.egg-info src/*.egg-info
	find . -name __pycache__ -type d -not -path './$(VENV)/*' -prune -exec rm -rf {} +
	rm -f $(RUN_DIR)/demo.db* $(RUN_DIR)/demo_guardian.db*
	@echo "kept: $(VENV), $(DB) and other run data (delete $(RUN_DIR) yourself if you want it gone)"
