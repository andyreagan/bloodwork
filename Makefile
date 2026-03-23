all: index.html correlations.html models.html

index.html: generate.py bloodwork_data.yaml fitness_data.yaml
	python3 generate.py

correlations.html: generate_correlations.py bloodwork_data.yaml fitness_data.yaml
	python3 generate_correlations.py

models.html: generate_models.py bloodwork_data.yaml fitness_data.yaml
	python3 generate_models.py
