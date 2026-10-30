"""Intake validation tests for the contract-v4 extraction schema (SUB-106).

Pins the strict closed-schema validation, the sanitization rules, the size
caps and the decimal-string confidence discipline of
``know_your_soul.extraction.schema`` — every rejection is a typed error,
never an exception with a raw value.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest

from _extraction_helpers import SOURCE_SHA256, make_args, make_method, make_payload
from know_your_soul.extraction import (
    ConfidenceNotDecimalError,
    ConfidenceOutOfRangeError,
    MethodSchemaError,
    PayloadSchemaError,
    PayloadTooLargeError,
    Submission,
    validate_submission,
)
from know_your_soul.extraction import schema as xs


class TestTopLevel:
    def test_valid_submission_validates(self) -> None:
        submission = validate_submission(make_args())
        assert isinstance(submission, Submission)
        assert submission.source_sha256 == SOURCE_SHA256

    def test_arguments_must_be_a_mapping(self) -> None:
        with pytest.raises(PayloadSchemaError):
            validate_submission(["not", "a", "mapping"])  # type: ignore[arg-type]

    def test_unknown_top_level_property_rejected(self) -> None:
        args = {**make_args(), "surprise": 1}
        with pytest.raises(PayloadSchemaError, match="surprise"):
            validate_submission(args)

    @pytest.mark.parametrize("missing", ["source_sha256", "payload", "method", "confidence"])
    def test_missing_required_property_rejected(self, missing: str) -> None:
        args = make_args()
        del args[missing]
        with pytest.raises(PayloadSchemaError, match=missing):
            validate_submission(args)

    def test_source_path_defaults_to_none(self) -> None:
        assert validate_submission(make_args()).source_path is None

    def test_source_path_must_be_a_string(self) -> None:
        with pytest.raises(PayloadSchemaError, match="source_path"):
            validate_submission(make_args(source_path=42))


class TestSourceSha256:
    @pytest.mark.parametrize("bad", ["", "abc", "z" * 64, "A" * 64, 123, None, SOURCE_SHA256 + "00"])
    def test_malformed_hash_rejected(self, bad: object) -> None:
        with pytest.raises(PayloadSchemaError, match="source_sha256"):
            validate_submission(make_args(source_sha256=bad))

    def test_hex_lowercase_only(self) -> None:
        with pytest.raises(PayloadSchemaError):
            validate_submission(make_args(source_sha256=SOURCE_SHA256.upper()))


class TestConfidence:
    @pytest.mark.parametrize("value", ["0", "1", "0.5", "1.0", "0.000000000001", "0.999999999999"])
    def test_decimal_strings_accepted(self, value: str) -> None:
        submission = validate_submission(make_args(confidence=value))
        assert submission.confidence == Decimal(value)

    @pytest.mark.parametrize("value", [0.5, 1.0, 0.1 + 0.2])
    def test_binary_floats_rejected(self, value: float) -> None:
        with pytest.raises(ConfidenceNotDecimalError):
            validate_submission(make_args(confidence=value))

    @pytest.mark.parametrize("value", [0, 1, True, False, None])
    def test_non_string_scalars_rejected(self, value: object) -> None:
        with pytest.raises(ConfidenceNotDecimalError):
            validate_submission(make_args(confidence=value))

    @pytest.mark.parametrize("value", ["1.5", "-0.1", "2", "-0"])
    def test_out_of_range_rejected(self, value: str) -> None:
        with pytest.raises(ConfidenceOutOfRangeError):
            validate_submission(make_args(confidence=value))

    @pytest.mark.parametrize("value", ["", "abc", "0,5", "1e-1", "NaN", "inf", ".5", "0.", " 0.5"])
    def test_non_decimal_strings_rejected(self, value: str) -> None:
        with pytest.raises(ConfidenceNotDecimalError):
            validate_submission(make_args(confidence=value))


class TestPayload:
    def test_payload_must_be_closed(self) -> None:
        payload = {**make_payload(), "surprise": 1}
        with pytest.raises(PayloadSchemaError, match="surprise"):
            validate_submission(make_args(payload=payload))

    @pytest.mark.parametrize("missing", ["text", "nodes", "edges"])
    def test_payload_requires_all_three_sections(self, missing: str) -> None:
        payload = make_payload()
        del payload[missing]
        with pytest.raises(PayloadSchemaError, match=missing):
            validate_submission(make_args(payload=payload))

    def test_text_must_be_a_string(self) -> None:
        with pytest.raises(PayloadSchemaError, match="text"):
            validate_submission(make_args(payload=make_payload(text=3)))  # type: ignore[arg-type]

    def test_text_cap(self) -> None:
        payload = make_payload(text="x" * (xs.MAX_TEXT_CHARS + 1))
        with pytest.raises(PayloadTooLargeError, match="text"):
            validate_submission(make_args(payload=payload))
        payload = make_payload(text="x" * xs.MAX_TEXT_CHARS)
        validate_submission(make_args(payload=payload))

    def test_nodes_cap(self) -> None:
        nodes = [{"name": f"n{i}", "kind": "leaf", "domain": "P1"} for i in range(xs.MAX_NODES + 1)]
        with pytest.raises(PayloadTooLargeError, match="nodes"):
            validate_submission(make_args(payload=make_payload(nodes=nodes, edges=[])))

    def test_edges_cap(self) -> None:
        node = {"name": "n", "kind": "leaf", "domain": "P1"}
        edges = [
            {"relation": "supports", "source": {"kind": "leaf", "name": "n"}, "target": {"kind": "leaf", "name": "n"}}
            for _ in range(xs.MAX_EDGES + 1)
        ]
        with pytest.raises(PayloadTooLargeError, match="edges"):
            validate_submission(make_args(payload=make_payload(nodes=[node], edges=edges)))

    def test_total_submission_byte_cap(self) -> None:
        big = "x" * (xs.MAX_TEXT_CHARS - 16)
        nodes = [{"name": f"n{i}", "kind": "leaf", "domain": "P1", "summary": big} for i in range(6)]
        payload = make_payload(text="t", nodes=nodes, edges=[])
        with pytest.raises(PayloadTooLargeError):
            validate_submission(make_args(payload=payload))


class TestProposedNodes:
    def test_node_must_be_a_closed_object(self) -> None:
        node = {"name": "n", "kind": "leaf", "domain": "P1", "surprise": True}
        with pytest.raises(PayloadSchemaError, match="surprise"):
            validate_submission(make_args(payload=make_payload(nodes=[node])))

    def test_node_requires_name_and_domain(self) -> None:
        with pytest.raises(PayloadSchemaError, match="name"):
            validate_submission(make_args(payload=make_payload(nodes=[{"kind": "leaf", "domain": "P1"}])))
        with pytest.raises(PayloadSchemaError, match="domain"):
            validate_submission(make_args(payload=make_payload(nodes=[{"name": "n", "kind": "leaf"}])))

    def test_node_kind_defaults_to_leaf(self) -> None:
        submission = validate_submission(
            make_args(payload=make_payload(nodes=[{"name": "n", "domain": "P2"}], edges=[]))
        )
        assert submission.payload.nodes[0].kind == "leaf"

    @pytest.mark.parametrize("kind", ["root", "primary", "child", "branch", "leaf"])
    def test_node_kind_vocabulary(self, kind: str) -> None:
        submission = validate_submission(
            make_args(payload=make_payload(nodes=[{"name": "n", "kind": kind, "domain": "P1"}], edges=[]))
        )
        assert submission.payload.nodes[0].kind == kind

    def test_node_kind_closed_vocabulary(self) -> None:
        node = {"name": "n", "kind": "chapter", "domain": "P1"}
        with pytest.raises(PayloadSchemaError, match="kind"):
            validate_submission(make_args(payload=make_payload(nodes=[node])))

    @pytest.mark.parametrize("domain", ["P1", "P2", "P3", "P4", "P5", "P6", "P7"])
    def test_domain_vocabulary(self, domain: str) -> None:
        submission = validate_submission(
            make_args(payload=make_payload(nodes=[{"name": "n", "domain": domain}], edges=[]))
        )
        assert submission.payload.nodes[0].domain == domain

    def test_domain_closed_vocabulary(self) -> None:
        node = {"name": "n", "kind": "leaf", "domain": "P8"}
        with pytest.raises(PayloadSchemaError, match="domain"):
            validate_submission(make_args(payload=make_payload(nodes=[node])))

    def test_name_cap_and_empty(self) -> None:
        with pytest.raises(PayloadSchemaError, match="name"):
            validate_submission(make_args(payload=make_payload(nodes=[{"name": "", "domain": "P1"}])))
        with pytest.raises(PayloadTooLargeError):
            validate_submission(
                make_args(payload=make_payload(nodes=[{"name": "n" * (xs.MAX_NAME_CHARS + 1), "domain": "P1"}]))
            )

    def test_summary_cap(self) -> None:
        node = {"name": "n", "domain": "P1", "summary": "s" * (xs.MAX_SUMMARY_CHARS + 1)}
        with pytest.raises(PayloadTooLargeError):
            validate_submission(make_args(payload=make_payload(nodes=[node])))

    def test_span_shape(self) -> None:
        node = {"name": "n", "domain": "P1", "span": {"line": 3, "col": 7}}
        submission = validate_submission(make_args(payload=make_payload(nodes=[node], edges=[])))
        assert submission.payload.nodes[0].span == (3, 7)

    @pytest.mark.parametrize("span", [{"line": 0}, {"line": -1}, {"line": "3"}, {"col": 1}, {"line": 1, "col": -1}])
    def test_bad_spans_rejected(self, span: dict) -> None:
        node = {"name": "n", "domain": "P1", "span": span}
        with pytest.raises(PayloadSchemaError, match="span"):
            validate_submission(make_args(payload=make_payload(nodes=[node])))

    def test_duplicate_node_keys_rejected(self) -> None:
        nodes = [{"name": "n", "kind": "leaf", "domain": "P1"}, {"name": "n", "kind": "leaf", "domain": "P2"}]
        with pytest.raises(PayloadSchemaError, match="duplicate"):
            validate_submission(make_args(payload=make_payload(nodes=nodes, edges=[])))

    def test_same_name_different_kind_is_a_distinct_key(self) -> None:
        nodes = [{"name": "n", "kind": "leaf", "domain": "P1"}, {"name": "n", "kind": "child", "domain": "P1"}]
        submission = validate_submission(make_args(payload=make_payload(nodes=nodes, edges=[])))
        assert len(submission.payload.nodes) == 2


class TestProposedEdges:
    def test_edge_must_be_a_closed_object(self) -> None:
        edge = {
            "relation": "supports",
            "source": {"kind": "child", "name": "Graft procedure"},
            "target": {"kind": "leaf", "name": "Witness"},
            "surprise": 1,
        }
        with pytest.raises(PayloadSchemaError, match="surprise"):
            validate_submission(make_args(payload=make_payload(edges=[edge])))

    def test_edge_relation_uses_the_v1_vocabulary(self) -> None:
        edge = {
            "relation": "mentions",
            "source": {"kind": "child", "name": "Graft procedure"},
            "target": {"kind": "leaf", "name": "Witness"},
        }
        with pytest.raises(PayloadSchemaError, match="relation"):
            validate_submission(make_args(payload=make_payload(edges=[edge])))

    def test_edge_endpoints_must_reference_payload_nodes(self) -> None:
        edge = {
            "relation": "supports",
            "source": {"kind": "child", "name": "Graft procedure"},
            "target": {"kind": "leaf", "name": "Ghost"},
        }
        with pytest.raises(PayloadSchemaError, match="Ghost"):
            validate_submission(make_args(payload=make_payload(edges=[edge])))

    def test_edge_endpoint_shape(self) -> None:
        edge = {"relation": "supports", "source": "Graft procedure", "target": {"kind": "leaf", "name": "Witness"}}
        with pytest.raises(PayloadSchemaError):
            validate_submission(make_args(payload=make_payload(edges=[edge])))

    def test_edge_evidence_cap(self) -> None:
        edge = {
            "relation": "supports",
            "source": {"kind": "child", "name": "Graft procedure"},
            "target": {"kind": "leaf", "name": "Witness"},
            "evidence": "e" * (xs.MAX_EVIDENCE_CHARS + 1),
        }
        with pytest.raises(PayloadTooLargeError):
            validate_submission(make_args(payload=make_payload(edges=[edge])))

    def test_empty_edges_list_is_valid(self) -> None:
        submission = validate_submission(make_args(payload=make_payload(edges=[])))
        assert submission.payload.edges == ()


class TestMethod:
    def test_method_must_be_closed(self) -> None:
        method = {**make_method(), "surprise": "x"}
        with pytest.raises(MethodSchemaError, match="surprise"):
            validate_submission(make_args(method=method))

    @pytest.mark.parametrize("missing", ["agent", "extracted_at"])
    def test_method_requires_agent_and_extracted_at(self, missing: str) -> None:
        method = make_method()
        del method[missing]
        with pytest.raises(MethodSchemaError, match=missing):
            validate_submission(make_args(method=method))

    def test_extracted_at_must_be_iso8601(self) -> None:
        with pytest.raises(MethodSchemaError, match="extracted_at"):
            validate_submission(make_args(method=make_method() | {"extracted_at": "yesterday"}))

    def test_method_field_caps(self) -> None:
        with pytest.raises(MethodSchemaError):
            validate_submission(make_args(method=make_method(agent="a" * 257)))

    def test_fingerprint_excludes_extracted_at(self) -> None:
        first = validate_submission(make_args(method=make_method() | {"extracted_at": "2026-10-30T12:00:00+00:00"}))
        second = validate_submission(make_args(method=make_method() | {"extracted_at": "2026-10-30T13:00:00+00:00"}))
        assert first.method.fingerprint() == second.method.fingerprint()

    def test_fingerprint_changes_with_agent_prompt_or_tool(self) -> None:
        base = validate_submission(make_args())
        assert (
            validate_submission(make_args(method=make_method(agent="other"))).method.fingerprint()
            != base.method.fingerprint()
        )
        assert (
            validate_submission(make_args(method=make_method(prompt_id="other"))).method.fingerprint()
            != base.method.fingerprint()
        )
        assert (
            validate_submission(make_args(method=make_method(tool="other"))).method.fingerprint()
            != base.method.fingerprint()
        )


class TestSanitization:
    def test_control_and_bidi_characters_are_stripped(self) -> None:
        payload = make_payload(text="a\x00b\x07c\u202ed")
        submission = validate_submission(make_args(payload=payload))
        assert submission.payload.text == "abcd"

    def test_html_is_escaped_in_text(self) -> None:
        payload = make_payload(text="<script>alert(1)</script>")
        submission = validate_submission(make_args(payload=payload))
        assert "<script>" not in submission.payload.text
        assert "&lt;script&gt;" in submission.payload.text

    def test_html_escape_covers_quotes_and_ampersand(self) -> None:
        payload = make_payload(text="""a & b "c" 'd' <e>""")
        submission = validate_submission(make_args(payload=payload))
        assert submission.payload.text == "a &amp; b &quot;c&quot; &#x27;d&#x27; &lt;e&gt;"

    def test_node_fields_are_sanitized(self) -> None:
        node = {"name": "n<img>", "domain": "P1", "summary": "<b>bold</b>"}
        submission = validate_submission(make_args(payload=make_payload(nodes=[node], edges=[])))
        assert submission.payload.nodes[0].name == "n&lt;img&gt;"
        assert submission.payload.nodes[0].summary == "&lt;b&gt;bold&lt;/b&gt;"

    def test_sanitization_happens_before_hashing(self) -> None:
        import hashlib as _hashlib

        from know_your_soul.domain import canonical_json as _canonical

        raw = make_args(payload=make_payload(text="<script>x</script>"))
        submission = validate_submission(raw)
        # The payload hash binds the SANITIZED canonical form, not the raw input.
        assert (
            submission.payload_sha256
            == _hashlib.sha256(_canonical(submission.payload.canonical()).encode("utf-8")).hexdigest()
        )
        assert submission.payload_sha256 != _hashlib.sha256(_canonical(raw["payload"]).encode("utf-8")).hexdigest()

    def test_validation_does_not_mutate_the_caller(self) -> None:
        args = make_args(payload=make_payload(text="<script>x</script>"))
        before = hashlib.sha256(repr(sorted(args.items())).encode()).hexdigest()
        validate_submission(args)
        assert hashlib.sha256(repr(sorted(args.items())).encode()).hexdigest() == before
        assert args["payload"]["text"] == "<script>x</script>"


class TestDeterminism:
    def test_two_validations_are_identical(self) -> None:
        first = validate_submission(make_args())
        second = validate_submission(make_args())
        assert first.payload_sha256 == second.payload_sha256
        assert first.payload.canonical() == second.payload.canonical()
        assert first.method.fingerprint() == second.method.fingerprint()

    def test_idempotency_key_and_receipt_id_are_content_derived(self) -> None:
        submission = validate_submission(make_args())
        assert submission.idem_key("actor-a") == submission.idem_key("actor-a")
        assert submission.idem_key("actor-a") != submission.idem_key("actor-b")
        assert submission.receipt_id("actor-a").startswith("ext-")
        assert len(submission.receipt_id("actor-a")) == len("ext-") + 32
        assert submission.receipt_id("actor-a") != submission.receipt_id("actor-b")

    def test_receipt_id_tracks_payload(self) -> None:
        base = validate_submission(make_args())
        other = validate_submission(make_args(payload=make_payload(text="different text")))
        assert base.receipt_id("actor-a") != other.receipt_id("actor-a")
