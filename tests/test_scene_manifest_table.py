"""La table SceneManifest d'un .grist est-elle celle que les widgets lisent ?

Contexte (2026-08-23). `export_grist` creait la table avec ses propres noms de
colonnes -- `content` pour le JSON, `created_at_iso` pour la date. Atlas cherche
`manifest_json` et `created_at` ; il trouvait `undefined`, echouait sur le
`JSON.parse`, et rendait une carte vide en signalant « manifest JSON invalide ».
Un .grist produit ici n'etait lisible par aucun widget de l'ecosysteme.

Le schema canonique est celui que qgis2grist ecrit (`index_v2.html`,
`ensureSceneManifestTable`) et qu'Atlas lit (`lib/scene-loader.js`,
`loadLatestSceneManifest`). Ces tests lisent le code source plutot que
d'executer l'export, qui exige QGIS.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_SOURCE = (Path(__file__).parent.parent / "src" / "qgis_bridge.py").read_text(
    encoding="utf-8"
)

# Les quatre colonnes du contrat de fait, partagees par qgis2grist, Atlas et
# ZEBRA. `n_layers` s'y ajoute chez nous, sans rien reclamer a personne.
COLONNES_CANONIQUES = ("manifest_json", "scene_hash", "source_file", "created_at")


def _bloc_creation() -> str:
    """Le CREATE TABLE SceneManifest, isole de son contexte."""
    debut = _SOURCE.find("CREATE TABLE SceneManifest")
    assert debut != -1, "le bloc de creation de la table a disparu"
    return _SOURCE[debut : _SOURCE.find('"""', debut)]


@pytest.mark.parametrize("colonne", COLONNES_CANONIQUES)
def test_la_table_porte_les_colonnes_que_les_widgets_lisent(colonne):
    assert colonne in _bloc_creation(), (
        f"colonne « {colonne} » absente : les widgets de l'ecosysteme ne "
        f"sauront pas lire cette table"
    )


@pytest.mark.parametrize("ancien", ["content", "created_at_iso"])
def test_les_anciens_noms_ne_reviennent_pas(ancien):
    """Ils ne cassaient rien de visible -- c'est bien le probleme."""
    assert re.search(rf"\b{ancien}\b", _bloc_creation()) is None, (
        f"« {ancien} » est de retour dans la table ; Atlas lira undefined"
    )


def test_les_colonnes_declarees_a_grist_suivent_la_table():
    """Deux endroits decrivent les memes colonnes : la table SQLite et le
    registre `_grist_Tables_column`. Ils doivent dire la meme chose, sinon la
    table existe en SQL et reste invisible dans l'interface."""
    debut = _SOURCE.find("_sm_columns = [")
    assert debut != -1, "la declaration des colonnes Grist a disparu"
    declarees = _SOURCE[debut : _SOURCE.find("]", debut)]
    for colonne in COLONNES_CANONIQUES:
        assert f'"{colonne}"' in declarees, (
            f"« {colonne} » absente du registre Grist : la colonne existera en "
            f"SQL mais pas dans l'interface"
        )


def test_la_date_est_un_horodatage_et_non_une_chaine():
    """Grist stocke les DateTime en secondes epoch. Une chaine ISO s'afficherait
    comme du texte dans une colonne typee date."""
    bloc = _SOURCE[_SOURCE.find("INSERT INTO SceneManifest") :][:900]
    assert "timestamp()" in bloc, "la date inseree n'est pas un horodatage epoch"
    assert "isoformat()" not in bloc, "une chaine ISO est inseree dans un DateTime"


def test_la_version_du_contrat_est_lue_avant_l_ancienne_graphie():
    """`version` est le champ du contrat publie ; `manifest_version` est
    l'ancienne graphie interne de qgis-sspcloud, encore en circulation."""
    # Pas la premiere occurrence : c'est l'initialisation a vide.
    bloc = _SOURCE[_SOURCE.find('scene_manifest_meta = {\n') :][:700]
    pos_version = bloc.find('_parsed.get("version")')
    pos_ancien = bloc.find('_parsed.get("manifest_version")')
    assert pos_version != -1, "le champ `version` du contrat n'est pas lu"
    assert pos_ancien == -1 or pos_version < pos_ancien, (
        "l'ancienne graphie est lue avant celle du contrat"
    )
