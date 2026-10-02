"""Lance websockify (noVNC) avec l'authentification du workspace.

Audit securite des acces (2026-09-26, SEC-2) : x11vnc tournait en `-nopw` et
websockify relayait le bureau QGIS a quiconque joignait le port 6080. En mode
`enforce`, websockify exige desormais une authentification HTTP Basic
(utilisateur `hub`, mot de passe = jeton du workspace), que le hub envoie en
relayant `/workspace/vnc/*`. En mode `permissive` ou `off`, websockify est
lance comme avant : le deploiement en deux temps reste possible.

Le jeton n'est jamais journalise. Il figure dans la ligne de commande de
websockify (`--auth-source`), visible des seuls processus du conteneur.
"""

from __future__ import annotations

import os
import sys

try:
    import workspace_auth
except ImportError:  # pragma: no cover - chemin d'import des tests
    from src import workspace_auth

_WEBSOCKIFY = os.environ.get("WEBSOCKIFY_BIN", "/usr/bin/websockify")
_WEB = os.environ.get("NOVNC_WEB_DIR", "/opt/novnc")
_PORT = os.environ.get("NOVNC_PORT", "6080")
_CIBLE = os.environ.get("NOVNC_TARGET", "localhost:5900")


def arguments() -> list[str]:
    """Ligne de commande de websockify selon le mode d'authentification."""
    args = [_WEBSOCKIFY, f"--web={_WEB}"]
    if workspace_auth.mode() == "enforce":
        jeton = workspace_auth.jeton_attendu()
        if not jeton:
            # Ferme par defaut : sans jeton calculable, personne ne passe.
            print("[noVNC] enforce sans jeton calculable : acces refuse a tous",
                  file=sys.stderr)
            jeton = os.urandom(32).hex()
        args += [
            "--auth-plugin", "BasicHTTPAuth",
            "--auth-source", f"{workspace_auth.UTILISATEUR_VNC}:{jeton}",
        ]
    args += [_PORT, _CIBLE]
    return args


if __name__ == "__main__":
    ligne = arguments()
    print(f"[noVNC] websockify, authentification : {workspace_auth.mode()}")
    os.execv(ligne[0], ligne)
