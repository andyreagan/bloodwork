all: index.html fitness.html correlations.html models.html

index.html: generate.py bloodwork_data.yaml fitness_data.yaml
	python3 generate.py

fitness.html: generate_fitness.py
	python3 generate_fitness.py

correlations.html: generate_correlations.py bloodwork_data.yaml fitness_data.yaml
	python3 generate_correlations.py

models.html: generate_models.py bloodwork_data.yaml fitness_data.yaml
	python3 generate_models.py
