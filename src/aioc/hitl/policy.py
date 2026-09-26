"""What needs a human: the approval rule of CONTRACTS.md sec 4.1, made executable (Day 16).

The rule has two halves. ``requires_approval`` must be true when ``risk`` is ``medium``,
``high``, or ``other`` - structural, and the contract model enforces it. **Or** when the
action mutates production state - "rollback, restart, scale, merge, config write, traffic
shift" - which is semantic: the action is one imperative line of prose, and nothing in the
schema says whether it writes. Until Day 16 that half was enforced by prompt wording alone.

`classify` is that half as code: patterns over the action line and its command for each
kind of production write the contract names, plus two it implies (a deploy, and a
destructive data operation). It is a floor, not a judge, and it errs one way on purpose -
a read-only action that happens to say "restart" is sent to a human who waves it through;
a write it failed to recognise is still gated if the model flagged it or rated it risky.
`requires_approval` ORs all three, so the gate fails closed: no single signal can release
an action that another signal says needs a human.

Two consumers, one rule: the Incident agent's runtime stamps ``requires_approval: true``
on an action this classifies as a write (upward only, the `agents/_status.py` shape - a
value the code can derive is not left to the model), and `aioc.hitl.gate` re-derives the
requirement from scratch rather than trusting the flag it was handed.
"""

from __future__ import annotations

import re
from enum import StrEnum

from aioc.contracts import RecommendedAction, RiskLevel


class MutationKind(StrEnum):
    """The production writes the approval rule names, plus the two it implies."""

    ROLLBACK = "rollback"
    RESTART = "restart"
    SCALE = "scale"
    MERGE = "merge"
    CONFIG_WRITE = "config_write"
    TRAFFIC_SHIFT = "traffic_shift"
    DEPLOY = "deploy"
    DESTRUCTIVE = "destructive"


_W = r"(?<![\w-])"  # word start that does not split a hyphenated or snake_case token

_PATTERNS: dict[MutationKind, re.Pattern[str]] = {
    MutationKind.ROLLBACK: re.compile(
        rf"{_W}(roll(ing|ed)?[\s-]?back|rollbacks?|revert(ing|ed)?|downgrad(e|ing)|"
        rf"rollout\s+undo|undo(ing)?)\b",
        re.I,
    ),
    MutationKind.RESTART: re.compile(
        rf"{_W}(restart(ing|ed)?|reboot(ing|ed)?|bounc(e|ing)|recycl(e|ing)|"
        rf"kill(ing)?|terminat(e|ing)|rollout\s+restart|delete\s+(the\s+)?pods?)\b",
        re.I,
    ),
    MutationKind.SCALE: re.compile(
        rf"{_W}(scal(e|ing)(\s+(up|down|out|in))?|replicas?|autoscal\w*)\b", re.I
    ),
    MutationKind.MERGE: re.compile(rf"{_W}(merg(e|ing)|gh\s+pr\s+merge)\b", re.I),
    MutationKind.CONFIG_WRITE: re.compile(
        # A verb, up to three words, then what it writes: "increase the payments-api
        # client timeout", "update the config". Or a config key named the way this stack
        # names them (UPPER_SNAKE, case-sensitive) after a write verb: "set DB_POOL_SIZE".
        rf"{_W}((set|update|change|edit|write|modify|restore)\s+(\S+\s+){{0,3}}"
        rf"(config\w*|env(ironment)?(\s+var\w*)?|settings?|flags?|limits?|timeouts?)|"
        rf"(set|update|change|restore|reset)\s+(the\s+)?(?-i:[A-Z][A-Z0-9]*_[A-Z0-9_]+)|"
        rf"kubectl\s+(apply|patch|edit|set)|configmap|"
        rf"(enable|disable|toggle|flip)\s+(\S+\s+){{0,3}}(feature\s+)?flags?|"
        rf"(increase|decrease|raise|lower|bump|tune|adjust|tighten|loosen|add|introduce|"
        rf"configure)\s+(\S+\s+){{0,3}}(pool|timeout|limit|threshold|size|capacity|quota|"
        rf"retries|ttl|circuit[\s-]?breaker|rate[\s-]?limit)\w*)\b",
        re.I,
    ),
    MutationKind.TRAFFIC_SHIFT: re.compile(
        rf"{_W}((shift|route|reroute|divert|move)\s+(the\s+)?\S*\s*traffic|drain(ing)?|"
        rf"fail\s?over|cordon|canary\s+weight|traffic\s+(shift|split|weight))\b",
        re.I,
    ),
    MutationKind.DEPLOY: re.compile(
        # "deploy" the verb, not the noun: "after deploy", "the last deploy", "deploy
        # history" name a past event, and reading them as a request to deploy gated a
        # monitoring action in a recorded live run.
        rf"{_W}(?<!after\s)(?<!since\s)(?<!before\s)(?<!the\s)(?<!last\s)(?<!recent\s)"
        rf"(?<!a\s)(?<!this\s)(?<!that\s)(?<!previous\s)"
        rf"((re)?deploy(ing)?(?!\s+(history|log|diff|notes|record)s?\b)|"
        rf"roll(ing)?\s+out|promot(e|ing)|hotfix)\b",
        re.I,
    ),
    MutationKind.DESTRUCTIVE: re.compile(
        rf"{_W}(delet(e|ing)|drop(ping)?|purg(e|ing)|flush(ing)?|truncat(e|ing)|wip(e|ing))\b",
        re.I,
    ),
}

# An instruction NOT to write: the cue, then at most two words, then the match ("hold off
# on rolling back", "do not restart", "avoid scaling"). A recorded live run recommended
# "Hold off on rolling back the release" - a non-action that read as a rollback. Only the
# negated match is dropped: "do not restart; roll back instead" is still a rollback.
_NEGATION = re.compile(
    r"\b(hold\s+off(\s+on)?|do\s+not|don't|avoid|no\s+need\s+to|instead\s+of|rather\s+than|"
    r"without|never|not)\s+(\S+\s+){0,2}$",
    re.I,
)

_CLAUSE = re.compile(r"[;,:\n]|\.\s")  # "v1.4.2" is not a clause break; ". " is

_RISKY = (RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.OTHER)


def classify(action: str, command: str | None = None) -> list[MutationKind]:
    """Every kind of production write the action line or its command reads as, in the
    order `MutationKind` declares them. ``[]`` means none was recognised - not that the
    action is proven read-only."""
    text = f"{action}\n{command or ''}"
    return [kind for kind, pattern in _PATTERNS.items() if _affirmed(pattern, text)]


def _affirmed(pattern: re.Pattern[str], text: str) -> bool:
    """True when some match of ``pattern`` is not an instruction against it."""
    for match in pattern.finditer(text):
        # Only the clause the match sits in: "do not restart X; roll back" negates the
        # restart, not the rollback.
        clause = _CLAUSE.split(text[: match.start()])[-1]
        if not _NEGATION.search(clause):
            return True
    return False


def approval_reasons(action: RecommendedAction) -> list[str]:
    """Why ``action`` needs a human, one reason per signal that says so; ``[]`` when none
    does. The three signals are independent and any one is enough."""
    reasons: list[str] = []
    if action.requires_approval:
        reasons.append("flagged requires_approval by the agent")
    if action.risk in _RISKY:
        detail = f" ({action.risk_detail})" if action.risk_detail else ""
        reasons.append(f"risk is {action.risk.value}{detail}")
    kinds = classify(action.action, action.command)
    if kinds:
        reasons.append("mutates production state: " + ", ".join(k.value for k in kinds))
    return reasons


def requires_approval(action: RecommendedAction) -> bool:
    """The sec 4.1 approval rule, both halves, failing closed."""
    return bool(approval_reasons(action))
