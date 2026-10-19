"""Executable ownership and boundary rules for AgentBus's flat two-context design."""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = yaml.safe_load((ROOT / "architecture-boundaries.yml").read_text())["architecture"]
MODULES = {
    "src/agentbus_announcement_lifecycle_service.py": ("service", "service"),
    "src/agentbus_channel_api_service.py": ("service", "service"),
    "src/agentbus_channel_grants_service.py": ("service", "service"),
    "src/agentbus_channel_ingress_service.py": ("service", "service"),
    "src/agentbus_channel_migration_service.py": ("service", "service"),
    "src/agentbus_channel_store_service.py": ("service", "service"),
    "src/agentbus_transaction_service.py": ("service", "service"),
    "src/agentbus_channel_migration_client.py": ("client", "client"),
    "src/agentbus_enrollment_client.py": ("client", "client"),
    "src/agentbus_enrollment_approval_service.py": ("service", "service"),
    "src/agentbus_message_lifecycle_service.py": ("service", "service"),
    "src/agentbus_lifecycle_migration_service.py": ("service", "service"),
    "src/agentbus_lifecycle_client.py": ("client", "client"),
    "src/agentbus_lifecycle_api_service.py": ("service", "service"),
    "src/agentbus_retention_service.py": ("service", "service"),
    "src/agentbus_scope_client.py": ("client", "client"),
    "src/agentbus_delivery_client.py": ("client", "client"),
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
    "src/agentbus_codex_wake_events_client.py": ("client", "client"),
    "src/agentbus_codex_wake_intent_client.py": ("client", "client"),
    "src/agentbus_wake_inbox_client.py": ("client", "client"),
    "src/agentbus_delivery_api_service.py": ("service", "service"),
    "src/agentbus_delivery_runtime_service.py": ("service", "service"),
    "src/agentbus_delivery_lifecycle_service.py": ("service", "service"),
    "src/agentbus_local_delivery_service.py": ("service", "service"),
    "src/agentbus_claim_recovery_service.py": ("service", "service"),
    "src/agentbus_federation_service.py": ("service", "service"),
    "src/agentbus_federation_directory_service.py": ("service", "service"),
    "src/agentbus_federation_inbox_service.py": ("service", "service"),
    "src/agentbus_federation_ordinary_service.py": ("service", "service"),
    "src/agentbus_federation_wire_service.py": ("service", "service"),
    "src/agentbus_federation_bridge_service.py": ("service", "service"),
    "src/agentbus_federation_slack_service.py": ("service", "service"),
    "src/agentbus_federation_runtime_service.py": ("service", "service"),
    "src/agentbus_federation_api_service.py": ("service", "service"),
    "src/agentbus_message_assurance_service.py": ("service", "service"),
    "src/agentbus_message_authorization_service.py": ("service", "service"),
    "src/agentbus_presentation_service.py": ("service", "service"),
    "src/agentbus_operator_presentation_service.py": ("service", "service"),
    "src/agentbus_operator_query_service.py": ("service", "service"),
    "src/agentbus_worker_status_service.py": ("service", "service"),
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


def boundary_violations(modules: dict[str, ast.Module]) -> list[str]:
    violations = []
    for relative, module in modules.items():
        layer = MODULES[relative][0]
        policy = CONFIG['layers'][layer]
        for node in ast.walk(module):
            names = ([alias.name for alias in node.names] if isinstance(node, ast.Import) else
                     [node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
            for name in names:
                target = 'src/' + name.removeprefix('src.').replace('.', '/') + '.py'
                if (any(name == prefix or name.startswith(prefix + '.') for prefix in policy['forbidden_import_prefixes'])
                        or (target in MODULES and MODULES[target][0] in policy['forbidden_layer_imports'])):
                    violations.append(f'{relative}:{node.lineno}:{name}')
    return violations


def test_context_import_boundaries() -> None:
    modules = {relative: tree(relative) for relative in MODULES}
    assert not boundary_violations(modules)
    # A forbidden dependency in a non-entrypoint must be caught too.
    mutated = dict(modules)
    mutated['src/agentbus_poll_client.py'] = ast.parse('from src.agentbus_participation_service import next_inbox_interval_values')
    assert boundary_violations(mutated)


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
    registry = yaml.safe_load((ROOT / 'governance/application-operations.yml').read_text())
    for population, name in [('cli', 'CLI_OPERATIONS'), ('api', 'API_OPERATIONS')]:
        value = assignments[name]
        assert isinstance(value, ast.Dict)
        keys = [ast.literal_eval(key) for key in value.keys]
        declared = [o for o in registry['operations'] if o['population'] == population]
        assert len(keys) == len(set(keys))
        assert set(keys) == {o['population_key'] for o in declared}
        for operation in declared:
            if operation['semantic_kind'] == 'query':
                assert operation['authoritative_mutation'] is False
                assert operation['allowed_mutation_ports'] == []
                assert operation['allowed_authority_ports'] == []
    from architecture_query_probe import assert_cli_query_nonmutation, assert_query_nonmutation
    assert_query_nonmutation(registry['operations'])
    assert_cli_query_nonmutation(registry['operations'])
    # Negative control: the same query authority rejects an actual SQLite write.
    import sqlite3
    try:
        assert_query_nonmutation(registry['operations'], inject=lambda store:
            store._db.execute("INSERT INTO metadata(key,value) VALUES ('query-mutation-control','bad')"))
    except sqlite3.DatabaseError as error:
        assert 'not authorized' in str(error)
    else:
        raise AssertionError('Query mutation control was not rejected')


def router_violations(handler: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    policy = CONFIG['thin_router_policy']
    branches = sum(isinstance(node, (ast.If, ast.Match, ast.Try)) for node in ast.walk(handler))
    loops = sum(isinstance(node, (ast.For, ast.AsyncFor, ast.While)) for node in ast.walk(handler))
    return branches > policy['max_branch_nodes'] or loops > policy['max_loop_nodes']


def test_http_routers_remain_thin_transport_adapters() -> None:
    registry = yaml.safe_load((ROOT / 'governance/application-operations.yml').read_text())
    declared = {symbol for operation in registry['operations'] if operation['population'] == 'api'
                for symbol in operation['entrypoints']}
    discovered = {f'{relative}::{node.name}':node for relative in MODULES
                  for node in tree(relative).body
                  if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('api_')}
    assert set(discovered) == declared
    assert not {name for name, handler in discovered.items() if router_violations(handler)}
    mutation = ast.parse('def api_session_enroll():\n' + '    if True: pass\n' * 5).body[0]
    assert router_violations(mutation)


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
