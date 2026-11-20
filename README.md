![SIGNUM — ink traces converge on a single mark printed on a paper strip.](assets/signum.gif)

# Signum

Signum keeps project decisions in the project. Agree on a rule in one Codex chat, then open another in the same folder. You can ask what you decided, why you chose it and what work it covers.

For a substantial change, Signum carries that decision into a contract with a file boundary and a check. You review the result and choose whether to accept it. A small edit can stay a small edit.

## A decision worth keeping

Suppose an import contains two rows with the same label. Should both survive? The answer affects the data model, and a passing test could just as easily preserve the wrong rule.

| In the chat | In the project |
| --- | --- |
| “Different IDs can share a label. Skip a repeat of the same ID.” | Signum shows the rule for confirmation. |
| “I agree.” | The project journal keeps that version and its source. |
| In a fresh chat: “How did we decide to handle duplicates?” | Codex can read the agreed rule from the same directory. |
| “Apply that rule to the importer.” | Review the affected files and check before work starts. After the check, accept the result or return it with a reason. |

## Try it in one project

From a checkout of this repository, add the marketplace and install the plugin:

```sh
codex plugin marketplace add .
codex plugin add signum@heurema
```

Open a fresh Codex chat in the directory you want to use. Say **“Set up Signum for this project.”** This creates the local journal and adds a short project instruction when one does not already exist. Repeating setup preserves existing records. Each project opts in separately.

The core runs on POSIX with Python 3.9 or newer and uses the standard library. From the Signum checkout, inspect another project directly:

```sh
python3 -B plugins/signum/skills/project-intent/scripts/signum.py --project /path/to/project status
```

## What travels with the project

- `.signum/state.json` holds decisions, proposals and their history.
- `.signum/delivery.json` records work starts, evidence and acceptance when used.
- A contract names the files it covers and where to put its check report. Keep the report and referenced files alongside the journal when moving a project.

The journal can contain your words and project paths. Keep it in the same trust boundary as the source. Checks run with the invoking process's permissions. [Working with decisions](plugins/signum/skills/project-intent/references/decisions.md) · [Verifying a delivery](plugins/signum/skills/project-intent/references/delivery.md)

The [backup utility](tools/project_backup.py) exports the paths you select and verifies the archive on restore. The [evidence checker](tools/check_signum.py) can recheck a recorded delivery later, for example when an attached report has gone missing. Both have a built-in `--help`.

## Optional Rust workflow

Install `signum-rust@heurema` from the same marketplace to create an empty Cargo workspace, attach checks to an existing Cargo project or choose a CLI, backend or web starter. The shipped workspaces use Cargo resolver 3 and the 2024 edition, which require Rust 1.85 or newer. Individual starters may require a newer toolchain for their locked dependencies. The empty-workspace option creates a single manifest. It leaves the choice of your first crate open. [Rust setup and checks](plugins/signum-rust/skills/rust-workflow/SKILL.md)

## Earlier Signum

V2 is the Codex-first redesign. The complete earlier codebase, including its Claude commands, proof gate and documentation, lives in [archive/v1](archive/v1/README.md). Its last tagged release was [v4.21.10](https://github.com/heurema/signum/tree/v4.21.10). The archive directory names the product generation. The Git history and release tags retain their original versions.

[Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [MIT license](LICENSE)
