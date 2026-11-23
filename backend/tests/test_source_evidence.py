from uuid import UUID

import pytest

from kf_oj.contracts.submission_evidence import (
    CodeBoundaryChange,
    CodeControlFlowCounts,
    CodeSubmissionRef,
)
from kf_oj.modules.submissions.source_evidence import compare_owned_sources, compiler_evidence


@pytest.mark.parametrize(
    ("language", "diagnostic", "column", "kind"),
    [
        ("c17", "main.c:2:5: warning: text", 5, "warning"),
        ("cpp20", "/work/main.cpp:2:4: error: text", 4, "error"),
        ("python313", '  File "/work/main.py", line 2', None, "location"),
        ("java21", "Main.java:2: error: text", None, "error"),
    ],
)
def test_compiler_locations_are_bounded_to_student_source(language, diagnostic, column, kind):
    report = compiler_evidence(
        language, diagnostic + "\n/other/main.c:1:5: note: irrelevant", 2, True
    )
    assert report.truncated and report.correction is None
    (actual,) = report.locations
    assert (actual.line, actual.column, actual.kind) == (2, column, kind)
    outside = compiler_evidence(language, diagnostic, 1, False)
    assert not outside.locations and not outside.truncated and outside.correction is None


@pytest.mark.parametrize(
    ("language", "diagnostic", "correction"),
    [
        ("c17", "main.c:2:5: error: expected ';' before 'return'", "missing_semicolon"),
        (
            "cpp20",
            "/work/main.cpp:2:3: error: expected ‘;’ before ‘return’",
            "missing_semicolon",
        ),
        ("java21", "Main.java:2: error: ';' expected", "missing_semicolon"),
        (
            "python313",
            '  File "./main.py", line 2\n    if condition\n                ^\n'
            "SyntaxError: expected ':'",
            "missing_colon",
        ),
        (
            "python313",
            '  File "main.py", line 2\n    perform_step()\n    ^\n'
            "IndentationError: expected an indented block after 'if' statement on line 1",
            "expected_indented_block",
        ),
        (
            "python313",
            '  File "/work/main.py", line 2\n    value = (first\n            ^\n'
            "SyntaxError: '(' was never closed",
            "unclosed_parenthesis",
        ),
    ],
)
def test_supported_corrections_require_a_mapped_location(language, diagnostic, correction):
    report = compiler_evidence(language, diagnostic, 3, False)
    assert report.correction is not None
    assert report.correction.kind == correction
    assert report.correction.location in report.locations
    assert report.correction.location.line == 2
    assert compiler_evidence(language, diagnostic, 1, False).correction is None


@pytest.mark.parametrize(
    ("language", "diagnostic"),
    [
        ("c17", "main.c:2:3: warning: expected ';' before 'return'"),
        ("c17", "main.c:2:3: error: #error expected ';' before 'return'"),
        ("c17", "other.c:2:3: error: expected ';' before 'return'"),
        ("python313", "  File \"main.py\", line 2\n    SyntaxError: expected ':'"),
        ("python313", '  File "main.py", line 2\n    if condition\n                ^\n'),
    ],
)
def test_unrecognized_or_incomplete_diagnostic_does_not_become_a_correction(language, diagnostic):
    assert compiler_evidence(language, diagnostic, 3, False).correction is None


@pytest.mark.parametrize(
    ("language", "opening", "before", "after", "closing"),
    [
        (
            language,
            b"int f(int n) {",
            b"return 0; // if while return",
            b'const char *label = "if while return";\nif (n) while (n--) return 1;',
            b"return 2;\n}",
        )
        for language in ("c17", "cpp20")
    ]
    + [
        (
            "java21",
            b"class Main { static int f(int n) {",
            b"return 0; // if while return",
            b'String label = "if while return";\nif (n > 0) while (n-- > 0) return 1;',
            b"return 2;\n}}",
        ),
        (
            "python313",
            b"def f(n):",
            b'    return "if while return"',
            b"    # if while return\n    if n:\n        for i in range(n):\n            return i",
            b"    return 2",
        ),
    ],
)
def test_control_keywords_only_count_changed_code(language, opening, before, after, closing):
    ref = CodeSubmissionRef(
        submission_id=UUID(int=1),
        evaluation_id=UUID(int=2),
        problem_version_id=UUID(int=3),
        language=language,
    )
    old, new = (b"\n".join((opening, changed, closing)).splitlines() for changed in (before, after))
    assert compare_owned_sources(ref, old, new).control_flow is None
    measured = compare_owned_sources(ref, old, new, collect_control_flow=True)
    assert measured.control_flow is not None
    assert measured.control_flow.before == CodeControlFlowCounts(
        branch_keywords=0, loop_keywords=0, return_keywords=1
    )
    assert measured.control_flow.after == CodeControlFlowCounts(
        branch_keywords=1, loop_keywords=1, return_keywords=1
    )
    assert (
        compare_owned_sources(
            ref, old, new + [b" " * 32768], collect_control_flow=True
        ).control_flow
        is None
    )


@pytest.mark.parametrize(
    ("language", "condition", "comment_start", "comment_end"),
    [
        ("c17", b"    if (left < right) {", b"/*", b"*/"),
        ("cpp20", b"    if (left < right) {", b"/*", b"*/"),
        ("java21", b"    if (left < right) {", b"/*", b"*/"),
        ("python313", b"    if left < right:", b"    example = '''", b"    '''"),
    ],
)
def test_boundary_changes_match_only_the_same_executable_condition(
    language, condition, comment_start, comment_end
):
    ref = CodeSubmissionRef(
        submission_id=UUID(int=1),
        evaluation_id=UUID(int=2),
        problem_version_id=UUID(int=3),
        language=language,
    )
    opening, body, ending = {
        "c17": (b"int f(int left, int right) {", [b"return 1;", b"}"], [b"return 0;", b"}"]),
        "cpp20": (b"int f(int left, int right) {", [b"return 1;", b"}"], [b"return 0;", b"}"]),
        "java21": (
            b"class Main { static int f(int left, int right) {",
            [b"return 1;", b"}"],
            [b"return 0;", b"}}"],
        ),
        "python313": (b"def f(left, right):", [b"        return 1"], [b"    return 0"]),
    }[language]

    def snippet(line):
        return [opening, line, *body, comment_start, line, comment_end, *ending]

    earlier = snippet(condition)
    inclusive = condition.replace(b"<", b"<=")
    later = snippet(inclusive)
    diff = compare_owned_sources(ref, earlier, later)
    assert diff.control_flow is None
    assert diff.boundary_changes == (
        CodeBoundaryChange(
            before_line=2,
            after_line=2,
            before_operator="<",
            after_operator="<=",
            changed_cases=("equal",),
        ),
    )
    reversed_condition = snippet(condition.replace(b"<", b">="))
    (reversed_change,) = compare_owned_sources(ref, earlier, reversed_condition).boundary_changes
    assert reversed_change.changed_cases == ("less", "equal", "greater")
    not_equal = snippet(condition.replace(b"<", b"!="))
    (unequal_change,) = compare_owned_sources(ref, earlier, not_equal).boundary_changes
    assert unequal_change.changed_cases == ("greater",)
    later[1] = inclusive.replace(b"right", b"left")
    assert compare_owned_sources(ref, earlier, later).boundary_changes == ()
