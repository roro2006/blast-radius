IMAGE ?= blast-radius-entry
PYTHON ?= python3
SAMPLE := $(CURDIR)/sample
OUTPUT := $(CURDIR)/output.jsonl

# The flags the scorer runs your image with. The scorer is linux/amd64, so on
# an Apple Silicon Mac --platform makes your local run match it.
RUN_FLAGS := --rm --platform linux/amd64 --network none --cpus 2 \
	--memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
	--tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
	--security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
	--volume $(SAMPLE)/commits:/data/commits:ro \
	--volume $(SAMPLE)/tests.json:/data/tests.json:ro

.PHONY: build sample check

build:
	docker build --platform linux/amd64 --tag $(IMAGE) .

# Runs your image once on the sample commits under the scorer's limits and
# scores what it printed.
sample: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	$(PYTHON) score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT)

# The output's rules only: every line well formed and every commit answered.
# A wrong selection still passes here; make sample counts the misses.
check: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	$(PYTHON) score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT) > /dev/null && echo "format ok"

# Fast loop without Docker: needs build/sqlglot (pinned source) and maps/maps.json.gz.
.PHONY: local maps
local:
	DATA=$(SAMPLE) SRC=$(CURDIR)/build/sqlglot $(PYTHON) -I selector.py > $(OUTPUT)
	$(PYTHON) score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT)

# Rebuild the maps from the pinned commit (about 6 minutes). Output is committed.
maps:
	docker build --platform linux/amd64 --tag blast-radius-maps tools/cov
	mkdir -p build/raw
	docker run --rm --platform linux/amd64 --volume $(CURDIR)/build/raw:/out blast-radius-maps
	$(PYTHON) tools/build_maps.py build/raw $(SAMPLE)/tests.json maps/maps.json.gz
