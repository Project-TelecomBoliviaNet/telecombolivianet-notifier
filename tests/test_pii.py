"""Tests para utilidades de enmascarado de PII."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.utils.pii import mask_phone


class TestMaskPhone:
    def test_full_bolivian_number(self):
        assert mask_phone("59176543210") == "591****210"

    def test_local_8_digits(self):
        result = mask_phone("76543210")
        assert "****" in result
        assert result.endswith("210")
        assert "765" not in result

    def test_already_formatted_with_plus(self):
        result = mask_phone("+59176543210")
        assert "****" in result

    def test_empty_string(self):
        assert mask_phone("") == "***"

    def test_very_short(self):
        assert mask_phone("123") == "***"

    def test_exactly_4_digits(self):
        result = mask_phone("1234")
        assert result.endswith("234")
        assert "****" in result

    def test_does_not_expose_middle_digits(self):
        result = mask_phone("59176543210")
        assert "7654" not in result  # dígitos del medio no visibles
        assert "321" not in result
