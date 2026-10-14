"""WallyCull (Foto-Aussortierer für WallyFrame): bewertet Fotos nach Qualität und Wert und sortiert schlechte, wertlose Bilder sicher aus."""

from . import pfade

__version__ = "0.6.0"

# Bei Änderungen an den Messungen erhöhen: dann wird alles neu berechnet.
ANALYSE_VERSION = 5

pfade.einrichten()
