# -*- coding: utf-8 -*-
"""Le lien d'un livrable doit s'ouvrir dans un navigateur.

Signalement d'une utilisatrice le 2026-09-18 : « je n'arrive pas a charger ce
PDF, le lien ne fonctionne pas ». Le pont rendait
`http://localhost:8080/api/files/<nom>` -- `localhost` designe la machine de
qui clique, donc ce lien ne pouvait structurellement jamais s'ouvrir.

Ces tests EXERCENT le code au lieu de le lire : un test de presence n'aurait
pas vu que l'URL construite est fausse.
"""
import ast
import os
import textwrap
from pathlib import Path

import pytest

_RACINE = Path(__file__).resolve().parents[1]
_SRC = (_RACINE / "src" / "qgis_bridge.py").read_text(encoding="utf-8")
_HUB = "https://user-x-qgis.user.lab.sspcloud.fr"


def _pont():
    """Recupere les helpers du pont, sans avoir besoin de QGIS."""
    arbre = ast.parse(_SRC)
    voulus = {"_lien_hub", "_chemin_d_export", "lire_etude_active"}
    bouts = {n.name: ast.get_source_segment(_SRC, n) for n in ast.walk(arbre)
             if isinstance(n, ast.FunctionDef) and n.name in voulus}
    assert voulus == set(bouts), bouts.keys()
    espace = {"Path": Path, "os": os}
    for nom in ("lire_etude_active", "_lien_hub", "_chemin_d_export"):
        exec(textwrap.dedent(bouts[nom]), espace)

    class _Pont:
        _lien_hub = espace["_lien_hub"]
        _chemin_d_export = espace["_chemin_d_export"]

    return _Pont()


@pytest.fixture()
def pont(monkeypatch):
    monkeypatch.setenv("HUB_URL", _HUB)
    return _pont()


# ── Le lien s'ouvre ──────────────────────────────────────────────────────


def test_un_export_d_etude_donne_un_lien_d_etude(pont):
    """Le hub sert ce chemin, verifie en conditions reelles (200)."""
    assert pont._lien_hub("/data/studies/49bd/exports/pdf/synthese.pdf") == (
        _HUB + "/studies/49bd/file/exports/pdf/synthese.pdf")


def test_un_fichier_a_la_racine_donne_un_lien_de_fichier(pont):
    assert pont._lien_hub("/data/webmap_123.html") == _HUB + "/files/webmap_123.html"


def test_un_simple_nom_est_compris(pont):
    """Les appelants donnent tantot un chemin, tantot un nom."""
    assert pont._lien_hub("webmap_123.html") == _HUB + "/files/webmap_123.html"


def test_aucun_lien_ne_mentionne_localhost(pont):
    for chemin in ("/data/x.pdf", "/data/studies/s/exports/x.pdf", "x.pdf"):
        assert "localhost" not in pont._lien_hub(chemin)


# ── Plutot rien qu'un lien qui ment ──────────────────────────────────────


def test_un_fichier_hors_de_data_ne_donne_pas_de_lien(pont):
    assert pont._lien_hub("/tmp/ailleurs.pdf") == ""


def test_sans_adresse_de_hub_aucun_lien_n_est_invente(monkeypatch):
    monkeypatch.delenv("HUB_URL", raising=False)
    assert _pont()._lien_hub("/data/x.pdf") == ""


def test_un_chemin_vide_ne_donne_pas_de_lien(pont):
    assert pont._lien_hub("") == ""
