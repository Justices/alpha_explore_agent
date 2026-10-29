#!/usr/bin/env python3
"""BRAIN alpha expression validator — static compliance checks (AGENTS.md rules).

Encodes the iron rules as code so a non-compliant expression can never
reach the platform:
  - single dataset, fields must exist in the live catalog
  - PPA only: operator_count <= 8 and at most 3 distinct fields
  - banned operators: trade_when / add / multiply
  - no '+' or '*' splicing of branches (strictest interpretation)
  - field-field '-' or '/' (difference / ratio) only with a recorded
    pre-registration justification (--allow-field-diff-ratio --reason)
  - VECTOR fields must be adapted by a vec_* operator first
  - ending-operator rules: no vec_* ending, no pure ts_* ending
    (ts_zscore / ts_rank are allowed endings), binary endings warn

Usage:
  alpha_validator.py check "EXPR" --dataset risk60 --catalog runs/datafields_risk60.json
  alpha_validator.py batch exprs.txt --dataset risk60 --catalog runs/datafields_risk60.json
Exit codes: 0 = all pass, 1 = at least one reject, 2 = input error.
"""
import argparse
import json
import os
import re
import sys

if __package__:
    from .ppa import PPA_MAX_FIELDS, PPA_MAX_OPERATORS
else:
    from ppa import PPA_MAX_FIELDS, PPA_MAX_OPERATORS

# ---------------------------------------------------------------- tokenizer
TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<string>"[^"]*"|'[^']*')
  | (?P<number>\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>==|!=|>=|<=|>|<|\+|-|\*|/|\?|:|\(|\)|,|=)
""", re.VERBOSE)


class ParseError(Exception):
    pass


def tokenize(text):
    tokens, pos = [], 0
    while pos < len(text):
        m = TOKEN_RE.match(text, pos)
        if not m:
            raise ParseError(f"unexpected character {text[pos]!r} at {pos}")
        pos = m.end()
        if m.lastgroup != "ws":
            tokens.append((m.lastgroup, m.group()))
    tokens.append(("eof", ""))
    return tokens


# ---------------------------------------------------------------- AST
class Node:
    __slots__ = ("kind", "value", "args", "kwargs", "children")

    def __init__(self, kind, value=None, args=None, kwargs=None, children=None):
        self.kind = kind            # num|str|ident|call|unary|binary|ternary
        self.value = value          # literal value / operator name / ident name
        self.args = args or []      # call positional args
        self.kwargs = kwargs or {}  # call named args
        self.children = children or []  # unary/binary/ternary operands


class Parser:
    def __init__(self, text):
        self.tokens = tokenize(text)
        self.i = 0

    def peek(self, ahead=0):
        return self.tokens[min(self.i + ahead, len(self.tokens) - 1)]

    def next(self):
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def expect(self, value):
        kind, val = self.next()
        if val != value:
            raise ParseError(f"expected {value!r}, got {val!r}")

    def parse(self):
        node = self.parse_ternary()
        if self.peek()[0] != "eof":
            raise ParseError(f"trailing input: {self.peek()[1]!r}")
        return node

    def parse_ternary(self):
        cond = self.parse_comparison()
        if self.peek()[1] == "?":
            self.next()
            a = self.parse_ternary()
            self.expect(":")
            b = self.parse_ternary()
            return Node("ternary", children=[cond, a, b])
        return cond

    def parse_comparison(self):
        left = self.parse_additive()
        while self.peek()[1] in ("==", "!=", ">=", "<=", ">", "<"):
            op = self.next()[1]
            right = self.parse_additive()
            left = Node("binary", value=op, children=[left, right])
        return left

    def parse_additive(self):
        left = self.parse_multiplicative()
        while self.peek()[1] in ("+", "-"):
            op = self.next()[1]
            right = self.parse_multiplicative()
            left = Node("binary", value=op, children=[left, right])
        return left

    def parse_multiplicative(self):
        left = self.parse_unary()
        while self.peek()[1] in ("*", "/"):
            op = self.next()[1]
            right = self.parse_unary()
            left = Node("binary", value=op, children=[left, right])
        return left

    def parse_unary(self):
        if self.peek()[1] == "-":
            self.next()
            return Node("unary", value="-", children=[self.parse_unary()])
        if self.peek()[1] == "+":
            self.next()
            return self.parse_unary()
        return self.parse_atom()

    def parse_atom(self):
        kind, val = self.next()
        if kind == "number":
            return Node("num", value=val)
        if kind == "string":
            return Node("str", value=val)
        if val == "(":
            node = self.parse_ternary()
            self.expect(")")
            return node
        if kind == "ident":
            if self.peek()[1] == "(":
                self.next()
                args, kwargs = [], {}
                if self.peek()[1] != ")":
                    while True:
                        if self.peek()[0] == "ident" and self.peek(1)[1] == "=":
                            key = self.next()[1]
                            self.next()  # '='
                            if key in kwargs:
                                raise ParseError(f"duplicate named argument: {key}")
                            kwargs[key] = self.parse_ternary()
                        else:
                            args.append(self.parse_ternary())
                        if self.peek()[1] == ",":
                            self.next()
                            continue
                        break
                self.expect(")")
                return Node("call", value=val, args=args, kwargs=kwargs)
            return Node("ident", value=val)
        raise ParseError(f"unexpected token {val!r}")


def walk(node, ancestors=()):
    yield node, ancestors
    kids = list(node.args) + list(node.kwargs.values()) + list(node.children)
    for kid in kids:
        yield from walk(kid, ancestors + (node,))


def subtree_has_field(node, field_names):
    return any(n.kind == "ident" and n.value in field_names
               for n, _ in walk(node))


def raw_field(node, field_names):
    if node.kind == "ident":
        return node.value in field_names
    return (node.kind == "call" and node.value.startswith("vec_")
            and len(node.args) == 1 and not node.kwargs
            and raw_field(node.args[0], field_names))


# ---------------------------------------------------------------- catalogs
def extract_rows(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("result", "results"):
            if key in data:
                rows = extract_rows(data[key])
                if rows:
                    return rows
    return []


def load_catalog(path):
    with open(path) as f:
        rows = extract_rows(json.load(f))
    catalog = {}
    for row in rows:
        if isinstance(row, dict) and "id" in row:
            ds = row.get("dataset") or {}
            catalog[row["id"]] = {
                "type": row.get("type", "MATRIX"),
                "dataset": ds.get("id") if isinstance(ds, dict) else None,
            }
    return catalog


def load_operators(path):
    if path and os.path.exists(path):
        with open(path) as f:
            rows = extract_rows(json.load(f))
        names = {r["name"] for r in rows
                 if isinstance(r, dict) and "name" in r}
        if names:
            return names
    return None  # unknown-operator check disabled


# ---------------------------------------------------------------- rules
BANNED_OPERATORS = {"trade_when", "add", "multiply"}
GROUP_NAMES = {"market", "sector", "industry", "subindustry",
               "country", "exchange", "currency"}
NON_FIELD_IDENTS = GROUP_NAMES | {"true", "false"}
TS_ALLOWED_ENDINGS = {"ts_zscore", "ts_rank"}


def regular_operator_names(rows):
    """Admit only operators whose live metadata explicitly includes REGULAR."""
    return {row["name"] for row in rows or []
            if isinstance(row, dict) and isinstance(row.get("name"), str) and row["name"]
            and isinstance(row.get("scope"), list) and "REGULAR" in row["scope"]}


def _live_signature(name, definition):
    """Parse only unambiguous callable definitions; never infer missing metadata."""
    if not isinstance(definition, str):
        return None
    matches = list(re.finditer(r"\b" + re.escape(name) + r"\s*\(", definition))
    if len(matches) != 1:
        return None
    parts, current, quote = [], [], None
    for char in definition[matches[0].end():]:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
        elif char in ("'", '"', "“", "‘"):
            quote = {"“": "”", "‘": "’"}.get(char, char)
            current.append(char)
        elif char == "(":
            return None
        elif char == ")":
            parts.append("".join(current).strip())
            break
        elif char == ",":
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    else:
        return None
    if quote:
        return None
    if parts == [""]:
        parts = []
    names, required, variadic, optional_seen = [], [], False, False
    for part in parts:
        if part in {"..", "..."}:
            if variadic:
                return None
            variadic = True
            continue
        key, separator, default = part.partition("=")
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or key in names:
            return None
        if separator:
            default = default.strip()
            is_number = bool(re.fullmatch(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?", default))
            is_literal = (default.casefold() in {"true", "false", "nan"} or is_number
                          or len(default) >= 2 and (default[0], default[-1]) in {
                              ("'", "'"), ('"', '"'), ("“", "”"), ("‘", "’")})
            if not is_literal:
                return None
            optional_seen = True
        elif optional_seen:
            return None
        else:
            required.append(key)
        names.append(key)
    return {"names": names, "required": required, "variadic": variadic}


def validate(expr, dataset, catalog, operators,
             allow_field_diff_ratio=False, diff_ratio_reason="", operator_definitions=None,
             ppa=False):
    """Return dict: verdict, fields, operator_count, errors, warnings."""
    errors, warnings = [], []

    def err(code, msg):
        errors.append({"code": code, "message": msg})

    def warn(code, msg):
        warnings.append({"code": code, "message": msg})

    try:
        root = Parser(expr).parse()
    except ParseError as e:
        return {"verdict": "REJECT", "fields": [], "operator_count": 0,
                "errors": [{"code": "E_PARSE", "message": str(e)}],
                "warnings": []}

    nodes = list(walk(root))
    calls = [n for n, _ in nodes if n.kind == "call"]
    op_count = sum(
        n.kind in {"call", "binary", "ternary"}
        or (n.kind == "unary" and n.children[0].kind != "num")
        for n, _ in nodes)

    definitions = None
    if operator_definitions is not None:
        definitions = {}
        for row in operator_definitions:
            if isinstance(row, dict) and isinstance(row.get("name"), str):
                name = row["name"]
                definition = row.get("definition")
                definitions[name] = definition if name not in definitions else None

    # operator inventory, banned operators, and unambiguous live signatures
    for c in calls:
        if operators is not None and c.value not in operators:
            err("E_UNKNOWN_OPERATOR", f"unknown operator: {c.value}")
        if c.value in BANNED_OPERATORS:
            err("E_BANNED_OPERATOR", f"banned operator: {c.value}")
        if definitions is not None and (operators is None or c.value in operators):
            signature = _live_signature(c.value, definitions.get(c.value))
            if signature is None:
                warn("W_OPERATOR_SIGNATURE_UNVERIFIED", f"live signature unavailable: {c.value}")
                continue
            names, required = signature["names"], signature["required"]
            if len(c.args) > len(names) and not signature["variadic"]:
                err("E_OPERATOR_ARITY", f"too many positional arguments: {c.value}")
            positional = set(names[:len(c.args)])
            if positional.intersection(c.kwargs):
                err("E_OPERATOR_ARGUMENT_DUPLICATE", f"argument supplied twice: {c.value}")
            if set(c.kwargs) - set(names):
                err("E_OPERATOR_KEYWORD", f"unknown named argument: {c.value}")
            if set(required) - (positional | set(c.kwargs)):
                err("E_OPERATOR_ARITY", f"required argument missing: {c.value}")

    # fields
    field_nodes = [(n, anc) for n, anc in nodes
                   if n.kind == "ident" and n.value not in NON_FIELD_IDENTS]
    field_names = sorted({n.value for n, _ in field_nodes})
    for name in field_names:
        if name not in catalog:
            err("E_UNKNOWN_FIELD", f"field not in catalog: {name}")
        elif catalog[name]["dataset"] not in (None, dataset):
            err("E_MIXED_DATASET",
                f"field {name} belongs to {catalog[name]['dataset']}, not {dataset}")
    if ppa and len(field_names) > PPA_MAX_FIELDS:
        err("E_TOO_MANY_FIELDS",
            f"{len(field_names)} distinct fields > {PPA_MAX_FIELDS}: {field_names}")

    # operator budget
    if ppa and op_count > PPA_MAX_OPERATORS:
        err("E_OPERATOR_COUNT", f"operator_count {op_count} > {PPA_MAX_OPERATORS}")

    # splicing / diff-ratio arithmetic
    field_set = set(field_names)
    for n, _ in nodes:
        if n.kind == "binary":
            op, left, right = n.value, *n.children
        elif n.kind == "call" and n.value in {"subtract", "divide"} and len(n.args) == 2:
            op, left, right = ("-" if n.value == "subtract" else "/"), *n.args
        else:
            continue
        if op in ("+", "*"):
            err("E_SPLICE_OP",
                f"binary '{op}' is prohibited (branch splicing)")
        elif op in ("-", "/"):
            left_field = subtree_has_field(left, field_set)
            right_field = subtree_has_field(right, field_set)
            if left_field != right_field:
                err("E_CONSTANT_ARITHMETIC",
                    f"signal-constant '{op}' is prohibited")
            elif left_field and right_field:
                if op == "-" and not (raw_field(left, field_set) and
                                       raw_field(right, field_set)):
                    err("E_PROCESSED_SUBTRACTION",
                        "subtraction of processed signal branches is prohibited")
                elif allow_field_diff_ratio and diff_ratio_reason.strip():
                    warn("W_DIFF_RATIO_JUSTIFIED",
                         f"field-field '{op}' allowed with recorded "
                         f"justification: {diff_ratio_reason}")
                else:
                    err("E_FIELD_DIFF_RATIO",
                        f"field-field '{op}' requires a pre-registered "
                        "justification (--allow-field-diff-ratio --reason)")

    # VECTOR adaptation: first call ancestor of a VECTOR field must be vec_*
    for n, anc in field_nodes:
        info = catalog.get(n.value)
        if not info or info["type"] != "VECTOR":
            continue
        first_call = next((a for a in reversed(anc) if a.kind == "call"), None)
        if first_call is None or not first_call.value.startswith("vec_"):
            err("E_VECTOR_NOT_ADAPTED",
                f"VECTOR field {n.value} is not directly inside a vec_* call")

    # group arguments of group_* calls
    for c in calls:
        if c.value.startswith("group_") and len(c.args) >= 2:
            g = c.args[1]
            if g.kind == "ident" and g.value not in GROUP_NAMES:
                info = catalog.get(g.value)
                if not info or info["type"] != "GROUP":
                    err("E_BAD_GROUP", f"invalid group argument: {g.value}")

    # ending-operator rules (outermost node)
    if root.kind == "call":
        if root.value.startswith("vec_"):
            err("E_VEC_ENDING", "vec_* cannot be the ending operator")
        elif root.value.startswith("ts_") and \
                root.value not in TS_ALLOWED_ENDINGS:
            err("E_TS_ENDING",
                f"pure time-series ending {root.value} on a level value "
                "is not cross-sectionally comparable")
        elif root.value in ("and", "or", "not"):
            warn("W_BINARY_ENDING",
                 "binary/logical ending discards signal intensity")
    elif root.kind == "binary" and root.value in ("==", "!=", ">=", "<=", ">", "<"):
        warn("W_BINARY_ENDING", "comparison ending discards signal intensity")
    elif root.kind == "ternary":
        warn("W_BINARY_ENDING", "ternary ending may discard signal intensity")
    elif root.kind == "ident":
        warn("W_RAW_ENDING", "bare field ending: raw distribution kept as-is")

    return {"verdict": "REJECT" if errors else "PASS",
            "fields": field_names, "operator_count": op_count,
            "errors": errors, "warnings": warnings}


# ---------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)
    for name in ("check", "batch"):
        p = sub.add_parser(name)
        p.add_argument("target", help="expression (check) or file path (batch)")
        p.add_argument("--dataset", required=True)
        p.add_argument("--catalog", required=True,
                       help="datafields JSON for the dataset")
        p.add_argument("--operators",
                       default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "operators_study_raw.json"))
        p.add_argument("--allow-field-diff-ratio", action="store_true")
        p.add_argument("--reason", default="")
        p.add_argument("--ppa", action="store_true", help="apply PPA structural limits")
        p.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        catalog = load_catalog(args.catalog)
    except Exception as e:
        print(f"cannot load catalog: {e}", file=sys.stderr)
        return 2
    if not catalog:
        print("catalog is empty", file=sys.stderr)
        return 2
    operators = load_operators(args.operators)

    if args.mode == "check":
        exprs = [args.target]
    else:
        try:
            with open(args.target) as f:
                content = f.read()
        except Exception as e:
            print(f"cannot read batch file: {e}", file=sys.stderr)
            return 2
        if args.target.endswith(".json"):
            exprs = json.loads(content)
        else:
            exprs = [ln.strip() for ln in content.splitlines()
                     if ln.strip() and not ln.strip().startswith("#")]

    any_reject = False
    for expr in exprs:
        res = validate(expr, args.dataset, catalog, operators,
                       args.allow_field_diff_ratio, args.reason, ppa=args.ppa)
        any_reject |= res["verdict"] == "REJECT"
        if args.json:
            print(json.dumps({"expression": expr, **res}, ensure_ascii=False))
        else:
            mark = "PASS  " if res["verdict"] == "PASS" else "REJECT"
            print(f"[{mark}] ops={res['operator_count']} "
                  f"fields={res['fields']}\n        {expr}")
            for e in res["errors"]:
                print(f"        ERROR {e['code']}: {e['message']}")
            for w in res["warnings"]:
                print(f"        warn  {w['code']}: {w['message']}")
    return 1 if any_reject else 0


if __name__ == "__main__":
    sys.exit(main())
