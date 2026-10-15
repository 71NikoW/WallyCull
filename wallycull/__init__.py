"""WallyCull: rates photos by technical quality and wall-art value and safely culls the weak ones."""

from . import paths

__version__ = "0.9.1"

# Bump whenever the measurements change: every image is then measured again.
ANALYSIS_VERSION = 6

paths.setup()
