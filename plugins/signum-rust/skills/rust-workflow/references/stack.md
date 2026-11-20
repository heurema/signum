# Stack choices

## Workspace boundaries

All three starters use a root package in an explicit Cargo workspace with a single lockfile. A workspace groups packages for building and testing. A library crate exposes the capability's public Rust API and can link into the same process. Keep the application as the composition point. A trait is useful when callers need interchangeable implementations. Private modules are sufficient for helpers that change together.

For example, a `company-reports` library could retrieve a report by company and period. Document what counts as the requested period, what a missing report means, and how an unavailable source is reported. Keep provider URLs, response formats and parsing private. A consumer can build a dossier from the returned report. If only the wire format changes, the consumer's existing example should still pass. If the period, public result or meaning of an error changes, check the consumer too.

From the workspace root, `cargo new --lib --vcs none crates/company-reports` registers the new member. `cargo add --path crates/company-reports -p signum-starter` wires it to the supplied application. Use the real application's package name in an existing project. Cargo updates the shared manifests and lockfile as needed; include those files when creating a capability. [Cargo workspaces](https://doc.rust-lang.org/cargo/reference/workspaces.html) · [Cargo package selection](https://doc.rust-lang.org/cargo/commands/cargo-test.html#package-selection) · [Rust visibility](https://doc.rust-lang.org/reference/visibility-and-privacy.html)

## Tools

Keep Cargo.lock and the project's Rust toolchain pin under version control. Where mise already manages Rust, use that pin. An existing rust-toolchain.toml can select a version and components for rustup instead. The baseline uses Cargo, rustfmt, Clippy, Python 3.9+ and mise file tasks. No background worker is required. [mise file tasks](https://mise.jdx.dev/tasks/file-tasks.html)

| Project or risk | Starting point | When to extend |
| --- | --- | --- |
| CLI | Clap derive, std::process from an integration test that launches the built binary | Add assert_cmd for more involved process and filesystem assertions. [Clap](https://docs.rs/clap/) · [assert_cmd](https://docs.rs/assert_cmd/) |
| Backend | Axum, Tokio, Tower; send a request through the real Router with ServiceExt::oneshot | Add a live socket or database fixture when the criterion concerns transport or persistence. [Axum testing example](https://github.com/tokio-rs/axum/tree/main/examples/testing) |
| Browser frontend | wasm-bindgen, web-sys, wasm-bindgen-test. The test runs in a real headless browser and checks the DOM. | For a reactive application, evaluate Leptos; for shared desktop/mobile/web UI, evaluate Dioxus. [Browser tests](https://wasm-bindgen.github.io/wasm-bindgen/wasm-bindgen-test/browsers.html) · [Leptos](https://leptos.dev/) · [Dioxus](https://dioxuslabs.com/) |
| Input/state invariants | Proptest with an independent model, named boundary examples and an explicit case count if needed | Cover relations such as idempotence or rejected input preserving state. Encode a failing case in a regression test. [Proptest](https://proptest-rs.github.io/proptest/intro.html) |
| Test strength | cargo-mutants for an explicit file or diff | Review surviving mutants. Compilation failures have their own count. Equivalent mutants may need a reasoned exception in the contract. [cargo-mutants](https://mutants.rs/) |
| Critical arithmetic/transition | Verus for a small supported executable module. Define both acceptance and rejection; challenge the property with a deliberate bad implementation. | Kani provides an alternative for checking actual Rust through a harness and bounded loops. Identify bounds, assumptions, trust and FFI boundaries. [Verus](https://github.com/verus-lang/verus) · [Kani](https://model-checking.github.io/kani/) · [PDD Kit](https://githubnext.com/projects/pdd-kit/) |
| Slow suite or concurrency | Keep Cargo test as a dependency-free starting point | Measure nextest for a growing native suite and retain a separate doctest run where applicable. Loom explores schedules for code expressed with its concurrency primitives. [nextest](https://nexte.st/) · [Loom](https://docs.rs/loom/) |

For the included browser starter use the wasm32-unknown-unknown Rust target, a wasm-bindgen-test-runner matching the locked wasm-bindgen crate, and a compatible browser and WebDriver. The web task defaults to the wasm-bindgen runner; existing CARGO_TARGET_WASM32_UNKNOWN_UNKNOWN_RUNNER takes precedence. Set CHROMEDRIVER or GECKODRIVER for a driver outside PATH. The starter README contains the static-site packaging steps.

For Verus set VERUS to the executable path if needed and VERUS_Z3_PATH to the matching Z3 binary. The proof lane accepts Verus library sources, requires a positive verified count and preserves failing process exits. A proof covers the stated contract under its assumptions. Project integration must connect that exact implementation to the product. Keep Verus, its Rust toolchain and solver on compatible versions. [Releases](https://github.com/verus-lang/verus/releases)

Existing projects can edit the attached mise task to select a package, features, timeout or framework. The native fast lane uses Cargo default features. The mutation timeout defaults to 60 seconds per mutant's test phase and can be changed with SIGNUM_MUTANT_TIMEOUT. Signum captures commands through its existing verification and delivery workflow.
