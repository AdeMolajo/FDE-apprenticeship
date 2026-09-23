# Security and permissions

What is exposed, to whom, and what bounds it. Current as of 23 September 2026.

## Attack surface

**Nothing inbound.** The tool is two command-line programs. There is no listener, no port, no API and no scheduled job. It runs only when someone types a command.

**Outbound:** HTTPS to `ollama.com` only, or to a local Ollama server if `OLLAMA_HOST` is set. `dd-identify --provider anthropic` would also reach `api.anthropic.com`.

## The data boundary that matters most

**The text of every page it reads is sent to Ollama Cloud.** On a real deal that is confidential client data leaving the machine, and it needs the client's and the firm's approval before this runs on anything real. Nothing in the tool enforces that.

The alternative is a local Ollama server via `OLLAMA_HOST`, where no page text leaves the machine. Step 2 has no Claude provider, so Ollama is currently the only route.

## Credentials

`OLLAMA_API_KEY` is read from the environment. The recommended store is the macOS Keychain, read at the point of use, so the key never appears in a file, a command line or shell history. A key grants full access to the Ollama account: there is no scoping or read-only variant, so rotation is the only control. One key was rotated during development after being exposed in a chat transcript.

`.env` files are ignored by git, and scans found no key committed.

## What the tool is allowed to touch

- **The data room is read-only.** It is opened for reading, symlinks are never followed, and nothing is written, moved or deleted there.
- **Writes are limited** to the `--out` report and the run log. `--out` is checked before any model call: it must be a file path, writable, and outside the data room, or the run refuses to start.
- **The model has no tools**, no filesystem access and no network of its own. Its only output is a validated JSON answer.

## What bounds a hostile document

- Page text is wrapped in a frame, and any lookalike of that frame's tag is escaped, so a document cannot close it (`framing.py`).
- The real instruction is restated after the page content.
- `source_document` and `source_page` are set by code, never by the model, so no document can change where a figure is attributed. Seven scripted attacks and three live attempts failed to move them.
- Pages containing frame lookalikes, and statement pages yielding no figures, are flagged in `review`.

## What the artefacts contain

Reports hold extracted client figures and are git-ignored. The run log holds paths, counts, durations, token counts and cost, **but no page content**.

## Bounds that do not exist

Anyone with the laptop and the key can run it: there is no authentication, authorisation or per-user audit beyond the run log's host field. There is no encryption beyond whatever the disk provides, and no retention or deletion policy for reports.
