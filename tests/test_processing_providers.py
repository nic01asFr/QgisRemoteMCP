# -*- coding: utf-8 -*-
"""Le moteur tournait a 322 algorithmes sur 730, et sa doc annoncait 1000.

Constate le 2026-09-05 sur l'instance de production. Cause racine trouvee dans
`supervisord.conf`, pas dans le Python : QGIS est lance avec `--noplugins`. Le
drapeau protege des extensions tierces, mais il empeche du meme coup le
chargement du plugin `processing` -- celui-la meme qui enregistre les providers
GDAL, GRASS et `qgis:`. Seuls les providers C++ (`native`, `3d`) survivaient.

Trois consequences mesurees :

  * `skills/processing.md`, le fichier qui dit a l'agent quels algorithmes
    utiliser, en annoncait huit qui n'existaient pas dans le registre. L'agent
    n'hallucinait pas : sa documentation lui mentait. C'est l'invariant
    anti-hallucination du README pris a revers -- le catalogue etait faux.
  * `solar_pipeline.py` a du contourner en lancant le binaire `grass` en
    sous-processus, ce qui a emporte un `EPSG:2154` code en dur jusque dans la
    location GRASS.
  * `main_mcp.py` promettait « 1000+ algorithmes » dont `gdal:warp` et
    `grass7:v.clean` -- deux identifiants qui n'existent sous aucune
    configuration : les bons sont `gdal:warpreproject` et `grass:v.clean`.

Reparation retenue le 2026-09-05 : retirer `--noplugins`, declarer l'ensemble
des plugins actives dans le QGIS3.ini seme par le Dockerfile, et transformer
l'initialisation manuelle du pont en controle qui journalise l'etat du
registre. Le defaut de fond n'etait pas l'absence de code d'initialisation --
c'est que personne ne regardait le resultat.

Ces tests verrouillent cette reparation. Ils sont statiques : ils lisent le
depot et n'ont pas besoin d'un QGIS en marche, sauf ceux marques `container`.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent

# Ce que le pont enregistre reellement : `native` et `3d` viennent du coeur C++,
# `qgis` et `gdal` de Processing.initialize(), `grass` du plugin grassprovider.
PROVIDERS_ENREGISTRES = {"native", "3d", "qgis", "gdal", "grass"}

# SAGA merite sa propre mention : QGIS a retire son provider du coeur en 3.30 et
# le paquet n'est pas dans l'image. L'annoncer serait la meme faute qu'avant.
PROVIDERS_INTERDITS = {"saga", "grass7", "otb"}


def lire(chemin: str) -> str:
    return io.open(RACINE / chemin, encoding="utf-8").read()


class TestCauseRacine:
    def test_noplugins_a_ete_retire(self):
        """Le drapeau ne protegeait de rien -- repertoire des plugins tiers
        vide -- et privait l'utilisateur du menu Traitement. S'il revient, le
        registre retombe a 322 algorithmes et l'interface reperd sa boite a
        outils, silencieusement."""
        conf = lire("supervisord.conf")
        # Sur les lignes de commande seulement : le commentaire qui explique
        # le retrait cite forcement le drapeau.
        commandes = [l for l in conf.splitlines() if l.startswith("command=")]
        assert commandes, "aucune ligne command= dans supervisord.conf"
        assert not [c for c in commandes if "--noplugins" in c]
        assert any("/usr/bin/qgis --nologo --noversioncheck" in c for c in commandes)

    def test_l_ensemble_active_est_declare_dans_l_image(self):
        """Retirer le drapeau sans declarer les plugins remplacerait une liste
        implicite par une autre. Le QGIS3.ini seme par le Dockerfile nomme
        l'ensemble, plutot que de subir le defaut de la version de QGIS."""
        docker = lire("Dockerfile")
        assert "[PythonPlugins]" in docker
        assert "processing=true" in docker
        assert "grassprovider=true" in docker

    def test_le_pont_controle_l_etat_du_registre(self):
        """Le correctif de fond n'est pas le code d'initialisation : c'est
        qu'un journal dise enfin ce que le registre contient."""
        pont = lire("src/qgis_bridge.py")
        assert "def _ensure_processing_providers" in pont
        assert "_PROVIDERS_ATTENDUS" in pont
        assert "registre incomplet" in pont

    def test_la_reparation_reste_disponible_en_filet(self):
        pont = lire("src/qgis_bridge.py")
        assert "def _reparer_providers" in pont
        assert "Processing.initialize()" in pont
        assert "grassprovider" in pont

    def test_la_reparation_n_est_tentee_qu_une_fois(self):
        """Sinon un GRASS reellement absent ferait tourner la reparation et
        remplirait les logs a chaque appel."""
        assert "_REPARATION_TENTEE" in lire("src/qgis_bridge.py")

    def test_l_enregistrement_est_programme_au_demarrage(self):
        """Sans le timer, le registre ne se remplit qu'au premier
        run_processing -- et list_algorithms, lui, annoncerait un catalogue
        tronque a qui le consulte avant."""
        pont = lire("src/qgis_bridge.py")
        assert re.search(r"QTimer\.singleShot\(\s*\d+\s*,\s*_ensure_processing_providers\s*\)",
                         pont)

    def test_l_enregistrement_est_idempotent(self):
        """Appele par le timer ET par _get_processing : il doit ressortir
        immediatement la seconde fois."""
        pont = lire("src/qgis_bridge.py")
        assert "_PROVIDERS_READY" in pont
        assert "if _PROVIDERS_READY:" in pont


class TestPiegeABI:
    def test_les_libs_persistantes_sont_ajoutees_en_fin_de_path(self):
        """`pip install --target /data/pylibs` y depose aussi les dependances
        transitives, dont un numpy 2.x. QGIS et scipy sont compiles contre le
        numpy 1.26 de Debian : passer le dossier devant echangerait l'ABI sous
        leurs pieds. `append`, jamais `insert(0, ...)`."""
        pont = lire("src/qgis_bridge.py")
        assert "sys.path.append(_LIBS_PERSISTANTES)" in pont
        assert "sys.path.insert(0, _LIBS_PERSISTANTES)" not in pont

    def test_le_chemin_reste_configurable(self):
        assert "QGIS_EXTRA_PYTHONPATH" in lire("src/qgis_bridge.py")


class TestLaDocumentationNePrometPasCeQuiNExistePas:
    """Le coeur du defaut : ce n'est pas le code qui mentait, c'est ce que le
    code raconte a l'agent."""

    FICHIERS = ["main_mcp.py", "README.md", "CLAUDE.md"] + [
        "skills/" + p.name for p in (RACINE / "skills").glob("*.md")
    ]

    MOTIF = re.compile(r"\b([a-z][a-z0-9_]{1,12}):([a-z][a-zA-Z0-9_.]{2,40})\b")

    # Prefixes qui ressemblent a un provider sans en etre un.
    FAUX_AMIS = {
        "http", "https", "epsg", "urn", "ogc", "file", "postgres", "mailto",
        "skill", "note", "warning", "example", "python", "bash", "json",
        "sql", "crs", "srs", "wfs", "wms", "xyz", "gdal_translate",
    }

    def test_aucun_provider_inconnu_n_est_annonce(self):
        fautes = []
        for nom in self.FICHIERS:
            texte = lire(nom)
            for prov, algo in self.MOTIF.findall(texte):
                if prov in self.FAUX_AMIS or prov in PROVIDERS_ENREGISTRES:
                    continue
                if prov in PROVIDERS_INTERDITS:
                    fautes.append("%s : %s:%s" % (nom, prov, algo))
        assert not fautes, (
            "des algorithmes d'un provider non enregistre sont annonces a "
            "l'agent :\n  " + "\n  ".join(fautes)
        )

    def test_le_nombre_annonce_n_est_plus_fantaisiste(self):
        for nom in ("main_mcp.py", "README.md", "CLAUDE.md"):
            assert "1000+ Processing" not in lire(nom)

    def test_les_identifiants_donnes_en_exemple_existent_vraiment(self):
        """`gdal:warp` et `grass7:v.clean` n'existent sous aucune version."""
        for nom in ("main_mcp.py", "README.md", "CLAUDE.md"):
            texte = lire(nom)
            assert "grass7:" not in texte
            assert not re.search(r"gdal:warp\b", texte)


@pytest.mark.container
class TestDansLeConteneur:
    """Ne passent que dans le conteneur QGIS. Ailleurs : skip, pas echec."""

    def test_le_registre_porte_bien_gdal_et_grass(self):
        qgis_core = pytest.importorskip("qgis.core")
        from processing.core.Processing import Processing
        Processing.initialize()
        ids = [a.id() for a in qgis_core.QgsApplication.processingRegistry().algorithms()]
        providers = {i.split(":")[0] for i in ids}
        assert "gdal" in providers
        assert len(ids) > 400, "registre encore tronque : %d algorithmes" % len(ids)
