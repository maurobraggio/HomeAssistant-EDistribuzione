"""Client per le API dati di E-Distribuzione (xs-misura-p.de-c1.eu1.cloudhub.io).

Rispetto ad auth.py, questa è un'API REST/JSON normale dietro un Bearer
token - molto meno fragile. L'unica particolarità è l'header `Method_User`,
che il backend usa come discriminatore di permessi per endpoint.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

import aiohttp

from .const import (
    MAGNITUDE_PRELEVATA,
    METHOD_USER_CURVA_GIORNO,
    METHOD_USER_CURVA_MESE,
    METHOD_USER_CURVA_PERIODO,
    METHOD_USER_ELENCO_POD,
    METHOD_USER_LETTURE,
    MISURE_DAILY_LOAD_PROFILE_URL,
    MISURE_GET_SUPPLIES_URL,
    MISURE_MONTHLY_LOAD_PROFILE_URL,
    MISURE_MONTHLY_TIME_OF_USE_URL,
    MISURE_READING_URL,
)

_LOGGER = logging.getLogger(__name__)


class ApiError(Exception):
    """Alzata quando l'API risponde con un meta.status non-OK o un errore di trasporto."""


class ApiClient:
    """Wrapper leggero sugli endpoint 'misure'. Auth/refresh è gestito a
    monte dal coordinator, che passa qui un access_token valido."""

    def __init__(self, session: aiohttp.ClientSession, access_token: str) -> None:
        self._session = session
        self._access_token = access_token

    def update_token(self, access_token: str) -> None:
        self._access_token = access_token

    def _headers(self, method_user: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Method_User": method_user,
            "Accept": "*/*",
            # Uno User-Agent generico va bene: è l'header Method_User quello
            # su cui il backend chiave davvero i permessi, non la stringa UA.
            "User-Agent": "HomeAssistant-edistribuzione",
            "client_id": "",
            "client_secret": "",
        }

    async def _get_json(self, url: str, method_user: str, params: dict) -> dict:
        async with self._session.get(
            url, headers=self._headers(method_user), params=params
        ) as resp:
            if resp.status == 401:
                raise ApiError("401 Unauthorized - access_token scaduto")
            if resp.status == 404:
                # Osservato chiedendo il giorno corrente all'01:01 (dati del
                # giorno prima verosimilmente non ancora pubblicati): il
                # backend risponde 404 invece di un 200 con data:[] vuoto
                # come nelle altre richieste andate a vuoto. Trattato come
                # "nessun dato disponibile ancora" (stessa semantica di
                # data:[] vuoto), non come errore fatale.
                body_preview = await resp.text()
                _LOGGER.debug(
                    "404 su %s (probabile 'nessun dato ancora disponibile', non "
                    "un errore). Corpo (primi 300 caratteri): %r",
                    url,
                    body_preview[:300],
                )
                return {"data": []}
            if resp.status >= 400:
                body_preview = await resp.text()
                _LOGGER.error(
                    "%s ha risposto %s. Corpo (primi 500 caratteri): %r",
                    url,
                    resp.status,
                    body_preview[:500],
                )
            resp.raise_for_status()
            payload = await resp.json(content_type=None)

        meta = payload.get("meta", {})
        if meta.get("status") not in ("OK", None):
            raise ApiError(f"API returned meta.status={meta.get('status')}: {meta}")
        return payload

    async def async_get_supplies(self) -> list[dict[str, Any]]:
        """Elenco dei POD (punti di prelievo) associati all'account."""
        # json={} invece di headers manuali con corpo assente: il backend è
        # MuleSoft (cloudhub.io), e con Content-Type dichiarato ma nessun
        # corpo il parser lato server fallisce silenziosamente con un 500
        # generico invece di un errore di validazione preciso.
        async with self._session.post(
            MISURE_GET_SUPPLIES_URL,
            headers=self._headers(METHOD_USER_ELENCO_POD),
            json={},
        ) as resp:
            if resp.status >= 400:
                body_preview = await resp.text()
                _LOGGER.error(
                    "getSupplies ha risposto %s. Corpo (primi 500 caratteri): %r",
                    resp.status,
                    body_preview[:500],
                )
            resp.raise_for_status()
            payload = await resp.json(content_type=None)
        try:
            return payload["data"][0]["pods"]
        except (KeyError, IndexError):
            return []

    async def async_get_reading(self, pod: str, date_from: date, date_to: date) -> list[dict[str, Any]]:
        """Letture ufficiali mensili cumulative (EA/ER per fascia + picchi POT)."""
        params = {
            "pointofdelivery": pod,
            "rangeDateFrom": date_from.isoformat(),
            "rangeDateTo": date_to.isoformat(),
        }
        payload = await self._get_json(MISURE_READING_URL, METHOD_USER_LETTURE, params)
        return payload.get("data", [])

    async def async_get_daily_load_profile(
        self,
        pod: str,
        date_from: date,
        date_to: date | None = None,
        magnitude: str = MAGNITUDE_PRELEVATA,
    ) -> list[dict[str, Any]]:
        """Curva di carico oraria/quartoraria.

        Se date_to è None, richiede un solo giorno (date_from == date_to
        nella richiesta). L'endpoint accetta rangeDateFrom/rangeDateTo come
        intervallo vero, confermato funzionante fino a 181 giorni in
        un'unica risposta.

        'magnitude' seleziona la direzione dell'energia (vedi MAGNITUDE_* in
        const.py) - il chiamante decide quale/quali richiedere, questa
        funzione non assume nulla sul default oltre alla prelevata.
        """
        if date_to is None:
            date_to = date_from
        params = {
            "pointofdelivery": pod,
            "rangeDateFrom": date_from.isoformat(),
            "rangeDateTo": date_to.isoformat(),
            "magnitude": magnitude,
        }
        payload = await self._get_json(
            MISURE_DAILY_LOAD_PROFILE_URL, METHOD_USER_CURVA_GIORNO, params
        )
        return payload.get("data", [])

    async def async_get_monthly_load_profile(
        self,
        date_from: date,
        date_to: date,
        pod: str,
        magnitude: str = MAGNITUDE_PRELEVATA,
    ) -> list[dict[str, Any]]:
        """Totale di consumo per giorno su un intervallo."""
        params = {
            "pointofdelivery": pod,
            "rangeDateFrom": date_from.isoformat(),
            "rangeDateTo": date_to.isoformat(),
            "magnitude": magnitude,
        }
        payload = await self._get_json(
            MISURE_MONTHLY_LOAD_PROFILE_URL, METHOD_USER_CURVA_MESE, params
        )
        return payload.get("data", [])

    async def async_get_monthly_time_of_use(
        self,
        pod: str,
        date_from: date,
        date_to: date,
        magnitude: str = MAGNITUDE_PRELEVATA,
    ) -> list[dict[str, Any]]:
        """Totale mensile per fascia (T1-T4) + picchi di potenza."""
        params = {
            "pointofdelivery": pod,
            "rangeDateFrom": date_from.isoformat(),
            "rangeDateTo": date_to.isoformat(),
            "magnitude": magnitude,
        }
        payload = await self._get_json(
            MISURE_MONTHLY_TIME_OF_USE_URL, METHOD_USER_CURVA_PERIODO, params
        )
        return payload.get("data", [])
