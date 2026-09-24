# -*- coding: utf-8 -*-
"""Decouper une couche au contour de la commune, pas au rectangle.

Incident S1 du 2026-09-24 (plan « comportements spatiaux », fiche §7) :
« uniquement les batis d'Aix-en-Provence ». La zone d'etude ne gardait que
le rectangle d'Aix, qui deborde sur les communes voisines (defaut T2).
L'agent a donc cherche le contour lui-meme en execute_python : sept
tentatives, trois couches `commune_aix` reduites a un point
([5.39, 43.54]), deux couches temporaires invalides, arret automatique sur
boucle d'erreurs -- et aucun decoupage (defauts T4, T5).

Desormais set_study_zone memorise le contour de la commune (geo.api.gouv.fr,
`fields=contour`), et clip_to_study_zone decoupe a ce contour, ecrit un
GeoPackage dans les donnees de l'etude et rend un bloc `verification`.

Tests sans QGIS (lecture de la source, fonctions pures executees), sauf la
classe marquee `container`.
"""
import ast
import textwrap
from pathlib import Path

import pytest

_RACINE = Path(__file__).resolve().parents[1]
_PONT = (_RACINE / "src" / "qgis_bridge.py").read_text(encoding="utf-8")
_AIDES = (_RACINE / "src" / "qgis_helpers.py").read_text(encoding="utf-8")
_MCP = (_RACINE / "main_mcp.py").read_text(encoding="utf-8")

_ZONE = _AIDES.split("def set_study_zone")[1].split("\ndef ")[0]
_CONTOUR = _AIDES.split("def get_study_zone_contour")[1].split("\ndef ")[0]
_DECOUPE = _PONT.split("def _action_clip_to_study_zone")[1].split("\n    def ")[0]


def _fonctions(source, *noms):
    arbre = ast.parse(source)
    bouts = {n.name: ast.get_source_segment(source, n) for n in ast.walk(arbre)
             if isinstance(n, ast.FunctionDef) and n.name in noms}
    assert set(noms) == set(bouts), set(noms) - set(bouts)
    espace = {}
    for nom in noms:
        exec(textwrap.dedent(bouts[nom]), espace)
    return espace


# Extrait reel de geo.api.gouv.fr/communes/13204?fields=nom,code,contour
# (Marseille 4e, 2026-09-24), reduit a un anneau ferme.
_ANNEAU = [[5.389079, 43.300309], [5.391321, 43.301012], [5.392576, 43.301431],
           [5.389079, 43.300309]]


# ── 1. La zone garde le contour ──────────────────────────────────────────

def test_polygone_geojson_vers_wkt():
    f = _fonctions(_AIDES, "_contour_geojson_vers_wkt")["_contour_geojson_vers_wkt"]
    wkt = f({"type": "Polygon", "coordinates": [_ANNEAU]})
    assert wkt.startswith("POLYGON ((5.389079 43.300309, ")
    assert wkt.endswith("5.389079 43.300309))")


def test_multipolygone_et_trou():
    f = _fonctions(_AIDES, "_contour_geojson_vers_wkt")["_contour_geojson_vers_wkt"]
    trou = [[5.3900, 43.3006], [5.3905, 43.3007], [5.3902, 43.3009], [5.3900, 43.3006]]
    wkt = f({"type": "MultiPolygon", "coordinates": [[_ANNEAU, trou], [_ANNEAU]]})
    assert wkt.startswith("MULTIPOLYGON (((")
    assert wkt.count("((") == 2, "deux polygones"
    assert "), (5.39 43.3006" in wkt, "le trou suit l'anneau exterieur"


@pytest.mark.parametrize("contour", [
    None, {}, {"type": "Point", "coordinates": [5.39, 43.54]},
    {"type": "Polygon", "coordinates": [[[5.39, 43.54], [5.4, 43.55]]]},
])
def test_pas_de_contour_sans_polygone(contour):
    """L'incident S1 : un « contour » reduit a un point ne vaut rien."""
    f = _fonctions(_AIDES, "_contour_geojson_vers_wkt")["_contour_geojson_vers_wkt"]
    assert f(contour) == ""


def test_le_contour_n_est_rendu_que_sur_demande():
    f = _fonctions(_AIDES, "_commune_to_result")["_commune_to_result"]
    reponse = {"nom": "Marseille 4e Arrondissement", "code": "13204",
               "contour": {"type": "Polygon", "coordinates": [_ANNEAU]}}
    assert "contour" not in f(reponse), "search_commune ordinaire : pas de 150 Ko de geometrie"
    assert f(reponse, avec_contour=True)["contour"]["type"] == "Polygon"
    assert f(reponse)["bbox"] == [5.389079, 43.300309, 5.392576, 43.301431]


def test_la_zone_demande_le_contour_et_le_memorise():
    assert "search_commune(target, avec_contour=True)" in _ZONE
    assert '"study_zone_contour_wkt", contour_wkt)' in _ZONE
    assert '"study_zone_contour_source", contour_source)' in _ZONE


def test_une_zone_sans_commune_efface_le_contour_precedent():
    """Scenario S3 : une nouvelle zone ne doit pas heriter de l'ancien contour.
    L'ecriture est inconditionnelle (au niveau de la fonction), et la valeur
    part vide pour une emprise, un point ou une adresse."""
    lignes = _ZONE.splitlines()
    ecriture = next(l for l in lignes if '"study_zone_contour_wkt", contour_wkt)' in l)
    assert ecriture.startswith("    Qgs"), "ecriture dans un bloc conditionnel"
    assert '    contour_wkt = ""' in _ZONE


def test_les_arrondissements_passent_par_le_code_insee():
    """geo.api.gouv.fr/communes/13204 rend le contour de l'arrondissement
    (verifie le 2026-09-24) ; search_commune y passe pour « Marseille 4e »."""
    bloc = _AIDES.split("def search_commune")[1].split("\ndef ")[0]
    assert "_commune_by_insee(insee, avec_contour=avec_contour)" in bloc
    assert "contour" in _AIDES.split("def _commune_by_insee")[1].split("\ndef ")[0]


def test_sans_contour_l_erreur_dit_quoi_faire():
    assert "set_study_zone" in _CONTOUR
    assert "Marseille 4e" in _CONTOUR
    assert "n'a pas de contour" in _CONTOUR


def test_la_zone_annonce_le_contour():
    assert '"contour": contour_source or None' in _ZONE
    assert "clip_to_study_zone" in _ZONE


# ── 2. Le decoupage ──────────────────────────────────────────────────────

def test_le_decoupage_part_du_contour_memorise():
    assert "qgis_helpers.get_study_zone_contour()" in _DECOUPE
    assert '"native:clip"' in _DECOUPE
    assert "bbox" not in _DECOUPE.split("proc.run(")[1].split(")")[0]


def test_le_resultat_est_un_geopackage_de_l_etude():
    assert "self._dossier_donnees_etude()" in _DECOUPE
    assert 'options.driverName = "GPKG"' in _DECOUPE
    assert "writeAsVectorFormatV3" in _DECOUPE
    bloc = _PONT.split("def _dossier_donnees_etude")[1].split("\n    def ")[0]
    assert 'Path("/data/studies") / etude / "data"' in bloc


def test_la_sortie_n_ecrase_pas_l_entree():
    assert "La sortie ecraserait la couche d'entree" in _DECOUPE


def test_un_nouveau_decoupage_remplace_le_precedent():
    assert "projet.removeMapLayers(remplacees)" in _DECOUPE


@pytest.mark.parametrize("texte, attendu", [
    ("batiment_Aix-en-Provence", "batiment_aix_en_provence"),
    ("Bâtiments (BD TOPO)_Marseille 4e Arrondissement",
     "batiments_bd_topo_marseille_4e_arrondissement"),
    ("  ", ""),
])
def test_le_nom_par_defaut_est_normalise(texte, attendu):
    f = _fonctions(_PONT, "_nom_normalise")["_nom_normalise"]
    assert f(texte) == attendu


def test_la_verification_du_decoupage():
    for cle in ('"avant"', '"apres"', '"retirees"', '"contour"', '"zone_etude"',
                '"fichier"', '"filtre"'):
        assert cle in _DECOUPE, cle
    assert 'self._retenir_verification("clip_to_study_zone", [verification])' in _DECOUPE
    assert "Aucune entite dans le contour" in _DECOUPE
    assert '"rien_retire"' in _DECOUPE
    assert '"rien_retire"' in _PONT.split("_ECHECS_DE_DONNEE = ")[1].split(")")[0]


def _seuil_debordement():
    ligne = next(l for l in _PONT.splitlines()
                 if l.strip().startswith("_RAPPORT_DEBORDEMENT_SUSPECT = "))
    return float(ligne.split("=")[1])


def _rapport(zone, couche):
    """Reproduit le calcul de _emprise_et_rapport."""
    aire_zone = (zone[2] - zone[0]) * (zone[3] - zone[1])
    return (couche[2] - couche[0]) * (couche[3] - couche[1]) / aire_zone


_AIX = [5.269537, 43.446031, 5.506288, 43.62598]


def test_un_chargement_par_rectangle_qui_ne_perd_rien_n_est_pas_suspect():
    """Les batiments qui touchent le rectangle le debordent un peu."""
    deborde = [_AIX[0] - 0.01, _AIX[1] - 0.01, _AIX[2] + 0.01, _AIX[3] + 0.01]
    assert _rapport(_AIX, deborde) < _seuil_debordement()


def test_un_decoupage_sans_effet_sur_le_departement_est_suspect():
    bouches_du_rhone = [4.23, 43.16, 5.81, 43.92]
    assert _rapport(_AIX, bouches_du_rhone) > _seuil_debordement()


# ── 3. L'outil est expose ────────────────────────────────────────────────

def test_l_outil_est_declare_et_branche():
    assert '"name": "clip_to_study_zone"' in _MCP
    assert '"clip_to_study_zone": _tool_clip_to_study_zone,' in _MCP
    bloc = _MCP.split("def _tool_clip_to_study_zone")[1].split("\ndef ")[0]
    assert "timeout=SOCKET_TIMEOUT_LONG" in bloc


def test_l_outil_est_long_pour_les_recettes_et_mutant_pour_le_pont():
    longues = _MCP.split("_LONG_TIMEOUT_ACTIONS = frozenset({")[1].split("})")[0]
    assert '"clip_to_study_zone"' in longues
    mutantes = _PONT.split("_MUTATING_ACTIONS = frozenset({")[1].split("})")[0]
    assert '"clip_to_study_zone"' in mutantes


def test_la_description_dit_contour_et_verification():
    bloc = _MCP.split('"name": "clip_to_study_zone"')[1].split('"name":')[0]
    assert "CONTOUR" in bloc and "verification" in bloc
    assert "GeoPackage" in bloc


# ── 4. Contrat en conteneur ──────────────────────────────────────────────

@pytest.mark.container
class TestDansLeConteneur:
    """Deux points dans le contour, un dehors : le decoupage en retire un."""

    def test_decoupe_au_contour(self, tmp_path, monkeypatch):
        qgis_core = pytest.importorskip("qgis.core")
        import sys
        if qgis_core.QgsApplication.instance() is None:
            app = qgis_core.QgsApplication([], False)
            app.initQgis()
        sys.path.insert(0, str(_RACINE / "src"))
        # Le module demarre son ecoute de socket a l'import : on n'en execute
        # que la partie qui precede, pour ne pas disputer la socket au pont
        # en service.
        espace = {"__name__": "qgis_bridge_essai"}
        exec(compile(_PONT.split("# ── Start bridge")[0], "qgis_bridge.py", "exec"), espace)
        qgis_bridge = type("M", (), {"QGISBridge": espace["QGISBridge"]})
        projet = qgis_core.QgsProject.instance()
        u = qgis_core.QgsExpressionContextUtils
        u.setProjectVariable(projet, "study_zone_name", "Carre")
        u.setProjectVariable(projet, "study_zone_bbox_4326", "[5.0, 43.0, 5.1, 43.1]")
        u.setProjectVariable(projet, "study_zone_contour_wkt",
                             "POLYGON ((5 43, 5.1 43, 5.1 43.1, 5 43.1, 5 43))")
        u.setProjectVariable(projet, "study_zone_contour_source", "test")
        couche = qgis_core.QgsVectorLayer("Point?crs=EPSG:4326", "points", "memory")
        entites = []
        for x, y in ((5.05, 43.05), (5.02, 43.08), (5.5, 43.5)):
            f = qgis_core.QgsFeature()
            f.setGeometry(qgis_core.QgsGeometry.fromPointXY(qgis_core.QgsPointXY(x, y)))
            entites.append(f)
        couche.dataProvider().addFeatures(entites)
        projet.addMapLayer(couche)
        monkeypatch.setattr(qgis_bridge.QGISBridge, "_dossier_donnees_etude",
                            staticmethod(lambda: tmp_path))
        r = qgis_bridge.QGISBridge()._action_clip_to_study_zone(
            {"layer_id": couche.id()})
        assert r.get("success"), r
        v = r["verification"]
        assert (v["avant"], v["apres"], v["retirees"]) == (3, 2, 1)
        assert r["name"] == "points_carre"
        assert Path(r["path"]).parent == tmp_path
