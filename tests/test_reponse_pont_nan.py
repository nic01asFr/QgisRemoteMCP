# -*- coding: utf-8 -*-
"""Une reponse du pont contenant NaN ne fait plus tomber l'API en 500.

Constate le 2026-09-26 : l'emprise d'une couche vide vaut NaN ; la reponse
HTTP echouait (« Out of range float values are not JSON compliant: nan »).
"""
import json
from pathlib import Path

_API = (Path(__file__).resolve().parents[1] / "src" / "api_server.py").read_text(encoding="utf-8")


def _lire(texte):
    """Reproduit _lire_reponse_pont."""
    return json.loads(texte, parse_constant=lambda _constante: None)


def test_nan_et_infinis_deviennent_null():
    brut = json.dumps({"emprise": [float("nan"), 1.0, float("inf"), -float("inf")]})
    assert "NaN" in brut
    lu = _lire(brut)
    assert lu == {"emprise": [None, 1.0, None, None]}
    json.dumps(lu, allow_nan=False)  # du JSON strict, comme l'exige la reponse HTTP


def test_toutes_les_lectures_du_pont_passent_par_la_garde():
    assert "parse_constant=lambda _constante: None" in _API
    code = _API.split("def _lire_reponse_pont")[1].split("\ndef ", 1)[1]
    assert "json.loads(data.decode())" not in code
    assert "json.loads(buffer.decode())" not in code
    assert "json.loads(line.decode())" not in code
