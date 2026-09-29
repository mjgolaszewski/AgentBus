SHELL := /bin/bash
UV ?= uv

.PHONY: check test-client test-service release-build

check: lint

test-client: test

test-service: contract-test

release-build: release-smoke

include Makefile.fragment
