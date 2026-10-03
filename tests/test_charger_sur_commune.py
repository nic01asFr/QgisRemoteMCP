# -*- coding: utf-8 -*-
"""« Charge le bati sur Aix » : le rendu attendu en un seul appel.

Vecu le 2026-10-03 sur nic01asfr : quatre messages et un redemarrage de QGIS
(memoire saturee) pour obtenir le bati decoupe a la commune, Aix seule et un
affichage propre. L'agent gardait la couche rectangle de 112 816 batiments a
cote des 54 557 d'Aix, et chargeait les vingt communes du rectangle.

charger_sur_commune enchaine des actions deja eprouvees du pont : zone,
chargement, decoupage, retrait du rectangle, contour seul, fond, cadrage.
Choix de rendu de l'utilisateur : contour seul, pas de style thematique,
orthophoto IGN.

Tests sans QGIS : l'outil MCP est execute contre un pont simule ; les
actions du pont sont lues dans la source.
"""
import ast
import textwrap
from pathlib import Path

_RACINE = Path(__file__).resolve().parents[1]
_PONT = (_RACINE / "src" / "qgis_bridge.py").read_text(encoding="utf-8")
_MCP = (_RACINE / "main_mcp.py").read_text(encoding="utf-8")


def _methode(nom):
    return _PONT.split(f"def {nom}(")[1].split("\n    def ")[0]


def _description(outil):
    bloc = _MCP.split(f'"name": "{outil}"')[1]
    return bloc.split('"description": "')[1].split('",\n')[0]


class _Pont:
    """Pont simule : rejoue une reponse par action et note les appels."""

    def __init__(self, **reponses):
        self.reponses = reponses
        self.appels = []

    def __call__(self, action, params, timeout=None):
        self.appels.append((action, params))
        return dict(self.reponses.get(action, {"success": True}))

    def actions(self):
        return [a for a, _ in self.appels]


def _outil(pont):
    arbre = ast.parse(_MCP)
    (code,) = [ast.get_source_segment(_MCP, n) for n in ast.walk(arbre)
               if isinstance(n, ast.FunctionDef) and n.name == "_tool_charger_sur_commune"]
    resumes = []
    espace = {
        "qgis_command": pont,
        "_validate_required": lambda a, *cles: next(
            (f"{c} is required" for c in cles if not a.get(c)), None),
        "_text": lambda r, indent=None: resumes.append(r) or "ok",
        "_auto_screenshot": lambda: "",
        "_error": lambda m: {"isError": True, "message": m},
        "SOCKET_TIMEOUT_LONG": 600,
    }
    exec(textwrap.dedent(code), espace)
    return espace["_tool_charger_sur_commune"], resumes


_BATI = {"success": True, "layer_id": "bati_rect", "name": "Bâtiments (BD TOPO)"}
_DECOUPE = {"success": True, "layer_id": "bati_aix", "name": "batiment_aix_en_provence",
            "path": "/data/studies/s/data/batiment_aix_en_provence.gpkg",
            "verification": {"apres": 54557, "avant": 112816}}
_CONTOUR = {"success": True, "layer_id": "commune_aix", "name": "Aix-en-Provence",
            "extent": [1, 2, 3, 4], "crs": "EPSG:2154",
            "verification": {"surface_km2": 187.6}}
_FOND = {"success": True, "layer_id": "ortho", "name": "Orthophotos IGN (WMTS)"}


def _pont_nominal():
    return _Pont(smart_load=_BATI, clip_to_study_zone=_DECOUPE,
                 contour_zone_etude=_CONTOUR, fond_de_plan=_FOND)


# ── 1. Le rendu attendu en un appel ──────────────────────────────────────


def test_l_enchainement_complet_dans_l_ordre():
    pont = _pont_nominal()
    outil, resumes = _outil(pont)
    outil({"id": "bdtopo_batiments", "commune": "Aix-en-Provence"})
    assert pont.actions() == ["set_study_zone", "smart_load", "clip_to_study_zone",
                              "remove_layer", "contour_zone_etude", "zoom_to_extent",
                              "fond_de_plan"]
    assert pont.appels[0][1] == {"target": "Aix-en-Provence"}
    (resume,) = resumes
    assert resume["couche"]["layer_id"] == "bati_aix"
    assert resume["verification"]["apres"] == 54557
    assert resume["commune"]["name"] == "Aix-en-Provence"
    assert resume["fond"] == "Orthophotos IGN (WMTS)"


def test_la_couche_rectangle_est_retiree_apres_le_decoupage():
    pont = _pont_nominal()
    outil, resumes = _outil(pont)
    outil({"id": "bdtopo_batiments"})
    assert ("remove_layer", {"layer_id": "bati_rect"}) in pont.appels
    assert resumes[0]["couche_rectangle_retiree"] is True


def test_le_cadrage_porte_sur_la_commune():
    pont = _pont_nominal()
    outil, _ = _outil(pont)
    outil({"id": "bdtopo_batiments"})
    assert ("zoom_to_extent", {"extent": [1, 2, 3, 4], "crs": "EPSG:2154"}) in pont.appels


def test_sans_commune_la_zone_courante_est_gardee():
    pont = _pont_nominal()
    outil, _ = _outil(pont)
    outil({"id": "bdtopo_batiments"})
    assert "set_study_zone" not in pont.actions()


def test_ni_style_ni_analyse_d_office():
    pont = _pont_nominal()
    outil, resumes = _outil(pont)
    outil({"id": "bdtopo_batiments", "commune": "Aix-en-Provence"})
    interdits = {"set_layer_style", "densite_par_maille", "compter_par_zone",
                 "execute_python", "run_processing"}
    assert not interdits & set(pont.actions())
    assert "ne la lance pas d'office" in resumes[0]["suite"]


def test_le_fond_peut_etre_omis():
    pont = _pont_nominal()
    outil, _ = _outil(pont)
    outil({"id": "bdtopo_batiments", "fond": False})
    assert "fond_de_plan" not in pont.actions()


# ── 2. Les echecs restent lisibles ───────────────────────────────────────


def test_un_raster_n_est_pas_decoupe_et_reste_charge():
    pont = _Pont(smart_load={"success": True, "layer_id": "mnt", "name": "MNT"},
                 clip_to_study_zone={"error": "La couche n'est pas vecteur"},
                 contour_zone_etude=_CONTOUR, fond_de_plan=_FOND)
    outil, resumes = _outil(pont)
    outil({"id": "ign_mnt"})
    assert "remove_layer" not in pont.actions()
    assert resumes[0]["couche"]["layer_id"] == "mnt"
    assert resumes[0]["decoupage"].startswith("non fait")


def test_une_commune_introuvable_arrete_tout():
    pont = _Pont(set_study_zone={"error": "Commune inconnue"})
    outil, _ = _outil(pont)
    reponse = outil({"id": "bdtopo_batiments", "commune": "Atlantide"})
    assert reponse["isError"] and "Atlantide" in reponse["message"]
    assert pont.actions() == ["set_study_zone"]


def test_l_id_est_exige():
    outil, _ = _outil(_pont_nominal())
    assert outil({"commune": "Aix-en-Provence"})["isError"]


# ── 3. Declaration et actions du pont ────────────────────────────────────


def test_l_outil_est_declare_branche_et_long():
    assert '"name": "charger_sur_commune"' in _MCP
    assert '"charger_sur_commune": _tool_charger_sur_commune' in _MCP
    longs = _MCP.split("_LONG_TIMEOUT_ACTIONS = frozenset({")[1].split("})")[0]
    assert '"charger_sur_commune"' in longs


def test_la_description_dit_le_reflexe_et_reste_courte():
    d = _description("charger_sur_commune")
    assert "charge / affiche" in d and "UN appel" in d and "ne les lance pas" in d
    assert len(d) < 900


def test_les_actions_du_pont_sont_mutantes():
    mutantes = _PONT.split("_MUTATING_ACTIONS = frozenset({")[1].split("})")[0]
    assert '"contour_zone_etude"' in mutantes and '"fond_de_plan"' in mutantes


def test_le_contour_est_un_trait_seul_en_tete_de_legende():
    code = _methode("_action_contour_zone_etude")
    assert '"style": "no"' in code
    assert "insertLayer(0, couche)" in code
    assert "_dossier_donnees_etude()" in code
    assert "get_study_zone_contour()" in code, "aucun telechargement : le contour memorise"


def test_le_fond_est_reutilise_et_place_sous_les_couches():
    code = _methode("_action_fond_de_plan")
    assert "reutilise" in code and "couche_service in (c.source()" in code
    assert "racine.addChildNode(clone)" in code
    assert '_FOND_PAR_DEFAUT = "ign_ortho_wmts"' in _PONT


# ── 4. Corrections apres l'essai en direct (2026-10-03) ──────────────────
# Le contour n'apparaissait pas (NameError : qgis_helpers non importe) et le
# fond orthophoto restait blanc (adresse WMTS ecrite comme du XYZ).


def test_toute_action_qui_utilise_qgis_helpers_l_importe():
    """Le module n'est pas importe en tete du pont : chaque methode qui s'en
    sert doit l'importer, sinon NameError a l'execution seulement."""
    arbre = ast.parse(_PONT)
    en_tete = any(isinstance(n, (ast.Import, ast.ImportFrom)) and any(
        a.name == "qgis_helpers" for a in n.names) for n in arbre.body)
    if en_tete:
        return
    fautives = []
    for f in ast.walk(arbre):
        if not isinstance(f, ast.FunctionDef):
            continue
        utilise = any(isinstance(n, ast.Name) and n.id == "qgis_helpers"
                      for n in ast.walk(f))
        importe = any(isinstance(n, ast.Import) and any(
            a.name == "qgis_helpers" for a in n.names) for n in ast.walk(f))
        if utilise and not importe:
            fautives.append(f.name)
    assert fautives == []


def _uri_wmts():
    aides = (_RACINE / "src" / "qgis_helpers.py").read_text(encoding="utf-8")
    (code,) = [ast.get_source_segment(aides, n) for n in ast.walk(ast.parse(aides))
               if isinstance(n, ast.FunctionDef) and n.name == "uri_wmts"]
    espace = {}
    exec(textwrap.dedent(code), espace)
    return espace["uri_wmts"], aides


def test_le_wmts_passe_par_le_getcapabilities():
    uri_wmts, _ = _uri_wmts()
    uri = uri_wmts("https://data.geopf.fr/wmts", "ORTHOIMAGERY.ORTHOPHOTOS")
    assert uri.endswith("&url=https://data.geopf.fr/wmts?SERVICE%3DWMTS%26REQUEST%3DGetCapabilities")
    assert "tileMatrixSet=PM" in uri and "layers=ORTHOIMAGERY.ORTHOPHOTOS" in uri
    assert "type=xyz" not in uri
    # Une URL deja munie d'une requete ne double pas le « ? ».
    assert uri_wmts("https://x/wmts?SERVICE=WMTS", "L").count("?") == 1


def test_les_deux_chemins_wmts_partagent_la_meme_adresse():
    """Vecu le 2026-10-03 : le correctif d'add_wmts n'avait pas suffi, le
    chargement du catalogue (pont) avait sa propre copie de l'ancienne forme."""
    _, aides = _uri_wmts()
    add_wmts = aides.split("def add_wmts(")[1].split("\ndef ")[0]
    assert "uri_wmts(" in add_wmts
    branche = _PONT.split('elif src_type == "wmts":')[1].split("elif src_type")[0]
    assert "qgis_helpers.uri_wmts(" in branche and "type=xyz" not in branche
    assert "tilematrixset={" not in _PONT, "plus aucune copie de l'ancienne adresse"


def test_les_outils_voisins_orientent_vers_charger_sur_commune():
    """Essai en direct du 2026-10-03 : un passage sur trois, le modele a suivi
    smart_load puis clip_to_study_zone, que la description de smart_load
    suggerait (« pour un chiffre dans la commune, clip_to_study_zone ensuite »)."""
    smart = _description("smart_load")
    assert smart.startswith("Pour « charge / affiche <donnees> sur <commune> », utilise charger_sur_commune")
    assert "clip_to_study_zone ensuite" not in smart
    assert "charger_sur_commune fait tout en un appel" in _description("clip_to_study_zone")
