---
paths:
  - "docker-compose.yml"
  - "docker/**"
  - "demo-app/**"
  - "Makefile"
  - ".env.example"
---
# Platform Layer - local stack, demo app, chaos

Engineer B's surface. These constraints exist because something downstream breaks without them, and
the breakage is usually days later and in a confusing place.

- Pin image tags. A floating `:latest` means "reproducible on my machine only", which is exactly what
  the Day 29 fresh-clone test is designed to catch.
- Postgres is `pgvector/pgvector`, never plain `postgres`. Plain postgres starts perfectly happily
  and then fails much later inside the retrieval code, which reads as a bug in Day 8's ingestion
  rather than an image choice made on Day 1.
- Data lives in named volumes. The Day 5 seed corpus (15-20 synthetic incidents) is both the RAG
  corpus and the eval set, so it must survive `docker compose down`. Only `make db-reset` is allowed
  to destroy it, and it is destructive by design - say so before running it.
- `docker/postgres/init/` runs once, on first initialisation of an empty data volume. Changing a file
  there has no effect on a stack that is already up; it needs `make db-reset` to take.
- The `--mode` strings in the `chaos-*` targets are the exact `FailureMode` enum members from
  CONTRACTS.md sec 4.1. That 1:1 mapping is what lets the Day 19 eval harness score agent output
  against injected ground truth, so these strings are not free to drift. Changing one is a contract
  change, not a rename.
- Chaos must be reversible: every failure mode has a path back to a healthy baseline via
  `make chaos-reset`. An injected fault that needs a stack rebuild to clear will not survive a demo.
- `make verify` checks the stack is *usable*, not merely running. Extend it when you add a service;
  a container reporting healthy while its extension is missing is the failure this target exists for.
- `.env.example` is committed and carries keys with no values. Never put a real value in it, and add
  the key here whenever you introduce a new one so a fresh clone knows what to fill in.
- Every recipe stays a single command that can be pasted into PowerShell. GNU Make is not installed
  on both machines, and the Makefile is the documented fallback path.
