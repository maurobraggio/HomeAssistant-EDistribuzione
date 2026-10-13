"""Fallback dati tramite il portale web Salesforce di E-Distribuzione.

Il pulsante di esportazione del portale non scarica un CSV già pronto da un
endpoint dedicato: il componente Aura chiama
``PED_CurveDiCaricoController.QueryLoadProfile`` e costruisce il CSV nel
browser dalla mappa quartoraria ricevuta. Questo client riproduce la parte
utile di quel flusso e normalizza la risposta nello stesso schema usato dal
backend REST dell'app, così la pipeline di import di Home Assistant resta una
sola.

La sessione UI viene aperta dal normale access_token OAuth (scope ``web``)
con la UI Bridge API ``services/oauth2/singleaccess``. Non vengono quindi
salvati password, cookie o token Aura: ad ogni nuovo access_token il contesto
web viene ricostruito automaticamente.

Questo protocollo resta più fragile dell'API REST: fwuid, cookie-token e
descriptor Aura sono dettagli interni della UI Salesforce. Per questo viene
usato solo come fallback e mai come prima scelta.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any
from urllib.parse import unquote, urljoin, urlparse
from zoneinfo import ZoneInfo

import aiohttp

from .const import (
    MAGNITUDE_IMMESSA,
    MAGNITUDE_PRELEVATA,
    OAUTH_SINGLEACCESS_URL,
    PORTAL_CURVE_PATH,
    PORTAL_CURVE_URL,
    SF_BASE,
)

_LOGGER = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
_FUSO_PORTALE = ZoneInfo("Europe/Rome")
_MAX_JS_REDIRECT = 5
_RE_ANTIBOT = re.compile(r"Pardon\s+Our\s+Interruption", re.IGNORECASE)

_PORTAL_MAGNITUDE = {
    MAGNITUDE_PRELEVATA: "A+",
    MAGNITUDE_IMMESSA: "A-",
}


class PortalApiError(Exception):
    """Il fallback del portale non ha potuto restituire dati affidabili."""


@dataclass
class _AuraContext:
    fwuid: str
    loaded_app_id: str
    token: str
    base_url: str
    page_url: str


def _base_portale(instance_url: str | None) -> str:
    """Base Experience Cloud dalla risposta OAuth, con fallback noto.

    ``instance_url`` di Salesforce può essere l'host ``my.site.com`` oppure
    il dominio personalizzato e può già includere ``/PortaleClienti``.
    """
    if not instance_url:
        return SF_BASE
    parsed = urlparse(instance_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return SF_BASE
    path = parsed.path.rstrip("/")
    if not path:
        path = "/PortaleClienti"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _singleaccess_url(base_url: str) -> str:
    return f"{base_url}/services/oauth2/singleaccess"


def _curve_url(base_url: str) -> str:
    return f"{base_url}/s/curvedicarico"


def _redirect_uri(base_url: str) -> str:
    """Percorso relativo richiesto da UI Bridge, incluso il site suffix."""
    suffix = urlparse(base_url).path.strip("/") or "PortaleClienti"
    return f"{suffix}/s/curvedicarico"


def _estrai_fwuid(html: str) -> str:
    match = re.search(r'"fwuid"\s*:\s*"([^"]+)"', html)
    if match:
        return match.group(1)
    match = re.search(r"%22fwuid%22%3A%22([^%]+)%22", html)
    if match:
        return match.group(1)
    raise PortalApiError("fwuid Aura non trovato nella pagina Curve di carico")


def _estrai_loaded_app_id(html: str) -> str:
    match = re.search(
        r'"APPLICATION@markup://siteforce:communityApp"\s*:\s*"([^"]+)"', html
    )
    if match:
        return match.group(1)
    match = re.search(
        r"%22APPLICATION%40markup%3A%2F%2Fsiteforce%3AcommunityApp%22%3A%22([^%]+)%22",
        html,
    )
    if match:
        return match.group(1)
    raise PortalApiError("Id dell'app Aura communityApp non trovato")


def _estrai_nome_cookie_token(html: str) -> str:
    """Aura offusca ``tokencookie`` al contrario come ``eikoocnekot``."""
    match = re.search(r'"eikoocnekot"\s*:\s*"([^"]+)"', html)
    if match:
        return match.group(1)
    match = re.search(r"%22eikoocnekot%22%3A%22([^%]+)%22", html)
    if match:
        return unquote(match.group(1))
    raise PortalApiError("Nome del cookie che contiene aura.token non trovato")


def _pulisci_risposta_aura(raw: str) -> dict[str, Any]:
    """Rimuove i prefissi anti-XSSI osservati sulle risposte Aura."""
    testo = raw.strip()
    if testo.startswith("while(1);"):
        testo = testo[len("while(1);") :]
    if testo.startswith("/*"):
        testo = testo[2:]
    if testo.endswith("*/"):
        testo = testo[:-2]
    try:
        payload = json.loads(testo)
    except json.JSONDecodeError as err:
        raise PortalApiError(
            f"Risposta non-JSON dall'endpoint Aura (primi 200 caratteri: {testo[:200]!r})"
        ) from err
    if not isinstance(payload, dict):
        raise PortalApiError("Risposta Aura di tipo inatteso")
    return payload


def _data_da_chiave(chiave: object) -> date:
    """Il portale usa chiavi osservate come ``YYYYMM-DD``."""
    cifre = re.sub(r"\D", "", str(chiave))
    if len(cifre) < 8:
        raise PortalApiError(f"Data non interpretabile nella curva del portale: {chiave!r}")
    try:
        return date(int(cifre[:4]), int(cifre[4:6]), int(cifre[6:8]))
    except ValueError as err:
        raise PortalApiError(f"Data non valida nella curva del portale: {chiave!r}") from err


def _valore_kwh(valore: object) -> str | None:
    if valore in (None, "", "--"):
        return None
    testo = str(valore).strip().replace(",", ".")
    if testo in ("", "--"):
        return None
    try:
        float(testo)
    except ValueError as err:
        raise PortalApiError(f"Valore quartorario non numerico dal portale: {valore!r}") from err
    return testo


def _valori_ordinati(valori: object) -> list[object]:
    if isinstance(valori, dict):
        return list(valori.values())
    if isinstance(valori, list):
        return valori
    raise PortalApiError(f"Struttura quartoraria inattesa: {type(valori).__name__}")


def _numero_quarti_reali(giorno: date) -> int:
    inizio = datetime.combine(giorno, time.min, tzinfo=_FUSO_PORTALE)
    fine = datetime.combine(
        date.fromordinal(giorno.toordinal() + 1), time.min, tzinfo=_FUSO_PORTALE
    )
    return int((fine.astimezone(UTC) - inizio.astimezone(UTC)).total_seconds() // 900)


def _normalizza_giorno(giorno: date, valori: object, magnitude: str) -> dict[str, Any] | None:
    grezzi = _valori_ordinati(valori)
    convertiti = [_valore_kwh(v) for v in grezzi]
    if not convertiti or all(v is None for v in convertiti):
        return None

    quarti_reali = _numero_quarti_reali(giorno)
    # Nell'export CSV del giorno primaverile il portale mantiene 96 colonne
    # e mette zero nei quattro slot 02:00-03:00 che non esistono. L'API
    # mobile invece restituisce 92 campioni reali: togliamo quegli slot per
    # conservare la stessa semantica temporale nella pipeline condivisa.
    if quarti_reali == 92 and len(convertiti) == 96:
        convertiti = convertiti[:8] + convertiti[12:]

    if len(convertiti) != quarti_reali:
        raise PortalApiError(
            f"Il {giorno.isoformat()} il portale ha restituito {len(convertiti)} quarti "
            f"d'ora, ma il giorno locale ne contiene {quarti_reali}; import interrotto "
            "per non spostare temporalmente i consumi"
        )

    campioni = [
        {"id": str(indice), "val": valore or "0"}
        for indice, valore in enumerate(convertiti, start=1)
    ]
    mezzanotte_locale = datetime.combine(giorno, time.min, tzinfo=_FUSO_PORTALE)
    inizio_utc = mezzanotte_locale.astimezone(UTC).isoformat(timespec="milliseconds")
    return {
        "readings": {
            # Il resto dell'integrazione usa A1/A2. Il portale usa A+/A-,
            # ma dopo la normalizzazione la direzione deve tornare nel
            # vocabolario interno per la guardia anti-duplicazione.
            "energyType": magnitude,
            "sampleDate": giorno.strftime("%Y%m%d"),
            "sampleValues": campioni,
        },
        "sampleFrequency": 15,
        "timeType": "CONS",
        "initialSample": inizio_utc,
    }


def _normalizza_mappa(
    mappa: object, magnitude: str, data_da: date, data_a: date
) -> list[dict[str, Any]]:
    if not isinstance(mappa, dict):
        raise PortalApiError("MappaDailyLoadProfile mancante o non valida")
    risultato: list[dict[str, Any]] = []
    for chiave, valori in mappa.items():
        giorno = _data_da_chiave(chiave)
        if giorno < data_da or giorno > data_a:
            continue
        normalizzato = _normalizza_giorno(giorno, valori, magnitude)
        if normalizzato is not None:
            risultato.append(normalizzato)
    risultato.sort(key=lambda elemento: elemento["readings"]["sampleDate"])
    return risultato


def _intervalli_mensili(data_da: date, data_a: date) -> list[tuple[date, date]]:
    intervalli: list[tuple[date, date]] = []
    cursore = data_da
    while cursore <= data_a:
        if cursore.month == 12:
            primo_mese_dopo = date(cursore.year + 1, 1, 1)
        else:
            primo_mese_dopo = date(cursore.year, cursore.month + 1, 1)
        fine = min(data_a, date.fromordinal(primo_mese_dopo.toordinal() - 1))
        intervalli.append((cursore, fine))
        cursore = primo_mese_dopo
    return intervalli


class PortalClient:
    """Client Aura usato automaticamente quando il backend REST fallisce."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._access_token = ""
        self._instance_url = ""
        self._context: _AuraContext | None = None
        self._request_counter = 0

    def update_token(self, access_token: str, instance_url: str = "") -> None:
        if access_token != self._access_token or instance_url != self._instance_url:
            self._context = None
        self._access_token = access_token
        self._instance_url = instance_url

    async def _get_html_seguendo_redirect_js(self, url: str) -> tuple[str, str]:
        headers = {"User-Agent": _USER_AGENT}
        corrente = url
        for _ in range(_MAX_JS_REDIRECT + 1):
            async with self._session.get(corrente, headers=headers) as resp:
                corpo = await resp.text()
                finale = str(resp.url)
                if resp.status >= 400:
                    raise PortalApiError(f"Il portale ha risposto HTTP {resp.status} su {finale}")
            if _RE_ANTIBOT.search(corpo):
                raise PortalApiError("La protezione anti-bot ha bloccato la sessione web")
            match = re.search(r"window\.location\.replace\(['\"]([^'\"]+)", corpo)
            if not match:
                match = re.search(r"window\.location\.href\s*=\s*['\"]([^'\"]+)", corpo)
            if not match:
                return corpo, finale
            corrente = urljoin(finale, unquote(match.group(1)))
        raise PortalApiError("Troppi redirect JavaScript aprendo la sessione web")

    def _cookie(self, nome: str, *urls: str) -> str | None:
        for url in urls:
            morsel = self._session.cookie_jar.filter_cookies(url).get(nome)
            if morsel is not None:
                return morsel.value
        return None

    async def _async_open_session(self) -> _AuraContext:
        if not self._access_token:
            raise PortalApiError("Access token assente: impossibile aprire la sessione web")

        base_url = _base_portale(self._instance_url)
        endpoint = _singleaccess_url(base_url)
        # Se instance_url non è utilizzabile, il dominio personalizzato è il
        # fallback. Non si prova una griglia di host: sono gli unici due host
        # restituiti/osservati per questa stessa org Salesforce.
        endpoints = [endpoint]
        if endpoint != OAUTH_SINGLEACCESS_URL:
            endpoints.append(OAUTH_SINGLEACCESS_URL)

        ultimo_errore: Exception | None = None
        frontdoor_uri: str | None = None
        for candidato in endpoints:
            candidato_base = candidato[: -len("/services/oauth2/singleaccess")]
            try:
                async with self._session.post(
                    candidato,
                    headers={
                        "Authorization": f"Bearer {self._access_token}",
                        "User-Agent": _USER_AGENT,
                    },
                    data={"redirect_uri": _redirect_uri(candidato_base)},
                ) as resp:
                    testo = await resp.text()
                    if resp.status != 200:
                        raise PortalApiError(
                            f"UI Bridge non disponibile su {urlparse(candidato).netloc} "
                            f"(HTTP {resp.status})"
                        )
                    try:
                        payload = json.loads(testo)
                    except json.JSONDecodeError as err:
                        raise PortalApiError("Risposta non-JSON dalla UI Bridge API") from err
                frontdoor_uri = payload.get("frontdoor_uri")
                if not frontdoor_uri:
                    raise PortalApiError(f"UI Bridge non ha restituito frontdoor_uri: {payload!r}")
                base_url = candidato_base
                break
            except (aiohttp.ClientError, PortalApiError) as err:
                ultimo_errore = err

        if frontdoor_uri is None:
            raise PortalApiError(f"Impossibile creare la sessione web: {ultimo_errore}")

        html, pagina_finale = await self._get_html_seguendo_redirect_js(frontdoor_uri)
        # Alcune configurazioni di frontdoor ignorano startURL: in quel caso
        # si carica esplicitamente la pagina delle curve con i cookie appena
        # stabiliti.
        if "curvedicarico" not in urlparse(pagina_finale).path.lower():
            html, pagina_finale = await self._get_html_seguendo_redirect_js(_curve_url(base_url))

        if "/login" in urlparse(pagina_finale).path.lower():
            raise PortalApiError("UI Bridge ha rimandato alla pagina di login")

        fwuid = _estrai_fwuid(html)
        loaded_app_id = _estrai_loaded_app_id(html)
        nome_cookie = _estrai_nome_cookie_token(html)
        token = self._cookie(nome_cookie, pagina_finale, base_url, SF_BASE, PORTAL_CURVE_URL)
        if token is None:
            raise PortalApiError(
                f"Il cookie Aura {nome_cookie!r} non è presente dopo il bootstrap"
            )

        self._context = _AuraContext(
            fwuid=fwuid,
            loaded_app_id=loaded_app_id,
            token=token,
            base_url=base_url,
            page_url=pagina_finale,
        )
        return self._context

    async def _async_query(
        self, data_da: date, data_a: date, magnitude: str
    ) -> list[dict[str, Any]]:
        if magnitude not in _PORTAL_MAGNITUDE:
            raise PortalApiError(f"Magnitude non supportata dal portale: {magnitude!r}")
        context = self._context or await self._async_open_session()
        self._request_counter += 1

        message = json.dumps({
            "actions": [{
                "id": f"{self._request_counter};a",
                "descriptor": "apex://PED_CurveDiCaricoController/ACTION$QueryLoadProfile",
                "callingDescriptor": "markup://c:PED_Curva_di_carico_main",
                "params": {
                    "Startdate": data_da.isoformat(),
                    "Enddate": data_a.isoformat(),
                    "Magnitude": _PORTAL_MAGNITUDE[magnitude],
                    "isDelegate": False,
                },
                "longRunning": True,
            }]
        })
        aura_context = json.dumps({
            "mode": "PROD",
            "fwuid": context.fwuid,
            "app": "siteforce:communityApp",
            "loaded": {
                "APPLICATION@markup://siteforce:communityApp": context.loaded_app_id
            },
            "dn": [],
            "globals": {"srcdoc": True},
            "uad": True,
        })

        try:
            async with self._session.post(
                f"{context.base_url}/s/sfsites/aura",
                params={
                    "r": self._request_counter,
                    "other.PED_CurveDiCarico.QueryLoadProfile": 1,
                },
                data={
                    "message": message,
                    "aura.context": aura_context,
                    "aura.pageURI": PORTAL_CURVE_PATH,
                    "aura.token": context.token,
                },
                headers={
                    "User-Agent": _USER_AGENT,
                    "Origin": f"{urlparse(context.base_url).scheme}://{urlparse(context.base_url).netloc}",
                    "Referer": context.page_url,
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                },
            ) as resp:
                raw = await resp.text()
                if resp.status >= 400:
                    raise PortalApiError(f"QueryLoadProfile ha risposto HTTP {resp.status}")
        except aiohttp.ClientError as err:
            raise PortalApiError(f"Errore di trasporto su QueryLoadProfile: {err}") from err

        payload = _pulisci_risposta_aura(raw)
        try:
            action = payload["actions"][0]
        except (KeyError, IndexError, TypeError) as err:
            raise PortalApiError("Risposta QueryLoadProfile senza actions") from err
        if action.get("state") != "SUCCESS":
            raise PortalApiError(f"QueryLoadProfile fallita: {action.get('error')}")
        try:
            mappa = action["returnValue"]["MappaDailyLoadProfile"]
        except (KeyError, TypeError) as err:
            raise PortalApiError("MappaDailyLoadProfile assente nella risposta") from err
        return _normalizza_mappa(mappa, magnitude, data_da, data_a)

    async def async_get_daily_load_profile(
        self,
        pod: str,
        data_da: date,
        data_a: date,
        magnitude: str,
    ) -> list[dict[str, Any]]:
        """Recupera il periodo come farebbe l'export CSV del portale.

        Il controller Aura osservato non accetta il POD come parametro: il
        POD è quello associato al contesto aperto dalla pagina. Il chiamante
        deve quindi usare questo metodo solo quando la config entry contiene
        un singolo POD; la guardia è nel coordinator per evitare qualunque
        possibile attribuzione errata su account multi-POD.
        """
        del pod  # il limite è intenzionale e documentato nel docstring
        risultato: list[dict[str, Any]] = []
        for inizio, fine in _intervalli_mensili(data_da, data_a):
            risultato.extend(await self._async_query(inizio, fine, magnitude))
        return risultato
