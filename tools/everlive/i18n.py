#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Small source-string translation layer for the Console Hub.

English is the source language and the default.  Catalogs are JSON objects
keyed by the exact English string; missing keys fall back to English.  This
keeps the Qt code free of compiled ``.qm`` tooling while still allowing
contributors to add a language by dropping in one JSON file.
"""

from __future__ import annotations

import json
import os
from typing import Dict, Iterable, Tuple


DEFAULT_LANGUAGE = "en"
LANGUAGES: "Dict[str, str]" = {
    "en": "English",
    "de": "Deutsch",
    "en-XA": "Pseudo (layout test)",
}
LOCALE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locale")

_current = DEFAULT_LANGUAGE
_catalog: Dict[str, str] = {}


def available_languages() -> Iterable[Tuple[str, str]]:
    return tuple(LANGUAGES.items())


def current_language() -> str:
    return _current


def _pseudo(text: str) -> str:
    return f"⟦{text}⟧"


def _load_catalog(code: str) -> Dict[str, str]:
    if code == "en":
        return {}
    if code == "en-XA":
        return {}
    path = os.path.join(LOCALE_DIR, f"{code}.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items()}


def set_language(code: str) -> str:
    """Select a language and load its catalog; fall back to English."""
    global _current, _catalog
    code = (code or DEFAULT_LANGUAGE).strip()
    if code not in LANGUAGES:
        code = DEFAULT_LANGUAGE
    _current = code
    _catalog = _load_catalog(code)
    return _current


def tr(text: str) -> str:
    if _current == "en-XA":
        return _pseudo(text)
    return _catalog.get(text, text)


def trf(template: str, **values) -> str:
    """Translate a template, then substitute named placeholders."""
    return tr(template).format(**values)
