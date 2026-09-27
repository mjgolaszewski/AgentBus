SHELL := /bin/bash
UV ?= uv

.PHONY: check test test-client test-service

check:
	$(UV) run --locked python scripts/check_source.py

test:
	PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(UV) run --locked --group dev pytest

test-client:
	PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(UV) run --locked --group dev pytest tests/test_launcher.py

test-service:
	PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(UV) run --locked --group dev pytest tests/test_service.py

-include Makefile.fragment
