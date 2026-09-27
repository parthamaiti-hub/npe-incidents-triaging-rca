"""CNCF Serverless Workflow 1.0 YAML <-> the flat task list stored in
WorkflowDefinitionVersion.document.

The stored shape is a list of {name?, call, with, retry?, function_version_id?,
function_version_number?}; the YAML shape an operator edits is

    document: {dsl: '1.0.0', namespace: npe-rca, name: ..., version: '...'}
    do:
      - checkErrorLogs:
          call: error_logs_v2
          with: {...}
          retry: {max_attempts: 3}

Only the DSL subset app/workflow_spec.py executes is accepted: sequential
`call` tasks with `with` params and an optional `retry` override. Anything
else (switch/fork/emit/...) is rejected here, at parse time, with a
line/column the editor can point at. Function pins are never part of the
YAML -- they are server-stamped by app.workflow_orchestrator.

This module only checks *shape*; whether each `call` exists, has its
required params, and has an implementation is app.workflow_orchestrator's
_validate_tasks' job (one set of rules for YAML and non-YAML callers).
"""

from dataclasses import dataclass, field

import yaml

MAX_YAML_BYTES = 64 * 1024

DSL_VERSION = "1.0.0"
NAMESPACE = "npe-rca"

_TASK_KEYS = {"call", "with", "retry"}
_RETRY_KEYS = {"max_attempts", "delay_seconds", "exponential_backoff"}
_TOP_LEVEL_KEYS = {"document", "do"}
_UNSUPPORTED_TASK_KINDS = {"switch", "fork", "emit", "listen", "for", "try", "raise", "run", "set", "wait", "do"}


@dataclass
class YamlIssue:
    message: str
    path: str = ""
    line: int | None = None
    column: int | None = None
    task_index: int | None = None

    def as_dict(self) -> dict:
        return {
            "message": self.message,
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "task_index": self.task_index,
        }


class WorkflowYamlError(ValueError):
    def __init__(self, kind: str, errors: list[YamlIssue]):
        self.kind = kind
        self.errors = errors
        super().__init__("; ".join(_describe(e) for e in errors))


def _describe(issue: YamlIssue) -> str:
    where = f"line {issue.line}: " if issue.line is not None else ""
    path = f"{issue.path}: " if issue.path else ""
    return f"{where}{path}{issue.message}"


@dataclass
class ParsedWorkflow:
    tasks: list[dict]
    # 1-based line of each task's entry under `do:`, parallel to `tasks` --
    # lets a later semantic error (unknown call, missing param) that only
    # knows its task index still point at a YAML line.
    task_lines: list[int] = field(default_factory=list)


def _task_key(task: dict, used: set[str]) -> str:
    base = task.get("name") or task["call"]
    key, n = base, 2
    while key in used:
        key, n = f"{base}_{n}", n + 1
    used.add(key)
    return key


def tasks_to_cncf_yaml(tasks: list[dict], *, name: str = "playbook", version: str | int = "draft") -> str:
    used: set[str] = set()
    do = []
    for task in tasks:
        body: dict = {"call": task["call"], "with": dict(task.get("with") or {})}
        if task.get("retry"):
            body["retry"] = dict(task["retry"])
        do.append({_task_key(task, used): body})
    document = {
        "document": {"dsl": DSL_VERSION, "namespace": NAMESPACE, "name": name, "version": str(version)},
        "do": do,
    }
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False, allow_unicode=True)


def _line(node: yaml.Node | None) -> tuple[int | None, int | None]:
    if node is None:
        return None, None
    return node.start_mark.line + 1, node.start_mark.column + 1


def _mapping_get(node: yaml.MappingNode, key: str) -> tuple[yaml.Node | None, yaml.Node | None]:
    for k, v in node.value:
        if isinstance(k, yaml.ScalarNode) and k.value == key:
            return k, v
    return None, None


def cncf_yaml_to_tasks(text: str) -> ParsedWorkflow:
    """Raises WorkflowYamlError(kind="syntax") for unparseable YAML and
    kind="schema" for YAML that parses but isn't the supported CNCF subset.
    All schema issues are collected, not just the first."""
    if len(text.encode("utf-8")) > MAX_YAML_BYTES:
        raise WorkflowYamlError("syntax", [YamlIssue(f"YAML exceeds {MAX_YAML_BYTES // 1024} KB limit")])

    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        problem = getattr(exc, "problem", None) or str(exc)
        issue = YamlIssue(str(problem))
        if mark is not None:
            issue.line, issue.column = mark.line + 1, mark.column + 1
        raise WorkflowYamlError("syntax", [issue]) from exc

    if not isinstance(data, dict) or not isinstance(root, yaml.MappingNode):
        raise WorkflowYamlError("schema", [YamlIssue("top level must be a mapping with a 'do' list", line=1, column=1)])

    issues: list[YamlIssue] = []

    for key_node, _ in root.value:
        if isinstance(key_node, yaml.ScalarNode) and key_node.value not in _TOP_LEVEL_KEYS:
            line, col = _line(key_node)
            issues.append(YamlIssue(f"'{key_node.value}' is not supported (only 'document' and 'do')", key_node.value, line, col))

    if "document" in data and not isinstance(data["document"], dict):
        _, node = _mapping_get(root, "document")
        issues.append(YamlIssue("must be a mapping", "document", *_line(node)))

    do_key, do_node = _mapping_get(root, "do")
    if do_node is None or not isinstance(data.get("do"), list) or not isinstance(do_node, yaml.SequenceNode):
        issues.append(YamlIssue("must be a list of named tasks", "do", *_line(do_node or do_key or root)))
        raise WorkflowYamlError("schema", issues)

    tasks: list[dict] = []
    task_lines: list[int] = []
    seen_names: dict[str, int] = {}

    for i, (item, item_node) in enumerate(zip(data["do"], do_node.value)):
        line, col = _line(item_node)
        task_lines.append(line)
        if not isinstance(item, dict) or len(item) != 1 or not isinstance(item_node, yaml.MappingNode):
            issues.append(YamlIssue("each task must be a single-key mapping '<taskName>: {call, with}'", f"do[{i}]", line, col, i))
            continue

        (task_name, body), = item.items()
        name_node, body_node = item_node.value[0]
        path = f"do[{i}].{task_name}"
        if not isinstance(task_name, str) or not task_name.strip():
            issues.append(YamlIssue("task name must be a non-empty string", f"do[{i}]", *_line(name_node), i))
            continue
        if task_name in seen_names:
            issues.append(YamlIssue(f"duplicate task name '{task_name}' (first used at do[{seen_names[task_name]}])", f"do[{i}]", *_line(name_node), i))
        seen_names.setdefault(task_name, i)

        if not isinstance(body, dict) or not isinstance(body_node, yaml.MappingNode):
            issues.append(YamlIssue("task body must be a mapping with 'call' and 'with'", path, *_line(body_node), i))
            continue

        unsupported = [k for k in body if k in _UNSUPPORTED_TASK_KINDS]
        for k in unsupported:
            key_node, _ = _mapping_get(body_node, k)
            issues.append(YamlIssue(f"'{k}' is not supported (only 'call' tasks)", f"{path}.{k}", *_line(key_node), i))
        for k in body:
            if k not in _TASK_KEYS and k not in _UNSUPPORTED_TASK_KINDS:
                key_node, _ = _mapping_get(body_node, k)
                issues.append(YamlIssue(f"unknown key '{k}' (allowed: call, with, retry)", f"{path}.{k}", *_line(key_node), i))
        if unsupported:
            continue

        call = body.get("call")
        if not isinstance(call, str) or not call.strip():
            _, call_node = _mapping_get(body_node, "call")
            issues.append(YamlIssue("'call' is required and must be a function name", f"{path}.call", *_line(call_node or body_node), i))
            continue

        with_ = body.get("with")
        if with_ is None:
            with_ = {}
        if not isinstance(with_, dict):
            _, with_node = _mapping_get(body_node, "with")
            issues.append(YamlIssue("must be a mapping of params", f"{path}.with", *_line(with_node), i))
            continue

        task: dict = {"call": call, "with": with_}
        # A task keyed by its own function name is the unnamed form (see
        # tasks_to_cncf_yaml) -- keep it unnamed so a round trip is lossless.
        if task_name != call:
            task["name"] = task_name

        retry = body.get("retry")
        if retry is not None:
            _, retry_node = _mapping_get(body_node, "retry")
            if not isinstance(retry, dict):
                issues.append(YamlIssue("must be a mapping", f"{path}.retry", *_line(retry_node), i))
                continue
            bad = sorted(set(retry) - _RETRY_KEYS)
            if bad:
                issues.append(YamlIssue(f"unknown retry keys {bad} (allowed: {sorted(_RETRY_KEYS)})", f"{path}.retry", *_line(retry_node), i))
                continue
            task["retry"] = retry

        tasks.append(task)

    if issues:
        raise WorkflowYamlError("schema", issues)
    if not tasks:
        raise WorkflowYamlError("schema", [YamlIssue("a playbook needs at least one task", "do", *_line(do_node))])
    return ParsedWorkflow(tasks=tasks, task_lines=task_lines)
