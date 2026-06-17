import pytest

from agents.util import extract_json


def test_plain_object():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_fenced():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_embedded_in_prose():
    assert extract_json('Here you go: {"a": [1, 2]} done') == {"a": [1, 2]}


def test_bare_list():
    assert extract_json("[1, 2, 3]") == [1, 2, 3]


def test_no_json_raises():
    with pytest.raises(ValueError):
        extract_json("there is no json here")
