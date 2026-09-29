# uv supplies pyyaml so the build does not depend on which python3 is on PATH.
PYTHON ?= uv run --no-project --with pyyaml python3

.PHONY: all update clean

all: index.html fitness.html correlations.html models.html

update: all
	git add -A
	git commit -m "Regenerate dashboards $(shell date +%Y-%m-%d)" || true
	git push

index.html: generate.py bloodwork_data.yaml fitness_data.yaml
	$(PYTHON) generate.py

fitness.html: generate_fitness.py
	$(PYTHON) generate_fitness.py

correlations.html: generate_correlations.py bloodwork_data.yaml fitness_data.yaml
	$(PYTHON) generate_correlations.py

models.html: generate_models.py bloodwork_data.yaml fitness_data.yaml
	$(PYTHON) generate_models.py

clean:
	rm -f index.html fitness.html correlations.html models.html
