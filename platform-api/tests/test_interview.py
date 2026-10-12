"""R02 访谈题目与答案 schema 测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentforge_platform.interview import (
    Answer,
    AnswerSubmission,
    Question,
    QuestionBatch,
    QuestionType,
    validate_answer,
    validate_submission,
)


def _single(**overrides) -> Question:
    base = {
        "question_id": "q1",
        "type": QuestionType.SINGLE_CHOICE,
        "prompt": "成员是否可以重复报名同一个活动？",
        "options": ["可以", "不可以"],
        "required": True,
        "affects": ["business_rules"],
    }
    base.update(overrides)
    return Question(**base)


def test_single_multi_text_questions_are_valid() -> None:
    assert _single().type is QuestionType.SINGLE_CHOICE
    multi = _single(type=QuestionType.MULTI_CHOICE, options=["发布", "报名"])
    assert multi.type is QuestionType.MULTI_CHOICE
    text = _single(type=QuestionType.TEXT, options=[], allow_custom=True)
    assert text.type is QuestionType.TEXT


def test_text_question_rejects_options() -> None:
    with pytest.raises(ValidationError):
        _single(type=QuestionType.TEXT, options=["a"])


def test_choice_question_requires_options() -> None:
    with pytest.raises(ValidationError):
        _single(options=[])


def test_choice_options_must_be_unique_and_nonempty() -> None:
    with pytest.raises(ValidationError):
        _single(options=["可以", "可以"])
    with pytest.raises(ValidationError):
        _single(options=["可以", "  "])


def test_affects_must_be_known_and_nonempty() -> None:
    with pytest.raises(ValidationError):
        _single(affects=[])
    with pytest.raises(ValidationError):
        _single(affects=["whatever"])


def test_affects_are_deduplicated() -> None:
    question = _single(affects=["entities", "entities", "permissions"])
    assert question.affects == ["entities", "permissions"]


def test_prompt_rejects_empty_long_and_technical_terms() -> None:
    with pytest.raises(ValidationError):
        _single(prompt="  ")
    with pytest.raises(ValidationError):
        _single(prompt="问题" * 200)
    with pytest.raises(ValidationError):
        _single(prompt="这个实体用 MyBatis 还是 ORM？")


def test_question_id_and_version_are_constrained() -> None:
    with pytest.raises(ValidationError):
        _single(question_id="1 bad id")
    with pytest.raises(ValidationError):
        _single(version=0)


def test_batch_enforces_round_and_count_limits() -> None:
    questions = [_single(question_id=f"q{i}") for i in range(1, 4)]
    assert QuestionBatch(round=1, questions=questions).round == 1

    with pytest.raises(ValidationError):
        QuestionBatch(round=5, questions=questions)  # 超过 4 轮
    with pytest.raises(ValidationError):
        QuestionBatch(round=1, questions=[*questions, _single(question_id="q4")])  # 超过 3 题


def test_batch_rejects_duplicate_question_ids() -> None:
    with pytest.raises(ValidationError):
        QuestionBatch(round=1, questions=[_single(question_id="q1"), _single(question_id="q1")])


def test_answer_binding_version_mismatch_is_rejected() -> None:
    question = _single(version=2)
    stale = Answer(question_id="q1", question_version=1, selected=["可以"])

    issues = validate_answer(question, stale)
    assert any("版本" in issue for issue in issues)


def test_answer_option_and_custom_rules() -> None:
    question = _single()

    assert validate_answer(question, Answer(
        question_id="q1", question_version=1, selected=["可以"]
    )) == []
    assert validate_answer(question, Answer(
        question_id="q1", question_version=1, selected=["可以", "不可以"]
    ))  # 单选多选
    assert validate_answer(question, Answer(
        question_id="q1", question_version=1, selected=["也许"]
    ))  # 选项越界
    assert validate_answer(question, Answer(
        question_id="q1", question_version=1, custom="其他"
    ))  # 不允许自定义


def test_answer_text_and_unsure_rules() -> None:
    text = _single(question_id="q2", type=QuestionType.TEXT, options=[], allow_custom=True)
    assert validate_answer(text, Answer(
        question_id="q2", question_version=1, custom="大约一百人"
    )) == []
    assert validate_answer(text, Answer(
        question_id="q2", question_version=1, selected=["可以"]
    ))  # 文本题不能选项
    assert validate_answer(text, Answer(
        question_id="q2", question_version=1, unsure=True, custom="x"
    ))  # 不确定不能同时给答案


def test_submission_unknown_question_and_pending() -> None:
    questions = [_single(question_id="q1"), _single(question_id="q2", required=True)]
    submission = AnswerSubmission(answers=[
        Answer(question_id="q1", question_version=1, selected=["可以"]),
        Answer(question_id="q9", question_version=1, selected=["可以"]),  # 未知题目
    ])

    result = validate_submission(questions, submission)

    assert any("未知题目" in issue for issue in result["issues"])
    assert result["resolved"] == ["q1"]
    assert result["pending"] == ["q2"]  # 未回答的必填题 → 待确认


def test_submission_unsure_stays_pending() -> None:
    questions = [_single(question_id="q1", required=True)]
    submission = AnswerSubmission(answers=[
        Answer(question_id="q1", question_version=1, unsure=True),
    ])

    result = validate_submission(questions, submission)

    assert result["issues"] == []
    assert result["resolved"] == []
    assert result["pending"] == ["q1"]  # "不确定"不等于已明确
