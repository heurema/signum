use wasm_bindgen::prelude::*;

#[wasm_bindgen]
pub fn render() -> Result<(), JsValue> {
    let document = web_sys::window()
        .and_then(|window| window.document())
        .ok_or_else(|| JsValue::from_str("document is unavailable"))?;
    let app = document
        .get_element_by_id("app")
        .ok_or_else(|| JsValue::from_str("missing #app"))?;
    app.set_text_content(Some("Hello from Rust!"));

    Ok(())
}
