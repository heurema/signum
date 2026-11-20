# Rust WASM starter

Use Rust with the `wasm32-unknown-unknown` target, plus `wasm-bindgen` and
`wasm-bindgen-test-runner` from the same CLI release as `wasm-bindgen` in
`Cargo.lock`.

```sh
cargo build --locked --target wasm32-unknown-unknown
wasm-bindgen --target web --out-dir pkg target/wasm32-unknown-unknown/debug/signum_starter.wasm
python3 -m http.server 8000 --bind 127.0.0.1
```

Open <http://127.0.0.1:8000> to see the Rust-rendered page. With Chrome and
a matching `chromedriver` on your path, run the DOM test in a headless browser.

```sh
CARGO_TARGET_WASM32_UNKNOWN_UNKNOWN_RUNNER=wasm-bindgen-test-runner \
  cargo test --locked --target wasm32-unknown-unknown --test browser_contract
```
