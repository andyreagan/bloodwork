# uv supplies pyyaml so the build does not depend on which python3 is on PATH.
PYTHON ?= uv run --no-project --with pyyaml python3

# Wearable databases the pages are built from; listing them as
# prerequisites makes `make` rebuild when the data changes, not only the code.
STRAVA_DB = $(HOME)/projects/2026/strava-database/strava.db
WHOOP_DB  = $(HOME)/projects/2026/whoop-database/whoop.db
GARMIN_DB = $(HOME)/projects/2026/garmin-database/garmin.db
DBS = $(STRAVA_DB) $(WHOOP_DB) $(GARMIN_DB)

.PHONY: all update clean

all: index.html fitness.html correlations.html models.html

update: all
	git add -A
	git commit -m "Regenerate dashboards $(shell date +%Y-%m-%d)" || true
	git push

index.html: generate.py ranges.py bloodwork_data.yaml fitness_data.yaml $(STRAVA_DB)
	$(PYTHON) generate.py

fitness.html: generate_fitness.py fitness_data.yaml $(DBS)
	$(PYTHON) generate_fitness.py

correlations.html: generate_correlations.py bloodwork_data.yaml fitness_data.yaml $(DBS)
	$(PYTHON) generate_correlations.py

models.html: generate_models.py bloodwork_data.yaml fitness_data.yaml $(DBS)
	$(PYTHON) generate_models.py

clean:
	rm -f index.html fitness.html correlations.html models.html
