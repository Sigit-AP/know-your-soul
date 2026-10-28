//! The AST walker — a byte-faithful Rust port of
//! `know_your_soul.ingest.extractor.TreeSitterExtractor` (wave W2 reference).
//!
//! Every rule is mirrored exactly: canonical child order, scope stack,
//! id allocation with `:col` suffix on collision, import/call/variable rules,
//! and the canonical artifact shape. The parity tests in
//! `tests/test_kernel_rust.py` prove byte-identity per fixture.

use std::collections::{BTreeMap, HashMap, HashSet};

use regex::Regex;
use sha2::{Digest, Sha256};
use tree_sitter::{Node, Parser};

use crate::canon::Json;
use crate::generated::detect_generated;
use crate::registry::{self, ImportRule, LanguageSpec, VariableRule};

pub const EXTRACTION_VERSION: i64 = 1;
pub const MAX_SOURCE_FILE_SIZE_BYTES: usize = 1024 * 1024;
const MEMBER_FIELDS: [&str; 4] = ["property", "attribute", "name", "field"];
const MAX_NAME_DEPTH: usize = 6;

// --------------------------------------------------------------------- errors

#[derive(Debug)]
pub struct KernelError {
    pub code: &'static str,
    pub message: String,
    /// Structured context so the Python bridge can rebuild the reference
    /// extractor's exception types (path/size/limit) exactly.
    pub path: Option<String>,
    pub size: Option<usize>,
    pub limit: Option<usize>,
}

impl KernelError {
    fn new(code: &'static str, message: String) -> Self {
        KernelError { code, message, path: None, size: None, limit: None }
    }

    pub fn file_too_large(path: &str, size: usize) -> Self {
        KernelError {
            path: Some(path.to_string()),
            size: Some(size),
            limit: Some(MAX_SOURCE_FILE_SIZE_BYTES),
            ..KernelError::new(
                "FILE_TOO_LARGE",
                format!(
                    "{} is {} bytes, over the {}-byte source limit; refusing to parse",
                    py_repr(path),
                    size,
                    MAX_SOURCE_FILE_SIZE_BYTES
                ),
            )
        }
    }

    pub fn unsupported_media_type(path: &str) -> Self {
        KernelError {
            path: Some(path.to_string()),
            ..KernelError::new(
                "UNSUPPORTED_MEDIA_TYPE",
                format!("{}: no language registered for this extension", py_repr(path)),
            )
        }
    }

    pub fn decode_error(path: &str) -> Self {
        KernelError {
            path: Some(path.to_string()),
            ..KernelError::new(
                "DECODE_ERROR",
                format!("{}: source is not valid UTF-8; refusing to guess an encoding", py_repr(path)),
            )
        }
    }

    fn extraction(message: String) -> Self {
        KernelError::new("EXTRACTION_ERROR", message)
    }
}

impl std::fmt::Display for KernelError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "{}", self.message)
    }
}

impl std::error::Error for KernelError {}

/// Python `repr()` of a string, restricted to realistic path/name inputs:
/// single quotes preferred, standard escapes for controls.
fn py_repr(text: &str) -> String {
    let has_single = text.contains('\'');
    let has_double = text.contains('"');
    let quote = if has_single && !has_double { '"' } else { '\'' };
    let mut out = String::new();
    out.push(quote);
    for ch in text.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || (c as u32) == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

// ------------------------------------------------------------------ datatypes

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExtractedNode {
    pub node_id: String,
    pub kind: String,
    pub name: String,
    pub qualified_name: String,
    pub path: String,
    pub start_line: usize,
    pub end_line: usize,
    pub start_col: usize,
    pub end_col: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExtractedEdge {
    pub source: String,
    pub target: String,
    pub kind: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnresolvedRef {
    pub source_node_id: String,
    pub target_name: String,
    pub name_tail: String,
    pub kind: String,
    pub line: usize,
    pub col: usize,
}

#[derive(Debug)]
pub struct ExtractionResult {
    pub path: String,
    pub language: String,
    pub sha256: String,
    pub size: usize,
    pub generated_hint: bool,
    pub parse_error: bool,
    pub extraction_version: i64,
    pub nodes: Vec<ExtractedNode>,
    pub edges: Vec<ExtractedEdge>,
    pub unresolved_refs: Vec<UnresolvedRef>,
}

impl ExtractionResult {
    /// The canonical artifact dict: sorted keys, sorted collections — mirrors
    /// `ExtractionResult.to_artifact` in the Python reference exactly.
    pub fn to_artifact(&self) -> Json {
        let mut nodes = self.nodes.clone();
        nodes.sort_by(|a, b| {
            (a.start_line, a.start_col, &a.node_id).cmp(&(b.start_line, b.start_col, &b.node_id))
        });
        let mut edges = self.edges.clone();
        edges.sort_by(|a, b| (&a.source, &a.target, &a.kind).cmp(&(&b.source, &b.target, &b.kind)));
        let mut refs = self.unresolved_refs.clone();
        refs.sort_by(|a, b| {
            (&a.source_node_id, &a.target_name, &a.kind, a.line, a.col).cmp(&(
                &b.source_node_id,
                &b.target_name,
                &b.kind,
                b.line,
                b.col,
            ))
        });

        let mut artifact = BTreeMap::new();
        artifact.insert(
            "edges".to_string(),
            Json::Arr(
                edges
                    .iter()
                    .map(|edge| {
                        let mut entry = BTreeMap::new();
                        entry.insert("kind".to_string(), Json::Str(edge.kind.clone()));
                        entry.insert("source".to_string(), Json::Str(edge.source.clone()));
                        entry.insert("target".to_string(), Json::Str(edge.target.clone()));
                        Json::Obj(entry)
                    })
                    .collect(),
            ),
        );
        artifact.insert("extraction_version".to_string(), Json::Int(self.extraction_version));
        artifact.insert("generated_hint".to_string(), Json::Bool(self.generated_hint));
        artifact.insert("language".to_string(), Json::Str(self.language.clone()));
        artifact.insert(
            "nodes".to_string(),
            Json::Arr(
                nodes
                    .iter()
                    .map(|node| {
                        let mut entry = BTreeMap::new();
                        entry.insert("end_col".to_string(), Json::Int(node.end_col as i64));
                        entry.insert("end_line".to_string(), Json::Int(node.end_line as i64));
                        entry.insert("kind".to_string(), Json::Str(node.kind.clone()));
                        entry.insert("name".to_string(), Json::Str(node.name.clone()));
                        entry.insert("node_id".to_string(), Json::Str(node.node_id.clone()));
                        entry.insert("path".to_string(), Json::Str(node.path.clone()));
                        entry.insert(
                            "qualified_name".to_string(),
                            Json::Str(node.qualified_name.clone()),
                        );
                        entry.insert("start_col".to_string(), Json::Int(node.start_col as i64));
                        entry.insert("start_line".to_string(), Json::Int(node.start_line as i64));
                        Json::Obj(entry)
                    })
                    .collect(),
            ),
        );
        artifact.insert("parse_error".to_string(), Json::Bool(self.parse_error));
        artifact.insert("path".to_string(), Json::Str(self.path.clone()));
        artifact.insert("sha256".to_string(), Json::Str(self.sha256.clone()));
        artifact.insert("size".to_string(), Json::Int(self.size as i64));
        artifact.insert(
            "unresolved_refs".to_string(),
            Json::Arr(
                refs.iter()
                    .map(|reference| {
                        let mut entry = BTreeMap::new();
                        entry.insert("col".to_string(), Json::Int(reference.col as i64));
                        entry.insert("kind".to_string(), Json::Str(reference.kind.clone()));
                        entry.insert("line".to_string(), Json::Int(reference.line as i64));
                        entry.insert(
                            "name_tail".to_string(),
                            Json::Str(reference.name_tail.clone()),
                        );
                        entry.insert(
                            "source_node_id".to_string(),
                            Json::Str(reference.source_node_id.clone()),
                        );
                        entry.insert(
                            "target_name".to_string(),
                            Json::Str(reference.target_name.clone()),
                        );
                        Json::Obj(entry)
                    })
                    .collect(),
            ),
        );
        Json::Obj(artifact)
    }

    pub fn canonical_json(&self) -> String {
        crate::canon::canonical_json(&self.to_artifact())
    }
}

// ----------------------------------------------------------------- identities

pub fn sha256_hex(data: &[u8]) -> String {
    let digest = Sha256::digest(data);
    let mut hex = String::with_capacity(64);
    for byte in digest {
        hex.push_str(&format!("{byte:02x}"));
    }
    hex
}

/// `kind:sha256(path:kind:name:line)[:col]` with the 32-hex truncated digest.
pub fn make_node_id(path: &str, kind: &str, name: &str, line: u64, col: Option<u64>) -> String {
    debug_assert!(!name.is_empty(), "node id requires a non-empty name");
    let preimage = format!("{path}:{kind}:{name}:{line}");
    let digest = &sha256_hex(preimage.as_bytes())[..32];
    let mut node_id = format!("{kind}:{digest}");
    if let Some(col) = col {
        node_id.push_str(&format!(":{col}"));
    }
    node_id
}

pub fn file_node_id(path: &str) -> String {
    format!("file:{path}")
}

fn name_tail(target: &str) -> String {
    let after_dot = target.rsplit('.').next().unwrap_or(target);
    after_dot.rsplit("::").next().unwrap_or(after_dot).to_string()
}

// ------------------------------------------------------------------ the walker

#[derive(Debug, Clone)]
struct Scope {
    node_id: Option<String>,
    kind: String,
    name: String,
}

struct IdAllocator {
    used: HashSet<String>,
}

impl IdAllocator {
    fn allocate(
        &mut self,
        path: &str,
        kind: &str,
        name: &str,
        line: usize,
        col: usize,
    ) -> Result<String, KernelError> {
        // The Python reference raises NodeIdError on an empty name (reachable
        // via `from . import x` — name_tail(".") == ""). The kernel mirrors
        // the refusal so the bridge can defer to the reference walker, which
        // reproduces the exact reference behaviour for the caller.
        if name.is_empty() {
            return Err(KernelError::extraction("name must be a non-empty string".to_string()));
        }
        let base = make_node_id(path, kind, name, line as u64, None);
        if !self.used.contains(&base) {
            self.used.insert(base.clone());
            return Ok(base);
        }
        let with_col = make_node_id(path, kind, name, line as u64, Some(col as u64));
        if !self.used.contains(&with_col) {
            self.used.insert(with_col.clone());
            return Ok(with_col);
        }
        Err(KernelError::extraction(format!(
            "{}: cannot allocate a unique id for {} {} at {}:{}",
            py_repr(path),
            kind,
            py_repr(name),
            line,
            col
        )))
    }
}

struct Walker<'a> {
    spec: &'a LanguageSpec,
    data: &'a [u8],
    path: &'a str,
    nodes: Vec<ExtractedNode>,
    edges: Vec<ExtractedEdge>,
    refs: Vec<UnresolvedRef>,
    ref_keys: HashSet<(String, String, String, usize, usize)>,
    allocator: IdAllocator,
    declaration_kinds: HashMap<&'a str, &'a str>,
    conditional_kinds: HashMap<&'a str, &'a (String, Vec<(String, String)>, String)>,
    container_types: HashMap<&'a str, &'a str>,
    call_types: HashSet<&'a str>,
    variable_rules: HashMap<&'a str, &'a VariableRule>,
    import_regexes: &'static [Option<Regex>],
}

/// Compiled import regexes per language, built once — `regex` compilation is
/// far too slow to redo per extraction (matters on small files).
fn import_regexes_for(language: &str) -> &'static [Option<Regex>] {
    static CACHE: std::sync::OnceLock<HashMap<String, Vec<Option<Regex>>>> = std::sync::OnceLock::new();
    let cache = CACHE.get_or_init(|| {
        registry::registry()
            .languages
            .iter()
            .map(|(name, spec)| {
                let compiled = spec
                    .import_rules
                    .iter()
                    .map(|rule| {
                        if rule.target_regex.is_empty() {
                            None
                        } else {
                            Some(
                                Regex::new(&rule.target_regex)
                                    .expect("registry regexes must compile"),
                            )
                        }
                    })
                    .collect();
                (name.clone(), compiled)
            })
            .collect()
    });
    &cache[language]
}

impl<'a> Walker<'a> {
    fn new(spec: &'a LanguageSpec, data: &'a [u8], path: &'a str) -> Self {
        Walker {
            spec,
            data,
            path,
            nodes: Vec::new(),
            edges: Vec::new(),
            refs: Vec::new(),
            ref_keys: HashSet::new(),
            allocator: IdAllocator { used: HashSet::new() },
            declaration_kinds: spec
                .declaration_kinds
                .iter()
                .map(|(node_type, kind)| (node_type.as_str(), kind.as_str()))
                .collect(),
            conditional_kinds: spec
                .conditional_kinds
                .iter()
                .map(|entry| (entry.0.as_str(), entry))
                .collect(),
            container_types: spec
                .container_types
                .iter()
                .map(|(node_type, field)| (node_type.as_str(), field.as_str()))
                .collect(),
            call_types: spec.call_types.iter().map(String::as_str).collect(),
            // Python dict semantics: later rules overwrite earlier ones.
            variable_rules: spec
                .variable_rules
                .iter()
                .map(|rule| (rule.node_type.as_str(), rule))
                .collect(),
            import_regexes: import_regexes_for(&spec.name),
        }
    }

    fn is_identifier(&self, node_type: &str) -> bool {
        self.spec.identifier_types.iter().any(|item| item == node_type)
            || node_type.ends_with("_identifier")
    }

    fn text(&self, node: Node) -> String {
        String::from_utf8_lossy(&self.data[node.start_byte()..node.end_byte()]).into_owned()
    }

    // ------------------------------------------------------------- dispatch

    fn walk(&mut self, node: Node, scopes: &mut Vec<Scope>) -> Result<(), KernelError> {
        let node_type = node.kind();
        if let Some(kind) = self.declaration_kind(node) {
            self.declare(node, &kind, scopes)?;
            return Ok(());
        }
        if let Some(container_field) = self.container_types.get(node_type) {
            let name = node
                .child_by_field_name(container_field)
                .map(|name_node| self.text(name_node))
                .unwrap_or_else(|| "<anonymous>".to_string());
            scopes.push(Scope {
                node_id: None,
                kind: "container".to_string(),
                name,
            });
            let mut cursor = node.walk();
            for child in node.children(&mut cursor) {
                self.walk(child, scopes)?;
            }
            scopes.pop();
            return Ok(());
        }
        if let Some(rule_index) = self.match_import(node) {
            self.emit_import(node, rule_index, scopes)?;
            return Ok(());
        }
        if self.call_types.contains(node_type) {
            self.emit_call(node, scopes);
            let mut cursor = node.walk();
            for child in node.children(&mut cursor) {
                self.walk(child, scopes)?;
            }
            return Ok(());
        }
        if let Some(rule) = self.variable_rules.get(node_type) {
            let rule = *rule;
            if self.variable_declared(node, rule, scopes)? {
                return Ok(());
            }
        }
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            self.walk(child, scopes)?;
        }
        Ok(())
    }

    fn declaration_kind(&self, node: Node) -> Option<String> {
        if let Some((_, mapping, default)) = self.conditional_kinds.get(node.kind()) {
            let child_type = node
                .child_by_field_name("type")
                .map(|child| child.kind())
                .unwrap_or("");
            for (candidate, kind) in mapping {
                if candidate == child_type {
                    return Some(kind.clone());
                }
            }
            return Some(default.clone());
        }
        self.declaration_kinds.get(node.kind()).map(|kind| kind.to_string())
    }

    // --------------------------------------------------------- declarations

    fn declare(&mut self, node: Node, kind: &str, scopes: &mut Vec<Scope>) -> Result<(), KernelError> {
        let name = match self.declaration_name(node) {
            Some(name) => name,
            None => {
                let mut cursor = node.walk();
                for child in node.children(&mut cursor) {
                    self.walk(child, scopes)?;
                }
                return Ok(());
            }
        };
        let mut kind = kind.to_string();
        if kind == "function" && self.spec.scoped_functions_become_methods {
            let innermost = scopes.last().expect("scope stack has the file root");
            if innermost.kind == "container"
                || self.spec.method_scope_kinds.iter().any(|item| item == &innermost.kind)
            {
                kind = "method".to_string();
            }
        }
        let start_line = node.start_position().row + 1;
        let start_col = node.start_position().column;
        let node_id = self.allocator.allocate(self.path, &kind, &name, start_line, start_col)?;
        let parent_id = nearest_node_id(scopes);
        let qualified = qualified_name(scopes, &name);
        self.nodes.push(ExtractedNode {
            node_id: node_id.clone(),
            kind: kind.clone(),
            name: name.clone(),
            qualified_name: qualified,
            path: self.path.to_string(),
            start_line,
            end_line: node.end_position().row + 1,
            start_col,
            end_col: node.end_position().column,
        });
        self.edges.push(ExtractedEdge {
            source: parent_id,
            target: node_id.clone(),
            kind: "contains".to_string(),
        });
        if self.spec.scope_kinds.iter().any(|item| item == &kind) {
            scopes.push(Scope {
                node_id: Some(node_id),
                kind,
                name,
            });
            let mut cursor = node.walk();
            for child in node.children(&mut cursor) {
                self.walk(child, scopes)?;
            }
            scopes.pop();
        } else {
            let mut cursor = node.walk();
            for child in node.children(&mut cursor) {
                self.walk(child, scopes)?;
            }
        }
        Ok(())
    }

    fn declaration_name(&self, node: Node) -> Option<String> {
        if let Some(resolved) = self.resolve_name(node, &self.spec.name_fields, 0) {
            return Some(resolved);
        }
        // fallback: first child whose type looks like an identifier (covers
        // field-less declarations such as Go package_clause / Java package).
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            if self.is_identifier(child.kind()) {
                return Some(self.text(child));
            }
        }
        None
    }

    fn resolve_name(&self, node: Node, fields: &[String], depth: usize) -> Option<String> {
        if depth > MAX_NAME_DEPTH {
            return None;
        }
        if self.is_identifier(node.kind()) {
            return Some(self.text(node));
        }
        for field in fields {
            if let Some(child) = node.child_by_field_name(field) {
                if let Some(resolved) = self.resolve_name(child, fields, depth + 1) {
                    if !resolved.is_empty() {
                        return Some(resolved);
                    }
                }
            }
        }
        None
    }

    // -------------------------------------------------------------- imports

    fn match_import(&self, node: Node) -> Option<usize> {
        for (index, rule) in self.spec.import_rules.iter().enumerate() {
            if rule.node_type != node.kind() {
                continue;
            }
            if !rule.match_text.is_empty() {
                let callee = self.call_target_name(node);
                let matches = callee
                    .as_ref()
                    .is_some_and(|name| rule.match_text.iter().any(|item| item == name));
                if !matches {
                    continue;
                }
            }
            return Some(index);
        }
        None
    }

    fn emit_import(
        &mut self,
        node: Node,
        rule_index: usize,
        scopes: &mut [Scope],
    ) -> Result<(), KernelError> {
        let rule: &ImportRule = &self.spec.import_rules[rule_index];
        let mut target: Option<String> = None;
        for field in &rule.target_fields {
            if let Some(child) = node.child_by_field_name(field) {
                target = Some(self.text(child));
                break;
            }
        }
        if target.is_none() && !rule.target_regex.is_empty() {
            let node_text = self.text(node);
            if let Some(regex) = &self.import_regexes[rule_index] {
                if let Some(captures) = regex.captures(&node_text) {
                    if let Some(group) = captures.get(1) {
                        target = Some(group.as_str().to_string());
                    }
                }
            }
        }
        let target = match target {
            Some(target) => target,
            None => return Ok(()),
        };
        // Python: target.strip().strip(rule.strip_chars).strip()
        let strip_chars: Vec<char> = rule.strip_chars.chars().collect();
        let target = target
            .trim()
            .trim_matches(|ch| strip_chars.contains(&ch))
            .trim()
            .to_string();
        if target.is_empty() {
            return Ok(());
        }
        let line = node.start_position().row + 1;
        let col = node.start_position().column;
        let name = name_tail(&target);
        let node_id = self.allocator.allocate(self.path, "import", &name, line, col)?;
        let parent_id = nearest_node_id(scopes);
        self.nodes.push(ExtractedNode {
            node_id: node_id.clone(),
            kind: "import".to_string(),
            name: name.clone(),
            qualified_name: name,
            path: self.path.to_string(),
            start_line: line,
            end_line: node.end_position().row + 1,
            start_col: col,
            end_col: node.end_position().column,
        });
        self.edges.push(ExtractedEdge {
            source: parent_id,
            target: node_id.clone(),
            kind: "contains".to_string(),
        });
        self.add_ref(node_id, target, "imports", line, col);
        Ok(())
    }

    // ---------------------------------------------------------------- calls

    fn emit_call(&mut self, node: Node, scopes: &[Scope]) {
        let target = match self.call_target_name(node) {
            Some(target) if !target.is_empty() => target,
            _ => return,
        };
        let source_id = nearest_callable_id(scopes);
        self.add_ref(
            source_id,
            target,
            "calls",
            node.start_position().row + 1,
            node.start_position().column,
        );
    }

    fn call_target_name(&self, node: Node) -> Option<String> {
        for field in &self.spec.call_name_fields {
            if let Some(child) = node.child_by_field_name(field) {
                return self.member_name(child);
            }
        }
        None
    }

    fn member_name(&self, node: Node) -> Option<String> {
        if self.is_identifier(node.kind()) {
            return Some(self.text(node));
        }
        for field in MEMBER_FIELDS {
            if let Some(child) = node.child_by_field_name(field) {
                if let Some(resolved) = self.member_name(child) {
                    if !resolved.is_empty() {
                        return Some(resolved);
                    }
                }
            }
        }
        if node.named_child_count() == 1 {
            return self.member_name(node.named_child(0).expect("named_child_count == 1"));
        }
        let text = self.text(node).trim().to_string();
        if text.is_empty() {
            None
        } else {
            Some(text)
        }
    }

    // ------------------------------------------------------------ variables

    fn variable_declared(
        &mut self,
        node: Node,
        rule: &VariableRule,
        scopes: &mut Vec<Scope>,
    ) -> Result<bool, KernelError> {
        let name_node = match node.child_by_field_name(&rule.name_field) {
            Some(name_node) if self.is_identifier(name_node.kind()) => name_node,
            _ => return Ok(false),
        };
        let value_node = node.child_by_field_name("value");
        if !rule.function_value_types.is_empty() {
            if let Some(value) = value_node {
                if rule.function_value_types.iter().any(|item| item == value.kind()) {
                    // `const f = (x) => ...` — a function node named by the
                    // variable, scoped over the function body.
                    let name = self.text(name_node);
                    let line = node.start_position().row + 1;
                    let col = node.start_position().column;
                    let node_id = self.allocator.allocate(self.path, "function", &name, line, col)?;
                    let parent_id = nearest_node_id(scopes);
                    let qualified = qualified_name(scopes, &name);
                    self.nodes.push(ExtractedNode {
                        node_id: node_id.clone(),
                        kind: "function".to_string(),
                        name: name.clone(),
                        qualified_name: qualified,
                        path: self.path.to_string(),
                        start_line: line,
                        end_line: node.end_position().row + 1,
                        start_col: col,
                        end_col: node.end_position().column,
                    });
                    self.edges.push(ExtractedEdge {
                        source: parent_id,
                        target: node_id.clone(),
                        kind: "contains".to_string(),
                    });
                    scopes.push(Scope {
                        node_id: Some(node_id),
                        kind: "function".to_string(),
                        name,
                    });
                    self.walk(value, scopes)?;
                    scopes.pop();
                    return Ok(true);
                }
            }
        }
        // plain variable/constant: only at file/class-ish scope (never in
        // function bodies — codegraph value-ref noise control).
        let innermost = &scopes.last().expect("scope stack has the file root").kind;
        let in_allowed_scope = innermost == "file"
            || innermost == "module"
            || self.spec.method_scope_kinds.iter().any(|item| item == innermost);
        if !in_allowed_scope {
            return Ok(false);
        }
        let name = self.text(name_node);
        let mut kind = rule.kind.clone();
        if rule.const_when_uppercase
            && name.to_uppercase() == name
            && name.chars().any(|ch| ch.is_alphabetic())
        {
            kind = "constant".to_string();
        }
        if !rule.const_keyword.is_empty() {
            if let Some(parent) = node.parent() {
                if let Some(first) = parent.child(0) {
                    if self.text(first) == rule.const_keyword {
                        kind = "constant".to_string();
                    }
                }
            }
        }
        let line = node.start_position().row + 1;
        let col = node.start_position().column;
        let node_id = self.allocator.allocate(self.path, &kind, &name, line, col)?;
        let parent_id = nearest_node_id(scopes);
        let qualified = qualified_name(scopes, &name);
        self.nodes.push(ExtractedNode {
            node_id: node_id.clone(),
            kind,
            name,
            qualified_name: qualified,
            path: self.path.to_string(),
            start_line: line,
            end_line: node.end_position().row + 1,
            start_col: col,
            end_col: node.end_position().column,
        });
        self.edges.push(ExtractedEdge {
            source: parent_id,
            target: node_id,
            kind: "contains".to_string(),
        });
        // walk the initializer for calls, without the declarator node itself
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            self.walk(child, scopes)?;
        }
        Ok(true)
    }

    // -------------------------------------------------------------- helpers

    fn add_ref(&mut self, source_id: String, target: String, kind: &str, line: usize, col: usize) {
        let key = (source_id.clone(), target.clone(), kind.to_string(), line, col);
        if !self.ref_keys.insert(key) {
            return;
        }
        self.refs.push(UnresolvedRef {
            source_node_id: source_id,
            name_tail: name_tail(&target),
            target_name: target,
            kind: kind.to_string(),
            line,
            col,
        });
    }
}

fn nearest_node_id(scopes: &[Scope]) -> String {
    for scope in scopes.iter().rev() {
        if let Some(node_id) = &scope.node_id {
            return node_id.clone();
        }
    }
    // unreachable: the file root always carries an id
    panic!("scope stack lost its file root");
}

fn nearest_callable_id(scopes: &[Scope]) -> String {
    for scope in scopes.iter().rev() {
        if (scope.kind == "function" || scope.kind == "method") && scope.node_id.is_some() {
            return scope.node_id.clone().expect("checked is_some");
        }
    }
    nearest_node_id(scopes)
}

fn qualified_name(scopes: &[Scope], name: &str) -> String {
    let mut segments: Vec<&str> = scopes
        .iter()
        .skip(1)
        .filter(|scope| !scope.name.is_empty())
        .map(|scope| scope.name.as_str())
        .collect();
    segments.push(name);
    segments.join("::")
}

// ------------------------------------------------------------- entry points

fn parser_for(spec: &LanguageSpec) -> Result<Parser, KernelError> {
    let language = language_by_parser_name(&spec.parser).ok_or_else(|| {
        KernelError::extraction(format!("no compiled grammar for parser {:?}", spec.parser))
    })?;
    let mut parser = Parser::new();
    parser
        .set_language(&language)
        .map_err(|error| KernelError::extraction(format!("failed to load grammar: {error}")))?;
    Ok(parser)
}

fn language_by_parser_name(parser: &str) -> Option<tree_sitter::Language> {
    match parser {
        "bash" => Some(tree_sitter_bash::LANGUAGE.into()),
        "c" => Some(tree_sitter_c::LANGUAGE.into()),
        "cpp" => Some(tree_sitter_cpp::LANGUAGE.into()),
        "csharp" => Some(tree_sitter_c_sharp::LANGUAGE.into()),
        "go" => Some(tree_sitter_go::LANGUAGE.into()),
        "java" => Some(tree_sitter_java::LANGUAGE.into()),
        "javascript" => Some(tree_sitter_javascript::LANGUAGE.into()),
        "php" => Some(tree_sitter_php::LANGUAGE_PHP.into()),
        "python" => Some(tree_sitter_python::LANGUAGE.into()),
        "ruby" => Some(tree_sitter_ruby::LANGUAGE.into()),
        "rust" => Some(tree_sitter_rust::LANGUAGE.into()),
        "typescript" => Some(tree_sitter_typescript::LANGUAGE_TYPESCRIPT.into()),
        _ => None,
    }
}

/// Extract one in-memory source string under the contract path `path`.
pub fn extract_source(
    path: &str,
    source: &str,
    language: Option<&str>,
    generated_hint: bool,
) -> Result<ExtractionResult, KernelError> {
    let resolved = match language {
        Some(language) => language.to_string(),
        None => match registry::language_for_path(path) {
            Some(language) => language.to_string(),
            None => return Err(KernelError::unsupported_media_type(path)),
        },
    };
    let encoded = source.as_bytes();
    if encoded.len() > MAX_SOURCE_FILE_SIZE_BYTES {
        return Err(KernelError::file_too_large(path, encoded.len()));
    }
    let spec = registry::get_spec(&resolved)
        .ok_or_else(|| KernelError::extraction(format!("unknown language {resolved:?}")))?;
    let mut parser = parser_for(spec)?;
    let tree = parser
        .parse(encoded, None)
        .ok_or_else(|| KernelError::extraction(format!("parser returned no tree for {path:?}")))?;
    let mut walker = Walker::new(spec, encoded, path);
    let line_count = source.bytes().filter(|byte| *byte == b'\n').count() + 1;
    let root_id = file_node_id(path);
    walker.nodes.push(ExtractedNode {
        node_id: root_id.clone(),
        kind: "file".to_string(),
        name: path.rsplit('/').next().unwrap_or(path).to_string(),
        qualified_name: path.to_string(),
        path: path.to_string(),
        start_line: 1,
        end_line: line_count.max(1),
        start_col: 0,
        end_col: 0,
    });
    let mut scopes = vec![Scope {
        node_id: Some(root_id),
        kind: "file".to_string(),
        name: path.to_string(),
    }];
    walker.walk(tree.root_node(), &mut scopes)?;
    Ok(ExtractionResult {
        path: path.to_string(),
        language: spec.name.clone(),
        sha256: sha256_hex(encoded),
        size: encoded.len(),
        generated_hint,
        parse_error: tree.root_node().has_error(),
        extraction_version: EXTRACTION_VERSION,
        nodes: walker.nodes,
        edges: walker.edges,
        unresolved_refs: walker.refs,
    })
}

/// Read `path` with the 1 MiB bound and extract it (mirrors Python
/// `extract_file`, including utf-8-sig decode and raw-byte sha256/size).
pub fn extract_file(path: &str, display_path: Option<&str>) -> Result<ExtractionResult, KernelError> {
    let contract_path = display_path.unwrap_or(path);
    let file_name = path.rsplit('/').next().unwrap_or(path);
    let language = match registry::language_for_path(file_name) {
        Some(language) => language,
        None => return Err(KernelError::unsupported_media_type(path)),
    };
    let metadata = std::fs::metadata(path)
        .map_err(|error| KernelError::extraction(format!("cannot stat {}: {error}", py_repr(path))))?;
    let size = metadata.len() as usize;
    if size > MAX_SOURCE_FILE_SIZE_BYTES {
        return Err(KernelError::file_too_large(path, size));
    }
    let data = std::fs::read(path)
        .map_err(|error| KernelError::extraction(format!("cannot read {}: {error}", py_repr(path))))?;
    if data.len() > MAX_SOURCE_FILE_SIZE_BYTES {
        // the file grew between stat and read — fail closed
        return Err(KernelError::file_too_large(path, data.len()));
    }
    // Python decodes with utf-8-sig: a leading BOM is stripped, then strict.
    let body = if data.starts_with(&[0xEF, 0xBB, 0xBF]) { &data[3..] } else { &data[..] };
    let source = match std::str::from_utf8(body) {
        Ok(source) => source,
        Err(_) => return Err(KernelError::decode_error(path)),
    };
    let generated = detect_generated(file_name, source);
    let mut result = extract_source(contract_path, source, Some(language), generated)?;
    // sha256/size track the raw file bytes (change detection), not the
    // BOM-stripped decode.
    result.sha256 = sha256_hex(&data);
    result.size = data.len();
    Ok(result)
}
