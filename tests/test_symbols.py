from agentsnap.symbols import MODULE, changed_symbols, is_python

from .conftest import SAMPLE

APP = SAMPLE["src/app.py"]


def test_edit_inside_function():
    new = APP.replace("hello {name}", "hi {name}")
    assert changed_symbols(APP, new) == {"greet"}


def test_edit_method():
    new = APP.replace("self.n += 1", "self.n += 2")
    assert changed_symbols(APP, new) == {"Counter.inc"}


def test_two_functions():
    new = APP.replace("hello", "hi").replace("bye", "ciao")
    assert changed_symbols(APP, new) == {"greet", "farewell"}


def test_new_function_and_module_code():
    new = APP + "\n\ndef extra():\n    return 1\n"
    assert changed_symbols(APP, new) == {"extra"}
    new = APP.replace("import os\n", "import os\nimport sys\n")
    assert changed_symbols(APP, new) == {MODULE}


def test_deleted_function():
    start = APP.index("def farewell")
    end = APP.index("class Counter")
    new = APP[:start] + APP[end:]
    assert "farewell" in changed_symbols(APP, new)


def test_unparseable_returns_none():
    assert changed_symbols(APP, "def broken(:\n") is None


def test_is_python():
    assert is_python("a/b.py") and is_python("x.pyi") and not is_python("x.js")
