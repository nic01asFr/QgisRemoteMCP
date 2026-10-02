"""Taches de fond au niveau des OUTILS MCP (et non des actions du pont).

Constat du 2026-10-02 : un `execute_python` de densite a tenu le tour de
l'agent et le chat plus de 12 minutes. `execute_async` existait, mais ne
savait soumettre qu'une action du pont : `run_recipe` (plusieurs actions
enchainees), `smart_load` ou les exports, qui ajoutent leur propre logique
autour du pont (capture, mise en forme du resultat), n'avaient pas
d'equivalent asynchrone. Et le resultat d'une action du pont n'est pas celui
de l'outil : l'agent aurait recu un contrat different selon le chemin.

Ce registre execute n'importe quel outil MCP en arriere-plan et rend, a la
fin, EXACTEMENT le contenu que l'outil aurait rendu en direct.

Regles :

* une seule tache a la fois : QGIS execute une action a la fois sur son fil
  principal. Les taches suivantes attendent leur tour (`queued`), dans l'ordre
  de soumission ;
* soumission idempotente : un `client_id` deja vu rend la tache existante au
  lieu d'en creer une seconde. Une soumission dont la reponse s'est perdue
  peut donc etre retentee sans executer deux fois le calcul ;
* battement reel : tant que le fil d'execution vit, `battement_at` avance
  chaque seconde. Un fil mort sans resultat passe en `error` ;
* annulation honnete : une tache en attente est annulee pour de bon ; une
  tache deja commencee ne peut pas etre interrompue (QGIS ne sait pas
  arreter un script en cours) et la reponse le dit.
"""
from __future__ import annotations

import contextvars
import itertools
import queue
import threading
import time
import uuid
from typing import Callable, Dict, Optional

PREFIXE_ID = "t-"

# Statuts terminaux : plus rien ne changera.
TERMINAUX = frozenset({"done", "error", "cancelled"})

# Outils qu'on ne met jamais en tache de fond : le suivi lui-meme, l'affichage
# interactif, et le redemarrage du moteur (qui tuerait la tache qui le porte).
REFUSES = frozenset({
    "execute_async", "poll_job", "cancel_job",
    "qgis_desktop_ui", "restart_qgis_engine",
})


def est_id_de_tache(job_id: str) -> bool:
    return isinstance(job_id, str) and job_id.startswith(PREFIXE_ID)


class RegistreTachesOutils:
    """File d'execution des outils en arriere-plan, un a la fois."""

    def __init__(
        self,
        executer: Callable[[str, dict], dict],
        outil_connu: Callable[[str], bool] | None = None,
        max_lignes: int = 200,
        ttl_fini_s: float = 3600.0,
        periode_battement_s: float = 1.0,
        horloge: Callable[[], float] = time.time,
    ):
        self._executer = executer
        self._outil_connu = outil_connu or (lambda nom: True)
        self._max_lignes = max_lignes
        self._ttl_fini_s = ttl_fini_s
        self._periode = periode_battement_s
        self._horloge = horloge
        self._verrou = threading.RLock()
        self._lignes: Dict[str, dict] = {}
        self._par_client: Dict[str, str] = {}
        self._file: "queue.Queue[str]" = queue.Queue()
        self._ordre = itertools.count()
        self._repartiteur: threading.Thread | None = None

    # ── Soumission ──────────────────────────────────────────────────────

    def soumettre(self, outil: str, arguments: dict | None,
                  client_id: str | None = None) -> dict:
        outil = (outil or "").strip()
        if not outil:
            return {"error": "execute_async : 'tool' est vide"}
        if outil in REFUSES:
            return {"error": f"execute_async : l'outil '{outil}' ne peut pas "
                             "etre lance en arriere-plan"}
        if not self._outil_connu(outil):
            return {"error": f"Unknown tool: {outil}"}
        if arguments is not None and not isinstance(arguments, dict):
            return {"error": "execute_async : 'arguments' doit etre un objet"}
        maintenant = self._horloge()
        with self._verrou:
            if client_id and client_id in self._par_client:
                existant = self._lignes.get(self._par_client[client_id])
                if existant is not None:
                    vue = self._vue(existant)
                    vue["deja_soumis"] = True
                    return vue
            job_id = PREFIXE_ID + uuid.uuid4().hex[:16]
            ligne = {
                "job_id": job_id,
                "kind": "tool",
                "tool": outil,
                "client_id": client_id or None,
                "status": "queued",
                "stage": "queued",
                "submitted_at": maintenant,
                "started_at": None,
                "finished_at": None,
                "battement_at": maintenant,
                "ordre": next(self._ordre),
                "annulation_demandee": False,
                "resultat": None,
                "error": None,
                # Le contexte (utilisateur courant en mode multi-utilisateur)
                # voyage avec la tache : le fil d'execution n'en herite pas.
                "_contexte": contextvars.copy_context(),
                "_arguments": dict(arguments or {}),
            }
            self._lignes[job_id] = ligne
            if client_id:
                self._par_client[client_id] = job_id
            self._evincer()
            self._demarrer_repartiteur()
        self._file.put(job_id)
        return self._vue(ligne)

    # ── Lecture ─────────────────────────────────────────────────────────

    def etat(self, job_id: str) -> Optional[dict]:
        with self._verrou:
            ligne = self._lignes.get(job_id)
            if ligne is None:
                return None
            return self._vue(ligne)

    def resultat(self, job_id: str) -> Optional[dict]:
        """Le contenu MCP rendu par l'outil, une fois la tache finie."""
        with self._verrou:
            ligne = self._lignes.get(job_id)
            return None if ligne is None else ligne.get("resultat")

    def lister(self) -> list:
        with self._verrou:
            lignes = sorted(self._lignes.values(), key=lambda l: l["ordre"],
                            reverse=True)
            return [self._vue(l) for l in lignes]

    def _position(self, ligne: dict) -> int | None:
        if ligne["status"] != "queued":
            return None
        avant = [l for l in self._lignes.values()
                 if l["status"] in ("queued", "running")
                 and l["ordre"] < ligne["ordre"]]
        return len(avant)

    def _vue(self, ligne: dict) -> dict:
        maintenant = self._horloge()
        vue = {k: v for k, v in ligne.items()
               if not k.startswith("_") and k not in ("resultat", "ordre")}
        vue["age_s"] = round(maintenant - ligne["submitted_at"], 2)
        vue["heartbeat_age_s"] = round(maintenant - ligne["battement_at"], 2)
        vue["queue_position"] = self._position(ligne)
        if ligne["status"] in TERMINAUX:
            vue["duration_s"] = round(
                (ligne["finished_at"] or maintenant)
                - (ligne["started_at"] or ligne["submitted_at"]), 2)
        resultat = ligne.get("resultat")
        if isinstance(resultat, dict) and resultat.get("isError"):
            vue["is_error"] = True
        return vue

    # ── Annulation ──────────────────────────────────────────────────────

    def annuler(self, job_id: str) -> dict:
        with self._verrou:
            ligne = self._lignes.get(job_id)
            if ligne is None:
                return {"error": f"Unknown job_id: {job_id}"}
            if ligne["status"] in TERMINAUX:
                return {"success": False, "status": "already_terminal",
                        "current_status": ligne["status"]}
            if ligne["status"] == "queued":
                ligne["annulation_demandee"] = True
                ligne["status"] = "cancelled"
                ligne["stage"] = "cancelled"
                ligne["finished_at"] = self._horloge()
                ligne["battement_at"] = ligne["finished_at"]
                return {"success": True, "status": "cancelled"}
            # Deja commencee : QGIS ne sait pas interrompre un traitement en
            # cours sur son fil principal. On le dit plutot que de mentir.
            ligne["annulation_demandee"] = True
            return {"success": False, "status": "already_dispatched_cannot_cancel",
                    "stage": ligne["stage"]}

    # ── Execution ───────────────────────────────────────────────────────

    def _demarrer_repartiteur(self) -> None:
        if self._repartiteur is not None and self._repartiteur.is_alive():
            return
        self._repartiteur = threading.Thread(
            target=self._boucle, daemon=True, name="taches-outils")
        self._repartiteur.start()

    def _boucle(self) -> None:
        while True:
            job_id = self._file.get()
            try:
                self._executer_une(job_id)
            except Exception:  # le repartiteur ne doit jamais mourir
                pass

    def _executer_une(self, job_id: str) -> None:
        with self._verrou:
            ligne = self._lignes.get(job_id)
            if ligne is None or ligne["status"] != "queued":
                return  # annulee ou evincee pendant l'attente
            ligne["status"] = "running"
            ligne["stage"] = "running"
            ligne["started_at"] = self._horloge()
            ligne["battement_at"] = ligne["started_at"]
            contexte = ligne["_contexte"]
            outil = ligne["tool"]
            arguments = ligne["_arguments"]

        sortie: dict = {}

        def _travail():
            try:
                sortie["resultat"] = contexte.run(self._executer, outil, arguments)
            except BaseException as exc:  # noqa: BLE001 -- tout doit remonter
                sortie["erreur"] = f"{type(exc).__name__}: {exc}"

        fil = threading.Thread(target=_travail, daemon=True,
                               name=f"tache-{job_id}")
        fil.start()
        while fil.is_alive():
            fil.join(self._periode)
            with self._verrou:
                if job_id in self._lignes:
                    self._lignes[job_id]["battement_at"] = self._horloge()

        with self._verrou:
            ligne = self._lignes.get(job_id)
            if ligne is None:
                return
            fin = self._horloge()
            ligne["finished_at"] = fin
            ligne["battement_at"] = fin
            ligne["stage"] = "finished"
            if "erreur" in sortie:
                ligne["status"] = "error"
                ligne["error"] = sortie["erreur"]
            elif not isinstance(sortie.get("resultat"), dict):
                ligne["status"] = "error"
                ligne["error"] = "l'outil n'a rendu aucun resultat"
            else:
                ligne["status"] = "done"
                ligne["resultat"] = sortie["resultat"]

    # ── Entretien ───────────────────────────────────────────────────────

    def _evincer(self) -> None:
        """Retire les taches finies depuis longtemps, puis les plus vieilles."""
        maintenant = self._horloge()
        for job_id in [j for j, l in self._lignes.items()
                       if l["status"] in TERMINAUX
                       and maintenant - (l["finished_at"] or maintenant) > self._ttl_fini_s]:
            self._oublier(job_id)
        if len(self._lignes) <= self._max_lignes:
            return
        finies = sorted((l for l in self._lignes.values() if l["status"] in TERMINAUX),
                        key=lambda l: l["ordre"])
        for ligne in finies[: len(self._lignes) - self._max_lignes]:
            self._oublier(ligne["job_id"])

    def _oublier(self, job_id: str) -> None:
        ligne = self._lignes.pop(job_id, None)
        if ligne and ligne.get("client_id"):
            self._par_client.pop(ligne["client_id"], None)


def contenu_de_suivi(etat: dict, resultat: dict | None) -> list:
    """Le contenu MCP rendu par `poll_job` pour une tache d'outil.

    Premier element : l'etat en JSON (meme vocabulaire que les taches du pont).
    Une fois la tache finie (`done`), les elements suivants sont le contenu
    rendu par l'outil lui-meme, inchange : texte, capture, ressources.
    """
    import json

    elements = [{"type": "text", "text": json.dumps(etat, default=str, indent=2)}]
    if etat.get("status") == "done" and isinstance(resultat, dict):
        elements.extend(resultat.get("content") or [])
    return elements


__all__ = [
    "PREFIXE_ID", "REFUSES", "TERMINAUX", "RegistreTachesOutils",
    "contenu_de_suivi", "est_id_de_tache",
]
