# -*- coding: utf-8 -*-
"""Toute couche produite porte un bloc `verification`.

Strategie qualite (2026-09-24, §2.1) : un outil qui cree une couche rend un
bloc calcule par l'outil lui-meme, jamais par le LLM. smart_load le faisait
depuis QgisRemoteMCP#4 ; run_processing, run_recipe et execute_python non.

Incident S1 du plan « comportements spatiaux », meme jour : l'agent a laisse
derriere lui `bati_temp` et `communes_temp` (invalides, zero entite), creees
par des execute_python en echec, et une couche de 51 450 444 entites a
l'emprise de la France et de l'outre-mer. Rien, dans les retours d'outils,
ne le disait.

Ces tests ne demandent pas QGIS : les fonctions du pont sont extraites de la
source et executees avec des doublures.
"""
import ast
import math
import os
import textwrap
from pathlib import Path

import pytest

_RACINE = Path(__file__).resolve().parents[1]
_PONT = (_RACINE / "src" / "qgis_bridge.py").read_text(encoding="utf-8")
_MCP = (_RACINE / "main_mcp.py").read_text(encoding="utf-8")

_AIX = [5.269537, 43.446031, 5.506288, 43.62598]
_FRANCE_ET_OUTRE_MER = [-63.28, -21.77, 55.9, 51.97]


def _methode(nom):
    return _PONT.split(f"def {nom}")[1].split("\n    def ")[0]


def _constante(nom):
    ligne = next(l for l in _PONT.splitlines() if l.startswith(f"{nom} = "))
    return int(ligne.split("=")[1].strip().replace("_", ""))


# ── Doublures ────────────────────────────────────────────────────────────

class _Crs:
    def __init__(self, authid="EPSG:2154"):
        self._authid = authid

    def isValid(self):
        return bool(self._authid)

    def authid(self):
        return self._authid


class _Geometrie:
    def __init__(self, valide=True):
        self._valide = valide

    def isNull(self):
        return False

    def isGeosValid(self):
        return self._valide


class _Entite:
    def __init__(self, valide=True):
        self._g = _Geometrie(valide)

    def geometry(self):
        return self._g


class _Requete:
    def setNoAttributes(self):
        return self


class _Vecteur:
    """Couche vecteur minimale ; `emprise` en EPSG:4326."""

    def __init__(self, n=10, source="/data/studies/s/data/bati.gpkg|layername=bati",
                 emprise=None, invalides=0, crs="EPSG:2154", lid="l1", nom="bati"):
        self._n, self._source, self.emprise = n, source, emprise
        self._invalides, self._crs, self._id, self._nom = invalides, crs, lid, nom
        self.parcourue = False

    def id(self):
        return self._id

    def name(self):
        return self._nom

    def source(self):
        return self._source

    def featureCount(self):
        return self._n

    def crs(self):
        return _Crs(self._crs)

    def getFeatures(self, requete=None):
        self.parcourue = True
        return iter([_Entite(False)] * self._invalides
                    + [_Entite(True)] * (self._n - self._invalides))


def _emprise_et_rapport(couche, zone):
    """Reproduit le calcul de _emprise_et_rapport sans transformation."""
    e = couche.emprise
    if not e:
        return None, None
    rapport = None
    if zone:
        aire_zone = (zone[2] - zone[0]) * (zone[3] - zone[1])
        rapport = ((e[2] - e[0]) * (e[3] - e[1])) / aire_zone
    return e, rapport


@pytest.fixture()
def pont():
    arbre = ast.parse(_PONT)
    voulus = {"_verification_couche", "_geometries_invalides",
              "_verifier_couches", "_retenir_verification",
              "_origine_de_la_couche", "lire_etude_active",
              "_verification_chargement", "_couche_depuis_fichier_de_sortie",
              "_lecture_emprise"}
    bouts = {n.name: ast.get_source_segment(_PONT, n) for n in ast.walk(arbre)
             if isinstance(n, ast.FunctionDef) and n.name in voulus}
    assert voulus == set(bouts), voulus - set(bouts)
    espace = {
        "Path": Path, "os": os,
        "QgsVectorLayer": _Vecteur, "QgsRasterLayer": type("R", (), {}),
        "QgsFeatureRequest": _Requete,
        "_RATIO_EMPRISE_ABERRANTE": _constante("_RATIO_EMPRISE_ABERRANTE"),
        "_PLAFOND_VALIDATION_GEOMETRIES": _constante("_PLAFOND_VALIDATION_GEOMETRIES"),
        "_BUDGET_VALIDATION_APPEL": _constante("_BUDGET_VALIDATION_APPEL"),
        "_PLAFOND_VERIFICATIONS": _constante("_PLAFOND_VERIFICATIONS"),
    }
    for code in bouts.values():
        exec(textwrap.dedent(code), espace)

    class _Pont:
        _ECHECS_DE_DONNEE = ("emprise_aberrante", "zero_entite",
                             "geometries_invalides", "crs_inconnu")
        _EXTENSIONS_VECTEUR = (".gpkg",)
        _EXTENSIONS_RASTER = (".tif",)
        _derniere_verification = None
        zone = (_AIX, "Aix-en-Provence")
        _origine_de_la_couche = espace["_origine_de_la_couche"]
        _geometries_invalides = staticmethod(espace["_geometries_invalides"])
        _emprise_et_rapport = staticmethod(_emprise_et_rapport)
        _lecture_emprise = staticmethod(espace["_lecture_emprise"])
        # (rectangle, commune) en km2 : QGIS les mesure, les tests les posent.
        surfaces = (None, None)
        _verification_couche = espace["_verification_couche"]
        _verifier_couches = espace["_verifier_couches"]
        _retenir_verification = espace["_retenir_verification"]
        _verification_chargement = espace["_verification_chargement"]
        _couche_depuis_fichier_de_sortie = espace["_couche_depuis_fichier_de_sortie"]

        def _zone_etude(self):
            return self.zone

        def _surfaces_zone(self, zone_bbox):
            return self.surfaces if zone_bbox else (None, None)

    return _Pont()


# ── 1. Le contenu du bloc ────────────────────────────────────────────────

def test_une_couche_saine_n_a_pas_d_avertissement(pont):
    b = pont._verification_couche(_Vecteur(emprise=_AIX), _AIX, "Aix-en-Provence")
    assert b["feature_count"] == 10
    assert b["crs"] == "EPSG:2154"
    assert b["origine"] == "fichier"
    assert b["zone_etude"] == "Aix-en-Provence"
    assert b["emprise_4326"] == _AIX
    assert b["geometries_invalides"] == 0
    assert "avertissement" not in b and "echecs" not in b


def test_l_incident_d_aix_est_signale_par_toute_couche(pont):
    b = pont._verification_couche(_Vecteur(emprise=_FRANCE_ET_OUTRE_MER),
                                  _AIX, "Aix-en-Provence")
    assert "emprise_aberrante" in b["echecs"]
    assert "ne presente aucun chiffre" in b["avertissement"]


def test_une_couche_en_memoire_doit_etre_exportee(pont):
    b = pont._verification_couche(
        _Vecteur(source="memory?geometry=Polygon&crs=EPSG:2154", emprise=_AIX),
        _AIX, "Aix-en-Provence")
    assert b["origine"] == "memoire"
    assert b["echecs"] == ["memoire"]
    assert "non sauvegardee" in b["avertissement"]
    assert "export_layer" in b["avertissement"]


def test_un_resultat_vide_est_signale(pont):
    b = pont._verification_couche(_Vecteur(n=0), _AIX, "Aix-en-Provence")
    assert "zero_entite" in b["echecs"]


def test_les_geometries_invalides_sont_comptees(pont):
    b = pont._verification_couche(_Vecteur(n=5, invalides=2, emprise=_AIX), _AIX, "Aix")
    assert b["geometries_invalides"] == 2
    assert "geometries_invalides" in b["echecs"]
    assert "native:fixgeometries" in b["avertissement"]


def test_un_crs_inconnu_est_signale(pont):
    b = pont._verification_couche(_Vecteur(crs=""), None, None)
    assert b["crs"] is None
    assert "crs_inconnu" in b["echecs"]


# ── 2. Le cout est plafonne ──────────────────────────────────────────────

def test_une_grosse_couche_n_est_pas_parcourue(pont):
    plafond = _constante("_PLAFOND_VALIDATION_GEOMETRIES")
    couche = _Vecteur(n=plafond + 1)
    b = pont._verification_couche(couche, None, None)
    assert not couche.parcourue
    assert str(b["geometries_invalides"]).startswith("non calcule")


def test_une_couche_distante_n_est_pas_parcourue(pont):
    couche = _Vecteur(n=10, source="url='https://data.geopf.fr/wfs/ows' typename='x'")
    b = pont._verification_couche(couche, None, None)
    assert not couche.parcourue
    assert "distance" in b["geometries_invalides"]


def test_le_budget_d_un_appel_borne_le_cout_total(pont):
    plafond = _constante("_PLAFOND_VALIDATION_GEOMETRIES")
    budget = _constante("_BUDGET_VALIDATION_APPEL")
    nombre = budget // plafond + 1
    couches = [_Vecteur(n=plafond, lid=f"l{i}") for i in range(nombre)]
    blocs, _ = pont._verifier_couches(couches, "execute_python")
    assert sum(c.parcourue for c in couches) == budget // plafond
    assert "plafond de controle" in blocs[-1]["geometries_invalides"]


def test_le_nombre_de_blocs_est_plafonne_comme_la_l2(pont):
    assert _constante("_PLAFOND_VERIFICATIONS") == 15
    couches = [_Vecteur(n=1, lid=f"l{i}") for i in range(20)]
    blocs, omises = pont._verifier_couches(couches, "execute_python")
    assert len(blocs) == 15 and omises == 5


# ── 3. La derniere verification est retenue pour le contexte ─────────────

def test_une_couche_en_memoire_n_est_pas_une_alerte_de_donnee(pont):
    pont._verifier_couches([_Vecteur(source="memory?x", emprise=_AIX)], "run_processing")
    assert pont._derniere_verification == {"outil": "run_processing", "alertes": []}


def test_une_emprise_aberrante_est_retenue(pont):
    pont._verifier_couches([_Vecteur(emprise=_FRANCE_ET_OUTRE_MER, nom="bati_aix")],
                           "execute_python")
    alerte = pont._derniere_verification["alertes"][0]
    assert alerte["name"] == "bati_aix"
    assert alerte["echecs"] == ["emprise_aberrante"]


# ── 4. Le chargement garde son contrat ───────────────────────────────────

def test_le_chargement_garde_filtre_suite_et_son_message(pont):
    v = pont._verification_chargement({"feature_count": 51_450_444},
                                      _Vecteur(emprise=_FRANCE_ET_OUTRE_MER),
                                      _AIX, "Aix-en-Provence")
    assert v["feature_count"] == 51_450_444, "le compte rendu reste celui du chargement"
    assert "pas le contour administratif" in v["filtre"]
    assert v["avertissement"].startswith("L'emprise chargee est")
    assert v["crs"] == "EPSG:2154" and v["origine"] == "fichier"


def test_le_chargement_retient_sa_verification():
    bloc = _methode("_action_smart_load")
    assert 'self._retenir_verification("smart_load", [result["verification"]])' in bloc


# ── 5. Les outils branches ───────────────────────────────────────────────

def test_run_processing_verifie_ses_sorties():
    bloc = _methode("_action_run_processing")
    assert 'self._verifier_couches(produites, "run_processing")' in bloc
    assert "_couche_depuis_fichier_de_sortie" in bloc, "les sorties fichier comptent aussi"


def test_une_sortie_qui_n_est_pas_un_fichier_de_couche_est_ignoree(pont):
    assert pont._couche_depuis_fichier_de_sortie(3) is None
    assert pont._couche_depuis_fichier_de_sortie("memory:") is None
    assert pont._couche_depuis_fichier_de_sortie("/nulle/part/x.gpkg") is None


def test_execute_python_verifie_les_couches_nouvelles():
    bloc = _methode("_action_execute_python")
    avant = bloc.index("ids_avant = set(QgsProject.instance().mapLayers().keys())")
    assert avant < bloc.index("exec(code, exec_globals)")
    # Le controle vient apres l'arret du minuteur, et pour tous les retours.
    verif = bloc.index('self._joindre_verification_des_nouvelles(reponse, ids_avant, "execute_python")')
    assert bloc.index("timer.cancel()") < verif
    assert "return {" not in bloc.split("timer.start()")[1]


def test_run_recipe_verifie_ses_couches_dans_les_deux_modes():
    for fonction in ("def _tool_run_recipe", "async def stream_run_recipe"):
        bloc = _MCP.split(fonction)[1].split("\ndef ")[0].split("\nasync def ")[0]
        assert "_couches_avant_recette" in bloc, fonction
        assert "_verification_recette" in bloc, fonction
        assert '"avertissement"' in bloc, fonction


def test_la_recette_s_appuie_sur_l_action_de_verification():
    assert "def _action_verify_layers" in _PONT
    bloc = _MCP.split("def _verification_recette")[1].split("\ndef ")[0]
    assert '"verify_layers"' in bloc
    assert '"sauf": avant' in bloc


# ── 6. L'emprise se lit sans contresens (defaut D2 du 2026-09-26) ────────
#
# Mesure apres le lot qualite 1 : `rapport_emprise_zone` a ete lu deux fois
# comme « rectangle / commune ». A Aix, « rectangle legerement plus grand
# que la commune (rapport 1.1) », alors que 1,1 rapportait l'emprise chargee
# au rectangle, et que ce rectangle couvre 2 fois la commune. Au Lavandou,
# « une zone 19 fois plus grande que la commune », alors que 19 rapportait
# l'emprise des routes chargees au rectangle.

_LAVANDOU = [6.348912, 43.125273, 6.454027, 43.208483]
# Surfaces communales : Aix, contour INSEE 13001 mesure le 2026-09-24 (fiche
# de mesure) ; Le Lavandou, geo.api.gouv.fr/communes/83070 (3 024 ha).
_COMMUNE_AIX_KM2 = 187.6
_COMMUNE_LAVANDOU_KM2 = 30.2


def _surface_rectangle_km2(b):
    """Reference independante de QGIS : aire exacte d'un rectangle
    lon/lat sur la sphere authalique (rayon 6 371,007 km)."""
    r = 6371.0072
    return (r * r * math.radians(b[2] - b[0])
            * (math.sin(math.radians(b[3])) - math.sin(math.radians(b[1]))))


def _agrandi(b, facteur_surface):
    """Rectangle de meme centre, `facteur_surface` fois plus vaste."""
    k = math.sqrt(facteur_surface)
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    dx, dy = (b[2] - b[0]) * k / 2, (b[3] - b[1]) * k / 2
    return [cx - dx, cy - dy, cx + dx, cy + dy]


def _phrase(lecture, motif):
    return next(p for p in lecture.split(". ") if motif in p)


def test_aix_le_rectangle_couvre_deux_fois_la_commune(pont):
    rectangle = _surface_rectangle_km2(_AIX)
    assert 375 < rectangle < 390, "le rectangle d'Aix fait environ 382 km2"
    pont.surfaces = (rectangle, _COMMUNE_AIX_KM2)
    v = pont._verification_chargement({"feature_count": 112_816},
                                      _Vecteur(emprise=_agrandi(_AIX, 1.1)),
                                      _AIX, "Aix-en-Provence")
    assert v["emprise_couche_sur_rectangle_zone"] == 1.1
    assert v["surface_rectangle_zone_km2"] == pytest.approx(rectangle, abs=0.1)
    assert v["surface_commune_km2"] == 187.6
    assert v["rectangle_sur_commune"] == 2.0
    lecture = v["lecture"]
    # Le 1,1 est rapporte au rectangle, et la phrase ne parle pas de commune.
    un_virgule_un = _phrase(lecture, "1,1")
    assert "rectangle de la zone" in un_virgule_un
    assert "commune" not in un_virgule_un
    # Le rapport a la commune est dit, avec ses deux surfaces.
    assert "environ 2 fois la surface de la commune" in lecture
    assert "km2 contre 187,6 km2" in lecture
    assert "un chiffre communal exige un decoupage" in lecture


def test_lavandou_dix_neuf_fois_le_rectangle_pas_la_commune(pont):
    rectangle = _surface_rectangle_km2(_LAVANDOU)
    pont.surfaces = (rectangle, _COMMUNE_LAVANDOU_KM2)
    v = pont._verification_chargement({"feature_count": 5_048},
                                      _Vecteur(emprise=_agrandi(_LAVANDOU, 19)),
                                      _LAVANDOU, "Le Lavandou")
    assert v["emprise_couche_sur_rectangle_zone"] == 19.0
    assert v["rectangle_sur_commune"] == 2.6
    assert "emprise_aberrante" not in v.get("echecs", []), "19 < seuil de 25"
    dix_neuf = _phrase(v["lecture"], "19 fois")
    assert "rectangle de la zone" in dix_neuf
    assert "ne dit rien de la commune" in v["lecture"]
    assert "environ 2,6 fois la surface de la commune" in v["lecture"]


def test_l_ancien_champ_ambigu_a_disparu(pont):
    pont.surfaces = (381.9, _COMMUNE_AIX_KM2)
    b = pont._verification_couche(_Vecteur(emprise=_AIX), _AIX, "Aix-en-Provence")
    assert "rapport_emprise_zone" not in b
    assert '["rapport_emprise_zone"]' not in _PONT, "plus aucun producteur"


def test_sans_contour_le_compte_porte_sur_le_rectangle(pont):
    pont.surfaces = (381.9, None)
    b = pont._verification_couche(_Vecteur(emprise=_AIX), _AIX, "Zone [5.27,43.45]")
    assert b["surface_rectangle_zone_km2"] == 381.9
    assert "surface_commune_km2" not in b and "rectangle_sur_commune" not in b
    assert "pas de contour communal" in b["lecture"]


def test_sans_zone_ni_emprise_pas_de_lecture(pont):
    b = pont._verification_couche(_Vecteur(n=0), None, None)
    assert "lecture" not in b
    assert "emprise_couche_sur_rectangle_zone" not in b


def test_une_emprise_aberrante_se_lit_aussi(pont):
    b = pont._verification_couche(_Vecteur(emprise=_FRANCE_ET_OUTRE_MER),
                                  _AIX, "Aix-en-Provence")
    assert "n'a pas ete bornee a la zone" in b["lecture"]
    assert "que le rectangle de la zone d'etude" in b["avertissement"]


def test_le_decoupage_dit_que_ses_comptes_sont_communaux():
    bloc = _methode("_action_clip_to_study_zone")
    litteral = bloc.split("verification = {")[1].split("\n        }")[0]
    assert '"lecture": (f"Couche decoupee au contour de' in litteral, \
        "la lecture du decoupage doit primer sur la lecture commune (setdefault)"


def test_les_surfaces_sont_mesurees_sur_l_ellipsoide():
    bloc = _methode("_surfaces_zone")
    assert "QgsDistanceArea()" in bloc
    assert 'setEllipsoid("WGS84")' in bloc
    assert '"study_zone_contour_wkt"' in bloc
    assert "/ 1e6" in bloc, "en km2"
