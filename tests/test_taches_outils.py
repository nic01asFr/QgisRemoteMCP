# -*- coding: utf-8 -*-
"""Taches de fond au niveau des outils MCP (constat du 2026-10-02).

Un `execute_python` de densite a tenu le chat plus de 12 minutes. L'agent
soumet desormais les outils longs en arriere-plan : `execute_async(tool=...)`
puis `poll_job`. Ces tests verrouillent le contrat dont il depend :

* le resultat final est EXACTEMENT celui de l'outil en direct ;
* une seule tache a la fois, les suivantes attendent dans l'ordre ;
* une soumission repetee avec le meme `client_id` n'execute pas deux fois ;
* le battement avance tant que la tache tourne ;
* l'annulation d'une tache commencee est refusee, et le dit.

Aucun QGIS : l'executeur est simule.
"""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

_RACINE = Path(__file__).resolve().parents[1]
if str(_RACINE) not in sys.path:
    sys.path.insert(0, str(_RACINE))

from src.taches_outils import (  # noqa: E402
    REFUSES, RegistreTachesOutils, contenu_de_suivi, est_id_de_tache,
)


def _attendre(predicat, delai=5.0):
    fin = time.time() + delai
    while time.time() < fin:
        if predicat():
            return True
        time.sleep(0.01)
    return False


class _Executeur:
    """Outil simule : bloque sur un evenement, compte ses executions."""

    def __init__(self):
        self.appels = []
        self.liberer = {}
        self.verrou = threading.Lock()
        self.en_cours = 0
        self.max_simultanes = 0

    def __call__(self, nom, args):
        with self.verrou:
            self.appels.append((nom, dict(args)))
            self.en_cours += 1
            self.max_simultanes = max(self.max_simultanes, self.en_cours)
        evenement = self.liberer.setdefault(args.get("cle", nom), threading.Event())
        evenement.wait(5)
        with self.verrou:
            self.en_cours -= 1
        if args.get("lever"):
            raise RuntimeError("panne simulee")
        return {"content": [{"type": "text", "text": json.dumps({"ok": True, "cle": args.get("cle")})},
                            {"type": "image", "data": "QUJD", "mimeType": "image/jpeg"}]}

    def ouvrir(self, cle):
        self.liberer.setdefault(cle, threading.Event()).set()


@pytest.fixture
def registre():
    executeur = _Executeur()
    r = RegistreTachesOutils(executeur, outil_connu=lambda n: n != "inconnu",
                             periode_battement_s=0.02)
    r.executeur = executeur
    return r


def test_la_soumission_rend_un_identifiant_de_tache(registre):
    vue = registre.soumettre("run_recipe", {"cle": "a"})
    assert est_id_de_tache(vue["job_id"])
    assert vue["tool"] == "run_recipe"
    assert vue["status"] in ("queued", "running")
    registre.executeur.ouvrir("a")


def test_le_resultat_final_est_celui_de_l_outil(registre):
    vue = registre.soumettre("smart_load", {"cle": "b"})
    registre.executeur.ouvrir("b")
    assert _attendre(lambda: registre.etat(vue["job_id"])["status"] == "done")
    resultat = registre.resultat(vue["job_id"])
    assert resultat["content"][0]["text"] == json.dumps({"ok": True, "cle": "b"})
    assert resultat["content"][1]["type"] == "image"


def test_une_seule_tache_a_la_fois(registre):
    v1 = registre.soumettre("execute_python", {"cle": "1"})
    v2 = registre.soumettre("execute_python", {"cle": "2"})
    assert _attendre(lambda: registre.etat(v1["job_id"])["status"] == "running")
    time.sleep(0.1)
    assert registre.etat(v2["job_id"])["status"] == "queued"
    assert registre.etat(v2["job_id"])["queue_position"] == 1
    registre.executeur.ouvrir("1")
    registre.executeur.ouvrir("2")
    assert _attendre(lambda: registre.etat(v2["job_id"])["status"] == "done")
    assert registre.executeur.max_simultanes == 1


def test_l_ordre_de_soumission_est_respecte(registre):
    ids = [registre.soumettre("run_processing", {"cle": str(i)})["job_id"] for i in range(3)]
    for i in range(3):
        registre.executeur.ouvrir(str(i))
    assert _attendre(lambda: all(registre.etat(j)["status"] == "done" for j in ids))
    assert [a[1]["cle"] for a in registre.executeur.appels] == ["0", "1", "2"]


def test_meme_client_id_n_execute_pas_deux_fois(registre):
    v1 = registre.soumettre("execute_python", {"cle": "c"}, client_id="tour-1:appel-1")
    v2 = registre.soumettre("execute_python", {"cle": "c"}, client_id="tour-1:appel-1")
    assert v1["job_id"] == v2["job_id"]
    assert v2.get("deja_soumis") is True
    registre.executeur.ouvrir("c")
    assert _attendre(lambda: registre.etat(v1["job_id"])["status"] == "done")
    assert len(registre.executeur.appels) == 1


def test_client_id_rend_la_tache_meme_finie(registre):
    v1 = registre.soumettre("export_layer", {"cle": "d"}, client_id="k")
    registre.executeur.ouvrir("d")
    assert _attendre(lambda: registre.etat(v1["job_id"])["status"] == "done")
    v2 = registre.soumettre("export_layer", {"cle": "d"}, client_id="k")
    assert v2["job_id"] == v1["job_id"] and v2["status"] == "done"
    assert len(registre.executeur.appels) == 1


def test_le_battement_avance_pendant_l_execution(registre):
    vue = registre.soumettre("execute_python", {"cle": "e"})
    assert _attendre(lambda: registre.etat(vue["job_id"])["status"] == "running")
    time.sleep(0.2)
    assert registre.etat(vue["job_id"])["heartbeat_age_s"] < 0.15
    registre.executeur.ouvrir("e")


def test_une_tache_en_attente_s_annule(registre):
    v1 = registre.soumettre("execute_python", {"cle": "f1"})
    v2 = registre.soumettre("execute_python", {"cle": "f2"})
    assert _attendre(lambda: registre.etat(v1["job_id"])["status"] == "running")
    reponse = registre.annuler(v2["job_id"])
    assert reponse == {"success": True, "status": "cancelled"}
    registre.executeur.ouvrir("f1")
    registre.executeur.ouvrir("f2")
    assert _attendre(lambda: registre.etat(v1["job_id"])["status"] == "done")
    time.sleep(0.1)
    assert registre.etat(v2["job_id"])["status"] == "cancelled"
    assert [a[1]["cle"] for a in registre.executeur.appels] == ["f1"]


def test_une_tache_commencee_ne_peut_pas_etre_interrompue(registre):
    vue = registre.soumettre("execute_python", {"cle": "g"})
    assert _attendre(lambda: registre.etat(vue["job_id"])["status"] == "running")
    reponse = registre.annuler(vue["job_id"])
    assert reponse["success"] is False
    assert reponse["status"] == "already_dispatched_cannot_cancel"
    registre.executeur.ouvrir("g")


def test_annuler_une_tache_finie(registre):
    vue = registre.soumettre("execute_python", {"cle": "h"})
    registre.executeur.ouvrir("h")
    assert _attendre(lambda: registre.etat(vue["job_id"])["status"] == "done")
    assert registre.annuler(vue["job_id"])["status"] == "already_terminal"


def test_une_exception_de_l_outil_donne_une_erreur(registre):
    vue = registre.soumettre("execute_python", {"cle": "i", "lever": True})
    registre.executeur.ouvrir("i")
    assert _attendre(lambda: registre.etat(vue["job_id"])["status"] == "error")
    assert "panne simulee" in registre.etat(vue["job_id"])["error"]


def test_les_outils_de_suivi_sont_refuses(registre):
    for nom in REFUSES:
        assert "error" in registre.soumettre(nom, {})
    assert "execute_async" in REFUSES and "restart_qgis_engine" in REFUSES


def test_un_outil_inconnu_est_refuse(registre):
    assert registre.soumettre("inconnu", {})["error"].startswith("Unknown tool")


def test_un_identifiant_inconnu(registre):
    assert registre.etat("t-inexistant") is None
    assert registre.annuler("t-inexistant")["error"].startswith("Unknown job_id")


def test_le_suivi_ajoute_le_contenu_de_l_outil_une_fois_fini():
    etat = {"job_id": "t-1", "status": "done"}
    resultat = {"content": [{"type": "text", "text": "{\"a\": 1}"}]}
    contenu = contenu_de_suivi(etat, resultat)
    assert json.loads(contenu[0]["text"])["status"] == "done"
    assert contenu[1] == {"type": "text", "text": "{\"a\": 1}"}
    # Tant que ce n'est pas fini, seul l'etat est rendu.
    assert len(contenu_de_suivi({"status": "running"}, resultat)) == 1


def test_le_contexte_de_l_utilisateur_voyage_avec_la_tache():
    import contextvars
    courant = contextvars.ContextVar("courant", default="defaut")
    vus = []
    r = RegistreTachesOutils(lambda n, a: vus.append(courant.get()) or {"content": []},
                             periode_battement_s=0.01)
    jeton = courant.set("alice")
    try:
        vue = r.soumettre("smart_load", {})
    finally:
        courant.reset(jeton)
    assert _attendre(lambda: r.etat(vue["job_id"])["status"] == "done")
    assert vus == ["alice"]


def test_les_vieilles_taches_finies_sont_evincees():
    horloge = [1000.0]
    r = RegistreTachesOutils(lambda n, a: {"content": []}, max_lignes=2,
                             ttl_fini_s=10, periode_battement_s=0.01,
                             horloge=lambda: horloge[0])
    premiers = [r.soumettre("smart_load", {})["job_id"] for _ in range(2)]
    assert _attendre(lambda: all(r.etat(j)["status"] == "done" for j in premiers))
    horloge[0] += 60
    r.soumettre("smart_load", {})
    assert all(r.etat(j) is None for j in premiers)


# ── Le serveur MCP branche bien le registre ─────────────────────────────

@pytest.fixture
def mcp():
    import main_mcp
    return main_mcp


def test_execute_async_accepte_un_outil(mcp, monkeypatch):
    monkeypatch.setitem(mcp.TOOL_HANDLERS, "outil_essai",
                        lambda args: {"content": [{"type": "text", "text": "fini"}]})
    rendu = mcp.execute_tool("execute_async", {"tool": "outil_essai", "arguments": {},
                                               "client_id": "essai-1"})
    vue = json.loads(rendu["content"][0]["text"])
    assert est_id_de_tache(vue["job_id"])
    assert _attendre(lambda: json.loads(mcp.execute_tool(
        "poll_job", {"job_id": vue["job_id"]})["content"][0]["text"])["status"] == "done")
    suivi = mcp.execute_tool("poll_job", {"job_id": vue["job_id"]})["content"]
    assert suivi[1] == {"type": "text", "text": "fini"}


def test_poll_job_d_une_tache_inconnue(mcp):
    rendu = mcp.execute_tool("poll_job", {"job_id": "t-0000"})
    assert "Unknown job_id" in rendu["content"][0]["text"]


def test_execute_async_sans_outil_garde_l_ancien_contrat(mcp):
    rendu = mcp.execute_tool("execute_async", {})
    assert rendu.get("isError") is True
    assert "'code' is required" in rendu["content"][0]["text"]


def test_le_schema_expose_tool_arguments_et_client_id(mcp):
    schema = next(t for t in mcp.TOOLS if t["name"] == "execute_async")["inputSchema"]
    for champ in ("tool", "arguments", "client_id"):
        assert champ in schema["properties"]
