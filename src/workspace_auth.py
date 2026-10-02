"""Authentification des appels entrants du workspace (API, MCP, noVNC).

Constate le 2026-09-26 (audit securite des acces, SEC-2) : l'API REST (8080,
dont `/api/execute`, Python arbitraire), le serveur MCP (8100) et noVNC
(6080) n'authentifiaient aucun appel ; x11vnc tournait en `-nopw` sur toutes
les interfaces. Tout pod capable de joindre le Service du workspace pilotait
QGIS et lisait les fichiers de l'utilisateur.

Regle, hors boucle locale et hors `/health` :
  - `X-Workspace-Token: <jeton>` ; le jeton vaut `WORKSPACE_TOKEN` s'il est
    defini, sinon HMAC-SHA256(HUB_API_KEY, "qgis-workspace-v1") en hexa.
    Le hub fait le meme calcul (qgis-sspcloud hub/hub/workspace_auth.py) a
    partir du meme Secret `qgis-hub-apikey` : rien de nouveau a stocker ;
  - ou `Authorization: Bearer <HUB_API_KEY>`, que les appels directs du hub
    envoient deja.

La boucle locale (127.0.0.1, ::1) passe sans jeton : le serveur MCP appelle
l'API en local, et la maintenance passe par
`kubectl exec ... curl localhost:8080/api/command`. Cela suppose qu'aucun
mandataire local (sidecar) ne relaie du trafic externe vers ces ports ; ce
n'est pas le cas du deploiement SSPCloud actuel.

Mode (`WORKSPACE_AUTH_MODE`) :
  - `permissive` (defaut) : un appel sans jeton valide est journalise puis
    servi. Sert au deploiement en deux temps (le hub envoie d'abord le jeton,
    le workspace l'exige ensuite) ;
  - `enforce` : il est refuse (401). Sans jeton calculable, tout appel non
    local est refuse : on ne retombe jamais sur « ouvert » ;
  - `off` : aucune verification (deconseille).
Une valeur inconnue vaut `enforce`. En `MULTI_USER_MODE`, le serveur MCP
garde sa propre authentification et ce module ne s'applique pas.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time

log = logging.getLogger("workspace_auth")

ENTETE_JETON = "x-workspace-token"
_LIBELLE = b"qgis-workspace-v1"
UTILISATEUR_VNC = "hub"
_MODES = ("off", "permissive", "enforce")
_HOTES_LOCAUX = frozenset({"127.0.0.1", "::1", "localhost"})
CHEMINS_LIBRES = frozenset({"/health"})

# Journalisation du mode permissif : une ligne par couple (chemin, hote) et
# par minute, pour que les journaux restent lisibles.
_derniers_avertissements: dict[tuple[str, str], float] = {}


def mode() -> str:
    valeur = (os.environ.get("WORKSPACE_AUTH_MODE", "permissive") or "permissive")
    valeur = valeur.strip().lower()
    return valeur if valeur in _MODES else "enforce"


def jeton_attendu() -> str:
    """Jeton attendu, ou chaine vide si ni WORKSPACE_TOKEN ni HUB_API_KEY."""
    explicite = os.environ.get("WORKSPACE_TOKEN", "").strip()
    if explicite:
        return explicite
    cle = os.environ.get("HUB_API_KEY", "").strip()
    if not cle:
        return ""
    return hmac.new(cle.encode("utf-8"), _LIBELLE, hashlib.sha256).hexdigest()


def _egal(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(
        a.encode("utf-8"), b.encode("utf-8"))


def presente_valide(entetes) -> bool:
    """Vrai si les en-tetes portent un jeton ou la cle du hub valides.

    `entetes` : tout objet a methode `get` insensible a la casse (Starlette)
    ou dictionnaire a cles en minuscules.
    """
    attendu = jeton_attendu()
    if _egal(entetes.get(ENTETE_JETON, "") or "", attendu):
        return True
    cle = os.environ.get("HUB_API_KEY", "").strip()
    autorisation = entetes.get("authorization", "") or ""
    if autorisation.startswith("Bearer ") and _egal(autorisation[7:].strip(), cle):
        return True
    return False


def decider(chemin: str, hote_client: str | None, entetes) -> tuple[bool, str]:
    """Rend (autorise, raison). La raison sert au journal et a la reponse."""
    m = mode()
    if m == "off":
        return True, "mode off"
    if chemin in CHEMINS_LIBRES:
        return True, "chemin libre"
    if (hote_client or "") in _HOTES_LOCAUX:
        return True, "boucle locale"
    if presente_valide(entetes):
        return True, "jeton valide"
    raison = ("aucun jeton calculable (HUB_API_KEY absente)"
              if not jeton_attendu() else "jeton absent ou invalide")
    if m == "permissive":
        cle = (chemin, hote_client or "?")
        maintenant = time.monotonic()
        if maintenant - _derniers_avertissements.get(cle, 0.0) > 60:
            _derniers_avertissements[cle] = maintenant
            log.warning(
                "workspace_auth PERMISSIF : %s depuis %s servi sans "
                "authentification (%s). Passer WORKSPACE_AUTH_MODE=enforce "
                "une fois le hub a jour.", chemin, hote_client, raison,
            )
        return True, f"permissif ({raison})"
    return False, raison


def origines_cors() -> tuple[list[str], str | None]:
    """Origines CORS de l'API : (liste explicite, expression reguliere).

    `WORKSPACE_CORS_ORIGINS` (liste separee par des virgules) les fixe.
    A defaut, seules les origines locales sont admises : en deploiement hub,
    aucun navigateur ne joint le workspace directement (pas d'ingress), il
    passe par le hub en meme origine. `*` reste possible pour un usage
    autonome, par choix explicite.
    """
    brut = os.environ.get("WORKSPACE_CORS_ORIGINS", "").strip()
    if brut:
        return [o.strip() for o in brut.split(",") if o.strip()], None
    return [], r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"
