"""Catalog check: static validation of a catalog YAML before it is loaded.

Every link from incident to playbook is a plain string or regex comparison
(see design/NPE_Incident_PlaybookMappingProcess.md §5-§6), so a typo, an
overlapping regex or a mis-set priority fails nothing -- it quietly sends
incidents to a different playbook or to "no playbook". This module finds
those mistakes from the YAML alone (plus, when given, what is already in
the database, since several catalog files load into one database).

Errors block dataloadscripts.load_catalog (nothing is written); warnings are
printed and the load continues (--strict makes them block too).

Runs:
  - automatically inside every `load_catalog` run, before anything is written;
  - on its own, writing nothing:
        uv run python -m dataloadscripts.check_catalog --file dataloadscripts/npe_real_source_systems.yaml
        uv run python -m dataloadscripts.load_catalog --file ... --check-only      (same thing)
  - in the test suite (tests/test_check_catalog.py), against both shipped catalogs.

Not covered here: edits made through the /catalog/* API (they bypass the
YAML), and conflicts that only show up in real incident text mentioning two
systems at once -- that needs a labeled sample set of tickets (§9.3).
"""

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from pymongo.database import Database

from app.categories import ANY_CATEGORY, DEFAULT_CATEGORY, RESERVED_CATEGORIES
from app.incident_parser import SIGNAL_TYPES
from app.schemas import CatalogIn

ERROR = "error"
WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    severity: str  # "error" | "warning"
    code: str
    message: str
    ids: tuple[str, ...] = ()

    def __str__(self) -> str:
        return f"{self.severity.upper():7} {self.code:24} {self.message}"


class CatalogCheckError(ValueError):
    """Raised by load_catalog when the check finds blocking problems."""

    def __init__(self, findings: list[Finding]):
        self.findings = findings
        super().__init__("catalog check failed:\n" + "\n".join(str(f) for f in findings))


@dataclass(frozen=True)
class _Rule:
    id: str
    source_system_id: str
    category: str
    signal_type: str
    signal_pattern: str
    priority: int
    from_catalog: bool

    @property
    def pair(self) -> tuple[str, str]:
        return self.source_system_id, self.category


# --------------------------------------------------------------------------
# Regex dialect: rules are evaluated by OPA (Go RE2), not Python `re`.
# --------------------------------------------------------------------------

_RE2_UNSUPPORTED_GROUPS = (
    ("(?=", "lookahead (?=...)"),
    ("(?!", "negative lookahead (?!...)"),
    ("(?<=", "lookbehind (?<=...)"),
    ("(?<!", "negative lookbehind (?<!...)"),
    ("(?>", "atomic group (?>...)"),
    ("(?(", "conditional group (?(...)"),
    ("(?P=", "named backreference (?P=...)"),
)


def re2_problems(pattern: str) -> list[str]:
    """Constructs Python's `re` accepts but Go RE2 (OPA's regex.match)
    doesn't -- a rule using one passes YAML validation and then silently
    never matches in classification."""
    problems: list[str] = []
    i, in_class = 0, False
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            n = pattern[i + 1]
            if not in_class and n in "123456789":
                problems.append(f"backreference \\{n}")
            elif n == "Z":
                problems.append("\\Z (RE2 has only \\z)")
            i += 2
            continue
        if in_class:
            in_class = c != "]"
            i += 1
            continue
        if c == "[":
            in_class = True
            i += 1
            if pattern.startswith("^", i):
                i += 1
            if pattern.startswith("]", i):  # a leading ']' is literal
                i += 1
            continue
        if c == "(":
            problems.extend(name for token, name in _RE2_UNSUPPORTED_GROUPS if pattern.startswith(token, i))
        elif c in "+*?" and pattern.startswith("+", i + 1):
            problems.append(f"possessive quantifier {c}+")
        i += 1
    return problems


# --------------------------------------------------------------------------
# Probe texts: literal examples a rule's own pattern matches.
# --------------------------------------------------------------------------


def _split_alternatives(pattern: str) -> list[str]:
    parts, current, depth, in_class, i = [], [], 0, False, 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            current.append(pattern[i : i + 2])
            i += 2
            continue
        if in_class:
            in_class = c != "]"
        elif c == "[":
            in_class = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "|" and depth == 0:
            parts.append("".join(current))
            current = []
            i += 1
            continue
        current.append(c)
        i += 1
    parts.append("".join(current))
    return parts


_CLASS_EXAMPLE = {"d": "1", "w": "a", "s": " "}


def _literal_example(alternative: str) -> str | None:
    """A concrete string for one top-level alternative made of literals,
    escapes, simple classes and quantifiers; None for anything with groups
    or flags (no probe rather than a wrong one)."""
    out: list[str] = []
    i = 0
    while i < len(alternative):
        c = alternative[i]
        if c == "\\":
            if i + 1 >= len(alternative):
                return None
            n = alternative[i + 1]
            if n in "bB":
                pass
            elif n in _CLASS_EXAMPLE:
                out.append(_CLASS_EXAMPLE[n])
            elif n.isalnum():
                return None
            else:
                out.append(n)
            i += 2
        elif c == "[":
            end = alternative.find("]", i + 2)
            content = alternative[i + 1 : end] if end != -1 else ""
            if not content or content.startswith("^"):
                return None
            if content.startswith("\\") and len(content) > 1:
                out.append(_CLASS_EXAMPLE.get(content[1], content[1]))
            else:
                out.append(content[0])
            i = end + 1
        elif c in "^$+*?":
            i += 1  # anchors dropped; quantifiers keep the single occurrence
        elif c == ".":
            out.append("x")
            i += 1
        elif c == "{":
            end = alternative.find("}", i)
            low = alternative[i + 1 : end].split(",")[0] if end != -1 else ""
            if not low.isdigit() or not out:
                return None
            count = int(low)  # the minimum repetitions always satisfy {n,m}
            if count == 0:
                out.pop()
            else:
                out.extend([out[-1]] * (count - 1))
            i = end + 1
        elif c in "()|":
            return None
        else:
            out.append(c)
            i += 1
    example = "".join(out)
    return example or None


def rule_examples(pattern: str) -> list[str]:
    examples = []
    for alternative in _split_alternatives(pattern):
        example = _literal_example(alternative)
        if example is not None and re.search(pattern, example):
            examples.append(example)
    return examples


def _matches(rule: _Rule, signals: list[tuple[str, str]]) -> bool:
    return any(rule.signal_type == kind and re.search(rule.signal_pattern, value) for kind, value in signals)


# --------------------------------------------------------------------------
# The check
# --------------------------------------------------------------------------


def _normalize_category(category: str) -> str:
    return re.sub(r"\s*/\s*", "/", re.sub(r"\s+", " ", category.strip())).casefold()


def _context(catalog: CatalogIn, db: Database | None) -> tuple[list[_Rule], dict[tuple[str, str], list[str]]]:
    """Catalog rules/playbooks, plus database rows this load won't replace
    (other catalog files share the database)."""
    rules = [
        _Rule(r.id, r.source_system_id, r.category, r.signal_type, r.signal_pattern, r.priority, True)
        for r in catalog.incident_mapping_rules
    ]
    playbooks: dict[tuple[str, str], list[str]] = {}
    for p in catalog.rca_playbooks:
        playbooks.setdefault((p.source_system_id, p.category), []).append(p.id)

    if db is not None:
        catalog_rule_ids = {r.id for r in rules}
        for doc in db["incident_mapping_rule"].find():
            if doc["_id"] not in catalog_rule_ids:
                rules.append(
                    _Rule(
                        doc["_id"], doc["source_system_id"], doc["category"], doc["signal_type"],
                        doc["signal_pattern"], doc["priority"], False,
                    )
                )
        catalog_playbook_ids = {p.id for p in catalog.rca_playbooks}
        for doc in db["workflow_definition"].find():
            if doc["_id"] not in catalog_playbook_ids:
                playbooks.setdefault((doc["source_system_id"], doc["category"]), []).append(doc["_id"])
    return rules, playbooks


def check_catalog(catalog: CatalogIn, db: Database | None = None) -> list[Finding]:
    """All findings, errors first. Pass `db` to also check against what is
    already loaded (other catalog files); without it the YAML is checked on
    its own."""
    findings: list[Finding] = []
    rules, playbooks = _context(catalog, db)
    catalog_rules = [r for r in rules if r.from_catalog]

    # -- errors -------------------------------------------------------------
    sections = {
        "teams": catalog.teams, "environments": catalog.environments, "source_systems": catalog.source_systems,
        "system_footprints": catalog.system_footprints, "incident_mapping_rules": catalog.incident_mapping_rules,
        "rca_playbooks": catalog.rca_playbooks,
    }
    for section, items in sections.items():
        seen: set[str] = set()
        for item in items:
            if item.id in seen:
                findings.append(Finding(ERROR, "duplicate_id", f"{section}: id {item.id!r} appears more than once -- the later row silently overwrites the earlier", (item.id,)))
            seen.add(item.id)

    for pair, ids in sorted(playbooks.items()):
        if len(set(ids)) > 1:
            findings.append(Finding(ERROR, "playbook_pair_conflict", f"{sorted(set(ids))} are all playbooks for {pair} -- only one playbook per (system, category) is allowed", tuple(sorted(set(ids)))))

    for rule in catalog_rules:
        if rule.signal_type not in SIGNAL_TYPES:
            findings.append(Finding(ERROR, "unknown_signal_type", f"{rule.id}: signal_type {rule.signal_type!r} is never extracted from incidents (known: {', '.join(SIGNAL_TYPES)}) -- the rule can never match", (rule.id,)))
        for problem in re2_problems(rule.signal_pattern):
            findings.append(Finding(ERROR, "regex_not_re2", f"{rule.id}: {problem} is not supported by OPA's regex engine (Go RE2) -- the rule would never match", (rule.id,)))

    by_signal: dict[tuple[str, str], list[_Rule]] = {}
    for rule in rules:
        by_signal.setdefault((rule.signal_type, rule.signal_pattern), []).append(rule)
    for (signal_type, pattern), same in sorted(by_signal.items()):
        if len(same) > 1 and any(r.from_catalog for r in same):
            ids = tuple(sorted(r.id for r in same))
            findings.append(Finding(ERROR, "duplicate_rule", f"{list(ids)} share signal_type={signal_type!r} and pattern {pattern!r} -- which one wins is decided by priority/id, not meaning", ids))

    spellings: dict[str, set[str]] = {}
    catalog_categories = {r.category for r in catalog_rules} | {p.category for p in catalog.rca_playbooks}
    reserved = {_normalize_category(c): c for c in RESERVED_CATEGORIES}
    for category in sorted(catalog_categories):
        exact = reserved.get(_normalize_category(category))
        if exact is not None and category != exact:
            findings.append(Finding(ERROR, "category_spelling", f"category {category!r} looks like the reserved {exact!r} but isn't spelled exactly -- it would be treated as an ordinary category", (category,)))
    for category in {r.category for r in rules} | {pair[1] for pair in playbooks}:
        if category not in RESERVED_CATEGORIES:
            spellings.setdefault(_normalize_category(category), set()).add(category)
    for variants in spellings.values():
        if len(variants) > 1 and variants & catalog_categories:
            findings.append(Finding(ERROR, "category_spelling", f"categories {sorted(variants)} differ only in case/spacing -- rules and playbooks must use one exact string or incidents end as 'no playbook'", tuple(sorted(variants))))

    # -- warnings -----------------------------------------------------------
    produced = {r.pair for r in rules if r.category != ANY_CATEGORY}
    any_systems = {r.source_system_id for r in rules if r.category == ANY_CATEGORY}
    default_playbook = {pair[0]: ids[0] for pair, ids in playbooks.items() if pair[1] == DEFAULT_CATEGORY}
    for p in catalog.rca_playbooks:
        pair = (p.source_system_id, p.category)
        if p.category == DEFAULT_CATEGORY:
            continue  # reached by fallback, never by a rule
        if pair not in produced:
            via = " (only via an ANY rule + LLM fallback, or a Retry override)" if p.source_system_id in any_systems else " (only via a Retry override)"
            findings.append(Finding(WARNING, "unreachable_playbook", f"{p.id} {pair}: no mapping rule produces this pair -- it never runs automatically{via}", (p.id,)))

    for pair in sorted({r.pair for r in catalog_rules if r.category != ANY_CATEGORY}):
        if pair not in playbooks:
            ids = tuple(sorted(r.id for r in catalog_rules if r.pair == pair))
            fallback = default_playbook.get(pair[0])
            outcome = (
                f"matching incidents run the system's DEFAULT playbook {fallback} (DefaultRCA)"
                if fallback
                else "matching incidents end as 'no playbook' (NeedManualIntervention)"
            )
            findings.append(Finding(WARNING, "route_without_playbook", f"{pair}: produced by {list(ids)} but has no playbook of its own -- {outcome}", ids))

    reported: set[tuple[str, frozenset]] = set()
    for rule in catalog_rules:
        for example in rule_examples(rule.signal_pattern):
            signals = [(rule.signal_type, example)] + ([("keyword", example)] if rule.signal_type != "keyword" else [])
            matched = [r for r in rules if _matches(r, signals)]
            if not matched:
                continue
            winner = min(matched, key=lambda r: (r.priority, r.id))
            top = [r for r in matched if r.priority == winner.priority]
            if winner.pair != rule.pair and winner.priority < rule.priority:
                code, involved = "shadowed_rule", (rule, winner)
                message = (
                    f"{rule.id} (P{rule.priority}, {rule.pair}) never wins on its own text {example!r}: "
                    f"{winner.id} (P{winner.priority}) also matches it and classifies it as {winner.pair}"
                )
            elif len({r.pair for r in top}) > 1:
                code, involved = "priority_tie", tuple(top)
                message = (
                    f"text {example!r} (from {rule.id}) matches {sorted(r.id for r in top)} at the same priority "
                    f"P{winner.priority} for different (system, category) pairs -- {winner.id} wins only because its id "
                    f"sorts first, giving {winner.pair}"
                )
            else:
                continue
            key = (code, frozenset(r.id for r in involved))
            if key not in reported:
                reported.add(key)
                findings.append(Finding(WARNING, code, message, tuple(sorted(key[1]))))

    return sorted(findings, key=lambda f: (f.severity != ERROR, f.code, f.message))


def blocking(findings: list[Finding], strict: bool = False) -> list[Finding]:
    return [f for f in findings if f.severity == ERROR or strict]


def print_report(findings: list[Finding], source: str, strict: bool = False) -> None:
    for finding in findings:
        print(finding)
    errors = sum(f.severity == ERROR for f in findings)
    status = "FAILED" if blocking(findings, strict) else "OK"
    print(f"{status:7} {source}: {errors} error(s), {len(findings) - errors} warning(s)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Check a catalog YAML without loading it.")
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--strict", action="store_true", help="exit non-zero on warnings too")
    parser.add_argument("--no-db", action="store_true", help="check the YAML alone, ignoring what is already loaded")
    args = parser.parse_args()

    from dataloadscripts.load_catalog import check_only

    sys.exit(check_only(args.file, use_db=not args.no_db, strict=args.strict))


if __name__ == "__main__":
    main()
