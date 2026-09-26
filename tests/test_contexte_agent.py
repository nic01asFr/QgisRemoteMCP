# -*- coding: utf-8 -*-
"""Le contexte rendu a l'agent dit l'etat reel et la suite utile.

Strategie qualite (2026-09-24, §3.3). Chaque outil qui modifie le projet
ajoute un `_context`, rendu a l'agent sous la forme

    --- Context: phase=analysis | zone=Aix-en-Provence | 3 layers (...)
        Hint: Analyze (run_processing, execute_python) or style layers ...

Phrase generique, en anglais, identique que la couche charge fasse 51 millions
d'entites a l'emprise de la France (incident S1) ou qu'elle soit juste.
L'agent enchainait donc sur l'analyse. Le contexte devient un contrat :
en francais, calcule sur l'etat reel, une ligne d'etat et une ligne de
suite, liste de couches plafonnee a 15 comme la L2 de l'agent.

Ces tests ne demandent pas QGIS.
"""
import ast
import textwrap
from pathlib import Path

import pytest

_RACINE = Path(__file__).resolve().parents[1]
_PONT = (_RACINE / "src" / "qgis_bridge.py").read_text(encoding="utf-8")
_MCP = (_RACINE / "main_mcp.py").read_text(encoding="utf-8")
_CONTEXTE = _PONT.split("def _build_context")[1].split("\n    def ")[0]


def _source_de(source, nom):
    arbre = ast.parse(source)
    return next(ast.get_source_segment(source, n) for n in ast.walk(arbre)
                if isinstance(n, ast.FunctionDef) and n.name == nom)


@pytest.fixture(scope="module")
def hint():
    arbre = ast.parse(_PONT)
    conseils = next(n for n in ast.walk(arbre) if isinstance(n, ast.Assign)
                    and getattr(n.targets[0], "id", "") == "_CONSEILS_D_ALERTE")
    espace = {}
    exec(textwrap.dedent(_source_de(_PONT, "_hint_contexte")), espace)

    class _Pont:
        _CONSEILS_D_ALERTE = ast.literal_eval(conseils.value)
        _hint_contexte = classmethod(espace["_hint_contexte"])

    return _Pont._hint_contexte


def _etat(**kw):
    base = {"zone": "Aix-en-Provence", "contour": True, "phase": "analysis",
            "nb_vecteurs": 1, "nb_rasters": 1, "memoire": [], "alertes": [],
            "par_bbox": []}
    base.update(kw)
    return base


_ALERTE_S1 = {"layer_id": "l1", "name": "batiments_bdtopo_aix_emprise",
              "echecs": ["emprise_aberrante"], "avertissement": "..."}


# ── 1. Etats types ───────────────────────────────────────────────────────

def test_etude_vide_invite_a_definir_la_zone(hint):
    etat, suite = hint(_etat(zone=None, contour=False, phase="setup",
                             nb_vecteurs=0, nb_rasters=0))
    assert etat.startswith("Zone : aucune | 0 couche(s)")
    assert "set_study_zone" in suite


def test_l_incident_s1_demande_de_corriger_avant_d_analyser(hint):
    """Alerte sur la derniere verification : la suite est de corriger,
    meme si d'autres choses restent a faire."""
    etat, suite = hint(_etat(alertes=[_ALERTE_S1], par_bbox=["batiments"],
                             memoire=["tampon"]))
    assert "alerte sur « batiments_bdtopo_aix_emprise » : emprise_aberrante" in etat
    assert suite.startswith("Corrige « batiments_bdtopo_aix_emprise » avant tout chiffre")
    assert "clip_to_study_zone" in suite
    assert "run_processing" not in suite


def test_sans_contour_on_recharge_au_lieu_de_decouper(hint):
    _, suite = hint(_etat(contour=False, alertes=[_ALERTE_S1]))
    assert "smart_load" in suite and "clip_to_study_zone" not in suite


def test_une_couche_vide_est_a_corriger(hint):
    alerte = dict(_ALERTE_S1, name="bati_temp", echecs=["zero_entite"])
    _, suite = hint(_etat(alertes=[alerte]))
    assert "« bati_temp »" in suite and "vide" in suite


def test_chargee_par_rectangle_on_decoupe_avant_de_compter(hint):
    etat, suite = hint(_etat(par_bbox=["batiment"]))
    assert "(contour communal)" in etat
    assert suite == ("Avant de compter dans la commune, decoupe « batiment » "
                     "au contour : clip_to_study_zone.")


def test_zone_sans_contour_le_compte_porte_sur_le_rectangle(hint):
    etat, suite = hint(_etat(zone="Point (5.4, 43.5)", contour=False,
                             par_bbox=["batiment"]))
    assert "(rectangle, sans contour)" in etat
    assert "rectangle" in suite and "clip_to_study_zone" not in suite


def test_une_couche_en_memoire_est_a_exporter(hint):
    etat, suite = hint(_etat(memoire=["Grille hexagonale"]))
    assert "1 en memoire non sauvee(s)" in etat
    assert "export_layer" in suite and "« Grille hexagonale »" in suite


def test_une_couche_en_memoire_n_est_pas_une_alerte(hint):
    etat, _ = hint(_etat(memoire=["x"]))
    assert "alerte" not in etat


@pytest.mark.parametrize("phase, attendu", [
    ("analysis", "run_processing"),
    ("cartography", "apply_layout_template"),
    ("export", "export_pdf"),
])
def test_sans_rien_a_corriger_la_phase_guide(hint, phase, attendu):
    _, suite = hint(_etat(phase=phase))
    assert attendu in suite


def test_sans_couche_vecteur_on_charge(hint):
    _, suite = hint(_etat(nb_vecteurs=0, phase="setup"))
    assert "smart_load" in suite


# ── 2. Le contrat de forme ───────────────────────────────────────────────

def test_court_meme_avec_des_noms_longs(hint):
    long = "x" * 300
    alertes = [dict(_ALERTE_S1, name=long)] * 4
    etat, suite = hint(_etat(zone=long, alertes=alertes, memoire=[long] * 30,
                             nb_vecteurs=30))
    assert len(etat) < 240 and len(suite) < 160
    assert "\n" not in etat + suite
    assert "(+3)" in etat


def test_plus_de_phrase_generique_en_anglais():
    for ancien in ('"Start with set_study_zone', '"Analyze (run_processing',
                   '"Apply a layout', "_phase_hint"):
        assert ancien not in _PONT, ancien


def test_la_liste_des_couches_est_plafonnee_comme_la_l2():
    assert "for l in vector_layers[:_PLAFOND_VERIFICATIONS]" in _CONTEXTE
    assert '"layers_omises"' in _CONTEXTE
    ligne = next(l for l in _PONT.splitlines() if l.startswith("_PLAFOND_VERIFICATIONS = "))
    assert ligne.split("=")[1].strip() == "15"


def test_le_contexte_lit_l_etat_reel():
    assert 'scope.variable("study_zone_contour_wkt")' in _CONTEXTE
    assert 'if a.get("layer_id") in presentes' in _CONTEXTE, "alerte sur couche retiree"
    assert 'l.providerType() == "memory"' in _CONTEXTE


def test_le_chargement_et_le_decoupage_tiennent_le_registre_des_rectangles():
    chargement = _PONT.split("def _action_smart_load")[1].split("\n    def ")[0]
    assert 'self._charges_par_bbox[result["layer_id"]]' in chargement
    decoupe = _PONT.split("def _action_clip_to_study_zone")[1].split("\n    def ")[0]
    assert '"_charges_par_bbox", {}).pop(couche.id(), None)' in decoupe


# ── 3. Le serveur MCP rend les deux lignes ───────────────────────────────

@pytest.fixture(scope="module")
def extraire():
    espace = {}
    exec(textwrap.dedent(_source_de(_MCP, "_extract_context")), espace)
    return espace["_extract_context"]


def test_deux_lignes_contexte_puis_suite(extraire):
    reponse = {"success": True, "_context": {"etat": "Zone : Aix", "suite": "Fais X."}}
    [bloc] = extraire(reponse)
    assert bloc["text"] == "\n--- Contexte : Zone : Aix\n    Suite : Fais X."
    assert "_context" not in reponse


def test_un_pont_plus_ancien_reste_lisible(extraire):
    reponse = {"_context": {"study_zone": "Aix", "layers": [{}, {}], "raster_count": 1,
                            "hint": "Analyze"}}
    [bloc] = extraire(reponse)
    assert bloc["text"].startswith("\n--- Contexte : Zone : Aix | 3 couche(s)")


def test_sans_contexte_rien(extraire):
    assert extraire({"success": True}) == []
