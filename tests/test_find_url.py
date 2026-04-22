import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "scripts" / "find-url.py"


def load_module():
    spec = importlib.util.spec_from_file_location("find_url", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FindUrlTests(unittest.TestCase):
    def test_module_exists_and_parses_sort(self):
        module = load_module()
        args = module.parse_args(["agent", "skills", "--sort", "visits", "--limit", "5"])
        self.assertEqual(args.keywords, ["agent", "skills"])
        self.assertEqual(args.sort, "visits")
        self.assertEqual(args.limit, 5)


if __name__ == "__main__":
    unittest.main()