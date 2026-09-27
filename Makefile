SHELL := /bin/bash
UV ?= uv

.PHONY: check test-client test-service release-build

check:
	$(UV) run --locked python scripts/check_source.py

test-client:
	PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(UV) run --locked --extra dev pytest tests/test_launcher.py

test-service:
	PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(UV) run --locked --extra dev pytest tests/test_service.py

release-build:
	$(UV) run --locked python scripts/build_release.py

include Makefile.fragment
