---
name: rust-workflow
description: Use when explicitly asked for an empty Cargo workspace through Signum Rust, for Signum Rust setup or its starter, or for Rust contracts in a project with its own Signum Rust mise task. Ordinary Rust projects use their own workflow.
---

# Rust workflow

For an existing project inspect its Cargo workspace, lockfile, mise tasks, target and current CI. Keep its versions and preferences. A project-owned executable at `mise-tasks/signum-rust` selects this profile. Read the local copy before using it. If a task of that name is already declared in mise configuration, resolve which project task will run before changing anything. A request to research a stack can be answered without attaching it.

On a request to attach the profile, run `python3 -B <this-skill>/scripts/setup.py attach PROJECT --kind cli|backend|web` with one kind at the actual workspace root. For a requested starter use `new` at a fresh path or in a project containing only its Signum onboarding files and optional Git metadata. A new starter includes a working example, public-interface test and Cargo.lock. For an existing Cargo project, attachment adds only the executable. Initializing Signum and scaffolding a Rust app are separate choices. The owner can change the task alongside the code. Use the project's toolchain pin; discuss a new dependency or toolchain installation separately when needed.

When the owner explicitly requests an empty Cargo workspace, run `python3 -B <this-skill>/scripts/setup.py workspace PROJECT`. This works at a new path or an existing directory with local files. It creates only a virtual `Cargo.toml` with resolver 3 and an empty member list, then verifies with Cargo metadata. The mode is repeatable and rejects an existing manifest with different contents or a new workspace inside an existing Cargo project. A general request to start work or initialize Signum does not select this mode.

Once the first package is agreed, create it from the workspace root, for example with `cargo new --lib --vcs none crates/<name>` for a library, and confirm Cargo registered it with `cargo metadata --no-deps --format-version 1`. Generate and keep the project's lockfile when its package workflow needs one. When the owner chooses a CLI, backend or web profile for the workspace, use `attach` at the root with that kind.

## Workspace and functional boundaries

Start each new Rust project as a Cargo workspace. The application package wires the application together. Place each cohesive business capability in a library package under `crates/<capability>`, with an explicit path dependency from its callers. Keep private helpers and provider-specific code inside the capability. A starter can contain just the root package until the first business capability appears. Attach to an existing layout as found; agree on any restructuring separately.

For a change, identify the owning crate and its caller-visible behavior: inputs, results, errors and relevant guarantees. Keep an internal change scoped to the owning crate's files and run `mise run signum-rust test -p PACKAGE` for native tests. If the public contract changes, inspect workspace consumers with `cargo tree --workspace --locked -i PACKAGE`. Include affected callers and relevant manifests or lockfile in the Signum contract before editing, and run the consumers' behavioral tests for the relevant features and targets. For the browser starter use `surface` to exercise its WASM consumer. Type-compatible changes can still alter meaning. [Workspace example](references/stack.md#workspace-boundaries)

## Select a check

In a substantive change, agree on the intended outcome, file scope, one observable success and a relevant counterexample through Signum. Select the smallest checks that establish them. Adapt names and package selection to an existing workspace. Proceed with the normal Signum start, source snapshot, verify, evidence and owner review. A command without the required observation leaves the criterion open. Use `mise run signum-rust LANE` from the project root.

| Contract need | Lane and evidence |
| --- | --- |
| Routine local feedback | `fast` checks formatting, Clippy and workspace tests. For web it checks WASM Clippy and executes the browser contract. |
| Build result | `build` creates native or WASM release artifacts and prints their directory. Web packaging follows the project's frontend setup. |
| Observable interface | `surface` runs the selected integration target — CLI process, HTTP router or browser. The supplied targets are cli_contract, http_contract and browser_contract. |
| Domain invariant | `property` or `property --test NAME` runs a real integration test. Use an independent oracle, examples at the boundary, and a meaningful input distribution. Define the test in the project when needed. Cargo takes `PROPTEST_CASES` from the environment unless the test sets its own count. |
| Sensitivity of tests to code changes | `mutants --file src/lib.rs` or `mutants --diff /path/to/change.diff`. One job, explicit scope. Review caught, missed, timeout and unviable counts separately. |
| Critical pure rule with explicit pre/postconditions | `proof path/to/module.rs`. Provide a compatible Verus and solver. Check that the verified module is the one the shipped code uses before crediting the product with a proof. |

Run in a small working copy with a committed lockfile. Compilation and mutation outputs go outside the copy. Optional `SIGNUM_RUST_CACHE` takes a dedicated absolute cache directory owned by the user; otherwise the task reuses a workspace-specific OS temporary cache. Release builds and mutation campaigns print their retained output locations. Record the smallest failing input or seed as a contract example when a generated case fails. Keep incidental tools and caches out of the contract and everyday chat context.

Read [Stack choices](references/stack.md) for versions, web environment, tool selection and proof boundaries.
