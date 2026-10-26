"""PyInstaller entry point (a package's __main__ cannot be frozen directly)."""

import sys

from wiimote_xinput.__main__ import main

sys.exit(main())
