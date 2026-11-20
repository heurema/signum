use std::process::Command;

#[test]
fn prints_greeting() {
    let output = Command::new(env!("CARGO_BIN_EXE_signum-starter"))
        .args(["--name", "Ada"])
        .output()
        .expect("run the CLI");

    assert!(output.status.success());
    assert_eq!(output.stdout, b"Hello, Ada!\n");
}

#[test]
fn requires_a_name() {
    let output = Command::new(env!("CARGO_BIN_EXE_signum-starter"))
        .output()
        .expect("run the CLI");

    assert_eq!(output.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&output.stderr).contains("--name"));
}
