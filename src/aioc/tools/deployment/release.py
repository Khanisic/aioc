"""The release diff: what changed for one service between two releases (Day 12).

A release of the demo stack is a git ref of the configured repository: the compose file
declares each service's image and environment, `.env`-style files carry the shared
configuration, and Kubernetes manifests under `infrastructure/` (kept as documented
manifests, never a build priority) declare the same things for a cluster. `diff_release`
therefore reads the manifests at both ends of a `base...head` comparison and diffs them
**structurally** - parsed, not pattern-matched on patch lines - so an image that moved
because a whole block was re-indented is not reported as a change, and a config key that
changed value is reported as changed rather than as removed-then-added.

**Config values never leave the parser.** The contract (sec 4.4, sec 7.3) says keys only,
and this module enforces it one step earlier than the wire: a parsed value is hashed the
moment it is read and only the hash is kept, so the comparison ("did it change?") is
possible and the value itself is not held anywhere a serialiser could reach. The same holds
for manifest values - `manifest_changes` is a list of dotted paths, nothing else.

**No `aioc.contracts` import** - the MCP boundary is JSON Schema (contract sec 6). Everything
here is plain dicts and dataclasses that the server serialises.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

import yaml

# ------------------------------------------------------------------------- what counts

_COMPOSE = re.compile(r"(^|/)(docker-)?compose[^/]*\.ya?ml$")
_ENV_FILE = re.compile(r"(^|/)(\.env[^/]*|[^/]+\.env)$")
_MANIFEST_DIRS = ("infrastructure/", "k8s/", "kubernetes/", "deploy/", "manifests/", "helm/")
_YAML = re.compile(r"\.ya?ml$")

# The Kubernetes kinds whose paths are reported bare (`spec.replicas`); every other kind is
# prefixed with its lowercase kind so `service.spec.ports[0].port` cannot be mistaken for
# the workload's own spec.
_WORKLOAD_KINDS = frozenset({"Deployment", "StatefulSet", "DaemonSet", "Pod", "Job", "CronJob"})


def classify_path(path: str) -> str | None:
    """``compose``, ``env``, ``manifest``, or ``None`` when the file carries no release
    identity (source, docs, tests)."""
    normalised = path.replace("\\", "/")
    if _COMPOSE.search(normalised):
        return "compose"
    if _ENV_FILE.search(normalised):
        return "env"
    if _YAML.search(normalised) and any(
        normalised.startswith(d) or f"/{d}" in normalised for d in _MANIFEST_DIRS
    ):
        return "manifest"
    return None


# ------------------------------------------------------------------------------ snapshot


def _digest(value: Any) -> str:
    """The only form in which a configuration or manifest value is ever retained."""
    return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()


@dataclass
class ReleaseSnapshot:
    """One service's release identity at one ref: config keys (hashed values), images by
    container, and every other manifest leaf (hashed) by dotted path."""

    config: dict[str, str] = field(default_factory=dict)  # key -> value digest
    images: dict[str, str] = field(default_factory=dict)  # container -> image reference
    manifest: dict[str, str] = field(default_factory=dict)  # dotted path -> value digest

    def merge(self, other: ReleaseSnapshot) -> None:
        """Fold another file's contribution in. A key declared in two files (an env file
        and the compose `environment` block, say) is one key whose digest covers both."""
        for key, digest in other.config.items():
            mine = self.config.get(key)
            self.config[key] = digest if mine is None else _digest((mine, digest))
        self.images.update(other.images)
        self.manifest.update(other.manifest)


def _flatten(value: Any, prefix: str = "") -> Iterator[tuple[str, str]]:
    """Every leaf of a parsed document as ``(dotted.path[with][indexes], digest)``."""
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield from _flatten(item, path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _flatten(item, f"{prefix}[{index}]")
    else:
        yield prefix, _digest(value)


def _env_keys_of(block: Any) -> dict[str, str]:
    """A compose `environment` block (map or `KEY=value` list) as key -> digest."""
    out: dict[str, str] = {}
    if isinstance(block, dict):
        for key, value in block.items():
            out[str(key)] = _digest(value)
    elif isinstance(block, list):
        for entry in block:
            if not isinstance(entry, str):
                continue
            key, sep, value = entry.partition("=")
            out[key.strip()] = _digest(value if sep else None)
    return out


_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def parse_env_file(text: str) -> ReleaseSnapshot:
    """`KEY=value` lines; comments and blanks ignored. Shared configuration - applies to
    every service, because the demo stack's `.env` does."""
    snapshot = ReleaseSnapshot()
    for line in text.splitlines():
        match = _ENV_LINE.match(line)
        if match:
            snapshot.config[match.group(1)] = _digest(match.group(2).strip())
    return snapshot


def _load_yaml_documents(text: str) -> list[Any]:
    try:
        return [doc for doc in yaml.safe_load_all(text) if doc is not None]
    except yaml.YAMLError:
        # A manifest that does not parse at one ref is a manifest with no structure to
        # report; the compare's file list still shows it changed.
        return []


def parse_compose(text: str, service: str) -> ReleaseSnapshot:
    """The `services.<service>` block: image, environment keys, and everything else as
    manifest paths prefixed `services.<service>.`."""
    snapshot = ReleaseSnapshot()
    documents = _load_yaml_documents(text)
    root = documents[0] if documents and isinstance(documents[0], dict) else {}
    services = root.get("services")
    block = services.get(service) if isinstance(services, dict) else None
    if not isinstance(block, dict):
        return snapshot
    image = block.get("image")
    if isinstance(image, str) and image.strip():
        snapshot.images[service] = image.strip()
    snapshot.config.update(_env_keys_of(block.get("environment")))
    rest = {k: v for k, v in block.items() if k not in ("image", "environment")}
    for path, digest in _flatten(rest, f"services.{service}"):
        snapshot.manifest[path] = digest
    return snapshot


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _names_service(doc: dict[str, Any], service: str) -> bool:
    metadata = _dict(doc.get("metadata"))
    if metadata.get("name") == service:
        return True
    labels = _dict(metadata.get("labels"))
    if labels.get("app") == service or labels.get("app.kubernetes.io/name") == service:
        return True
    template = _dict(_dict(doc.get("spec")).get("template"))
    return _dict(_dict(template.get("metadata")).get("labels")).get("app") == service


def _pod_spec_of(doc: dict[str, Any]) -> dict[str, Any] | None:
    spec = doc.get("spec")
    if not isinstance(spec, dict):
        return None
    if isinstance(spec.get("containers"), list):
        return spec  # a bare Pod
    template = spec.get("template")
    if isinstance(template, dict) and isinstance(template.get("spec"), dict):
        inner: dict[str, Any] = template["spec"]
        if isinstance(inner.get("containers"), list):
            return inner
    job = spec.get("jobTemplate")  # CronJob
    if isinstance(job, dict):
        return _pod_spec_of(job)
    return None


def parse_manifest(text: str, service: str) -> ReleaseSnapshot:
    """Every Kubernetes document naming the service: container images by container name,
    `env[].name` and ConfigMap `data` keys as config keys, the rest as manifest paths."""
    snapshot = ReleaseSnapshot()
    for doc in _load_yaml_documents(text):
        if not isinstance(doc, dict) or not _names_service(doc, service):
            continue
        kind = str(doc.get("kind") or "")
        prefix = "" if kind in _WORKLOAD_KINDS else kind.lower()
        stripped: dict[str, Any] = {k: v for k, v in doc.items() if k not in ("metadata",)}
        if kind == "ConfigMap" and isinstance(doc.get("data"), dict):
            for key, value in doc["data"].items():
                snapshot.config[str(key)] = _digest(value)
            stripped.pop("data", None)
        pod = _pod_spec_of(doc)
        if pod is not None:
            for container in pod.get("containers") or []:
                if not isinstance(container, dict):
                    continue
                name = str(container.get("name") or service)
                image = container.get("image")
                if isinstance(image, str) and image.strip():
                    snapshot.images[name] = image.strip()
                for env in container.get("env") or []:
                    if isinstance(env, dict) and isinstance(env.get("name"), str):
                        snapshot.config[env["name"]] = _digest(env.get("value"))
            # Images and env are reported in their own sections; strip them from the
            # manifest paths so one change is one finding.
            stripped = _without_container_identity(stripped)
        for path, digest in _flatten(stripped, prefix):
            snapshot.manifest[path] = digest
    return snapshot


def _without_container_identity(doc: Any) -> Any:
    if isinstance(doc, dict):
        out = {}
        for key, value in doc.items():
            if key == "containers" and isinstance(value, list):
                out[key] = [
                    {k: v for k, v in c.items() if k not in ("image", "env")}
                    if isinstance(c, dict)
                    else c
                    for c in value
                ]
            else:
                out[key] = _without_container_identity(value)
        return out
    if isinstance(doc, list):
        return [_without_container_identity(item) for item in doc]
    return doc


def parse_release_file(path: str, text: str | None, service: str) -> ReleaseSnapshot:
    """Dispatch on the file's role. ``None`` text (the file does not exist at this ref)
    is an empty snapshot, so an added or removed manifest diffs naturally."""
    if text is None:
        return ReleaseSnapshot()
    kind = classify_path(path)
    if kind == "compose":
        return parse_compose(text, service)
    if kind == "env":
        return parse_env_file(text)
    if kind == "manifest":
        return parse_manifest(text, service)
    return ReleaseSnapshot()


def snapshot_release(files: Iterable[tuple[str, str | None]], service: str) -> ReleaseSnapshot:
    """Fold every release file's contribution at one ref into one snapshot."""
    total = ReleaseSnapshot()
    for path, text in files:
        total.merge(parse_release_file(path, text, service))
    return total


# ---------------------------------------------------------------------------------- diff

_DIGEST_SUFFIX = re.compile(r"@(sha256:[0-9a-f]{64})$")


def split_digest(image: str) -> tuple[str, str | None]:
    """``repo:tag@sha256:...`` -> (``repo:tag``, ``sha256:...``); no digest -> (image, None)."""
    match = _DIGEST_SUFFIX.search(image)
    if not match:
        return image, None
    return image[: match.start()], match.group(1)


def diff_snapshots(before: ReleaseSnapshot, after: ReleaseSnapshot) -> dict[str, Any]:
    """The contract sec 7.3 sections, keys and paths only."""
    added = sorted(set(after.config) - set(before.config))
    removed = sorted(set(before.config) - set(after.config))
    changed = sorted(
        key
        for key in set(before.config) & set(after.config)
        if before.config[key] != after.config[key]
    )
    image_changes: list[dict[str, Any]] = []
    for container in sorted(set(before.images) | set(after.images)):
        old, new = before.images.get(container), after.images.get(container)
        if old == new or new is None:
            # A container that disappeared is a manifest change (its path is gone), not an
            # image change: `to_image` is required and there is nothing to put in it.
            continue
        from_image, from_digest = split_digest(old) if old else (None, None)
        to_image, to_digest = split_digest(new)
        image_changes.append(
            {
                "container": container,
                "from_image": from_image,
                "to_image": to_image,
                "from_digest": from_digest,
                "to_digest": to_digest,
            }
        )
    manifest_changes = sorted(
        path
        for path in set(before.manifest) | set(after.manifest)
        if before.manifest.get(path) != after.manifest.get(path)
    )
    return {
        "config_keys_added": added,
        "config_keys_removed": removed,
        "config_keys_changed": changed,
        "image_changes": image_changes,
        "manifest_changes": manifest_changes,
    }
