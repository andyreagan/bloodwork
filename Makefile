all: index.html correlations.html

index.html: generate.py bloodwork_data.yaml fitness_data.yaml
	python3 generate.py

correlations.html: generate_correlations.py bloodwork_data.yaml fitness_data.yaml
	python3 generate_correlations.py
