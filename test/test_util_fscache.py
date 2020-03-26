#
# Tests for the `osbuild.util.fscache` module.
#


import unittest

import osbuild.util.fscache as fscache


class TestFsCache(unittest.TestCase):
    def test_setup(self):
        _ = fscache.Cache(None)

    @unittest.expectedFailure
    def test_setup_none(self):
        _ = fscache.Cache()

    @unittest.expectedFailure
    def test_setup_invalid_fd(self):
        _ = fscache.Cache(-1)


if __name__ == "__main__":
    unittest.main()
