# -*- coding: utf-8 -*-
"""Densite par maille et comptage par zone : des outils, pas du PyQGIS.

Constat live du 2026-10-02 : « Sur Rousset, affiche les trames vertes et
bleues, la densite batie et les reseaux ». Chargements corrects, puis trois
execute_python pour une densite batie par maille ; le dernier a depasse
12 minutes (boucle entite par entite, sans index spatial). QGIS fait le meme
calcul en quelques secondes avec native:creategrid et
native:countpointsinpolygon sur les centroides.

densite_par_maille et compter_par_zone enchainent ces algorithmes, ecrivent
un GeoPackage dans les donnees de l'etude, appliquent un style gradue et
rendent un bloc `verification` (total compte = total dans l'emprise).

Tests sans QGIS (lecture de la source, fonctions pures executees), sauf la
classe marquee `container`.
"""
import ast
import textwrap
from pathlib import Path

import pytest

_RACINE = Path(__file__).resolve().parents[1]
_PONT = (_RACINE / "src" / "qgis_bridge.py").read_text(encoding="utf-8")
_MCP = (_RACINE / "main_mcp.py").read_text(encoding="utf-8")


def _methode(nom):
    return _PONT.split(f"def {nom}(")[1].split("\n    def ")[0]


def _fonctions(source, *noms):
    arbre = ast.parse(source)
    bouts = {n.name: ast.get_source_segment(source, n) for n in ast.walk(arbre)
             if isinstance(n, ast.FunctionDef) and n.name in noms}
    assert set(noms) == set(bouts), set(noms) - set(bouts)
    espace = {}
    for nom in noms:
        exec(textwrap.dedent(bouts[nom]), espace)
    return espace


def _pure(nom):
    return _fonctions(_PONT, nom)[nom]


def _description(outil):
    bloc = _MCP.split(f'"name": "{outil}"')[1]
    return bloc.split('"description": "')[1].split('",\n')[0]


_OUTILS = ("densite_par_maille", "compter_par_zone")


# ── 1. Les outils sont exposes ───────────────────────────────────────────

@pytest.mark.parametrize("outil", _OUTILS)
def test_l_outil_est_declare_et_branche(outil):
    assert f'"name": "{outil}"' in _MCP
    assert f'"{outil}": _tool_{outil},' in _MCP
    bloc = _MCP.split(f"def _tool_{outil}")[1].split("\ndef ")[0]
    assert f'qgis_command("{outil}"' in bloc
    assert "timeout=SOCKET_TIMEOUT_LONG" in bloc
    assert f"def _action_{outil}(self, params: dict)" in _PONT


@pytest.mark.parametrize("outil", _OUTILS)
def test_l_outil_est_long_pour_les_recettes_et_mutant_pour_le_pont(outil):
    longues = _MCP.split("_LONG_TIMEOUT_ACTIONS = frozenset({")[1].split("})")[0]
    assert f'"{outil}"' in longues
    mutantes = _PONT.split("_MUTATING_ACTIONS = frozenset({")[1].split("})")[0]
    assert f'"{outil}"' in mutantes


@pytest.mark.parametrize("outil", _OUTILS)
def test_la_description_dit_natif_et_verification(outil):
    d = _description(outil)
    assert "`verification`" in d
    assert "JAMAIS une boucle PyQGIS" in d
    assert "native:countpointsinpolygon" in d
    assert "GeoPackage" in d and "style gradue" in d
    # Chaque schema expose coute du contexte a chaque iteration de l'agent.
    assert len(d) < 800


def test_la_densite_annonce_maille_emprise_et_mesures():
    d = _description("densite_par_maille")
    assert "taille_m, defaut 200" in d
    assert "contour de la zone d'etude" in d
    for mesure in ("nombre", "surface", "part_surface"):
        assert mesure in d


def test_execute_python_renvoie_vers_les_outils_de_comptage():
    d = _description("execute_python")
    assert "densite_par_maille" in d and "compter_par_zone" in d
    assert len(d) < 1000


def _outil_mcp(nom):
    arbre = ast.parse(_MCP)
    voulus = {f"_tool_{nom}", "_params_de_comptage"}
    bouts = [ast.get_source_segment(_MCP, n) for n in ast.walk(arbre)
             if isinstance(n, ast.FunctionDef) and n.name in voulus]
    assert len(bouts) == 2
    envoyes = []
    espace = {
        "qgis_command": lambda action, params, timeout=None: envoyes.append((action, params)) or {},
        "_text": lambda r, indent=None: "ok", "_auto_screenshot": lambda: "",
        "_error": lambda m: {"isError": True, "message": m},
        "SOCKET_TIMEOUT_LONG": 600,
    }
    for code in bouts:
        exec(textwrap.dedent(code), espace)
    return espace[f"_tool_{nom}"], envoyes


def test_l_outil_densite_transmet_ses_parametres():
    outil, envoyes = _outil_mcp("densite_par_maille")
    outil({"layer": "batiment_rousset", "taille_m": 100, "mesure": "part_surface",
           "emprise": "", "name": None})
    assert envoyes == [("densite_par_maille", {"layer": "batiment_rousset",
                                               "taille_m": 100,
                                               "mesure": "part_surface"})]
    assert outil({"taille_m": 200})["isError"]


def test_l_outil_par_zone_exige_les_zones():
    outil, envoyes = _outil_mcp("compter_par_zone")
    erreur = outil({"layer": "batiment_rousset"})
    assert erreur["isError"] and "zones" in erreur["message"]
    outil({"layer_id": "bati_1", "zones": "IRIS Rousset"})
    assert envoyes == [("compter_par_zone", {"layer_id": "bati_1",
                                             "zones": "IRIS Rousset"})]


# ── 2. La chaine est native et indexee ───────────────────────────────────

_CHAINE = _methode("_compter_par_polygones")
_DENSITE = _methode("_action_densite_par_maille")
_PAR_ZONE = _methode("_action_compter_par_zone")
_FIN = _methode("_finir_comptage")


def test_le_comptage_passe_par_les_algorithmes_natifs():
    for algo in ("native:centroids", "native:extractbylocation",
                 "native:createspatialindex", "native:countpointsinpolygon"):
        assert f'"{algo}"' in _CHAINE, algo
    assert '"native:creategrid"' in _DENSITE


def test_aucune_boucle_python_sur_les_entites_source():
    """Le defaut du 2026-10-02 : un parcours entite par entite."""
    assert "getFeatures" not in _CHAINE
    assert "getFeatures" not in _DENSITE and "getFeatures" not in _PAR_ZONE
    assert "for " not in _CHAINE.split("formules = ")[0].split('"native:centroids"')[1]


def test_la_surface_somme_le_poids_des_centroides():
    assert '"WEIGHT": "_surface_m2"' in _CHAINE
    assert "area($geometry)" in _CHAINE
    assert "min(100" in _CHAINE, "une part de surface ne depasse pas 100 %"


def test_la_grille_est_projetee_en_metres_et_bornee():
    assert "self._crs_metrique(couche)" in _DENSITE
    assert 'QgsCoordinateReferenceSystem("EPSG:2154")' in _methode("_crs_metrique")
    assert "QgsUnitTypes.DistanceMeters" in _methode("_crs_metrique")
    assert "self._PLAFOND_MAILLES" in _DENSITE
    assert "taille_conseillee_m" in _DENSITE


def test_l_emprise_est_le_contour_puis_le_rectangle_puis_la_couche():
    masque = _methode("_masque_de_zone")
    contour = masque.index("get_study_zone_contour()")
    rectangle = masque.index("QgsGeometry.fromRect")
    assert contour < rectangle
    assert "l'emprise de la couche" in masque
    assert "self._masque_de_zone(couche, crs, emprise)" in _DENSITE


def test_les_mailles_hors_du_contour_sont_retirees():
    apres_grille = _DENSITE.split('"native:creategrid"')[1]
    assert '"native:extractbylocation"' in apres_grille.split("_compter_par_polygones")[0]


def test_la_sortie_est_un_geopackage_de_l_etude():
    ecrire = _methode("_ecrire_couche_d_etude")
    assert "self._dossier_donnees_etude()" in ecrire
    assert 'options.driverName = "GPKG"' in ecrire
    assert "projet.removeMapLayers(remplacees)" in ecrire
    assert "La sortie ecraserait une couche" in ecrire
    assert "self._ecrire_couche_d_etude(comptees, nom, [couche])" in _DENSITE
    assert "[couche, zones]" in _PAR_ZONE


def test_le_style_est_gradue():
    style = _methode("_styler_comptage")
    assert "QgsGraduatedSymbolRenderer(champ, classes)" in style
    assert '"0 (vide)"' in style
    assert "self._styler_comptage(" in _FIN


def test_la_verification_du_comptage():
    for cle in ('"total_source"', '"total_source_dans_emprise"', '"hors_emprise"',
                '"emprise"', '"taille_maille_m"', '"crs_calcul"', '"fichier"'):
        assert cle in _DENSITE, cle
    for cle in ('"total_compte"', '"lecture"', '"duree_s"', '"ecart"'):
        assert cle in _FIN, cle
    assert "self._retenir_verification(outil, [verification])" in _FIN
    assert '"rien_compte"' in _FIN and '"ecart_comptage"' in _FIN
    assert '"rien_compte"' in _PONT.split("_ECHECS_DE_DONNEE = ")[1].split(")")[0]


def test_les_zones_gardent_leurs_champs():
    """Une couche de quartiers peut deja porter un champ « nombre »."""
    assert 'self._champ_libre(noms, "nombre")' in _PAR_ZONE
    f = _pure("_champ_libre")
    assert f(["id", "nom"], "nombre") == "nombre"
    assert f(["id", "NOMBRE", "nombre_2"], "nombre") == "nombre_3"


# ── 3. Fonctions pures ───────────────────────────────────────────────────

@pytest.mark.parametrize("emprise, taille, attendu, n", [
    ((893_120, 6_270_050, 894_310, 6_271_180), 200,
     (893_000, 6_270_000, 894_400, 6_271_200), 7 * 6),
    ((0, 0, 400, 400), 200, (0, 0, 400, 400), 4),
    ((10, 10, 10, 10), 200, (0, 0, 200, 200), 1),
])
def test_la_grille_est_calee_sur_la_maille(emprise, taille, attendu, n):
    cadre, mailles = _pure("_grille_alignee")(*emprise, taille)
    assert cadre == attendu and mailles == n


@pytest.mark.parametrize("largeur, hauteur", [(60_000, 40_000), (12_000, 9_000),
                                              (150_000, 150_000)])
def test_la_taille_conseillee_tient_sous_le_plafond(largeur, hauteur):
    plafond = 250_000
    taille = _pure("_taille_pour_plafond")(largeur, hauteur, plafond)
    assert taille % 50 == 0
    _, n = _pure("_grille_alignee")(0.5, 0.5, largeur + 0.5, hauteur + 0.5, taille)
    assert n <= plafond


def test_les_statistiques_des_mailles():
    s = _pure("_statistiques_mailles")([0, 0, 3, 1, 7, 0, 4, None])
    assert s == {"mailles": 8, "vides": 4, "occupees": 4, "min": 0, "max": 7,
                 "mediane": 0.5, "mediane_occupees": 3.5}
    assert _pure("_statistiques_mailles")([]) == {"mailles": 0, "vides": 0,
                                                  "occupees": 0}


def test_les_classes_ignorent_les_mailles_vides():
    bornes = _pure("_bornes_classes")([0] * 50 + list(range(1, 101)))
    assert bornes[0][0] == 1 and bornes[-1][1] == 100
    assert len(bornes) == 5
    assert all(b[0] < b[1] for b in bornes)
    assert all(bornes[i][1] == bornes[i + 1][0] for i in range(len(bornes) - 1))


def test_des_comptes_repetes_fusionnent_les_classes():
    f = _pure("_bornes_classes")
    assert f([0, 1, 1, 1, 1, 2]) == [(1.0, 2.0)]
    assert f([0, 3, 3]) == [(3.0, 3.0)]
    assert f([0, 0]) == []


@pytest.mark.parametrize("x, attendu", [(12345, "12 345"), (4.0, "4"),
                                        (1234.56, "1 234,6"), (None, "?")])
def test_les_nombres_a_la_francaise(x, attendu):
    assert _pure("_nombre_fr")(x) == attendu


_V = {"cadre": "Grille de 512 mailles carrees de 200 m sur le contour de Rousset",
      "source": "batiment_rousset", "unite": "maille", "mesure": "nombre",
      "total_compte": 8412, "total_source_dans_emprise": 8412, "hors_emprise": 0,
      "mailles": 512, "vides": 140, "min": 0, "max": 87, "mediane": 9,
      "mediane_occupees": 14, "densite_max_km2": 2175}


def test_la_lecture_dit_ce_qui_a_ete_compte():
    lecture = _pure("_lecture_comptage")(_V)
    assert lecture.startswith("Grille de 512 mailles carrees de 200 m sur le "
                              "contour de Rousset : 8 412 entite(s)")
    assert "toutes celles de la couche dans cette emprise" in lecture
    assert "140 maille(s) vide(s) sur 512" in lecture
    assert "de 0 a 87, mediane 9 (14 parmi les mailles occupees)" in lecture
    assert "2 175 par km2" in lecture
    assert "hors de" not in lecture


def test_la_lecture_signale_un_ecart():
    lecture = _pure("_lecture_comptage")(dict(_V, total_compte=8400, hors_emprise=1203))
    assert "sur 8 412 dans cette emprise : 12 non attribuee(s)" in lecture
    assert "Signale cet ecart" in lecture
    assert "1 203 entite(s) de la couche sont hors de cette emprise" in lecture


def test_la_lecture_signale_des_zones_qui_se_chevauchent():
    v = dict(_V, unite="zone", cadre="12 zones de « IRIS »", total_compte=9000,
             densite_max_km2=None)
    lecture = _pure("_lecture_comptage")(v)
    assert "des zones se chevauchent" in lecture and "Ne somme pas" in lecture
    assert "par km2" not in lecture


def test_la_lecture_d_une_surface_dit_l_approximation():
    v = dict(_V, mesure="part_surface", max=64.5, mediane=12.25,
             mediane_occupees=18, densite_max_km2=None)
    lecture = _pure("_lecture_comptage")(v)
    assert "Part de surface couverte (%) par maille : de 0 a 64,5" in lecture
    assert "maille de son centroide (approximation)" in lecture


def test_une_mesure_inconnue_est_refusee_en_clair():
    assert "Mesure inconnue" in _methode("_parametres_de_comptage")
    assert "demande des polygones" in _DENSITE and "demande des polygones" in _PAR_ZONE


# ── 4. Contrat en conteneur ──────────────────────────────────────────────

def _pont_de_conteneur(tmp_path, monkeypatch):
    qgis_core = pytest.importorskip("qgis.core")
    import sys
    if qgis_core.QgsApplication.instance() is None:
        app = qgis_core.QgsApplication([], False)
        app.initQgis()
    sys.path.insert(0, str(_RACINE / "src"))
    # Comme test_clip_zone_etude : on n'execute que ce qui precede l'ecoute de
    # la socket, pour ne pas la disputer au pont en service.
    espace = {"__name__": "qgis_bridge_essai"}
    exec(compile(_PONT.split("# ── Start bridge")[0], "qgis_bridge.py", "exec"), espace)
    pont = espace["QGISBridge"]
    monkeypatch.setattr(pont, "_dossier_donnees_etude", staticmethod(lambda: tmp_path))
    projet = qgis_core.QgsProject.instance()
    u = qgis_core.QgsExpressionContextUtils
    # Les essais comptent sur l'emprise de la couche : pas de contour herite
    # d'un autre test.
    u.setProjectVariable(projet, "study_zone_name", "Essai")
    u.setProjectVariable(projet, "study_zone_contour_wkt", "")
    return qgis_core, pont, projet


def _points(qgis_core, projet, coords, nom="points"):
    couche = qgis_core.QgsVectorLayer("Point?crs=EPSG:2154", nom, "memory")
    entites = []
    for x, y in coords:
        f = qgis_core.QgsFeature()
        f.setGeometry(qgis_core.QgsGeometry.fromPointXY(qgis_core.QgsPointXY(x, y)))
        entites.append(f)
    couche.dataProvider().addFeatures(entites)
    couche.updateExtents()
    projet.addMapLayer(couche)
    return couche


@pytest.mark.container
class TestDansLeConteneur:

    def test_quatre_mailles_et_le_total(self, tmp_path, monkeypatch):
        qgis_core, pont, projet = _pont_de_conteneur(tmp_path, monkeypatch)
        coords = [(900_050, 6_270_050), (900_060, 6_270_070), (900_150, 6_270_350),
                  (900_350, 6_270_150), (900_390, 6_270_390)]
        couche = _points(qgis_core, projet, coords)
        r = pont()._action_densite_par_maille(
            {"layer_id": couche.id(), "taille_m": 200, "emprise": "couche"})
        assert r.get("success"), r
        v = r["verification"]
        assert v["mailles"] == 4
        assert v["total_compte"] == v["total_source_dans_emprise"] == 5
        assert (v["min"], v["max"], v["vides"]) == (1, 2, 0)
        assert Path(r["path"]).parent == tmp_path
        produite = projet.mapLayer(r["layer_id"])
        assert produite.renderer().type() == "graduatedSymbol"

    def test_cent_mille_entites_en_quelques_secondes(self, tmp_path, monkeypatch):
        import random
        import time
        qgis_core, pont, projet = _pont_de_conteneur(tmp_path, monkeypatch)
        alea = random.Random(13)
        coords = [(900_000 + alea.random() * 5_000, 6_270_000 + alea.random() * 4_000)
                  for _ in range(100_000)]
        couche = _points(qgis_core, projet, coords, "cent_mille")
        debut = time.time()
        r = pont()._action_densite_par_maille(
            {"layer_id": couche.id(), "taille_m": 200, "emprise": "couche"})
        duree = time.time() - debut
        assert r.get("success"), r
        assert r["verification"]["total_compte"] == 100_000
        # 12 minutes en PyQGIS entite par entite le 2026-10-02.
        assert duree < 60, f"{duree:.1f} s"

    def test_compter_par_zone(self, tmp_path, monkeypatch):
        qgis_core, pont, projet = _pont_de_conteneur(tmp_path, monkeypatch)
        couche = _points(qgis_core, projet,
                         [(10, 10), (20, 20), (150, 50), (500, 500)])
        zones = qgis_core.QgsVectorLayer("Polygon?crs=EPSG:2154&field=nombre:integer",
                                         "quartiers", "memory")
        entites = []
        for wkt in ("POLYGON ((0 0, 100 0, 100 100, 0 100, 0 0))",
                    "POLYGON ((100 0, 200 0, 200 100, 100 100, 100 0))"):
            f = qgis_core.QgsFeature(zones.fields())
            f.setGeometry(qgis_core.QgsGeometry.fromWkt(wkt))
            f.setAttributes([-1])
            entites.append(f)
        zones.dataProvider().addFeatures(entites)
        projet.addMapLayer(zones)
        r = pont()._action_compter_par_zone(
            {"layer_id": couche.id(), "zones": "quartiers"})
        assert r.get("success"), r
        v = r["verification"]
        assert v["champ"] == "nombre_2", "le champ existant est garde"
        assert (v["total_compte"], v["total_source_dans_emprise"], v["hors_emprise"]) == (3, 3, 1)
