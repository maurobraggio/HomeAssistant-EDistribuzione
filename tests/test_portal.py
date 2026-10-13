"""Protocollo automatico del fallback portale/CSV, senza rete reale."""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import pytest
from yarl import URL

PKG_DIR = Path(__file__).parent.parent / "custom_components" / "edistribuzione"


def _load_portal_module():
    pkg_name = "edistribuzione_test_portal"
    if f"{pkg_name}.portal" in sys.modules:
        return sys.modules[f"{pkg_name}.portal"]

    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(PKG_DIR)]
    sys.modules[pkg_name] = pkg

    def _load(modname: str, filename: str):
        spec = importlib.util.spec_from_file_location(f"{pkg_name}.{modname}", PKG_DIR / filename)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"{pkg_name}.{modname}"] = mod
        spec.loader.exec_module(mod)
        return mod

    _load("const", "const.py")
    return _load("portal", "portal.py")


portal = _load_portal_module()


def _quarti(valore: str = "0.125", quanti: int = 96) -> dict[str, str]:
    return {f"q{i:03}": valore for i in range(1, quanti + 1)}


class TestParsingBootstrap:
    def test_contesto_diretto(self):
        html = (
            '{"fwuid":"FW-1","loaded":{'
            '"APPLICATION@markup://siteforce:communityApp":"APP-9"},'
            '"eikoocnekot":"__Host-AURA-123"}'
        )
        assert portal._estrai_fwuid(html) == "FW-1"
        assert portal._estrai_loaded_app_id(html) == "APP-9"
        assert portal._estrai_nome_cookie_token(html) == "__Host-AURA-123"

    def test_contesto_percent_encoded(self):
        html = (
            "%22fwuid%22%3A%22FW-2%22%2C%22loaded%22%3A%7B"
            "%22APPLICATION%40markup%3A%2F%2Fsiteforce%3AcommunityApp%22%3A%22APP-10%22"
            "%7D%2C%22eikoocnekot%22%3A%22COOKIE-AURA%22"
        )
        assert portal._estrai_fwuid(html) == "FW-2"
        assert portal._estrai_loaded_app_id(html) == "APP-10"
        assert portal._estrai_nome_cookie_token(html) == "COOKIE-AURA"


class TestNormalizzazione:
    def test_mappa_portale_diventa_schema_api_mobile(self):
        curva = portal._normalizza_mappa(
            {"202610-10": _quarti()},
            portal.MAGNITUDE_PRELEVATA,
            date(2026, 10, 10),
            date(2026, 10, 10),
        )
        assert len(curva) == 1
        giorno = curva[0]
        assert giorno["readings"]["energyType"] == portal.MAGNITUDE_PRELEVATA
        assert giorno["readings"]["sampleDate"] == "20261010"
        assert len(giorno["readings"]["sampleValues"]) == 96
        # Ottobre è ancora in ora legale il giorno 10: mezzanotte italiana
        # corrisponde alle 22:00 UTC del giorno prima.
        assert giorno["initialSample"] == "2026-10-09T22:00:00.000+00:00"

    def test_valori_vuoti_sono_zero_senza_spostare_gli_indici(self):
        valori = list(_quarti().values())
        valori[1] = "--"
        valori[2] = ""
        curva = portal._normalizza_mappa(
            {"202601-15": valori},
            portal.MAGNITUDE_IMMESSA,
            date(2026, 1, 15),
            date(2026, 1, 15),
        )
        campioni = curva[0]["readings"]["sampleValues"]
        assert campioni[1] == {"id": "2", "val": "0"}
        assert campioni[2] == {"id": "3", "val": "0"}

    def test_giorno_primaverile_elimina_i_quattro_slot_inesistenti(self):
        valori = list(_quarti().values())
        valori[8:12] = ["0", "0", "0", "0"]
        curva = portal._normalizza_mappa(
            {"202603-29": valori},
            portal.MAGNITUDE_PRELEVATA,
            date(2026, 3, 29),
            date(2026, 3, 29),
        )
        assert len(curva[0]["readings"]["sampleValues"]) == 92

    def test_numero_quarti_incompatibile_blocca_import(self):
        with pytest.raises(portal.PortalApiError, match="ha restituito 2 quarti"):
            portal._normalizza_mappa(
                {"202610-10": ["1", "2"]},
                portal.MAGNITUDE_PRELEVATA,
                date(2026, 10, 10),
                date(2026, 10, 10),
            )

    def test_intervallo_viene_spezzato_per_mese(self):
        assert portal._intervalli_mensili(date(2026, 1, 30), date(2026, 3, 2)) == [
            (date(2026, 1, 30), date(2026, 1, 31)),
            (date(2026, 2, 1), date(2026, 2, 28)),
            (date(2026, 3, 1), date(2026, 3, 2)),
        ]


class _Cookie:
    def __init__(self, value: str):
        self.value = value


class _CookieJar:
    def __init__(self):
        self.cookies: dict[str, _Cookie] = {}

    def filter_cookies(self, _url):
        return self.cookies


class _Response:
    def __init__(self, status: int, body: str, url: str):
        self.status = status
        self._body = body
        self.url = URL(url)

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Session:
    def __init__(self):
        self.cookie_jar = _CookieJar()
        self.posts: list[dict] = []

    def post(self, url, **kwargs):
        self.posts.append({"url": url, **kwargs})
        if "singleaccess" in url:
            return _Response(
                200,
                json.dumps({
                    "frontdoor_uri": "https://private.e-distribuzione.it/frontdoor-once"
                }),
                url,
            )

        payload = {
            "actions": [{
                "state": "SUCCESS",
                "returnValue": {"MappaDailyLoadProfile": {"202610-10": _quarti()}},
            }]
        }
        return _Response(200, f"while(1);{json.dumps(payload)}", url)

    def get(self, url, **kwargs):
        del kwargs
        if "frontdoor-once" in url:
            self.cookie_jar.cookies["__Host-AURA"] = _Cookie("csrf-token")
            html = (
                '{"fwuid":"FW","loaded":{'
                '"APPLICATION@markup://siteforce:communityApp":"APP"},'
                '"eikoocnekot":"__Host-AURA"}'
            )
            return _Response(
                200,
                html,
                "https://private.e-distribuzione.it/PortaleClienti/s/curvedicarico",
            )
        raise AssertionError(f"GET inattesa: {url}")


async def test_flusso_completo_ui_bridge_aura_e_normalizzazione():
    sessione = _Session()
    client = portal.PortalClient(sessione)
    client.update_token("access-token")

    curva = await client.async_get_daily_load_profile(
        "IT001E12345678",
        date(2026, 10, 10),
        date(2026, 10, 10),
        portal.MAGNITUDE_PRELEVATA,
    )

    assert len(curva) == 1
    assert len(curva[0]["readings"]["sampleValues"]) == 96
    assert len(sessione.posts) == 2
    bridge, aura = sessione.posts
    assert bridge["headers"]["Authorization"] == "Bearer access-token"
    assert "access-token" not in bridge["url"]
    assert aura["data"]["aura.token"] == "csrf-token"
    message = json.loads(aura["data"]["message"])
    params = message["actions"][0]["params"]
    assert params["Magnitude"] == "A+"
    assert params["Startdate"] == "2026-10-10"
    assert urlparse(aura["url"]).path.endswith("/s/sfsites/aura")
