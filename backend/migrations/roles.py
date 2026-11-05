"""Bind historical privilege templates to this database's configured roles.

Default names preserve the existing synthetic migration contract. Only trusted
SQL templates use this helper; user values must never be passed to it.
"""

import re

from alembic import op


def execute_role_sql(statement: str) -> None:
    attributes = op.get_context().config.attributes
    roles = {
        "gl_api_app": attributes["gl_runtime_role"],
        "gl_api_migrator": attributes["gl_migrator_role"],
    }

    def identifier(match: re.Match) -> str:
        return '"' + roles[match.group()].replace('"', '""') + '"'

    bound = re.sub(r"\b(?:gl_api_app|gl_api_migrator)\b", identifier, statement)
    op.execute(bound)
