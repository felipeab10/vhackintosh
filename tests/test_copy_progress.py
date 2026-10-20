"""Regression tests for rsync's carriage-return progress stream."""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "appliance/archiso/airootfs/usr/local/bin/vhack-copy-system"
LOADER = SourceFileLoader("vhack_copy_system", str(SCRIPT))
SPEC = importlib.util.spec_from_loader("vhack_copy_system", LOADER)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class RsyncProgressParserTests(unittest.TestCase):
    def test_emits_each_carriage_return_update_without_waiting_for_newline(self):
        parser = MODULE.RsyncProgressParser()

        self.assertEqual(parser.feed("1,024  0%  12.4MB/s  0:00:10\r"), [
            (0, "12.4MB/s", "0:00:10"),
        ])
        self.assertEqual(parser.feed("524,288  7%  9.8MB/s  0:00:08\r"), [
            (7, "9.8MB/s", "0:00:08"),
        ])

    def test_handles_pipe_chunks_that_split_a_progress_record(self):
        parser = MODULE.RsyncProgressParser()

        self.assertEqual(parser.feed("1,024  2%  1."), [])
        self.assertEqual(parser.feed("5MB/s  0:00:04\r"), [
            (2, "1.5MB/s", "0:00:04"),
        ])

    def test_handles_a_final_update_without_a_trailing_delimiter(self):
        parser = MODULE.RsyncProgressParser()
        parser.feed("1,024  99%  1.5MB/s  0:00:01")

        self.assertEqual(parser.finish(), (99, "1.5MB/s", "0:00:01"))

    def test_ignores_status_lines_that_are_not_progress(self):
        parser = MODULE.RsyncProgressParser()

        self.assertEqual(parser.feed("sending incremental file list\r"), [])
        self.assertEqual(parser.finish(), None)


if __name__ == "__main__":
    unittest.main()
