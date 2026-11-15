"""Prompts as versioned artifacts (requirement L2).

prompts/<version>/<task>.txt holds a template with "### system" and
"### user" sections. The version recorded with every response is
"<version>@<sha256 of the template files>", so editing a template without
bumping the version is still visible in the trace.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os

from ..common.config import get_settings

TASKS = ("plan", "derive_queries", "draft")


@functools.lru_cache(maxsize=16)
def _load(version: str) -> tuple[dict, str]:
    base = os.path.join(get_settings().prompts_dir, version)
    if not os.path.isdir(base):
        raise ValueError(f"unknown prompt version {version!r}")
    templates, h = {}, hashlib.sha256()
    for task in TASKS:
        with open(os.path.join(base, f"{task}.txt"), encoding="utf-8") as fh:
            raw = fh.read()
        h.update(raw.encode())
        sys_part, user_part = raw.split("### user", 1)
        templates[task] = (sys_part.replace("### system", "", 1).strip(), user_part.strip())
    return templates, h.hexdigest()[:10]


def available_versions() -> list[str]:
    base = get_settings().prompts_dir
    return sorted(d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d)))


def version_tag(version: str) -> str:
    return f"{version}@{_load(version)[1]}"


def render(version: str, task: str, payload: dict, **params) -> list[dict]:
    templates, _ = _load(version)
    system, user = templates[task]
    for k, v in params.items():
        system = system.replace("{" + k + "}", str(v))
    user = user.replace("{input_json}", json.dumps(payload, ensure_ascii=False, indent=1))
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
