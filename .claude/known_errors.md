# Known errors

## Config CSV parsing ate inner spaces (2026-09-27)
- **Error:** `.replace(" ", "")` before `split(",")` turned `bad name` into the valid `badname`, so invalid container names passed validation.
- **Fix:** split on `,` then `strip()` each item (`_csv()` in `bot/config.py`). Validate the item as written, never a normalized copy.

## discord.ui.View timeout never fires in tests (2026-09-27)
- **Error:** `view.wait()` hung forever with a fake `interaction.response.send_message`.
- **Cause:** discord.py starts the timeout task in `View._start_listening_from_store` (discord/ui/view.py ~L602), which only runs when a real send registers the view.
- **Fix:** test fakes call `view._start_listening_from_store(fake_store)` on send (`tests/helpers.py`). Production code needs nothing. Always run tests with `-o faulthandler_timeout=N` so a hang dumps a traceback instead of blocking.

## `\u200b` written as a literal invisible character (2026-09-27)
- **Error:** a `"\u200b"` escape typed into a file-writing tool call came out as a real zero-width space in the source. Tests still passed, but the code was unreadable.
- **Fix:** write non-printing characters as escapes through a script (`s.replace("\u200b", "\\u200b")`), then check with `grep -n 'u200b'` or `cat -v`.

## Method named `list` shadowed the builtin in annotations (2026-09-27)
- **Error:** `DockerService.list()` made `-> list[ContainerMemory]` inside the class refer to the method. It only "worked" because `from __future__ import annotations` keeps annotations as strings. `typing.get_type_hints` or mypy would break (mypy: "Function ... is not valid as a type").
- **Fix:** renamed it to `containers()`. Never name methods after builtins used in the same class's annotations. Run `mypy` (strict) as part of the test loop.

## `pip download --python-version` evaluated markers against the host interpreter (2026-09-27)
- **Error:** checking Linux cp312 wheels from a 3.14 venv tried to fetch `audioop-lts; python_version >= "3.13"`.
- **Fix:** run `pip download --platform ... --python-version 3.12` from a real 3.12 interpreter (`uv python install 3.12`), or resolve with `uv pip compile --python-version`.

## Fetched-page summary contradicted the source (2026-09-27)
- **Error:** a WebFetch summary of GitHub's runner docs said arm64 Linux runners are private-repo only. The docs source (`github/docs` `data/reusables/actions/supported-github-runners.md`) lists `ubuntu-24.04-arm` under "Standard ... runners for public repositories", which are free.
- **Fix:** before a summarized page drives a design decision, check the raw source (repo markdown, `action.yml` at the pinned SHA, package metadata).
