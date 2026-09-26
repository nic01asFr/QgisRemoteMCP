# -*- coding: utf-8 -*-
"""Les descriptions d'outils annoncent `verification` et le decoupage.

Le modele (qwen3-6-35b-moe) choisit ses outils sur leur description. Un
bloc `verification` qu'aucune description ne mentionne est un bloc qu'il
ne pense pas a lire ; un clip_to_study_zone qu'aucune description ne cite
laisse la place au decoupage ecrit a la main (incident S1 du 2026-09-24).

Les descriptions restent courtes : chaque outil expose coute du contexte.
Ces tests ne demandent pas QGIS.
"""
from pathlib import Path

import pytest

_MCP = (Path(__file__).resolve().parents[1] / "main_mcp.py").read_text(encoding="utf-8")


def _description(outil):
    bloc = _MCP.split(f'"name": "{outil}"')[1]
    return bloc.split('"description": "')[1].split('",\n')[0]


@pytest.mark.parametrize("outil", ["smart_load", "run_processing", "execute_python",
                                   "clip_to_study_zone"])
def test_la_verification_est_annoncee(outil):
    assert "`verification`" in _description(outil)


@pytest.mark.parametrize("outil", ["smart_load", "run_processing", "execute_python",
                                   "set_study_zone"])
def test_le_decoupage_au_contour_est_cite(outil):
    assert "clip_to_study_zone" in _description(outil)


def test_la_zone_dit_quand_elle_a_un_contour():
    d = _description("set_study_zone")
    assert "contour administratif" in d
    assert "Marseille 4e" in d
    assert "rectangle" in d


def test_run_processing_n_annonce_plus_saga():
    """SAGA n'est pas dans l'image (retire du coeur de QGIS en 3.30)."""
    d = _description("run_processing")
    assert "no SAGA" in d
    assert "SAGA." not in d


def test_une_sortie_temporaire_est_a_exporter():
    assert "export_layer" in _description("run_processing")


@pytest.mark.parametrize("outil, plafond", [
    ("smart_load", 1400), ("run_processing", 500), ("execute_python", 1000),
    ("set_study_zone", 600), ("clip_to_study_zone", 800),
])
def test_les_descriptions_restent_courtes(outil, plafond):
    assert len(_description(outil)) < plafond
