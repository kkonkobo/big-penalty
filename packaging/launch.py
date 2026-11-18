"""PyInstaller entry point for the desktop app; all logic lives in jobhunt.desktop."""

import sys

from jobhunt.desktop import main

sys.exit(main())
