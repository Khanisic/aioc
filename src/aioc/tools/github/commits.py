"""A commit message as the facts an agent reads (Day 21), shared by every tool that lists
commits: the three GitHub tools and `diff_release`.

A message body is prose an agent re-reads on every round of its tool loop, and in the
four-agent run of 2026-09-30 a twenty-commit list was 16.8k characters, nine tenths of it
bodies. The subject line is the commit's own summary of itself; what is dropped is said to
be dropped, and the one fact a GitHub merge commit hides in its body - the pull request's
title - is read out of it.
"""

from __future__ import annotations

import re

_MERGE_SUBJECT = re.compile(r"^Merge pull request #\d+ ")


def commit_headline(message: str) -> tuple[str, bool, str | None]:
    """A commit message as the facts an agent reads: the subject line, whether anything
    after it was dropped, and - for a GitHub merge commit, whose subject names only a
    branch - the pull request's title, which is the first line of its body. Every string
    returned is a verbatim line of the message, so a quote of it grounds."""
    lines = message.strip().splitlines()
    if not lines:
        return "", False, None
    subject = lines[0].strip()
    body = [line.strip() for line in lines[1:] if line.strip()]
    title = body[0] if body and _MERGE_SUBJECT.match(subject) else None
    return subject, bool(body), title
