# Evaluations

`evaluation.xml` holds 10 read-only, independent, verifiable Q&A pairs, per
the mcp-builder skill's Phase 4 methodology (`reference/evaluation.md`).

## How this adapts to a local-file MCP server

The methodology assumes a live external service with existing data an
agent can explore purely with read-only calls (Slack messages, GitHub
issues, ...). This server's "data" is the user's own local files, so
there's no persistent corpus to explore out of the box, and the two
tools that create data (`audiobook_start_conversion`,
`audiobook_continue_conversion`/`run_until_done`) are explicitly not
read-only.

The adaptation: a one-time setup script produces a completed job on disk
(analogous to "an account that already has data in it"), and the 10
questions are answerable using *only* read-only tools
(`audiobook_inspect_epub`, `audiobook_get_status`,
`audiobook_list_conversions`, `audiobook_inspect_chapter_boundary`,
`audiobook_verify_output`) against that pre-existing state -- never
`start_conversion`/`continue_conversion`/`run_until_done`/
`patch_chapter_boundary`/`cancel_conversion`.

One honest limitation versus the methodology as written for a live
service with a large corpus: this domain doesn't support genuinely
"dozens of tool calls" or extensive result-paging per question, since a
single small book is inherently a small amount of data. Each question
here takes 1-3 tool calls (e.g. list the job to get its id, then read its
status; or read status to get a file path, then verify that file) rather
than dozens. All ten answers below were captured by calling the actual
tool implementations against this fixture, not hand-computed.

## Reproducing the fixture

```bash
python3 evaluations/setup_eval_fixture.py
```

This builds `/tmp/audiobook_mcp_eval/source/fixture_book.{epub,mp3}`,
runs the real `Pipeline` (the same code `server.py`'s tools call) to
completion, and registers the job -- so afterward,
`audiobook_list_conversions(out_dir="/tmp/audiobook_mcp_eval/out")` finds
job id `fixture_book`, matching `evaluation.xml`'s expected answers. If
`~/.audiobook_mcp/registry.json` already has an entry for these exact
paths, re-running just confirms/resumes it rather than duplicating it;
delete that registry file first for a fully clean run.

## Running the evaluation

With the fixture in place and the server registered with an MCP client
(see the top-level README), use `mcp-builder`'s `scripts/evaluation.py`
against `evaluation.xml` the same way you would for any other server
built with that skill -- point it at this server and this file, and it
will drive a fresh agent through all 10 questions with access only to
this server's tools.

## Why the metadata-lookup tools aren't in evaluation.xml

`audiobook_lookup_book_metadata` queries live third-party APIs
(ratings, rating counts, and occasionally publisher/description text do
change over time for a real book). The evaluation guide is explicit that
question answers must not depend on dynamic "current state" data (see
`reference/evaluation.md`, requirement 12) -- so questions built around
its output wouldn't be stable in the way this methodology requires.
`audiobook_fetch_cover_image`/`audiobook_embed_cover_metadata` are
covered instead by `tests/test_tool_integration.py`, which exercises the
real tool functions end-to-end against a local `file://` URL.
