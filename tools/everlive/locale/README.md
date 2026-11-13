# Console Hub translations

The Console Hub uses English source strings as catalog keys.  A language is a
JSON object mapping the exact English string to the translated string:

```json
{
  "QUICK CONTROLS": "SCHNELLSTEUERUNG",
  "Flow {flow}%": "Flow {flow} %"
}
```

Missing keys fall back to English.  This keeps a partially updated catalog
safe to use while a translation is still in progress.

## Adding a language

1. Copy `de.json` to `<language-code>.json`.
2. Translate the values, not the keys.
3. Keep every `{placeholder}` exactly as it appears in the key.  `trf()`
   substitutes named values after translation.  Do not translate
   `{present:.1f}` and similar format specifiers.
4. Add the display name to `LANGUAGES` in `../i18n.py`:
   ```python
   LANGUAGES = {
       "en": "English",
       "de": "Deutsch",
       "fr": "Français",
   }
   ```
5. Run:
   ```bash
   python3 -m unittest discover -s tools/everlive/tests
   ```
   The catalog test checks that placeholders match the source keys.
6. Launch the Hub with the new language selected under
   **Advanced → System → Sprache** and check long strings at 100%, 125% and
   150% scale.

## Testing layout without a translation

Select **Pseudo (layout test)** (`en-XA`).  Every string is wrapped in
`⟦…⟧`, which makes truncation and cramped layouts visible without a real
translation.

## Adding a new string

Wrap visible text at the point of use:

```python
QLabel(tr("QUICK CONTROLS"))
QLabel(trf("Flow {flow}%", flow=value))
```

Use `trf` for templates with values.  Do not concatenate translated fragments
into a sentence; put the whole sentence in the template so translators can
reorder it.
