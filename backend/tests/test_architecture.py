"""Keep the API independent and the legacy bot SQL implementation canonical."""

import ast
from pathlib import Path

from gl_api.bot_contract import ARG_TYPES
from gl_api.bot_repository import Repository
from gl_bot.repository import Repository as LegacyRepository

ROOT = Path(__file__).resolve().parents[1]


def test_api_does_not_import_bot_or_application_orm():
    forbidden = {"gl_bot", "sqlalchemy", "psycopg"}
    for source in (ROOT / "gl_api").rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            imports = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            assert not forbidden.intersection(name.split(".")[0] for name in imports), source


def test_internal_allowlist_matches_async_repository_methods():
    tree = ast.parse((ROOT / "gl_api" / "bot_repository.py").read_text())
    definitions = {
        node.name: node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)
    }
    for operation, kinds in ARG_TYPES.items():
        method = getattr(Repository, operation)
        definition = definitions[operation]
        assert callable(method)
        assert len(definition.args.args) - 1 == len(kinds), operation


def test_legacy_bot_reuses_api_sql_instead_of_forking_it():
    assert LegacyRepository is Repository
