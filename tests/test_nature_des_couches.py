# -*- coding: utf-8 -*-
"""Une couche et un fichier ne sont pas la meme chose.

Trois natures coexistent dans un meme projet -- mesure du 2026-09-18 sur
l'instance de production, trois couches, trois natures differentes :

    Batiments (BD TOPO)   /data/studies/<sid>/data/batiment_*.gpkg
    CartoDB Dark Matter   crs=EPSG:3857&type=xyz&url=https://...
    Grille hexagonale     memory?geometry=Polygon&crs=EPSG:2154

La troisieme n'existe sur AUCUN disque : elle disparait au prochain
redemarrage de QGIS, sans avertissement -- et c'est typiquement le resultat
d'un traitement qu'on vient de calculer. Rien ne le disait a l'agent, qui
traitait donc un resultat volatile comme un acquis.
"""
import ast
import os
import tempfile
import textwrap
from pathlib import Path

import pytest

_RACINE = Path(__file__).resolve().parents[1]
_SRC = (_RACINE / "src" / "qgis_bridge.py").read_text(encoding="utf-8")


class _Couche:
    def __init__(self, source):
        self._source = source

    def source(self):
        return self._source


@pytest.fixture()
def pont():
    arbre = ast.parse(_SRC)
    voulus = {"_origine_de_la_couche", "lire_etude_active"}
    bouts = {n.name: ast.get_source_segment(_SRC, n) for n in ast.walk(arbre)
             if isinstance(n, ast.FunctionDef) and n.name in voulus}
    assert voulus == set(bouts), bouts.keys()
    espace = {"Path": Path, "os": os}
    for nom in ("lire_etude_active", "_origine_de_la_couche"):
        exec(textwrap.dedent(bouts[nom]), espace)

    class _Pont:
        _origine_de_la_couche = espace["_origine_de_la_couche"]

    return _Pont()


# ── Les trois natures ────────────────────────────────────────────────────


def test_une_couche_en_memoire_est_annoncee_volatile(pont):
    r = pont._origine_de_la_couche(
        _Couche("memory?geometry=Polygon&crs=EPSG:2154&field=id:int8"))
    assert r["origine"] == "memoire"
    assert r["perdue_au_redemarrage"] is True
    assert "export_layer" in r["conseil"], "il faut dire comment la garder"


@pytest.mark.parametrize("source", [
    "crs=EPSG:3857&format&type=xyz&url=https://cartodb-basemaps-a.global.ssl.fastly.net/",
    "url='https://data.geopf.fr/wfs/ows' typename='BDTOPO_V3:batiment'",
])
def test_une_couche_distante_est_reconnue(pont, source):
    r = pont._origine_de_la_couche(_Couche(source))
    assert r["origine"] == "service distant"
    assert "perdue_au_redemarrage" not in r


def test_une_couche_de_fichier_nomme_son_fichier(pont):
    chemin = tempfile.mkstemp(suffix=".gpkg")[1]
    r = pont._origine_de_la_couche(_Couche(chemin + "|layername=x"))
    assert r["origine"] == "fichier"
    assert r["fichier"] == chemin, "le suffixe |layername ne fait pas partie du chemin"
    assert r["fichier_present"] is True
    assert "avertissement" not in r


# ── Le cas qui ne se voit pas dans la legende ────────────────────────────


def test_un_fichier_disparu_est_signale(pont):
    """Une telle couche s'affiche encore : rien ne dit qu'elle est morte."""
    r = pont._origine_de_la_couche(
        _Couche("/data/studies/xxx/data/disparu.gpkg|layername=x"))
    assert r["fichier_present"] is False
    assert "avertissement" in r


# ── C'est bien rendu a l'agent ───────────────────────────────────────────


def test_chaque_couche_porte_son_origine():
    bloc = _SRC.split("def _action_get_project_info")[1].split("\n    def ")[0]
    assert "self._origine_de_la_couche(layer)" in bloc


def test_l_agent_sait_quoi_faire_de_chaque_nature():
    """Rendre l'information ne suffit pas : il faut dire ce qu'elle implique."""
    mcp = (_RACINE / "main_mcp.py").read_text(encoding="utf-8")
    bloc = mcp.split('"name": "get_project_info"')[1][:2000]
    for attendu in ("origine", "memoire", "smart_load", "export_layer"):
        assert attendu in bloc, attendu


def test_l_agent_est_prevenu_que_fichiers_et_couches_different():
    mcp = (_RACINE / "main_mcp.py").read_text(encoding="utf-8")
    bloc = mcp.split('"name": "get_project_info"')[1][:2000]
    assert "Ne confonds pas une couche avec un fichier" in bloc
    assert "list_files" in bloc
