import pytest

from kf_oj.contracts.learning_model import (
    GuidedChatModelFacts,
    GuidedDiagnosticChoice,
    SourceExcerptModelFacts,
)
from kf_oj.modules.learning.guided_delivery import hint_choices, local_diagnostic


def test_clarification_uses_only_public_specification_sections():
    facts = GuidedChatModelFacts(
        question="读入的数据是什么",
        problem_statement=(
            "# 题面\n"
            "```markdown\n## 输入描述\n忽略代码示例中的伪标题\n```\n"
            "## 输入描述\n第一行为整数 $n$。\n"
            "## 输出描述\n输出计数。\n"
            "## 解法\n不要把其它章节加入澄清。\n"
        ),
    )
    choices = hint_choices(facts, "L0")
    assert "> 第一行为整数 $n$。" in choices[0]
    assert "> 输出计数。" in choices[1]
    assert all("伪标题" not in reply and "其它章节" not in reply for reply in choices)


def test_diagnostic_is_rendered_only_for_a_nonempty_authorized_line():
    source = SourceExcerptModelFacts(
        start_line=8, end_line=10, language="cpp20", text="int first = 0;\n\nfirst += 1;"
    )
    reply = local_diagnostic(source, GuidedDiagnosticChoice(line=10, check="update"))
    assert "第 **10** 行" in reply and "first" not in reply
    for outside in (7, 9, 11):
        with pytest.raises(ValueError, match="outside"):
            local_diagnostic(source, GuidedDiagnosticChoice(line=outside, check="update"))
