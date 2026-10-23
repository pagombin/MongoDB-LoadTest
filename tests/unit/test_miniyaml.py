from pathlib import Path

import pytest

from ballast import miniyaml

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[2]

CASES = [
    "a: 1\nb: two\nc: 3.5\nd: off\ne: ~\nf: ''\ng: \"x # y\"  # comment\n",
    'server:\n  listen: 127.0.0.1:8443\n  public_url: ""\ntls:\n  mode: off\n  self_signed_hosts: ["localhost", 127.0.0.1, a.example]\n',
    "list:\n  - one\n  - 2\n  - true\nnested:\n  - name: a\n    size: 1\n  - name: b\n    size: 2\n",
    "list:\n- a\n- b\nafter: 1\n",
    "flow: {a: 1, b: [x, y], c: 'it''s'}\n",
    "text: |\n  line one\n  line two\nfolded: >\n  one\n  two\nstrip: |-\n  kept\nnext: 1\n",
    "# only a comment\n",
    "",
    'url: http://host:80/path#frag\nemoji: "\\u00e9t\\u00e9"\nneg: -5\nhex: 0x1F\nexp: 1e3\nversion: "1.2"\nver2: 1.2\n',
    "empty_map:\nnext: 3\n",
    "key with spaces: v\n'quoted key': w\n",
    "deep:\n  a:\n    b:\n      c: [1, 2]\n",
]


@pytest.mark.parametrize("text", CASES)
def test_matches_pyyaml(text):
    assert miniyaml.safe_load(text) == yaml.safe_load(text)


def test_shipped_config_files_parse_identically():
    files = [ROOT / "config" / "ballast.yaml.example"]
    for path in files:
        text = path.read_text()
        assert miniyaml.safe_load(text) == yaml.safe_load(text)


@pytest.mark.parametrize("text", ["a: &x 1\nb: *x\n", "a: !!str 1\n", "a: 1\n---\nb: 2\n", "a: 1\n  b: 2\n", "a: 1\na: 2\n"])
def test_refuses_what_it_cannot_read(text):
    with pytest.raises(miniyaml.YAMLError):
        miniyaml.safe_load(text)


@pytest.mark.parametrize(
    "word", ["y", "n", "Yes", "NO", "On", "oFF", "1e3", "1.5e+3", ".5", "-.inf", "1_000", "0.", "012", "+7", "null", "Null", "1:30", "a b"]
)
def test_scalar_resolution_matches_pyyaml(word):
    assert miniyaml.safe_load(f"k: {word}\n") == yaml.safe_load(f"k: {word}\n")
