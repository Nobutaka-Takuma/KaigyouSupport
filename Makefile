# Convenience targets. Everything here is a thin wrapper over the commands
# documented in the README.

DATABASE_URL ?= postgresql://kaigyou:kaigyou@127.0.0.1:5432/kaigyou
export DATABASE_URL

PY := .venv/bin

.PHONY: setup db migrate fetch sample stats scores status api web test clean

setup:
	python3 -m venv .venv
	$(PY)/pip install -e "./server[dev]"
	cd web && npm install

db:
	docker compose up -d db

migrate:
	$(PY)/kaigyou-etl migrate

## Attempt every configured public data source. Exits 2 if any could not be
## obtained; the reasons are recorded and shown by `make status`.
fetch:
	$(PY)/kaigyou-etl run-all

## Synthetic development data, clearly labelled as such everywhere.
sample:
	$(PY)/kaigyou-etl generate-sample

stats:
	$(PY)/kaigyou-etl refresh-stats

scores:
	$(PY)/kaigyou-etl compute-scores

status:
	$(PY)/kaigyou-etl status

api:
	$(PY)/uvicorn kaigyou_api.main:app --reload --port 8000

web:
	cd web && npm run dev

test:
	$(PY)/python -m pytest server/tests -q

clean:
	rm -rf .venv web/node_modules web/dist
