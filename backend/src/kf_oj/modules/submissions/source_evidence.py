"""Bounded structural evidence; excludes source and diagnostic text from snapshots."""

import re
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import cast

from pygments.lexers.c_cpp import CLexer, CppLexer
from pygments.lexers.jvm import JavaLexer
from pygments.lexers.python import PythonLexer
from pygments.token import Keyword

from kf_oj.contracts.submission_evidence import (
    MAX_SOURCE_EXCERPT_BYTES,
    MAX_SOURCE_EXCERPT_LINES,
    CodeBoundaryChange,
    CodeControlFlowCounts,
    CodeControlFlowDiff,
    CodeDiffEvidence,
    CodeDiffRegion,
    CodeSubmissionRef,
    ComparisonCase,
    ComparisonOperator,
    CompilerCorrection,
    CompilerCorrectionKind,
    CompilerLocation,
)

_SOURCE_NAMES = {
    "c17": "main.c",
    "cpp20": "main.cpp",
    "python313": "main.py",
    "java21": "Main.java",
}
_DIFF_LINES = 512
_DIFF_REGIONS = 16
_BOUNDARY_LIMIT = 8
_STRUCTURE_BYTES = 32768
_LEXERS = {"c17": CLexer, "cpp20": CppLexer, "python313": PythonLexer, "java21": JavaLexer}
_SOURCE_LINE_BREAK = re.compile(rb"\r\n|[\r\n]")
_CONTROL_KEYWORDS = {
    **dict.fromkeys(
        ("if", "elif", "else", "switch", "case", "default", "match"), "branch_keywords"
    ),
    **dict.fromkeys(("for", "while", "do"), "loop_keywords"),
    "return": "return_keywords",
}
_OPERAND = r"(?:[A-Za-z_][A-Za-z_0-9]*|[0-9]+)"
_COMPARISON = rf"(?P<left>{_OPERAND})[ \t]*(?P<operator>[<>]=?|==|!=)[ \t]*(?P<right>{_OPERAND})"
_RELATIONS: tuple[ComparisonCase, ...] = ("less", "equal", "greater")
_TRUE_WHEN: dict[ComparisonOperator, frozenset[ComparisonCase]] = {
    "<": frozenset(("less",)),
    "<=": frozenset(("less", "equal")),
    ">": frozenset(("greater",)),
    ">=": frozenset(("equal", "greater")),
    "==": frozenset(("equal",)),
    "!=": frozenset(("less", "greater")),
}
_BRACED_CONDITION = re.compile(
    rf"[ \t]*(?P<keyword>if|while)[ \t]*\([ \t]*{_COMPARISON}[ \t]*\)[ \t]*\{{?[ \t]*"
)
_PYTHON_CONDITION = re.compile(rf"[ \t]*(?P<keyword>if|elif|while)[ \t]+{_COMPARISON}[ \t]*:[ \t]*")
_DIAGNOSTIC_LINES = 256
_LOCATIONS = 8
_NUMBER = r"([1-9][0-9]{0,5})"
_GNU_SEMICOLON = re.compile(r"^expected ['‘];['’](?: before | after | at end of input$)")
_JAVA_SEMICOLON = re.compile(r"^['‘];['’] expected$")
_PYTHON_MARK = re.compile(r"[ \t]+[~^]+")
_PYTHON_CORRECTIONS: dict[str, CompilerCorrectionKind] = {
    "SyntaxError: expected ':'": "missing_colon",
    "SyntaxError: '(' was never closed": "unclosed_parenthesis",
}


@dataclass(frozen=True, slots=True)
class ParsedCompilerEvidence:
    locations: tuple[CompilerLocation, ...]
    truncated: bool
    correction: CompilerCorrection | None


def source_excerpt(source: bytes, start_line: int, end_line: int) -> str:
    """Select complete editor lines while preserving their bytes and interior line breaks."""
    if not (1 <= start_line <= end_line < start_line + MAX_SOURCE_EXCERPT_LINES):
        raise ValueError("invalid excerpt line range")
    start = 0
    line = 1
    for match in _SOURCE_LINE_BREAK.finditer(source):
        if line == end_line:
            end = match.start()
            break
        line += 1
        if line == start_line:
            start = match.end()
    else:
        if line != end_line:
            raise ValueError("excerpt ends past the last line")
        end = len(source)
    if end - start > MAX_SOURCE_EXCERPT_BYTES:
        raise ValueError("source excerpt exceeds the byte limit")
    return source[start:end].decode("utf-8")


def _read_conditions(
    language: str, source: str, changed_lines: set[int]
) -> tuple[CodeControlFlowCounts, dict[int, re.Match[str]]]:
    starts = [0, *(index + 1 for index, char in enumerate(source) if char == "\n")]
    lines = source.split("\n")
    pattern = _PYTHON_CONDITION if language == "python313" else _BRACED_CONDITION
    counts: Counter[str] = Counter()
    conditions = {}
    for offset, kind, value in _LEXERS[language]().get_tokens_unprocessed(source):
        category = _CONTROL_KEYWORDS.get(value) if kind in Keyword else None
        if category is None or (line := bisect_right(starts, offset)) not in changed_lines:
            continue
        counts[category] += 1
        if value in ("if", "elif", "while"):
            condition = pattern.fullmatch(lines[line - 1])
            if condition and condition.start("keyword") == offset - starts[line - 1]:
                conditions[line] = condition
    return CodeControlFlowCounts(
        branch_keywords=counts["branch_keywords"],
        loop_keywords=counts["loop_keywords"],
        return_keywords=counts["return_keywords"],
    ), conditions


def _boundary_changes(
    regions: tuple[CodeDiffRegion, ...],
    before: dict[int, re.Match[str]],
    after: dict[int, re.Match[str]],
) -> tuple[CodeBoundaryChange, ...]:
    changes = []
    for region in regions:
        if region.kind != "replace" or region.before_count != region.after_count:
            continue
        for offset in range(region.before_count):
            old_line, new_line = region.before_line + offset, region.after_line + offset
            old, new = before.get(old_line), after.get(new_line)
            if old is None or new is None:
                continue
            old_op = cast(ComparisonOperator, old["operator"])
            new_op = cast(ComparisonOperator, new["operator"])
            if old_op == new_op:
                continue
            if any(old[key] != new[key] for key in ("keyword", "left", "right")):
                continue
            differs = _TRUE_WHEN[old_op] ^ _TRUE_WHEN[new_op]
            changes.append(
                CodeBoundaryChange(
                    before_line=old_line,
                    after_line=new_line,
                    before_operator=old_op,
                    after_operator=new_op,
                    changed_cases=tuple(relation for relation in _RELATIONS if relation in differs),
                )
            )
            if len(changes) >= _BOUNDARY_LIMIT:
                return tuple(changes)
    return tuple(changes)


def _changed_syntax(
    language: str,
    before: list[bytes],
    after: list[bytes],
    regions: tuple[CodeDiffRegion, ...],
) -> tuple[CodeControlFlowDiff | None, tuple[CodeBoundaryChange, ...]]:
    if not regions:
        return None, ()
    left, right = (b"\n".join(lines[:_DIFF_LINES]) + b"\n" for lines in (before, after))
    if max(len(left), len(right)) > _STRUCTURE_BYTES:
        return None, ()
    try:
        left_text, right_text = left.decode("utf-8"), right.decode("utf-8")
    except UnicodeDecodeError:
        return None, ()
    before_lines = {
        line
        for region in regions
        for line in range(region.before_line, region.before_line + region.before_count)
    }
    after_lines = {
        line
        for region in regions
        for line in range(region.after_line, region.after_line + region.after_count)
    }
    old_counts, old_conditions = _read_conditions(language, left_text, before_lines)
    new_counts, new_conditions = _read_conditions(language, right_text, after_lines)
    return (
        CodeControlFlowDiff(before=old_counts, after=new_counts),
        _boundary_changes(regions, old_conditions, new_conditions),
    )


def compare_owned_sources(
    earlier: CodeSubmissionRef,
    before: list[bytes],
    after: list[bytes],
    *,
    collect_control_flow: bool = False,
) -> CodeDiffEvidence:
    comparison = SequenceMatcher(None, before[:_DIFF_LINES], after[:_DIFF_LINES], autojunk=False)
    regions = [
        CodeDiffRegion(
            kind=kind,
            before_line=left + 1,
            before_count=left_end - left,
            after_line=right + 1,
            after_count=right_end - right,
        )
        for kind, left, left_end, right, right_end in comparison.get_opcodes()
        if kind != "equal"
    ]
    visible = tuple(regions[:_DIFF_REGIONS])
    control_flow, boundaries = _changed_syntax(earlier.language, before, after, visible)
    return CodeDiffEvidence(
        earlier=earlier,
        regions=visible,
        truncated=max(len(before), len(after)) > _DIFF_LINES or len(regions) > _DIFF_REGIONS,
        control_flow=control_flow if collect_control_flow else None,
        boundary_changes=boundaries,
    )


def _python_correction(context: list[str]) -> CompilerCorrectionKind | None:
    # py_compile follows the file header with an indented source excerpt, an
    # optional caret line and an unindented exception message.
    if len(context) < 2 or not context[0].startswith("    "):
        return None
    message = context[1]
    if _PYTHON_MARK.fullmatch(message):
        if len(context) < 3:
            return None
        message = context[2]
    if message.startswith("IndentationError: expected an indented block"):
        return "expected_indented_block"
    return _PYTHON_CORRECTIONS.get(message)


def compiler_evidence(
    language: str, diagnostics: str, source_lines: int, truncated: bool
) -> ParsedCompilerEvidence:
    name = re.escape(_SOURCE_NAMES[language])
    path = rf"(?:/work/|\./)?{name}"
    compiler = re.compile(
        rf"^{path}:{_NUMBER}(?::{_NUMBER})?:\s*(fatal error|error|warning|note):\s*(.*)$"
    )
    python = re.compile(rf'^\s*File "{path}", line {_NUMBER}(?:,|\s|$)')
    locations = {}
    all_lines = diagnostics.splitlines()
    lines = all_lines[:_DIAGNOSTIC_LINES]
    correction = None
    for index, text in enumerate(lines):
        correction_kind = None
        match = compiler.match(text)
        if match:
            line, column, kind, message = match.groups()
            item = CompilerLocation(
                line=int(line),
                column=None if column is None else int(column),
                kind="error" if kind == "fatal error" else kind,
            )
            if kind == "error" and (
                (language in ("c17", "cpp20") and _GNU_SEMICOLON.match(message))
                or (language == "java21" and _JAVA_SEMICOLON.fullmatch(message))
            ):
                correction_kind = "missing_semicolon"
        elif language == "python313" and (match := python.match(text)):
            item = CompilerLocation(line=int(match[1]), kind="location")
            correction_kind = _python_correction(lines[index + 1 : index + 4])
        else:
            continue
        if item.line <= source_lines:
            locations[(item.line, item.column, item.kind)] = item
            if correction is None and correction_kind is not None and len(locations) <= _LOCATIONS:
                correction = CompilerCorrection(location=item, kind=correction_kind)
    return ParsedCompilerEvidence(
        locations=tuple(locations.values())[:_LOCATIONS],
        truncated=truncated or len(all_lines) > _DIAGNOSTIC_LINES or len(locations) > _LOCATIONS,
        correction=correction,
    )
