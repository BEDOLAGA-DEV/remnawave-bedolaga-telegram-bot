"""Traffic purchases orchestrate CRUD and accounting, never the reverse."""

import ast
from pathlib import Path


def test_subscription_crud_does_not_import_traffic_orchestrators():
    source = Path('app/database/crud/subscription.py').read_text()
    forbidden = {
        'app.services.traffic_accounting_service',
        'app.services.traffic_purchase_service',
    }
    imports = {node.module for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom)}
    assert not imports & forbidden
