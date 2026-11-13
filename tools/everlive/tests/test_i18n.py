#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Tests for the lightweight Console Hub translation layer."""

from __future__ import annotations

import json
import os
import string
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import i18n  # noqa: E402


class TranslationTests(unittest.TestCase):
    def tearDown(self) -> None:
        i18n.set_language("en")

    def test_english_is_source(self) -> None:
        i18n.set_language("en")
        self.assertEqual(i18n.tr("QUICK CONTROLS"), "QUICK CONTROLS")

    def test_german_catalog(self) -> None:
        i18n.set_language("de")
        self.assertEqual(i18n.tr("QUICK CONTROLS"), "SCHNELLSTEUERUNG")
        self.assertEqual(i18n.tr("Cancel"), "Abbrechen")

    def test_missing_key_falls_back(self) -> None:
        i18n.set_language("de")
        self.assertEqual(
            i18n.tr("This key is intentionally absent"),
            "This key is intentionally absent")

    def test_pseudo_locale(self) -> None:
        i18n.set_language("en-XA")
        self.assertEqual(i18n.tr("Fullscreen"), "⟦Fullscreen⟧")

    def test_unknown_language_falls_back(self) -> None:
        self.assertEqual(i18n.set_language("xx"), "en")
        self.assertEqual(i18n.tr("Fullscreen"), "Fullscreen")

    def test_template_substitution(self) -> None:
        i18n.set_language("de")
        self.assertEqual(
            i18n.trf("Flow {flow}%", flow=75), "Flow 75 %")

    def test_catalog_placeholders_match_source(self) -> None:
        path = os.path.join(i18n.LOCALE_DIR, "de.json")
        with open(path, encoding="utf-8") as handle:
            catalog = json.load(handle)
        self.assertGreater(len(catalog), 200)
        for source, translated in catalog.items():
            source_fields = {name for _, name, _, _ in
                             string.Formatter().parse(source) if name}
            translated_fields = {name for _, name, _, _ in
                                 string.Formatter().parse(translated) if name}
            self.assertEqual(
                source_fields, translated_fields,
                f"placeholder mismatch for {source!r}")


if __name__ == "__main__":
    unittest.main()
