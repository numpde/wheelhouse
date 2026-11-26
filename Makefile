override SHELL := /bin/bash
override .SHELLFLAGS := -eu -o pipefail -c
override .DEFAULT_GOAL := help
override REPOSITORY_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))

# Capture literal command-line inputs before Make or the shell can interpret them.
# Omitted inputs are selected at the terminal by the function below.
override export WHEELHOUSE_EXPLICIT_INPUTS := $(foreach name,DOCKER_CONTEXT,$(if $(filter command line,$(origin $(name))),$(name)))
override export WHEELHOUSE_DOCKER_CONTEXT := $(value DOCKER_CONTEXT)
unexport DOCKER_CONTEXT

.PHONY: help
help: ## List workflows; inputs may prompt on a terminal and environment values never select them.
	@awk -F ':.*## ' '/^[a-z][a-z0-9_./-]*:.*## / { printf "  make %-28s %s\n", $$1, $$2 }' "$(REPOSITORY_ROOT)/Makefile"

# Shared shell behavior stays in shell files; Make only passes literal inputs.
override operator_inputs := source "$(REPOSITORY_ROOT)/make/operator.sh"

.PHONY: image package test proof tests/repo-checks tests/unit tests/integration publish/release
image: ## Build the pinned tools image; may use the network. Requires DOCKER_CONTEXT.
package: ## Build dist/wheelhouse.pyz and its SHA256SUMS; requires DOCKER_CONTEXT.
test: ## Run all repository and bundle tests; requires DOCKER_CONTEXT.
proof: test ## Alias for test.
tests/repo-checks: ## Run formatting, release-note, and bundle checks; requires DOCKER_CONTEXT.
tests/unit: ## Run artifact tests offline; requires DOCKER_CONTEXT.
tests/integration: ## Acquire online and verify installation offline; requires DOCKER_CONTEXT.
publish/release: ## Test and publish the newest release note's version; requires DOCKER_CONTEXT.
image package test tests/repo-checks tests/unit tests/integration publish/release:
	@$(operator_inputs); select_docker; \
	bash "$(REPOSITORY_ROOT)/make/run.sh" "$@" "$$docker_host"
