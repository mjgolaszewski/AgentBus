"""Executable ownership and boundary rules for AgentBus's flat two-context design."""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = yaml.safe_load((ROOT / "architecture-boundaries.yml").read_text())["architecture"]
MODULES = {
    "src/agentbus_client.py": ("client", "client"),
    "src/agentbus_transport_client.py": ("client", "client"),
    "src/agentbus_poll_client.py": ("client", "client"),
    "src/agentbus_presentation_client.py": ("client", "client"),
    "src/agentbus_parser_client.py": ("client", "client"),
    "src/agentbus_operator_client.py": ("client", "client"),
    "src/agentbus_rotation_client.py": ("client", "client"),
    "src/agentbus_rename_client.py": ("client", "client"),
    "src/agentbus_codex_rpc_client.py": ("client", "client"),
    "src/agentbus_codex_wake_client.py": ("client", "client"),
    "src/agentbus_wake_inbox_client.py": ("client", "client"),
    "src/agentbus_claim_recovery_service.py": ("service", "service"),
    "src/agentbus_message_assurance_service.py": ("service", "service"),
    "src/agentbus_message_authorization_service.py": ("service", "service"),
    "src/agentbus_presentation_service.py": ("service", "service"),
    "src/agentbus_reply_policy_service.py": ("service", "service"),
    "src/agentbus_request_limits_service.py": ("service", "service"),
    "src/agentbus_send_policy_service.py": ("service", "service"),
    "src/agentbus_slack_reply_service.py": ("service", "service"),
    "src/agentbus_slack_command_service.py": ("service", "service"),
    "src/agentbus_slack_socket_service.py": ("service", "service"),
    "src/agentbus_metrics_service.py": ("service", "service"),
    "src/agentbus_send_rate_service.py": ("service", "service"),
    "src/agentbus_settings_service.py": ("service", "service"),
    "src/agentbus_service.py": ("service", "service"),
    "src/agentbus_status_api_service.py": ("service", "service"),
    "src/agentbus_participation_service.py": ("service", "service"),
    "src/agentbus_participation_store_service.py": ("service", "service"),
    "src/agentbus_control_store_service.py": ("service", "service"),
    "src/agentbus_participation_api_service.py": ("service", "service"),
}


def tree(relative: str) -> ast.Module:
    return ast.parse((ROOT / relative).read_text(encoding="utf-8"), filename=relative)


def imports(relative: str) -> set[str]:
    values: set[str] = set()
    for node in ast.walk(tree(relative)):
        if isinstance(node, ast.Import):
            values.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            values.add(node.module)
    return values


def test_architecture_registry_covers_every_production_module() -> None:
    roots = [ROOT / root for root in CONFIG["source_roots"]]
    discovered = {
        path.relative_to(ROOT).as_posix()
        for root in roots
        for path in root.rglob("*.py")
        if path.name != "__init__.py"
    }
    assert set(MODULES) == discovered


def test_production_modules_respect_loc_cap() -> None:
    cap = CONFIG["production_module_policy"]["max_loc"]
    violations = {
        relative: len((ROOT / relative).read_text(encoding="utf-8").splitlines())
        for relative in MODULES
        if len((ROOT / relative).read_text(encoding="utf-8").splitlines()) > cap
    }
    assert not violations


def test_production_modules_map_to_exactly_one_layer() -> None:
    layers = CONFIG["layers"]
    assert set(layers) == {"client", "service"}
    for relative, (layer, _context) in MODULES.items():
        assert Path(relative).stem.endswith(layer)
        assert layers[layer]["required"] is True


def test_production_modules_map_to_exactly_one_bounded_context() -> None:
    assert CONFIG["bounded_contexts"]["context_root_tokens"] == ["client", "service"]
    assert {context for _layer, context in MODULES.values()} == {"client", "service"}
    metadata = CONFIG["package_metadata_ownership"]
    assert metadata == [{
        "path": "src/__init__.py",
        "layer": "package_metadata",
        "context": "shared_kernel",
        "exports": ["agentbus_client", "agentbus_service"],
    }]


def test_context_import_boundaries() -> None:
    client_imports = imports("src/agentbus_client.py")
    service_imports = imports("src/agentbus_service.py")
    for prefix in CONFIG["layers"]["client"]["forbidden_import_prefixes"]:
        assert not any(value == prefix or value.startswith(prefix + ".") for value in client_imports)
    assert "src.agentbus_client" not in service_imports
    assert not service_imports.intersection({"argparse", "fcntl", "subprocess"})


def test_command_and_query_populations_are_closed() -> None:
    client = tree("src/agentbus_client.py")
    service = tree("src/agentbus_service.py")
    assignments: dict[str, ast.expr] = {}
    for module in (client, service):
        for node in module.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name, value = node.targets[0].id, node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                name, value = node.target.id, node.value
            else:
                continue
            if name in {"CLI_OPERATIONS", "API_OPERATIONS"}:
                assignments[name] = value
    assert set(assignments) == {"CLI_OPERATIONS", "API_OPERATIONS"}
    assert all(isinstance(value, ast.Dict) and value.keys for value in assignments.values())


def test_http_routers_remain_thin_transport_adapters() -> None:
    policy = CONFIG["thin_router_policy"]
    handlers = {
        node.name: node
        for node in tree("src/agentbus_service.py").body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("api_")
    }
    assert set(handlers) == {"api_send", "api_read", "api_info", "api_inbox", "api_claim"}
    status_handlers = {node.name: node for node in tree("src/agentbus_status_api_service.py").body
                       if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("api_")}
    assert set(status_handlers) == {"api_health", "api_status"}
    handlers.update(status_handlers)
    for handler in handlers.values():
        branches = sum(isinstance(node, (ast.If, ast.Match, ast.Try)) for node in ast.walk(handler))
        loops = sum(isinstance(node, (ast.For, ast.AsyncFor, ast.While)) for node in ast.walk(handler))
        assert branches <= policy["max_branch_nodes"], handler.name
        assert loops <= policy["max_loop_nodes"], handler.name


def test_cross_context_duplication_stays_below_declared_block_size() -> None:
    minimum = CONFIG["duplication_policy"]["min_duplicate_lines"]
    assert CONFIG["duplication_policy"]["scoped_to_bounded_context"] is True
    blocks: defaultdict[tuple[str, ...], set[str]] = defaultdict(set)
    for relative in MODULES:
        lines = [line.strip() for line in (ROOT / relative).read_text(encoding="utf-8").splitlines()]
        for index in range(max(0, len(lines) - minimum + 1)):
            block = tuple(lines[index:index + minimum])
            if any(block):
                blocks[block].add(relative)
    duplicates = [paths for paths in blocks.values() if len(paths) > 1]
    assert not duplicates
