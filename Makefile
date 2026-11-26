ifndef ENGINE_CONTEXT
$(error Set ENGINE_CONTEXT, for example: make test ENGINE_CONTEXT=science)
endif

IMAGE := wheelhouse:proof
DOCKER := docker --context "$(ENGINE_CONTEXT)"
RUN := $(DOCKER) run --rm --pull never \
    -v "$(CURDIR)/tests:/tests:ro" -w /tests --entrypoint python

.PHONY: test proof image

test: image
	$(RUN) --network none $(IMAGE) -m unittest -v test_artifacts
	@set -eu; \
	volume=$$($(DOCKER) volume create); \
	trap '$(DOCKER) volume rm "$$volume" >/dev/null' 0; \
	$(RUN) -v "$$volume:/trial" $(IMAGE) \
	    -m unittest -v test_wheelhouse.Online; \
	$(RUN) --network none -v "$$volume:/trial:ro" $(IMAGE) \
	    -m unittest -v test_wheelhouse.Offline

proof: test

image:
	$(DOCKER) build --tag $(IMAGE) .
