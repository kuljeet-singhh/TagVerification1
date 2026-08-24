"""
Helpers for the test suite. Nothing here is collected as a test.

videos.py builds MP4s in memory; it has no test_ prefix, so pytest never picked it up, but
sitting among the test modules it was importable as a top-level `videos` and read as one more
thing to run. tests/ holds conftest.py, test modules, and this package — nothing else.
"""
