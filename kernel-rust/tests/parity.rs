//! Rust-side sanity tests: every registry language parses its fixture,
//! extraction is deterministic across runs, and ids follow the frozen format.

use know_your_soul_kernel::walker::{
    extract_file, extract_source, make_node_id, sort_edges_canonical, sort_nodes_canonical,
    sort_refs_canonical,
};
use know_your_soul_kernel::registry;

const FIXTURES: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../tests/fixtures/ingest");

#[test]
fn all_fixtures_extract_deterministically() {
    // Dual-engine languages only (engine == "rust"); swift and dart are
    // Python-fallback by design (no parse-identical buildable crate — see
    // the Cargo.toml note).
    let cases: [(&str, &str); 17] = [
        ("python", "python/sample.py"),
        ("javascript", "javascript/sample.js"),
        ("typescript", "typescript/sample.ts"),
        ("go", "go/sample.go"),
        ("rust", "rust/sample.rs"),
        ("java", "java/sample.java"),
        ("c", "c/sample.c"),
        ("cpp", "cpp/sample.cpp"),
        ("csharp", "csharp/sample.cs"),
        ("ruby", "ruby/sample.rb"),
        ("php", "php/sample.php"),
        ("bash", "bash/sample.sh"),
        ("kotlin", "kotlin/sample.kt"),
        ("scala", "scala/sample.scala"),
        ("lua", "lua/sample.lua"),
        ("haskell", "haskell/sample.hs"),
        ("perl", "perl/sample.pl"),
    ];
    for (language, relative) in cases {
        let path = format!("{FIXTURES}/{relative}");
        let first = extract_file(&path, Some(relative)).unwrap_or_else(|e| panic!("{language}: {e}"));
        let second = extract_file(&path, Some(relative)).unwrap();
        assert_eq!(first.canonical_json(), second.canonical_json(), "{language} not deterministic");
        assert_eq!(first.language, language);
        assert!(first.nodes.len() > 1, "{language}: expected declarations beyond the file node");
        let file_id = format!("file:{relative}");
        assert!(
            first.nodes.iter().any(|node| node.node_id == file_id),
            "{language}: file node missing"
        );
        // F1 emission-order contract: raw collections are canonically sorted.
        let mut nodes = first.nodes.clone();
        sort_nodes_canonical(&mut nodes);
        assert_eq!(
            nodes.iter().map(|n| &n.node_id).collect::<Vec<_>>(),
            first.nodes.iter().map(|n| &n.node_id).collect::<Vec<_>>(),
            "{language}: raw nodes not in canonical order"
        );
        let mut edges = first.edges.clone();
        sort_edges_canonical(&mut edges);
        assert_eq!(
            edges
                .iter()
                .map(|e| (&e.source, &e.target, &e.kind))
                .collect::<Vec<_>>(),
            first
                .edges
                .iter()
                .map(|e| (&e.source, &e.target, &e.kind))
                .collect::<Vec<_>>(),
            "{language}: raw edges not in canonical order"
        );
        let mut refs = first.unresolved_refs.clone();
        sort_refs_canonical(&mut refs);
        assert_eq!(
            refs.iter()
                .map(|r| (&r.source_node_id, &r.target_name, &r.kind, r.line, r.col))
                .collect::<Vec<_>>(),
            first
                .unresolved_refs
                .iter()
                .map(|r| (&r.source_node_id, &r.target_name, &r.kind, r.line, r.col))
                .collect::<Vec<_>>(),
            "{language}: raw refs not in canonical order"
        );
    }
}

#[test]
fn extract_source_language_override_and_detection() {
    let source = "def f():\n    pass\n";
    let detected = extract_source("x/y.py", source, None, false).unwrap();
    let explicit = extract_source("x/y.py", source, Some("python"), false).unwrap();
    assert_eq!(detected.canonical_json(), explicit.canonical_json());
}

#[test]
fn error_codes_match_contract() {
    let error = extract_source("x/y.txt", "hello", None, false).unwrap_err();
    assert_eq!(error.code, "UNSUPPORTED_MEDIA_TYPE");
    let big = "x".repeat(1024 * 1024 + 1);
    let error = extract_source("x/big.py", &big, Some("python"), false).unwrap_err();
    assert_eq!(error.code, "FILE_TOO_LARGE");
}

#[test]
fn make_node_id_format() {
    let id = make_node_id("p.py", "function", "f", 3, None);
    assert!(id.starts_with("function:"));
    assert_eq!(id.len(), "function:".len() + 32);
    let with_col = make_node_id("p.py", "function", "f", 3, Some(7));
    assert_eq!(with_col, format!("{id}:7"));
}

#[test]
fn registry_lists_seventeen_dual_engine_languages() {
    assert_eq!(registry::supported_languages().len(), 17);
}
