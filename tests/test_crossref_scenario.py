"""Unittest coverage for the documented cross-reference replay."""

from __future__ import annotations

import os
import tempfile
import unittest

from archive_handoff.crossref_scenario import (
    assert_demo_expectations,
    run_crossref_demo,
)


class CrossrefDemoCase(unittest.TestCase):
    def test_demo_replay_and_expectations(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "crossref")
            result = run_crossref_demo(data_dir)
            self.assertTrue(result["ok"], result)
            assert_demo_expectations(result)
            # The frozen edition artifacts and the summary are on disk.
            self.assertTrue(os.path.exists(os.path.join(data_dir, "crossref_demo.json")))
            editions = sorted(
                f for f in os.listdir(os.path.join(data_dir, "crossref_editions"))
                if f.endswith(".json")
            )
            self.assertEqual(
                editions,
                ["see-v1.json", "see-v2.json", "see-v3.json", "see-v4.json", "see-v5.json"],
            )


if __name__ == "__main__":
    unittest.main()
