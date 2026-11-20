#![cfg(target_arch = "wasm32")]

use signum_starter::render;
use wasm_bindgen_test::{wasm_bindgen_test, wasm_bindgen_test_configure};

wasm_bindgen_test_configure!(run_in_browser);

#[wasm_bindgen_test]
fn renders_into_the_page() {
    let document = web_sys::window().unwrap().document().unwrap();
    let app = document.create_element("main").unwrap();
    app.set_id("app");
    document.body().unwrap().append_child(&app).unwrap();

    render().unwrap();

    assert_eq!(app.text_content().as_deref(), Some("Hello from Rust!"));
    app.remove();
}
