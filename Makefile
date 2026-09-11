PYTHON ?= python3

.PHONY: install demo demo-crustdata-replay test check authority agent executor frontend stack neo4j cli

install:
	$(PYTHON) -m pip install -e ".[dev]"

demo:
	PYTHONPATH=backend $(PYTHON) -m writai.demo

demo-crustdata-replay:
	PYTHONPATH=backend $(PYTHON) -m writai.crustdata_demo

test:
	PYTHONPATH=backend $(PYTHON) -m pytest

# One gate, one definition. scripts/check.sh is what CI runs, so a step added
# there reaches every reviewer without anyone remembering to edit this file too.
check:
	PYTHON_BIN=$(PYTHON) bash scripts/check.sh

authority:
	PYTHONPATH=backend $(PYTHON) -m uvicorn writai.services.authority_api:app --port 8001 --reload

agent:
	PYTHONPATH=backend $(PYTHON) -m uvicorn writai.services.agent_api:app --port 8002 --reload

executor:
	PYTHONPATH=backend $(PYTHON) -m uvicorn writai.services.executor_api:app --port 8003 --reload

frontend:
	cd frontend && npm run dev

stack:
	./scripts/run_stack.sh

neo4j:
	docker compose up neo4j

cli:
	PYTHONPATH=backend $(PYTHON) -m writai.cli --help
