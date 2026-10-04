"""Every text file the engine reads or writes names its encoding: the locale's codec (cp1252 on Windows) garbles or
refuses what another step wrote as UTF-8, e.g. a digest's ✅ or a company name with an accent."""
import ast
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "job-search" / "scripts"


def _mode(call: ast.Call, at: int, default: str) -> str:
    arg = call.args[at] if len(call.args) > at else next((k.value for k in call.keywords if k.arg == "mode"), None)
    return arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else default


def unnamed(source: str) -> list:
    """Lines of read_text/write_text/open calls in text mode without encoding=."""
    out = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or any(k.arg == "encoding" for k in node.keywords):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
        if name in ("read_text", "write_text"):
            out.append(node.lineno)
        elif name in ("open", "fdopen"):
            owner = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else ""
            if owner in ("os", "webbrowser") and name == "open":
                continue
            at = 0 if isinstance(f, ast.Attribute) and owner not in ("gzip", "io", "codecs", "os") else 1  # Path.open
            if "b" not in _mode(node, at, "rb" if owner == "gzip" else "r"):
                out.append(node.lineno)
    return out


class Encoding(unittest.TestCase):
    def test_every_text_file_names_its_encoding(self):
        found = {str(p.relative_to(SCRIPTS)): lines for p in sorted(SCRIPTS.rglob("*.py"))
                 if (lines := unnamed(p.read_text(encoding="utf-8")))}
        self.assertEqual(found, {})

    def test_the_check_sees_each_form(self):
        src = ("p.read_text()\np.write_text(t)\np.open('a')\nopen(f)\nopen(f, 'w')\ngzip.open(g, 'rt')\n"
               "os.fdopen(fd, 'w')\nio.open(f, 'w')\n"
               "p.read_text(encoding='utf-8')\nopen(f, 'rb')\np.open('rb')\nos.open(f, 0)\ngzip.open(g)\n"
               "os.fdopen(fd, 'wb')\nio.open(f, 'rb')\n")
        self.assertEqual(unnamed(src), [1, 2, 3, 4, 5, 6, 7, 8])


if __name__ == "__main__":
    unittest.main()
