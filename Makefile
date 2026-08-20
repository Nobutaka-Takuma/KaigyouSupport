# Convenience targets. Everything here is a thin wrapper over the commands
# documented in the README.

DATABASE_URL ?= postgresql://kaigyou:kaigyou@127.0.0.1:5432/kaigyou
export DATABASE_URL

PY := .venv/bin

.PHONY: setup db migrate fetch load-local sample drop-sample stats scores status api web test clean

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

## Load the four public datasets from files you downloaded yourself, then
## rebuild the scores. This is the normal route: several of the publishers
## distribute through a click-through form rather than a stable URL.
##
##   make load-local DIR=~/Downloads
##
## Override any name that differs from the published default, e.g.
##   make load-local DIR=~/Downloads MESH=tblT001141H13.txt
CLINICS ?= 031_dental_facility_info_20260601.csv
MESH    ?= tblT001141H13.txt
MESH_BASELINE ?= tblT000847H13.txt
STATIONS ?= S12-25_GML.zip
BOUNDARIES ?= N03-20240101_13_GML.zip

load-local:
	@test -n "$(DIR)" || (echo "usage: make load-local DIR=<downloads directory>"; exit 1)
	$(PY)/kaigyou-etl run mhlw_dental_clinics   --input "$(DIR)/$(CLINICS)"
	$(PY)/kaigyou-etl run estat_population_mesh --input "$(DIR)/$(MESH)" \
	                                            --baseline "$(DIR)/$(MESH_BASELINE)"
	$(PY)/kaigyou-etl run mlit_stations         --input "$(DIR)/$(STATIONS)"
	$(PY)/kaigyou-etl run mlit_municipalities   --input "$(DIR)/$(BOUNDARIES)"
	$(PY)/kaigyou-etl drop-sample
	$(MAKE) stats scores status

## Synthetic development data, clearly labelled as such everywhere.
sample:
	$(PY)/kaigyou-etl generate-sample

drop-sample:
	$(PY)/kaigyou-etl drop-sample

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
