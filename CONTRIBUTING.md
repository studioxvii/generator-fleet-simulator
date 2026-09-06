# Contributing

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --require-hashes -r requirements-dev.lock
python3 -m pip install --no-build-isolation --no-deps -e .
```

## Development Workflow

1. Create a branch for your change.
2. Keep changes focused and include tests for behavior changes.
3. Run `python3 -m pytest` before opening a pull request.
4. Update `README.md` or `MODBUS_REFERENCE.md` if you change user-visible behavior or protocol details.

## Repository workflow

This public repository is the active codebase. Keep internal customer material,
private audit evidence, and credentials outside this repository.

- Use a focused branch and pull request for each change.
- Merge only after required CI checks pass.
- The maintainer reviews the diff and merges using squash merge.
- Use Tom Hammond's configured author identity for maintainer commits. Do not
  add AI co-author trailers. Preserve accurate attribution for outside contributors.
- Keep releases marked as previews until platform acceptance is complete.

## Project Conventions

- Default runtime configuration should stay safe for local use.
- Changes to command handling or register mapping should include tests.
- Avoid breaking the Modbus register contract without documenting the change clearly.
- Treat `requirements.txt` and `requirements-dev.txt` as abstract inputs. When
  either changes, regenerate both hashed lock files with the commands in
  `README.md`, review the resolved dependency diff, and run the full suite.
- Keep `VERSION`, Python package metadata, Compose, launcher fallbacks, release
  notes, and Community Edition download copy synchronized.
- Follow `RELEASE_PROCESS.md` for tags, anonymous image-pull proof, SBOM,
  signature, receipt, and bundle publication.
- Generated artifacts should not be committed unless explicitly approved. Examples include `build/`, `dist/`, `*.egg-info/`, `.pytest_cache/`, `__pycache__/`, `.pycache-verify/`, `.venv/`, `generator_state.json`, `generator_state.json.bak`, and duplicate local files such as `requirements-dev 2.txt` or `setup 2.py`.

## Pull Requests

- Describe the behavior change, not only the code change.
- Call out any simulator-model assumptions you changed.
- Include screenshots or short notes if you changed the dashboard UX.
- Link relevant public issues and test results.
- Prefer squash merge for change branches.
- Refresh from `main` after any PR merges before continuing related work.

## License of contributions

Submit contributions under the MIT License in `LICENSE`. Only submit material
that you have the right to license. Preserve third-party notices and identify
any added dependency or external asset and its license in the pull request.
