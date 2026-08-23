"""Unit tests for the GitHub agent (Day 11).

Same pattern as the Docs suite: the Anthropic client is a scripted fake injected through
`LLMClient`, and the MCP toolset is an in-process fake behind the `Toolset` seam whose
handlers return canned contract envelopes - so everything here is deterministic, offline,
and key-free. Under test is the agent's plumbing - explicit context passing, the two-phase
tool loop, fact stamping from the ledger, the grounding rejections, the honest
`ToolCallRef`s - not the model's judgement (the Day 19 eval harness owns that).
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.types import TextBlock, ToolUseBlock

from aioc.agents import (
    GITHUB_EMIT_TOOL_NAME,
    GITHUB_SYSTEM_PROMPT,
    GitHubAgent,
    GitHubAgentError,
)
from aioc.agents.github import _EMIT_SCHEMA, GitHubReport, _apply_guidance
from aioc.contracts import AgentName, ErrorClass, GitHubAgentResponse, PullRequestState
from aioc.llm import LLMClient, LLMSettings, ToolResult, ToolSpec, Usage

# --------------------------------------------------------------------------- fakes


class _FakeMessages:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("fake client ran out of scripted responses")
        return self._responses.pop(0)


class _FakeAnthropic:
    def __init__(self, responses: list[Any]) -> None:
        self.messages = _FakeMessages(responses)


_SHA = "37b6cc02d8a1f4e6b9c0d2e4f6a8b0c2d4e6f8a0"
_SHA_2 = "c729c72aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_TITLE = "Day 10: first real end-to-end demo + checkpoint GIF"
_MESSAGE = "Day 10: the first real end-to-end demo, with the checkpoint GIF (#12)"


def _pr_envelope() -> dict[str, Any]:
    return {
        "ok": True,
        "data": {
            "repository": "m-misbahuddin/aioc",
            "pull_request": {
                "number": 12,
                "title": _TITLE,
                "state": "merged",
                "draft": False,
                "merged_at": "2026-08-22T03:36:15Z",
                "created_at": "2026-08-22T03:30:00Z",
                "author": "m-misbahuddin",
                "head_sha": _SHA,
                "head_ref": "d10-first-real-demo",
                "base_ref": "main",
                "html_url": "https://github.com/m-misbahuddin/aioc/pull/12",
                "files_changed": 8,
                "additions": 549,
                "deletions": 48,
                "touched_paths": ["CLAUDE.md", "HANDOFF.md", "scripts/demo_day10.py"],
                "body": "Adds the demo script.",
                "body_truncated": False,
            },
            "files": [],
            "commits": [
                {
                    "sha": _SHA,
                    "short_sha": _SHA[:7],
                    "message": _MESSAGE,
                    "authored_at": "2026-08-22T03:35:07Z",
                    "html_url": None,
                    "pull_request_number": 12,
                    "touched_paths": [],
                },
                {
                    "sha": _SHA_2,
                    "short_sha": _SHA_2[:7],
                    "message": "Langfuse auth fails fast with a region hint",
                    "authored_at": "2026-08-22T03:20:00Z",
                    "html_url": None,
                    "pull_request_number": None,
                    "touched_paths": [],
                },
            ],
            "redacted_lines": 0,
        },
        "meta": {
            "truncated": False,
            "total_available": 8,
            "returned": 0,
            "token_estimate": 321,
            "query_ms": 80,
            "source": "github",
            "as_of": "2026-08-22T10:00:00Z",
        },
    }


def _error_envelope() -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "class": "permission",
            "code": "GITHUB_SCOPE_MISSING",
            "message": "No GITHUB_TOKEN is configured.",
            "retryable": False,
            "retry_after_ms": None,
            "details": {"required_scope": "contents:read"},
            "remediation": "Set GITHUB_TOKEN.",
        },
    }


class _FakeToolset:
    """An open toolset: three tools whose handlers return canned envelopes."""

    server_name = "aioc-github"

    def __init__(self, envelope: dict[str, Any]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._envelope = envelope

    @property
    def tools(self) -> list[ToolSpec]:
        def handler(name: str) -> Any:
            def run(args: dict[str, Any]) -> ToolResult:
                self.calls.append((name, args))
                return ToolResult(
                    content=json.dumps(self._envelope), is_error=not self._envelope["ok"]
                )

            return run

        return [
            ToolSpec(
                name=n, description=f"{n} desc", input_schema={"type": "object"}, handler=handler(n)
            )
            for n in ("get_pull_request", "list_commits", "diff_refs")
        ]


def _message(
    content: list[Any], *, stop_reason: str, in_tokens: int = 500, out_tokens: int = 100
) -> Any:
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=content,
        model="claude-sonnet-5",
        usage=SimpleNamespace(input_tokens=in_tokens, output_tokens=out_tokens),
    )


def _tool_call_message(name: str, args: dict[str, Any], call_id: str = "toolu_1") -> Any:
    return _message(
        [ToolUseBlock(type="tool_use", id=call_id, name=name, input=args)], stop_reason="tool_use"
    )


def _done_message() -> Any:
    return _message([TextBlock(type="text", text="I have what I need.")], stop_reason="end_turn")


def _emit_message(payload: dict[str, Any], *, stop_reason: str = "tool_use") -> Any:
    return _message(
        [ToolUseBlock(type="tool_use", id="toolu_emit", name=GITHUB_EMIT_TOOL_NAME, input=payload)],
        stop_reason=stop_reason,
    )


def _report(**overrides: Any) -> dict[str, Any]:
    """A contract-valid GitHubReport payload grounded in the fake PR envelope above."""
    payload: dict[str, Any] = {
        "status": "complete",
        "status_detail": None,
        "summary": "PR #12 added the end-to-end demo script; low risk, no service code touched.",
        "findings": {
            "ref": None,
            "pull_requests": [
                {
                    "number": 12,
                    "risk": {
                        "value": "low",
                        "confidence": 0.8,
                        "evidence": ["ev_1"],
                        "reasoning": "Only docs and a dev script changed.",
                        "detail": None,
                    },
                    "summary": {
                        "value": "Adds the Day 10 demo script and the checkpoint GIF.",
                        "confidence": 0.85,
                        "evidence": ["ev_1"],
                        "reasoning": "Title and touched paths say so.",
                        "detail": None,
                    },
                }
            ],
            "commit_shas": [_SHA, _SHA_2[:10]],
            "suspect_changes": [
                {
                    "change_ref": "#12",
                    "change_type": "code",
                    "change_type_detail": None,
                    "symptom_link": {
                        "value": "Does not touch the service path, so it cannot explain the spike.",
                        "confidence": 0.7,
                        "evidence": ["ev_1"],
                        "reasoning": "Touched paths are docs and scripts only.",
                        "detail": None,
                    },
                }
            ],
            "diff_summary": {
                "value": "A demo script and documentation; no runtime behaviour changes.",
                "confidence": 0.8,
                "evidence": ["ev_1", "ev_2"],
                "reasoning": "Derived from the PR title and commit message.",
                "detail": None,
            },
        },
        "evidence": [
            {
                "id": "ev_1",
                "source_type": "pull_request",
                "source_type_detail": None,
                "source_ref": "#12",
                "excerpt": _TITLE,
                "observed_at": None,
                "uri": "https://github.com/m-misbahuddin/aioc/pull/12",
                "tool_call_id": None,
            },
            {
                "id": "ev_2",
                "source_type": "commit",
                "source_type_detail": None,
                "source_ref": _SHA,
                "excerpt": _MESSAGE,
                "observed_at": None,
                "uri": None,
                "tool_call_id": None,
            },
        ],
        "gaps": [],
        "overall_confidence": 0.8,
    }
    payload.update(overrides)
    return payload


def _agent(
    responses: list[Any], envelope: dict[str, Any] | None = None
) -> tuple[GitHubAgent, _FakeMessages, _FakeToolset]:
    fake = _FakeAnthropic(responses)
    settings = LLMSettings(model="claude-sonnet-5", max_tokens=4096)
    client = LLMClient(settings, client=fake)  # type: ignore[arg-type]
    toolset = _FakeToolset(envelope if envelope is not None else _pr_envelope())

    @contextmanager
    def factory() -> Any:
        yield toolset

    agent = GitHubAgent(client, toolset=factory, repository="m-misbahuddin/aioc")
    return agent, fake.messages, toolset


_CONTEXT = (
    "User query: 'What did PR #12 change?' Repository m-misbahuddin/aioc. No incident is "
    "in progress; this is a change-analysis question."
)
_QUERY = "What did PR #12 change, and how risky is it?"

_SCRIPT = [
    _tool_call_message("get_pull_request", {"number": 12}),
    _done_message(),
]


# ------------------------------------------------------------------------ happy path


def test_analyze_returns_a_validated_response_with_facts_stamped_from_the_tool():
    agent, messages, toolset = _agent([*_SCRIPT, _emit_message(_report())])
    usage = Usage()

    resp = agent.analyze(
        _QUERY, context=_CONTEXT, request_id="req_1", invocation_id="inv_1", usage=usage
    )

    assert isinstance(resp, GitHubAgentResponse)
    assert resp.agent is AgentName.GITHUB
    assert resp.request_id == "req_1" and resp.invocation_id == "inv_1"
    assert toolset.calls == [("get_pull_request", {"number": 12})]

    # Facts came from the envelope, not the model (which only sent number + judgements).
    pr = resp.findings.pull_requests[0]
    assert pr.title == _TITLE
    assert pr.state is PullRequestState.MERGED and pr.state_detail is None
    assert pr.head_sha == _SHA and pr.files_changed == 8 and pr.additions == 549
    assert pr.touched_paths == ["CLAUDE.md", "HANDOFF.md", "scripts/demo_day10.py"]
    assert pr.risk.value == "low"
    assert resp.findings.repository == "m-misbahuddin/aioc"

    # Commits by SHA (a unique prefix resolves), facts stamped.
    shas = [c.sha for c in resp.findings.commits]
    assert shas == [_SHA, _SHA_2]
    assert resp.findings.commits[0].message == _MESSAGE
    assert resp.findings.commits[0].authored_at == datetime(2026, 8, 22, 3, 35, 7, tzinfo=UTC)
    assert resp.findings.commits[0].pull_request_number == 12

    # One honest ToolCallRef per wire call, and evidence points at it.
    assert len(resp.tool_calls) == 1
    ref = resp.tool_calls[0]
    assert ref.tool_name == "get_pull_request" and ref.server == "aioc-github"
    assert ref.ok is True and ref.error_class is None
    assert ref.tokens_returned == 321 and ref.truncated is False
    assert ref.id.startswith("tc_")
    assert all(e.tool_call_id == ref.id for e in resp.evidence)

    # Cost: two loop calls plus the forced emit call, all accumulated.
    assert len(messages.calls) == 3
    assert usage.input_tokens == 1500 and usage.output_tokens == 300


def test_context_is_passed_explicitly_and_the_emit_call_is_forced():
    agent, messages, _ = _agent([*_SCRIPT, _emit_message(_report())])
    agent.analyze(_QUERY, context=_CONTEXT)

    first = messages.calls[0]
    assert first["system"] == GITHUB_SYSTEM_PROMPT
    user_text = first["messages"][0]["content"]
    assert _CONTEXT in user_text and _QUERY in user_text
    assert {t["name"] for t in first["tools"]} == {"get_pull_request", "list_commits", "diff_refs"}
    assert "tool_choice" not in first or first["tool_choice"] is None

    emit = messages.calls[2]
    assert emit["tool_choice"] == {"type": "tool", "name": GITHUB_EMIT_TOOL_NAME}
    assert GITHUB_EMIT_TOOL_NAME in {t["name"] for t in emit["tools"]}
    # The whole investigation (assistant turns + tool results) precedes the emit request.
    roles = [m["role"] for m in emit["messages"]]
    assert roles == ["user", "assistant", "user", "assistant", "user"]


@pytest.mark.parametrize("bad", ["", "   "])
def test_empty_context_is_refused_before_any_call(bad: str):
    agent, messages, toolset = _agent([])
    with pytest.raises(ValueError, match="context must be non-empty"):
        agent.analyze(_QUERY, context=bad)
    assert messages.calls == [] and toolset.calls == []


# ------------------------------------------------------------------ grounding rejections


def test_a_pull_request_the_agent_never_fetched_is_rejected():
    report = _report()
    report["findings"]["pull_requests"][0]["number"] = 99
    agent, _, _ = _agent([*_SCRIPT, _emit_message(report)])
    with pytest.raises(GitHubAgentError, match="#99 was never fetched"):
        agent.analyze(_QUERY, context=_CONTEXT)


def test_a_commit_the_agent_never_fetched_is_rejected():
    report = _report()
    report["findings"]["commit_shas"] = ["deadbeefdeadbeef"]
    agent, _, _ = _agent([*_SCRIPT, _emit_message(report)])
    with pytest.raises(GitHubAgentError, match="never fetched"):
        agent.analyze(_QUERY, context=_CONTEXT)


def test_a_suspect_change_must_reference_fetched_data():
    report = _report()
    report["findings"]["suspect_changes"][0]["change_ref"] = "#7"
    agent, _, _ = _agent([*_SCRIPT, _emit_message(report)])
    with pytest.raises(GitHubAgentError, match="suspect change '#7' was never fetched"):
        agent.analyze(_QUERY, context=_CONTEXT)


def test_a_paraphrased_excerpt_is_rejected():
    report = _report()
    report["evidence"][0]["excerpt"] = "Day 10: the first real demo and a GIF"
    agent, _, _ = _agent([*_SCRIPT, _emit_message(report)])
    with pytest.raises(GitHubAgentError, match="does not appear verbatim"):
        agent.analyze(_QUERY, context=_CONTEXT)


def test_evidence_citing_an_unfetched_source_is_rejected():
    report = _report()
    report["evidence"][1]["source_ref"] = "0123456789abcdef"
    agent, _, _ = _agent([*_SCRIPT, _emit_message(report)])
    with pytest.raises(GitHubAgentError, match="no tool call returned"):
        agent.analyze(_QUERY, context=_CONTEXT)


# ---------------------------------------------------------------- failed tool calls


def _failed_report() -> dict[str, Any]:
    return _report(
        status="partial",
        summary="GitHub could not be read.",
        findings={
            "ref": None,
            "pull_requests": [],
            "commit_shas": [],
            "suspect_changes": [],
            "diff_summary": {
                "value": None,
                "confidence": 0.0,
                "evidence": [],
                "reasoning": "The tool returned a permission error.",
                "detail": None,
            },
        },
        evidence=[
            {
                "id": "ev_ctx",
                "source_type": "other",
                "source_type_detail": "coordinator context",
                "source_ref": "context",
                "excerpt": "No incident is in progress",
                "observed_at": None,
                "uri": None,
                "tool_call_id": None,
            }
        ],
        gaps=[
            {
                "id": "gap_1",
                "description": "GITHUB_SCOPE_MISSING: no token configured.",
                "kind": "insufficient_permission",
                "kind_detail": None,
                "blocks_field": "findings.diff_summary.value",
                "suggested_agent": None,
                "suggested_query": None,
                "resolvable": False,
            }
        ],
        overall_confidence=0.1,
    )


def test_a_failed_tool_call_is_recorded_with_its_error_class():
    agent, _, _ = _agent([*_SCRIPT, _emit_message(_failed_report())], envelope=_error_envelope())
    resp = agent.analyze(_QUERY, context=_CONTEXT)

    assert resp.status.value == "partial"
    ref = resp.tool_calls[0]
    assert ref.ok is False and ref.error_class is ErrorClass.PERMISSION
    assert ref.tokens_returned is None and ref.truncated is False
    assert resp.findings.diff_summary.value is None
    assert resp.gaps[0].resolvable is False


def test_complete_status_with_no_successful_tool_call_is_rejected():
    report = _failed_report()
    report["status"] = "complete"
    report["findings"]["diff_summary"] = {
        "value": "Nothing to report.",
        "confidence": 0.3,
        "evidence": [],
        "reasoning": "Guessing.",
        "detail": None,
    }
    agent, _, _ = _agent([*_SCRIPT, _emit_message(report)], envelope=_error_envelope())
    with pytest.raises(GitHubAgentError, match="no tool call succeeded"):
        agent.analyze(_QUERY, context=_CONTEXT)


# ------------------------------------------------------------------- output plumbing


def test_truncated_emit_output_is_reported_as_truncation_not_as_a_schema_error():
    agent, _, _ = _agent(
        [*_SCRIPT, _emit_message({"status": "complete"}, stop_reason="max_tokens")]
    )
    with pytest.raises(GitHubAgentError, match="max_tokens"):
        agent.analyze(_QUERY, context=_CONTEXT)


def test_model_not_calling_the_emit_tool_fails_clearly():
    agent, _, _ = _agent([*_SCRIPT, _done_message()])
    with pytest.raises(GitHubAgentError, match="did not call"):
        agent.analyze(_QUERY, context=_CONTEXT)


def test_emit_schema_is_generated_from_the_frozen_models_and_annotated():
    props = _EMIT_SCHEMA["properties"]
    assert set(props) >= {"status", "summary", "findings", "evidence", "gaps", "overall_confidence"}
    assert "fetched data" in props["status"]["description"]
    assert "ReportedPullRequest" in _EMIT_SCHEMA["$defs"]
    # Facts are not the model's to fill in: the reported PR carries number + judgements only.
    assert set(_EMIT_SCHEMA["$defs"]["ReportedPullRequest"]["properties"]) == {
        "number",
        "risk",
        "summary",
    }


def test_guidance_drift_fails_loudly():
    schema = GitHubReport.model_json_schema()
    del schema["properties"]["summary"]
    with pytest.raises(RuntimeError, match="github emit schema guidance is out of sync"):
        _apply_guidance(schema)
