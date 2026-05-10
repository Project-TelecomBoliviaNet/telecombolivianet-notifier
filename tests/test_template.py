"""Tests unitarios para template.py — render() y parse_context()."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.services.template import render, parse_context


class TestRender:
    def test_basic_substitution(self):
        msg, missing = render("Hola {{nombre}}", {"nombre": "Juan"})
        assert msg == "Hola Juan"
        assert missing == []

    def test_multiple_variables(self):
        msg, missing = render(
            "{{nombre}}, su pago de Bs. {{monto}} del periodo {{periodo}} fue registrado.",
            {"nombre": "Ana", "monto": "199.00", "periodo": "Mayo 2025"}
        )
        assert msg == "Ana, su pago de Bs. 199.00 del periodo Mayo 2025 fue registrado."
        assert missing == []

    def test_missing_variable_kept_as_is(self):
        # US-02: variables faltantes se reportan en la lista
        msg, missing = render("Hola {{nombre}}, plan: {{plan}}", {"nombre": "Pedro"})
        assert "Pedro" in msg
        assert "{{plan}}" in msg
        assert "plan" in missing
        assert "nombre" not in missing

    def test_empty_context_returns_all_missing(self):
        msg, missing = render("Hola {{nombre}}", {})
        assert msg == "Hola {{nombre}}"
        assert "nombre" in missing

    def test_multiline_template(self):
        tmpl = "Estimado/a {{nombre}},\n\nSu servicio *{{plan}}* fue suspendido."
        msg, missing = render(tmpl, {"nombre": "Luis", "plan": "Plan Oro"})
        assert "Luis" in msg
        assert "Plan Oro" in msg
        assert "\n\n" in msg
        assert missing == []

    def test_repeated_variable(self):
        msg, missing = render("{{nombre}} - {{nombre}}", {"nombre": "Carlos"})
        assert msg == "Carlos - Carlos"
        assert missing == []

    def test_spaces_inside_braces_not_matched(self):
        msg, missing = render("{{ nombre }}", {"nombre": "test"})
        assert "{{ nombre }}" in msg  # el regex \w+ no captura espacios
        assert missing == []  # no es una variable válida, no se reporta

    def test_multiple_missing_vars_all_reported(self):
        msg, missing = render("{{a}} {{b}} {{c}}", {})
        assert set(missing) == {"a", "b", "c"}


class TestParseContext:
    def test_valid_json(self):
        ctx = parse_context('{"nombre": "Juan", "monto": "199.00"}')
        assert ctx["nombre"] == "Juan"
        assert ctx["monto"] == "199.00"

    def test_empty_string(self):
        assert parse_context("") == {}

    def test_none(self):
        assert parse_context(None) == {}

    def test_invalid_json(self):
        assert parse_context("not-json") == {}

    def test_empty_json(self):
        assert parse_context("{}") == {}
