.PHONY: all fetch parse generate push clean

SYNOLOGY := andyreagan@100.120.245.106
ORG_SOURCE := /var/services/homes/andyreagan/org/bloodwork.org

all: fetch parse generate

fetch:
	@echo "→ Fetching bloodwork.org from Synology..."
	rsync -a $(SYNOLOGY):$(ORG_SOURCE) ./

parse: bloodwork.org
	@echo "→ Parsing bloodwork.org → bloodwork.db..."
	python3 parse.py

generate: bloodwork.db
	@echo "→ Generating index.html..."
	python3 generate.py

push:
	git add -A
	git commit -m "Update bloodwork dashboard $$(date +%Y-%m-%d)" || true
	git push

clean:
	rm -f bloodwork.db index.html

# Full rebuild + push
deploy: all push
