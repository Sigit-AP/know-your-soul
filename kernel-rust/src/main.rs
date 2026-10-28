//! `kys-kernel` — the standalone CLI of the Rust extraction kernel.
//!
//! Usage: `kys-kernel <file> [--display-path PATH]`
//!
//! Prints the canonical JSON artifact (one line + trailing newline) to stdout.
//! On failure, prints `{"error":{"code":...,"message":...}}` to stderr and
//! exits with code 2. Exit 1 is reserved for usage errors.

use std::collections::BTreeMap;
use std::process::ExitCode;

use know_your_soul_kernel::canon::{canonical_json, Json};
use know_your_soul_kernel::walker::{extract_file, KernelError};

fn usage() -> &'static str {
    "usage: kys-kernel <file> [--display-path PATH]"
}

fn error_json(error: &KernelError) -> String {
    let mut inner = BTreeMap::new();
    inner.insert("code".to_string(), Json::Str(error.code.to_string()));
    inner.insert("message".to_string(), Json::Str(error.message.clone()));
    let mut outer = BTreeMap::new();
    outer.insert("error".to_string(), Json::Obj(inner));
    canonical_json(&Json::Obj(outer))
}

fn run(args: &[String]) -> Result<String, (i32, String)> {
    let mut file: Option<String> = None;
    let mut display_path: Option<String> = None;
    let mut index = 0;
    while index < args.len() {
        match args[index].as_str() {
            "--display-path" => {
                index += 1;
                display_path = Some(
                    args.get(index)
                        .ok_or_else(|| (1, usage().to_string()))?
                        .clone(),
                );
            }
            flag if flag.starts_with("--") => return Err((1, usage().to_string())),
            positional => {
                if file.is_some() {
                    return Err((1, usage().to_string()));
                }
                file = Some(positional.to_string());
            }
        }
        index += 1;
    }
    let file = file.ok_or_else(|| (1, usage().to_string()))?;
    match extract_file(&file, display_path.as_deref()) {
        Ok(result) => Ok(result.canonical_json()),
        Err(error) => Err((2, error_json(&error))),
    }
}

fn main() -> ExitCode {
    // Deep ASTs recurse; give the walk a generous stack (the Python reference
    // dies to RecursionError far earlier).
    let args: Vec<String> = std::env::args().skip(1).collect();
    let handle = std::thread::Builder::new()
        .stack_size(256 * 1024 * 1024)
        .spawn(move || run(&args))
        .expect("failed to spawn worker thread");
    match handle.join().expect("worker thread panicked") {
        Ok(json) => {
            println!("{json}");
            ExitCode::SUCCESS
        }
        Err((code, message)) => {
            eprintln!("{message}");
            ExitCode::from(code as u8)
        }
    }
}
