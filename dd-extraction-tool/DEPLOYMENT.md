# Deployment

How this pipeline actually got deployed, as of 23 September 2026. Read it as a description, not an aspiration: there is no server, container, scheduler or service anywhere in it.

## The environment

| | |
| --- | --- |
| Host | One macOS laptop. No shared or remote environment exists |
| Runtime | Python 3.9.6 in `dd-extraction-tool/.venv` |
| Install | `pip install -e ".[dev]"`, an **editable** install: the commands run the source in the working tree, so a `git pull` changes behaviour immediately, with no build or restart |
| Commands | `dd-identify` and `dd-extract`, console scripts from `pyproject.toml`, hard-wired to the venv's interpreter |
| Package version | `0.1.0`, static. It is not bumped per change, so it does not identify what is running |
| Model runtime | Ollama Cloud (`https://ollama.com`), model `gpt-oss:20b`. Nothing runs locally. `dd-identify --provider anthropic` exists but has never been run live |

## How code got here

1. Work on `dev`, with `pytest` (103 tests) run before each commit.
2. Push `dev`, then a pull request into `main`. PRs #5 to #9 merged on 23 September 2026.
3. **Merging deploys nothing.** The deployed artefact is the checkout the venv points at. An installation updates when someone runs `git pull` in it; `pip install -e .` is only needed when dependencies or entry points change.

There is no staging environment. The closest thing to a pre-deploy gate is the golden suite in `eval/`, run by hand: 13/16 cases on every field at the last run.

## Rollback

`git revert`, then pull. Verified end to end on the MXN currency change: deployed as `bcdee42`, reverted as `649b979`, after which the behaviour probe, the command's help text, the test count and the file bytes all matched the pre-change state.

Revert rather than `reset` plus force-push, because the commit was already pushed and history is shared. There is no artefact registry to roll back, and nothing at the provider changes.

**A revert restores code, not outputs.** Reports written while a change was live keep whatever that version produced, and a rerun overwrites the report at the same path. Anything produced during a bad window has to be rerun deliberately.

## Known gaps

- **Nothing records which code produced a report.** The run log stores the model and timestamps but no git SHA or version, so a report cannot be tied to a commit. Recording the SHA in the run log is the cheapest fix.
- **No CI.** Tests and the golden suite run only when someone remembers.
- **No environment separation.** The same checkout serves development and real use.
