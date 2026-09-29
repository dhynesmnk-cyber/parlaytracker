# Qwen reply fixtures: SYNTHETIC

These replies are **hand-written to the `ExtractedSlip` schema. They are not real model output.**
They exist so the parser, the resolver and the Screenshot page can be tested before a
`QWEN_API_KEY` exists. Phase 6's exit needs 10 real slips from at least two sportsbooks processed
with their real responses saved here; replace or add to these then, and note which are real.

| File | Shape |
|---|---|
| `single_valid.json` | a clean single, odds as integers |
| `sgp_valid.json` | a same-game parlay with three legs |
| `unicode_minus.json` | odds and lines as strings, with `−` (U+2212), as slips print them |
| `partial.json` | most fields null; one leg missing its market |
| `bad_fields.json` | fields of the wrong type: the rest of the slip must survive |
| `fenced.txt` | valid JSON wrapped in a markdown code fence, with chatter |
| `malformed.txt` | not JSON |
| `empty.json` | valid JSON with nothing read |
