"""Day 8 done-when, live: does the Docs agent answer from the seeded corpus with citations?

    uv run python scripts/check_day8_docs.py            # 1 live Claude call
    uv run python scripts/check_day8_docs.py --query "..."

One Claude API call (the Docs agent's structured report), plus one Voyage query-embedding
call when VOYAGE_API_KEY is set (fractions of a cent; without the key retrieval runs
lexical-only and the check still proves the done-when). Needs the Docker stack up - the
corpus is what the agent answers from.

**What this checks that the offline tests cannot.** `tests/test_docs_agent.py` proves the
grounding checks against scripted payloads; here the *model* writes the report, so what is
being proven is that a real model, given real retrieved documents, produces a
contract-valid `DocsAgentResponse` whose every claim cites a document retrieval actually
returned with a verbatim quote - the whole Day 8 done-when in one call. The agent's own
grounding checks raise on violation, so a passing run *is* the proof; the evaluation below
restates the facts for the record rather than re-deriving them.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runlog import RunRecorder  # noqa: E402 - needs the sys.path insert above

from aioc.agents import DocsAgent  # noqa: E402
from aioc.llm import LLMSettings, Usage  # noqa: E402
from aioc.retrieval import CorpusSearcher, EmbeddingSettings, default_embedder  # noqa: E402

DEFAULT_QUERY = (
    "How have we fixed payments-api memory growth in the past, and what was the root cause?"
)

CONTEXT = (
    "Routine documentation lookup; no live incident is in progress. The demo services are "
    "checkout-api, payments-api, and inventory-api. Answer from the historical incident "
    "corpus only."
)


def _evaluate(resp: Any, usage: Usage) -> list[str]:
    """Return the list of complaints. Empty means the done-when held live."""
    complaints: list[str] = []
    cov = resp.findings.coverage

    supported = [c for c in resp.findings.claims if c.supported]
    if not supported:
        complaints.append("no supported claim - the corpus answer never materialised")
    if not any(c.sources for c in supported):
        complaints.append("no claim cites a document")
    if resp.findings.answer.value is None and resp.status.value not in (
        "insufficient_evidence",
        "error",
    ):
        complaints.append("answer.value is null on a non-insufficient status")
    if cov.documents_retrieved == 0:
        complaints.append("retrieval returned nothing - is the corpus seeded?")
    if cov.documents_cited == 0:
        complaints.append("documents_cited is 0 despite retrieved documents")
    if not any(tc.tool_name == "search_corpus" for tc in resp.tool_calls):
        complaints.append("the retrieval call is not recorded in tool_calls")
    if usage.input_tokens <= 0 or usage.output_tokens <= 0:
        complaints.append("cost is zero - the Usage accumulator did not thread through")
    return complaints


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--query", default=DEFAULT_QUERY, help="the documentation query to run")
    args = parser.parse_args(argv)

    if LLMSettings().anthropic_api_key is None:
        print("ANTHROPIC_API_KEY is not set (shell or .env).", file=sys.stderr)
        return 2

    embedder = default_embedder()
    mode_note = (
        f"hybrid (model {EmbeddingSettings().model})" if embedder else "lexical-only (no key)"
    )
    print(f"Day 8 docs check - 1 live Claude call; retrieval {mode_note}\n")

    agent = DocsAgent(retriever=CorpusSearcher(embedder))
    usage = Usage()
    start = time.monotonic()

    with RunRecorder(
        kind="llm",
        name="day8-docs",
        command="check_day8_docs.py",
        metadata={"query": args.query, "retrieval": mode_note},
    ) as run:
        try:
            resp = agent.answer(args.query, context=CONTEXT, usage=usage)
        except Exception as exc:
            run.event(
                "docs",
                outcome="failed",
                duration_ms=(time.monotonic() - start) * 1000,
                type="llm_call",
                message=f"{type(exc).__name__}: {exc}",
            )
            print(f"  FAIL  {type(exc).__name__}: {exc}")
            return 1

        duration_ms = (time.monotonic() - start) * 1000
        complaints = _evaluate(resp, usage)
        passed = not complaints
        cov = resp.findings.coverage

        run.event(
            "docs",
            outcome="passed" if passed else "failed",
            duration_ms=duration_ms,
            type="llm_call",
            data={
                "query": args.query,
                "status": resp.status.value,
                "claims": len(resp.findings.claims),
                "supported_claims": sum(1 for c in resp.findings.claims if c.supported),
                "documents_searched": cov.documents_searched,
                "documents_retrieved": cov.documents_retrieved,
                "documents_cited": cov.documents_cited,
                "corpus_snapshot": cov.corpus_snapshot,
                "unanswered": list(cov.unanswered),
                "overall_confidence": resp.overall_confidence,
                "usage": {"in": usage.input_tokens, "out": usage.output_tokens},
                "complaints": complaints,
            },
            message="; ".join(complaints) if complaints else "docs agent answered with citations",
        )
        run.artifact("docs_response.json", resp.model_dump_json(indent=2))

        print(f"  status     {resp.status.value}")
        print(
            f"  coverage   searched {cov.documents_searched}, retrieved "
            f"{cov.documents_retrieved}, cited {cov.documents_cited}, "
            f"snapshot {cov.corpus_snapshot}"
        )
        for claim in resp.findings.claims:
            mark = "cited" if claim.sources else "UNSUPPORTED"
            docs = ", ".join(s.document_id for s in claim.sources) or "-"
            print(f"  claim      [{mark}] ({claim.confidence:.2f}) {claim.statement[:70]}")
            print(f"             sources: {docs}")
        print(f"  answer     ({resp.findings.answer.confidence:.2f}) {resp.findings.answer.value}")
        print(f"  cost       {usage.input_tokens} in / {usage.output_tokens} out")
        for complaint in complaints:
            print(f"  ! {complaint}")

    print(f"\n--- docs agent: {'PASS' if passed else 'FAIL'} ---")
    print(f"records: {run.dir}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
