"""Validate BRAIN selection and combo expressions against live operator scope."""

from alpha_research.expressions.validator import Parser, ParseError


class SuperScopeError(ValueError):
    pass


def validate_super_expressions(selection: str, combo: str, operator_rows: list):
    """Check syntax and live operator scope without imposing Regular rules."""
    if not isinstance(operator_rows, list) or not operator_rows:
        raise SuperScopeError("live Super operator catalog is unavailable")
    supported = {
        scope: {row["name"] for row in operator_rows
                if isinstance(row, dict) and isinstance(row.get("name"), str)
                and isinstance(row.get("scope"), list)
                and scope in row["scope"]}
        for scope in ("SELECTION", "COMBO")}
    for label, expression, scope in (
            ("selection", selection, "SELECTION"),
            ("combo", combo, "COMBO")):
        if not isinstance(expression, str) or not expression.strip():
            raise SuperScopeError(f"{label} expression is required")
        try:
            ast = Parser(expression).parse()
        except ParseError as exc:
            raise SuperScopeError(f"{label} syntax is invalid: {exc}") from exc
        stack = [ast]
        while stack:
            node = stack.pop()
            if node.kind == "call" and node.value not in supported[scope]:
                raise SuperScopeError(
                    f"{label} operator is not live-supported: {node.value}")
            stack.extend(node.args)
            stack.extend(node.kwargs.values())
            stack.extend(node.children)
    return {"selection": "valid", "combo": "valid"}
