"""Build the only replies a low-level model selection can deliver."""

import re

from kf_oj.contracts.learning_model import (
    GuidedChatModelFacts,
    GuidedDiagnosticCheck,
    GuidedDiagnosticChoice,
    ProgressiveHintLevel,
    ReviewEvaluationCategory,
    SourceExcerptModelFacts,
)

_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.+?)\s*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_CLARIFICATIONS = (
    (
        ("输入", "输入描述", "输入格式", "input", "input format"),
        "先分清每行读什么，以及这些字段分别代表一个值、一组数据，还是测试组数。",
    ),
    (
        ("输出", "输出描述", "输出格式", "output", "output format"),
        "先用一句话说明要求输出的对象，再核对输出的行数、单位和顺序。",
    ),
    (
        ("约束", "数据范围", "限制", "constraints", "limits"),
        "把题面给出的最小规模、最大规模和可能出现的特殊值分开列出来。",
    ),
)
_DIRECTIONS = (
    "手算一个最小的合法例子。每走一步，记下当时已知量和还需要决定的量。",
    "先找出处理一个元素后一定成立的条件。用相邻两步检查这个条件有没有被破坏。",
    "先估算直接做法需要重复多少次操作，再检查哪些结果会被重复计算。",
)
_BOUNDARIES = (
    "从题面允许的最小规模开始，手算第一步和最后一步，检查初值和结束条件是否一致。",
    "从公开约束挑一个边界值，分别检查边界两侧哪些分支会执行、哪些分支不会执行。",
)
_EVALUATION_CHECKS: dict[ReviewEvaluationCategory, str] = {
    "accepted": "这次提交已通过。用公开样例重述哪条状态条件保证了结果。",
    "compilation_error": "这次提交遇到编译错误。先在提交详情查看首条编译诊断的位置。",
    "wrong_answer": "这次提交的判定是答案错误。先对照公开样例的首处输出差异。",
    "runtime_error": "这次提交遇到运行错误。先用最小合法输入手算每一步读写的有效范围。",
    "resource_limit": "这次提交遇到资源限制。按公开约束估算操作次数、占用空间和输出量。",
}
_LOCAL_CHECKS: dict[GuidedDiagnosticCheck, str] = {
    "values": "手算这行执行前后的变量值，先找哪个值第一次与预期不同。",
    "bounds": "写出这次访问的下标及合法范围，分别代入最小和最大边界核对。",
    "branch": "找两个能让这个条件分别为真和为假的合法输入，对照各自应走的分支。",
    "update": "用一次状态更新核对旧值和新值，检查需要保留的量是否被提前覆盖。",
    "numeric_range": "计算这一步中间值的最大绝对值，再核对表达式和变量类型的可表示范围。",
    "input": "逐项核对这一行读取的值及顺序，检查输入结束或多组数据时是否仍对应题面格式。",
}
LOCAL_SOURCE_REQUEST = "可先授权一段本人源码，再针对其中一行做局部核对。"


def _public_sections(statement: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    active = None
    fence = ""
    for line in statement.splitlines():
        if marker := _FENCE.match(line):
            delimiter, suffix = marker.groups()
            if not fence:
                fence = delimiter
            elif delimiter.startswith(fence) and not suffix.strip():
                fence = ""
            continue
        if fence or line.startswith(("    ", "\t")):
            continue
        if heading := _HEADING.match(line):
            active = sections.setdefault(heading.group(1).strip(" #：:").casefold(), [])
        elif active is not None and (snippet := line.strip()):
            active.append(snippet)
    excerpts = {}
    for heading, lines in sections.items():
        if excerpt := " ".join(lines):
            excerpt = excerpt if len(excerpt) <= 280 else excerpt[:279] + "…"
            excerpts[heading] = re.sub(r"([`*_\[\]<>])", r"\\\1", excerpt)
    return excerpts


def hint_choices(
    facts: GuidedChatModelFacts, level: ProgressiveHintLevel | None
) -> tuple[str, ...]:
    if level == "L0":
        sections = _public_sections(facts.problem_statement or "")
        return tuple(
            f"题面公开内容\n\n> {excerpt}\n\n{instruction}"
            if (excerpt := next((sections[title] for title in titles if title in sections), ""))
            else instruction
            for titles, instruction in _CLARIFICATIONS
        )
    if level == "L1":
        sections = _public_sections(facts.problem_statement or "")
        constraints = next((sections[key] for key in _CLARIFICATIONS[2][0] if key in sections), "")
        if constraints:
            return (
                *_DIRECTIONS[:2],
                f"先根据这一段公开约束估算直接做法的操作次数。\n\n> {constraints}",
            )
        return _DIRECTIONS
    if level in ("L2", "L3"):
        category = facts.submission.evaluation.category if facts.submission else None
        check = _EVALUATION_CHECKS.get(category)
        if level == "L3":
            return (LOCAL_SOURCE_REQUEST, check) if check is not None else (LOCAL_SOURCE_REQUEST,)
        options = _BOUNDARIES
        if facts.public_sample_count:
            options = (
                "用题面样例 1 逐步列出中间状态，再和公开的预期输出比较。先找第一个分歧。",
                *options,
            )
        return (*options, check) if check is not None else options
    return ()


def local_diagnostic(source: SourceExcerptModelFacts, selected: GuidedDiagnosticChoice) -> str:
    offset = selected.line - source.start_line
    lines = source.text.split("\n")
    if selected.line > source.end_line or not 0 <= offset < len(lines) or not lines[offset].strip():
        raise ValueError("diagnostic line is outside the authorized nonempty source")
    return f"核对第 **{selected.line}** 行。{_LOCAL_CHECKS[selected.check]}"
