//! Canonical JSON writer — byte-compatible with Python's
//! `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
//!
//! Python escapes `"`, `\`, the shorthand controls `\b \t \n \f \r`, and every
//! other codepoint below U+0020 as `\u00xx` (lowercase hex); everything else
//! (including DEL, non-ASCII, U+2028/2029) is emitted raw. Keys sort by
//! Unicode codepoint, which equals Rust's byte-wise `str` ordering for UTF-8.

use std::collections::BTreeMap;

#[derive(Debug, Clone, PartialEq)]
pub enum Json {
    Bool(bool),
    Int(i64),
    Str(String),
    Arr(Vec<Json>),
    Obj(BTreeMap<String, Json>),
}

pub fn canonical_json(value: &Json) -> String {
    let mut out = String::new();
    write_value(value, &mut out);
    out
}

fn write_value(value: &Json, out: &mut String) {
    match value {
        Json::Bool(true) => out.push_str("true"),
        Json::Bool(false) => out.push_str("false"),
        Json::Int(number) => out.push_str(&number.to_string()),
        Json::Str(text) => write_string(text, out),
        Json::Arr(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                write_value(item, out);
            }
            out.push(']');
        }
        Json::Obj(entries) => {
            out.push('{');
            for (index, (key, item)) in entries.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                write_string(key, out);
                out.push(':');
                write_value(item, out);
            }
            out.push('}');
        }
    }
}

fn write_string(text: &str, out: &mut String) {
    out.push('"');
    for ch in text.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            ch if (ch as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", ch as u32));
            }
            ch => out.push(ch),
        }
    }
    out.push('"');
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn escapes_match_python_rules() {
        let mut obj = BTreeMap::new();
        obj.insert("b".to_string(), Json::Str("q\"\\\n\r\t\u{08}\u{0c}\u{01}\u{1f}\u{7f}é日🎉".to_string()));
        obj.insert("a".to_string(), Json::Int(-3));
        obj.insert("c".to_string(), Json::Bool(false));
        let text = canonical_json(&Json::Obj(obj));
        assert_eq!(
            text,
            "{\"a\":-3,\"b\":\"q\\\"\\\\\\n\\r\\t\\b\\f\\u0001\\u001f\u{7f}é日🎉\",\"c\":false}"
        );
    }

    #[test]
    fn empty_containers() {
        assert_eq!(canonical_json(&Json::Arr(vec![])), "[]");
        assert_eq!(canonical_json(&Json::Obj(BTreeMap::new())), "{}");
    }
}
