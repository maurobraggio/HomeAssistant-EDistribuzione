"""Test per api.py: costruzione header e parsing delle risposte, nessuna
chiamata di rete reale.

Come tests/test_auth.py, api.py non dipende da Home Assistant, ma viene
comunque caricato via importlib bypassando __init__.py (che lo fa).

I payload di successo usati come fixture NON sono inventati: sono risposte
reali osservate testando il protocollo (2 POD veri via async_get_supplies,
una curva di carico giornaliera reale via async_get_daily_load_profile) -
anonimizzati sostituendo indirizzo e codice fiscale con valori di fantasia,
la struttura è quella vera.
"""
from __future__ import annotations

import datetime
import importlib.util
import sys
import types
from pathlib import Path

import aiohttp
import pytest

PKG_DIR = Path(__file__).parent.parent / "custom_components" / "edistribuzione"


def _load_api_module():
    pkg_name = "edistribuzione_test_api"
    if f"{pkg_name}.api" in sys.modules:
        return sys.modules[f"{pkg_name}.api"]

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
    return _load("api", "api.py")


api = _load_api_module()


# ---------------------------------------------------------------------------
# Infrastruttura minima per simulare le risposte
# ---------------------------------------------------------------------------


class _Risposta:
    def __init__(self, status: int, corpo):
        self.status = status
        self._corpo = corpo

    async def json(self, content_type=None):
        return self._corpo

    async def text(self):
        import json as json_mod

        return json_mod.dumps(self._corpo, ensure_ascii=False)

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=None, history=(), status=self.status, message=f"status {self.status}"
            )

    @property
    def headers(self):
        return {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Sessione:
    """Route per URL: {frammento_url: (status, corpo)}. Registra anche i
    params dell'ultima GET, per verificare cosa viene mandato (es. magnitude)."""

    def __init__(self, rotte: dict[str, tuple[int, object]]):
        self._rotte = rotte
        self.ultimi_params: dict | None = None

    def _risposta_per(self, url: str) -> _Risposta:
        for frammento, (status, corpo) in self._rotte.items():
            if frammento in url:
                return _Risposta(status, corpo)
        raise AssertionError(f"Nessuna rotta configurata per l'URL: {url}")

    def get(self, url, headers=None, params=None):
        self.ultimi_params = params
        return self._risposta_per(url)

    def post(self, url, headers=None, json=None):
        return self._risposta_per(url)


def _client(rotte: dict[str, tuple[int, object]], token: str = "test-token"):
    return api.ApiClient(_Sessione(rotte), access_token=token), None


def _client_e_sessione(rotte: dict[str, tuple[int, object]], token: str = "test-token"):
    sessione = _Sessione(rotte)
    return api.ApiClient(sessione, access_token=token), sessione


# ---------------------------------------------------------------------------
# _headers
# ---------------------------------------------------------------------------


class TestHeaders:
    def test_contiene_bearer_token(self):
        client, _ = _client({})
        headers = client._headers("QUALSIASI")
        assert headers["Authorization"] == "Bearer test-token"

    def test_contiene_method_user_richiesto(self):
        client, _ = _client({})
        headers = client._headers("ELENCO_POD")
        assert headers["Method_User"] == "ELENCO_POD"

    def test_update_token_aggiorna_authorization(self):
        client, _ = _client({})
        client.update_token("nuovo-token")
        headers = client._headers("X")
        assert headers["Authorization"] == "Bearer nuovo-token"


# ---------------------------------------------------------------------------
# async_get_supplies
# ---------------------------------------------------------------------------

PAYLOAD_SUPPLIES_REALE = {
    "data": [
        {
            "pods": [
                {
                    "IdPod": "IT001E12345678",
                    "SupplyStatusId": "ATT",
                    "TaxCode": "RSSMRA80A01H501U",
                    "VatNumber": None,
                    "PointOfMeasurePostalCode": "00100",
                    "PointOfMeasureProvince": "RM",
                    "PointOfMeasureStreet": "ROMA",
                    "PointOfMeasureMunicipality": "ROMA",
                    "VoltageLevel": "BM",
                    "ContractualPower": 3,
                    "AvailablePower": 3.3,
                    "HasPlant": False,
                },
                {
                    "IdPod": "IT001E87654321",
                    "SupplyStatusId": "ATT",
                    "TaxCode": "RSSMRA80A01H501U",
                    "PointOfMeasureProvince": "RM",
                    "ContractualPower": 6,
                    "AvailablePower": 6.6,
                    "HasPlant": False,
                },
            ]
        }
    ]
}


class TestAsyncGetSupplies:
    async def test_estrae_i_pod_dal_payload_reale(self):
        client, _ = _client({"getSupplies": (200, PAYLOAD_SUPPLIES_REALE)})
        pods = await client.async_get_supplies()
        assert len(pods) == 2
        assert pods[0]["IdPod"] == "IT001E12345678"
        assert pods[1]["IdPod"] == "IT001E87654321"

    async def test_payload_senza_data_ritorna_lista_vuota(self):
        client, _ = _client({"getSupplies": (200, {})})
        assert await client.async_get_supplies() == []

    async def test_data_vuoto_ritorna_lista_vuota(self):
        client, _ = _client({"getSupplies": (200, {"data": []})})
        assert await client.async_get_supplies() == []

    async def test_status_errore_solleva_eccezione(self):
        client, _ = _client({"getSupplies": (500, {"meta": {"status": "KO"}})})
        with pytest.raises(aiohttp.ClientResponseError):
            await client.async_get_supplies()


# ---------------------------------------------------------------------------
# async_get_daily_load_profile (via _get_json)
# ---------------------------------------------------------------------------

PAYLOAD_CURVA_REALE = {
    "data": [
        {
            "readings": {
                "energyType": "A1",
                "sampleDate": "20260801",
                "sampleValues": [
                    {"id": "1", "val": "0.359"},
                    {"id": "2", "val": "0.358"},
                    {"id": "3", "val": "0.287"},
                    {"id": "4", "val": "0.349"},
                ],
            },
            "sampleFrequency": 15,
            "timeType": "CONS",
            "initialSample": "2026-07-31T22:00:00.000+00:00",
        }
    ]
}

# Confermato con un test reale su un intervallo VERO (rangeDateFrom !=
# rangeDateTo): l'endpoint ha davvero restituito tutti i giorni richiesti in
# un'unica risposta (fino a 181 testati, qui solo 3 per brevità).
PAYLOAD_CURVA_MULTIGIORNO_REALE = {
    "data": [
        {
            "readings": {"energyType": "A1", "sampleDate": "20260101", "sampleValues": [
                {"id": "1", "val": "0.100"}, {"id": "2", "val": "0.100"},
            ]},
            "sampleFrequency": 15, "timeType": "CONS",
            "initialSample": "2025-12-31T23:00:00.000+00:00",
        },
        {
            "readings": {"energyType": "A1", "sampleDate": "20260102", "sampleValues": [
                {"id": "1", "val": "0.200"}, {"id": "2", "val": "0.200"},
            ]},
            "sampleFrequency": 15, "timeType": "CONS",
            "initialSample": "2026-01-01T23:00:00.000+00:00",
        },
        {
            "readings": {"energyType": "A1", "sampleDate": "20260103", "sampleValues": [
                {"id": "1", "val": "0.300"}, {"id": "2", "val": "0.300"},
            ]},
            "sampleFrequency": 15, "timeType": "CONS",
            "initialSample": "2026-01-02T23:00:00.000+00:00",
        },
    ]
}


class TestAsyncGetDailyLoadProfile:
    async def test_intervallo_vero_ritorna_piu_giorni(self):
        """Non va assunto che serva un ciclo giorno per giorno: un intervallo
        vero restituisce più giorni in un'unica risposta."""
        client, _ = _client({"querydailyloadprofile": (200, PAYLOAD_CURVA_MULTIGIORNO_REALE)})
        curva = await client.async_get_daily_load_profile(
            "IT001E12345678", datetime.date(2026, 1, 1), datetime.date(2026, 1, 3)
        )
        assert len(curva) == 3
        date_ricevute = [g["readings"]["sampleDate"] for g in curva]
        assert date_ricevute == ["20260101", "20260102", "20260103"]

    async def test_estrae_i_dati_dal_payload_reale(self):
        client, _ = _client({"querydailyloadprofile": (200, PAYLOAD_CURVA_REALE)})
        curva = await client.async_get_daily_load_profile("IT001E12345678", datetime.date(2026, 8, 1))
        assert len(curva) == 1
        assert curva[0]["sampleFrequency"] == 15
        assert len(curva[0]["readings"]["sampleValues"]) == 4

    async def test_404_e_trattato_come_nessun_dato_non_come_errore(self):
        """Il backend risponde 404 (non 200 con data:[] vuoto) quando i dati
        del giorno richiesto non sono ancora pubblicati: va trattato come
        'nessun dato ancora', non come errore fatale."""
        client, _ = _client({"querydailyloadprofile": (404, "")})
        curva = await client.async_get_daily_load_profile("IT001E12345678", datetime.date(2026, 8, 20))
        assert curva == []

    async def test_401_solleva_errore_specifico(self):
        client, _ = _client({"querydailyloadprofile": (401, {})})
        with pytest.raises(api.ApiError, match="401"):
            await client.async_get_daily_load_profile("IT001E12345678", datetime.date(2026, 8, 1))

    async def test_meta_status_ko_solleva_errore(self):
        client, _ = _client(
            {"querydailyloadprofile": (200, {"meta": {"status": "KO", "message": "Generic Error"}})}
        )
        with pytest.raises(api.ApiError, match="KO"):
            await client.async_get_daily_load_profile("IT001E12345678", datetime.date(2026, 8, 1))

    async def test_meta_status_ok_passa(self):
        client, _ = _client(
            {
                "querydailyloadprofile": (
                    200,
                    {"meta": {"status": "OK"}, "data": PAYLOAD_CURVA_REALE["data"]},
                )
            }
        )
        curva = await client.async_get_daily_load_profile("IT001E12345678", datetime.date(2026, 8, 1))
        assert len(curva) == 1

    async def test_meta_assente_e_trattato_come_ok(self):
        """Il payload reale osservato non ha affatto una chiave 'meta': deve
        passare comunque (meta.status is None -> considerato OK)."""
        client, _ = _client({"querydailyloadprofile": (200, PAYLOAD_CURVA_REALE)})
        curva = await client.async_get_daily_load_profile("IT001E12345678", datetime.date(2026, 8, 1))
        assert curva == PAYLOAD_CURVA_REALE["data"]

    async def test_date_to_omesso_richiede_un_solo_giorno(self):
        """Senza date_to, un solo giorno (rangeDateFrom == rangeDateTo)."""
        client, sessione = _client_e_sessione({"querydailyloadprofile": (200, PAYLOAD_CURVA_REALE)})
        await client.async_get_daily_load_profile("IT001E12345678", datetime.date(2026, 8, 1))
        assert sessione.ultimi_params["rangeDateFrom"] == sessione.ultimi_params["rangeDateTo"]

    async def test_date_to_specificato_viene_accettato(self):
        client, _ = _client({"querydailyloadprofile": (200, PAYLOAD_CURVA_REALE)})
        curva = await client.async_get_daily_load_profile(
            "IT001E12345678", datetime.date(2026, 8, 1), datetime.date(2026, 8, 3)
        )
        assert curva == PAYLOAD_CURVA_REALE["data"]


# ---------------------------------------------------------------------------
# magnitude: deve finire nei params col valore passato dal chiamante
# (è quello che permette al coordinator di chiedere sia la prelevata che
# l'immessa con la stessa funzione, senza nessuna modifica qui).
# ---------------------------------------------------------------------------


class TestMagnitudeParametro:
    async def test_default_e_la_prelevata(self):
        client, sessione = _client_e_sessione({"querydailyloadprofile": (200, PAYLOAD_CURVA_REALE)})
        await client.async_get_daily_load_profile("IT001E12345678", datetime.date(2026, 8, 1))
        assert sessione.ultimi_params["magnitude"] == api.MAGNITUDE_PRELEVATA

    async def test_magnitude_esplicita_viene_passata_cosi_com_e(self):
        client, sessione = _client_e_sessione({"querydailyloadprofile": (200, PAYLOAD_CURVA_REALE)})
        await client.async_get_daily_load_profile(
            "IT001E12345678", datetime.date(2026, 8, 1), magnitude="A2"
        )
        assert sessione.ultimi_params["magnitude"] == "A2"

    async def test_due_chiamate_con_magnitude_diverse_sono_indipendenti(self):
        """Il coordinator chiama questa funzione due volte, una per
        direzione: verifica che non ci sia stato condiviso tra le chiamate
        che faccia trapelare la prima magnitude nella seconda."""
        client, sessione = _client_e_sessione({"querydailyloadprofile": (200, PAYLOAD_CURVA_REALE)})
        await client.async_get_daily_load_profile(
            "IT001E12345678", datetime.date(2026, 8, 1), magnitude="A1"
        )
        assert sessione.ultimi_params["magnitude"] == "A1"
        await client.async_get_daily_load_profile(
            "IT001E12345678", datetime.date(2026, 8, 1), magnitude="A2"
        )
        assert sessione.ultimi_params["magnitude"] == "A2"
