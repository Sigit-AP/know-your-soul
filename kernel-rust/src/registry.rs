//! The language registry: deserialized from `registry.json`, the byte-exact
//! dump of `know_your_soul.ingest.languages` produced by
//! `tools/dump_language_registry.py`. One source of truth, two implementations.

use std::collections::BTreeMap;

use serde::Deserialize;

#[derive(Debug, Deserialize)]
pub struct Registry {
    pub extension_map: BTreeMap<String, String>,
    pub format_version: u32,
    pub languages: BTreeMap<String, LanguageSpec>,
}

#[derive(Debug, Deserialize)]
pub struct LanguageSpec {
    pub call_name_fields: Vec<String>,
    pub call_types: Vec<String>,
    pub conditional_kinds: Vec<(String, Vec<(String, String)>, String)>,
    pub container_types: Vec<(String, String)>,
    pub declaration_kinds: Vec<(String, String)>,
    pub identifier_types: Vec<String>,
    pub import_rules: Vec<ImportRule>,
    pub method_scope_kinds: Vec<String>,
    pub name: String,
    pub name_fields: Vec<String>,
    pub parser: String,
    pub scope_kinds: Vec<String>,
    pub scoped_functions_become_methods: bool,
    pub variable_rules: Vec<VariableRule>,
}

#[derive(Debug, Deserialize)]
pub struct ImportRule {
    pub match_text: Vec<String>,
    pub node_type: String,
    pub strip_chars: String,
    pub target_fields: Vec<String>,
    pub target_regex: String,
}

#[derive(Debug, Deserialize)]
pub struct VariableRule {
    pub const_keyword: String,
    pub const_when_uppercase: bool,
    pub function_value_types: Vec<String>,
    pub kind: String,
    pub name_field: String,
    pub node_type: String,
}

const REGISTRY_JSON: &str = include_str!("../registry.json");

static REGISTRY: std::sync::OnceLock<Registry> = std::sync::OnceLock::new();

pub fn registry() -> &'static Registry {
    REGISTRY.get_or_init(|| serde_json::from_str(REGISTRY_JSON).expect("registry.json must be valid"))
}

pub fn get_spec(language: &str) -> Option<&'static LanguageSpec> {
    registry().languages.get(language)
}

pub fn supported_languages() -> Vec<&'static str> {
    registry().languages.keys().map(String::as_str).collect()
}

/// The PurePosixPath `.suffix` of the final path component, lowercased.
pub fn suffix_of(path: &str) -> String {
    let name = path.rsplit('/').next().unwrap_or(path);
    if name == "." || name == ".." {
        return String::new();
    }
    match name.rfind('.') {
        // ".foo" (leading dot, no other dot) has no suffix in pathlib.
        Some(0) if !name[1..].contains('.') => String::new(),
        // "foo." — trailing dot means an empty suffix in pathlib.
        Some(index) if index == name.len() - 1 => String::new(),
        Some(index) => name[index..].to_lowercase(),
        None => String::new(),
    }
}

pub fn language_for_path(path: &str) -> Option<&'static str> {
    let suffix = suffix_of(path);
    if suffix.is_empty() {
        return None;
    }
    registry().extension_map.get(&suffix).map(String::as_str)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn registry_loads_all_phase1_languages() {
        assert_eq!(supported_languages().len(), 12);
        assert!(get_spec("python").is_some());
        assert!(get_spec("bash").is_some());
    }

    #[test]
    fn extension_lookup_matches_pathlib_suffix_rules() {
        assert_eq!(language_for_path("a/b/sample.py"), Some("python"));
        assert_eq!(language_for_path("SAMPLE.PY"), Some("python"));
        assert_eq!(language_for_path("x.h"), Some("c"));
        assert_eq!(language_for_path("x.txt"), None);
        assert_eq!(language_for_path(".py"), None);
        assert_eq!(language_for_path("noext"), None);
        assert_eq!(language_for_path("a.tar.gz"), None);
        assert_eq!(language_for_path("x."), None);
        assert_eq!(language_for_path(".."), None);
    }
}
