SHELL := /bin/bash
UV ?= uv

.PHONY: check test test-client test-service release-build governance-truthfulness release-check governance-exposure-scan governance-validate

check:
	$(UV) run --locked python scripts/check_source.py

test:
	PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(UV) run --locked --group dev pytest

test-client:
	PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(UV) run --locked --group dev pytest tests/test_launcher.py

test-service:
	PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(UV) run --locked --group dev pytest tests/test_service.py

release-build:
	$(UV) run --locked python scripts/build_release.py

BCF_PYTHON ?= python3
BCF_EVIDENCE_DIR ?= .artifacts/bcf

governance-truthfulness:
	$(BCF_PYTHON) scripts/governance_truth.py --repo-root . --evidence-dir $(BCF_EVIDENCE_DIR)

governance-exposure-scan:
	@cd . && $(BCF_PYTHON) scripts/check_governance_exposure.py --repo-root .

governance-validate:
	@cd . && $(BCF_PYTHON) scripts/validate_governance_yaml.py --repo-root .

release-check:
	@mkdir -p $(BCF_EVIDENCE_DIR)
	@for gate in governance-exposure-scan governance-validate; do \
		$(BCF_PYTHON) scripts/governance_evidence.py --repo-root . run --gate $$gate --output $(BCF_EVIDENCE_DIR)/$$gate || exit $$?; \
	done
	$(MAKE) BCF_PYTHON=$(BCF_PYTHON) governance-truthfulness
