"""
Run with:

blender -b -P testrunner.py
"""

import unittest
from pathlib import Path

_my_dir = Path(__file__).resolve().parent


def main() -> None:
    # Use a custom discovery so that the test files can end with `_test.py`.
    # This places the test files closer to the files under test, and also
    # matches Blender's test file locations in C++ and the standard Go test
    # locations.
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(_my_dir), pattern="*_test.py")
    runner = unittest.TextTestRunner()
    runner.run(suite)


if __name__ == "__main__":
    main()
