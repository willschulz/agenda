"""Static guard for the board's SortableJS cleanup.

SortableJS keeps every instance's element in a module-level list until
.destroy() is called, and render() rebuilds the board on every change. A raw
Sortable.create() call outside the tracked helper leaks every board it was
attached to, which grew the Mac app's web process to several GB over days.
"""

import re
import unittest
from pathlib import Path

INDEX = Path(__file__).resolve().parent.parent / "static" / "index.html"


def _function_body(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    end = source.index("\nfunction ", start + 1)
    return source[start:end]


class SortableCleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = INDEX.read_text(encoding="utf-8")

    def test_sortables_created_only_through_tracked_helper(self):
        raw = re.findall(r"Sortable\.create\(", self.source)
        helper = _function_body(self.source, "createSortable")
        self.assertEqual(len(raw), 1, "use createSortable(), not Sortable.create()")
        self.assertIn("Sortable.create(", helper)
        self.assertIn("_liveSortables.add(", helper)

    def test_helper_does_not_sweep_before_new_board_is_attached(self):
        # Sweeping during a build destroys the new board's own instances,
        # because its containers are not in the document yet.
        helper = _function_body(self.source, "createSortable")
        self.assertNotIn("sweepDetachedSortables", helper)

    def test_both_board_renders_sweep_after_swapping_in(self):
        for name in ("render", "renderCompletedView"):
            body = _function_body(self.source, name)
            swap = body.index("root.replaceChildren(...sections);")
            self.assertIn("sweepDetachedSortables();", body[swap:], name)

    def test_sweep_skips_active_drags(self):
        sweep = _function_body(self.source, "sweepDetachedSortables")
        self.assertIn("Sortable.active", sweep)
        self.assertIn("Sortable.dragged", sweep)
        self.assertIn(".destroy()", sweep)


if __name__ == "__main__":
    unittest.main()
