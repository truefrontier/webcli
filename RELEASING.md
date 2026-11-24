# Releasing

There is no release script, Makefile, or GitHub Actions workflow in this repo.
`BUILD_SPEC.md` is the Phase 1 build spec. `CHANGELOG.md` is the version history.
The only release markers are git tags named `v<webcli version>` (`v0.4.0` through
`v0.5.2` are annotated; `v0.6.0` is a lightweight tag on that version's commit).
Nothing has been tagged since `v0.6.0`. There are no GitHub Releases.

This file is the manual publish for **truefrontier-webcli 0.17.2**, then the
**llmcli 0.7.2** alias. It does not publish anything by itself.

Publish from a terminal on the merge commit of this change, after it is on `main`.
`pipx` downloads `build` and `twine` into its own cache for the length of each
command. No `pip install`, no `~/.pypirc`, and no `TWINE_USERNAME` /
`TWINE_PASSWORD` / `TWINE_API_TOKEN`.

`pipx` 1.18 (current on PyPI as of 2026-10-30) accepts `pipx run build` and runs
that package's only script, `pyproject-build`, printing
`NOTE: running app 'pyproject-build' from 'build'`. An older pipx that rejects
`build` can use `pipx run --spec build pyproject-build` in the same places.
`pipx run twine` matches the `twine` script directly.

These commands were checked with build 1.6.1, twine 7.0.0, and hatchling 1.33.0
(PyPI, 2026-10-30). `pipx run` itself tracks the current releases of those tools.

## Blocker before uploading `llmcli`

`https://pypi.org/pypi/llmcli/json` and `https://pypi.org/simple/llmcli/` both
return 404 (checked 2026-10-30). That does not mean PyPI will accept the name.

Warehouse still rejects a new project whose `ultranormalize_name` matches an
existing project: strip `.`, `_`, and `-`, map `l`/`L`/`i`/`I` to `1`, map
`o`/`O` to `0`, then lowercase
([warehouse migration d18d443f89f0](https://github.com/pypi/warehouse/blob/main/warehouse/migrations/versions/d18d443f89f0_ultranormalize_name_function.py)).
`llmcli` and the live project [`lim-cli` 21.2.7](https://pypi.org/project/lim-cli/)
("LiminalInfo command line app") both become `11mc11`.

On 2026-10-19 the upload of this name was rejected as too similar to an existing
project (commit `5bd7b20`). Expect the same HTTP 400 from `twine upload`:

```text
The name 'llmcli' is too similar to an existing project.
```

A pending trusted publisher does not reserve `llmcli` and does not skip that
check. The first publish creates the project, and that is when the check runs.
Asking PyPI to allow the name is a PEP 541 request:
<https://pypi.org/help/#project-name>. `truefrontier-webcli` 0.17.2 can still
be uploaded. If the `llmcli` upload is rejected, leave the alias unpublished.

[`large-language-model-cli` 0.10](https://pypi.org/project/large-language-model-cli/)
documents a command named `llmcli`. Its project name does not ultranormalize to
`11mc11`.

## Order

`llmcli` 0.7.2 and `truefrontier-llmcli` 0.7.2 both declare
`Requires-Dist: truefrontier-webcli==0.17.2` (exact `==`). PyPI does not resolve
that pin at upload time. `pip install llmcli` fails until `truefrontier-webcli`
0.17.2 is actually on the index. Upload webcli first, confirm the project page,
then upload the alias.

PyPI today:

| Project | On PyPI | In this repo |
|---|---|---|
| `truefrontier-webcli` | 0.4.0, 0.4.1, 0.5.0, 0.5.1, **0.5.2** | **0.17.2** |
| `llmcli` | no project | **0.7.2**, pin `truefrontier-webcli==0.17.2` |
| `truefrontier-llmcli` | 0.4.0, 0.4.1, **0.5.0** (pin `==0.5.0`) | **0.7.2**, pin `==0.17.2` (version left as-is) |

Versions between 0.5.2 and 0.17.2 were never published. Uploading 0.17.2 is a
normal version jump. The same is true for an optional `truefrontier-llmcli`
0.7.2 upload (0.5.1 through 0.7.1 were never published).

## Token

Create one at <https://pypi.org/manage/account/token/>.

- Scope: **Entire account**. A token scoped only to `truefrontier-webcli` can
  upload 0.17.2 and cannot create the new `llmcli` project.
- Username: `__token__` (passed on the command; twine 7 also defaults to it for
  pypi.org).
- At the prompt `Enter your API token:`, paste the token, including the `pypi-`
  prefix. The prompt hides it. Twine 7 may first log
  `This environment is not supported for trusted publishing`. On a laptop that
  line is expected; the token prompt follows.
- Revoke the token at the same page when the uploads are done.

Run the uploads in a local terminal. If twine does not prompt and PyPI answers
403, macOS Keychain already has a credential for `https://upload.pypi.org/legacy/`
and twine used that. Delete that Keychain item and run the upload again.

## 1. truefrontier-webcli 0.17.2

From the repo root:

```sh
rm -rf dist
pipx run build
pipx run twine check dist/truefrontier_webcli-0.17.2.tar.gz dist/truefrontier_webcli-0.17.2-py3-none-any.whl
pipx run twine upload --username __token__ dist/truefrontier_webcli-0.17.2.tar.gz dist/truefrontier_webcli-0.17.2-py3-none-any.whl
```

Confirm <https://pypi.org/project/truefrontier-webcli/> shows 0.17.2 before
continuing. The wheel's console scripts are `webcli`, `llmcli`, `chromecli`,
and `chromecli-host`. It contains the Python package `webcli/chromecli/`.
The MV3 extension in `extensions/chromecli/` is not in the wheel (Load unpacked;
`docs/chromecli.md`).

## 2. llmcli 0.7.2

```sh
rm -rf packaging/llmcli-bare/dist
pipx run build packaging/llmcli-bare
pipx run twine check packaging/llmcli-bare/dist/llmcli-0.7.2.tar.gz packaging/llmcli-bare/dist/llmcli-0.7.2-py3-none-any.whl
pipx run twine upload --username __token__ packaging/llmcli-bare/dist/llmcli-0.7.2.tar.gz packaging/llmcli-bare/dist/llmcli-0.7.2-py3-none-any.whl
```

The wheel is an empty hatchling alias (`bypass-selection`) whose only install
requirement is `truefrontier-webcli==0.17.2`.

## 3. Optional: truefrontier-llmcli 0.7.2

Same pin, so this also waits on step 1. The repo version is already 0.7.2.
PyPI's latest is 0.5.0. Skip this step to leave the published companion at 0.5.0.

```sh
rm -rf packaging/llmcli/dist
pipx run build packaging/llmcli
pipx run twine check packaging/llmcli/dist/truefrontier_llmcli-0.7.2.tar.gz packaging/llmcli/dist/truefrontier_llmcli-0.7.2-py3-none-any.whl
pipx run twine upload --username __token__ packaging/llmcli/dist/truefrontier_llmcli-0.7.2.tar.gz packaging/llmcli/dist/truefrontier_llmcli-0.7.2-py3-none-any.whl
```

`truefrontier-chromecli` is a separate alias and is not part of this release
(`packaging/chromecli/README.md` says it is not published).

## Tag

Tags are not part of the pull request. Create the tag on `main` after the
merge, on the commit that was uploaded. Follow the existing annotated-tag
convention (`v` plus the webcli version). There is no separate llmcli tag.

```sh
git checkout main
git pull origin main
git tag -a v0.17.2 -m "webcli 0.17.2 / llmcli 0.7.2"
git push origin v0.17.2
```

Push that tag only after the manual uploads above, and only once
`.github/workflows/` still has no publish workflow. With the workflow from the
next section installed, the tag push is the publish, and a second `twine upload`
of the same files is rejected.

No intermediate tags (`v0.6.1` through `v0.17.1`) are required.
`packaging/homebrew/webcli.rb` still points at the `truefrontier-webcli` 0.5.2
sdist. Update that formula's URL and sha256 in a later change, after 0.17.2
is on PyPI.

## Optional: trusted publishing

Not installed. GitHub runs workflows only from `.github/workflows/`, and this
repo has no `.github` directory. The workflow file to add later is
[`docs/pypi-publish.yml`](docs/pypi-publish.yml). Copy it to
`.github/workflows/pypi-publish.yml` in a follow-up PR. Leave it out of the
commit you tag for the manual upload above.

It publishes on a `v*` tag. Each package has its own build job and its own
publish job. PyPI's current guide treats building inside the publish job as
unsupported, and the publish action's OIDC token stays on the publish job only.
`publish-llmcli` waits until `publish-webcli` has succeeded, then uploads the
alias. `truefrontier-llmcli` is commented out.

Action pins checked on 2026-10-30: `actions/checkout` v7.0.1,
`actions/setup-python` v7.1.0, `actions/upload-artifact` v7.0.2,
`actions/download-artifact` v8.0.2, `pypa/gh-action-pypi-publish` v1.14.3,
and `build` 1.6.1. Pinned; leave them until a deliberate bump.

### One-time setup on pypi.org

Do this once, before the first tag push that contains the workflow. The
workflow filename registered below is `pypi-publish.yml` (the file name only).
The GitHub environment name is `pypi` on every job. The PyPI form's environment
field has to be that same string.

1. On GitHub, open `truefrontier/webcli` → Settings → Environments → New
   environment. Name it `pypi`. Turn on required reviewers and add the people
   who may approve a publish. Create this environment before the first tag
   push. GitHub will otherwise create `pypi` on first use with no reviewer.
2. Existing project `truefrontier-webcli`:
   <https://pypi.org/manage/project/truefrontier-webcli/settings/publishing/>
   → Add a GitHub Actions publisher:
   - Owner: `truefrontier`
   - Repository name: `webcli`
   - Workflow name: `pypi-publish.yml`
   - Environment name: `pypi`
3. New project `llmcli` (the project page does not exist yet). Account sidebar
   → Publishing, <https://pypi.org/manage/account/publishing/>, add a **pending**
   GitHub Actions publisher:
   - PyPI project name: `llmcli`
   - Owner: `truefrontier`
   - Repository name: `webcli`
   - Workflow name: `pypi-publish.yml`
   - Environment name: `pypi`

   A pending publisher does not create or reserve the project. The name check
   in the blocker section still runs on first use. If someone else registers
   `llmcli` first, this pending publisher is invalidated.
4. Optional, and only if the `truefrontier-llmcli` jobs in the workflow are
   uncommented. That project already exists, so this is a normal publisher, not
   a pending one:
   <https://pypi.org/manage/project/truefrontier-llmcli/settings/publishing/>
   with the same owner, repository, workflow name, and environment name.

Then merge the follow-up that adds `.github/workflows/pypi-publish.yml` and
push an annotated `v*` tag. Approve the `pypi` environment when GitHub asks.
The webcli job mints a short-lived token for `truefrontier-webcli`. The llmcli
job runs only after that job succeeds and mints a token for `llmcli`, which
creates the project from the pending publisher.
