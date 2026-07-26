PYTHON ?= python3

.PHONY: install demo test check authority agent executor frontend stack neo4j cli

install:
	$(PYTHON) -m pip install -e ".[dev]"

demo:
	PYTHONPATH=backend $(PYTHON) -m dragback.demo

test:
	PYTHONPATH=backend $(PYTHON) -m pytest

check:
	PYTHONPATH=backend $(PYTHON) -m pytest
	$(PYTHON) -m ruff check backend
	$(PYTHON) -m mypy backend
	$(PYTHON) -m compileall -q backend
	cd frontend && npm test
	cd frontend && npm run typecheck
	cd frontend && npm run build

authority:
	PYTHONPATH=backend $(PYTHON) -m uvicorn dragback.services.authority_api:app --port 8001 --reload

agent:
	PYTHONPATH=backend $(PYTHON) -m uvicorn dragback.services.agent_api:app --port 8002 --reload

executor:
	PYTHONPATH=backend $(PYTHON) -m uvicorn dragback.services.executor_api:app --port 8003 --reload

frontend:
	cd frontend && npm run dev

stack:
	./scripts/run_stack.sh

neo4j:
	docker compose up neo4j

cli:
	PYTHONPATH=backend $(PYTHON) -m dragback.cli --help
