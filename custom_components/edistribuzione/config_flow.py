"""Config flow di edistribuzione.

Flusso di setup: user (email+password) -> otp -> pod (multi-select tra i POD
dell'account). Reauth: gli stessi step user/otp (distinti da un ramo interno
su self._reauth_entry), senza tornare a scegliere i POD - solo il
refresh_token della entry esistente viene aggiornato.

Niente selezione di distributore o comune: un solo distributore, un solo
flusso.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)

from .api import ApiClient
from .auth import (
    AuthClient,
    BloccoAntibot,
    InvalidBrowserRedirect,
    InvalidCredentials,
    InvalidOtp,
    ParsingError,
    TroppeSessioni,
)
from .browser_oauth import OAUTH_BROWSER_EXCHANGE_PATH, register_browser_oauth_view
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_INSTANCE_URL,
    CONF_ORA_RICHIESTA,
    CONF_PODS,
    CONF_REFRESH_TOKEN,
    CONF_TIPO_POD,
    DOMAIN,
    ORA_MINIMA_RICHIESTA,
    TIPO_POD_DEFAULT,
    TIPO_POD_PRODUZIONE,
    TIPO_POD_SCAMBIO,
)

_LOGGER = logging.getLogger(__name__)

# Il codice OTP è Optional (non Required) perché lo stesso form serve anche a
# richiedere un nuovo codice senza averne uno da inserire: chi non ha
# ricevuto nulla spunta la casella e sottomette il form vuoto.
STEP_OTP_SCHEMA = vol.Schema({
    vol.Optional("otp", default=""): str,
    vol.Optional("richiedi_nuovo_codice", default=False): bool,
})

AVVISO_OTP_REINVIATO = (
    "Ho chiesto a E-Distribuzione un nuovo codice: controlla email e SMS. Usa l'ultimo arrivato."
)
AVVISO_OTP_INVIO_NON_CONFERMATO = (
    "Attenzione: E-Distribuzione non ha confermato l'invio del codice. Se non "
    "ti arriva nulla, chiudi le altre sessioni aperte (esci dall'app "
    "ufficiale e dal sito), poi spunta \"Richiedi un nuovo codice\" qui sotto "
    "e invia il form senza inserire nessun codice."
)
AVVISO_OTP_SOLO_DA_QUI = (
    "Il codice deve essere quello inviato da questa configurazione: un OTP "
    "generato sul sito o nell'app appartiene a un'altra sessione di login e "
    "verrebbe rifiutato."
)


def _etichetta_pod(pod_info: dict) -> str:
    """'IT001E12345678 - Via Roma 1, Milano (MI)' invece del solo codice
    POD nel selettore, così si riconosce a colpo d'occhio quale immobile è
    senza dover controllare altrove."""
    indirizzo = (
        f"{pod_info.get('PointOfMeasureStreetPrefix', '')} "
        f"{pod_info.get('PointOfMeasureStreet', '')} "
        f"{pod_info.get('PointOfMeasureStreetNumber', '')}, "
        f"{pod_info.get('PointOfMeasureMunicipality', '')} "
        f"({pod_info.get('PointOfMeasureProvince', '')})"
    ).strip()
    return f"{pod_info['IdPod']} - {indirizzo}"


class EdistribuzioneConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow per l'integrazione edistribuzione."""

    VERSION = 1

    def __init__(self) -> None:
        self._session = None
        self._auth: AuthClient | None = None
        self._email: str | None = None
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._browser_auth_url: str | None = None
        self._browser_exchange_url: str | None = None
        self._pods_disponibili: list[dict] = []
        self._reauth_entry: config_entries.ConfigEntry | None = None

    # ------------------------------------------------------------------
    # Login email/password -> OTP
    # ------------------------------------------------------------------

    async def _reinvia_otp(self) -> tuple[str | None, str | None]:
        """Richiede un nuovo OTP dentro la sessione di login corrente.

        È l'unico modo di ottenere un codice valido per Home Assistant
        quando il primo non arriva: un OTP generato sul sito o nell'app
        appartiene a un'altra sessione di login e non può essere convalidato
        qui. Ritorna (chiave_errore, avviso) da passare al form.
        """
        try:
            confermato = await self._auth.async_resend_otp()
        except TroppeSessioni:
            _LOGGER.warning("Reinvio OTP rifiutato: troppe sessioni aperte sull'account")
            return "troppe_sessioni", None
        except Exception:  # noqa: BLE001 - vedi commento in async_step_user
            _LOGGER.exception("Reinvio del codice OTP fallito")
            return "cannot_connect", None
        return None, (AVVISO_OTP_REINVIATO if confermato else AVVISO_OTP_INVIO_NON_CONFERMATO)

    def _form_otp(self, errors: dict[str, str], avviso: str):
        return self.async_show_form(
            step_id="otp",
            data_schema=STEP_OTP_SCHEMA,
            errors=errors,
            description_placeholders={"avviso": avviso},
        )

    async def _otp_senza_codice(self, user_input: dict[str, Any], avviso: str):
        """Gestisce i submit del form OTP che non portano un codice da
        convalidare: richiesta di un nuovo codice, o campo lasciato vuoto.

        Ritorna il form da mostrare, oppure None se c'è un codice e si può
        procedere con async_submit_otp.
        """
        if user_input.get("richiedi_nuovo_codice"):
            errore, avviso_reinvio = await self._reinvia_otp()
            return self._form_otp({"base": errore} if errore else {}, avviso_reinvio or avviso)
        if not user_input.get("otp"):
            return self._form_otp({"base": "otp_mancante"}, avviso)
        return None

    def _avviso_iniziale(self) -> str:
        """Avviso da mostrare la prima volta che si arriva sul form OTP:
        segnala il caso in cui il portale non ha confermato l'invio del
        codice (l'utente aspetterebbe altrimenti un OTP che non arriverà
        mai, senza che nulla glielo dica)."""
        if getattr(self._auth, "otp_invio_confermato", None) is False:
            return AVVISO_OTP_INVIO_NON_CONFERMATO
        return AVVISO_OTP_SOLO_DA_QUI

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Propone subito il browser; il login headless resta un'alternativa."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["browser_login", "password_login"],
        )

    async def async_step_password_login(
        self, user_input: dict[str, Any] | None = None
    ):
        """Vecchio percorso email/password + OTP eseguito dal container."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._email = user_input["email"]
            # Sessione dedicata (non quella condivisa di Home Assistant):
            # questo login passa per una catena di redirect Salesforce che
            # dipende da un cookie di sessione impostato a metà strada, che
            # la sessione condivisa non garantisce di persistere in modo
            # affidabile per questo dominio - il sintomo è un loop di
            # redirect infinito perché il server non vede mai tornare il
            # cookie. Creata una volta e riusata tra i retry di questo
            # stesso flow.
            if self._session is None:
                self._session = async_create_clientsession(self.hass)
            self._auth = AuthClient(self._session)

            try:
                await self._auth.async_begin_login(user_input["email"], user_input["password"])
            except InvalidCredentials:
                errors["base"] = "invalid_auth"
            except TroppeSessioni:
                # Credenziali giuste, ma l'account ha troppe sessioni aperte:
                # nessun OTP viene inviato, non ha senso proseguire allo step
                # successivo a chiederlo.
                _LOGGER.warning("Login rifiutato: troppe sessioni aperte sull'account")
                errors["base"] = "troppe_sessioni"
            except BloccoAntibot:
                return await self.async_step_antibot()
            except ParsingError:
                _LOGGER.exception("Parsing della pagina di login fallito")
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001 - qualunque altro errore imprevisto
                # aiohttp.ClientError, timeout, risposta non-JSON dove ce ne
                # aspettavamo una, o qualunque altra cosa che auth.py non
                # incapsula nelle sue eccezioni dedicate: meglio un errore
                # nel form (col traceback nei log) che far esplodere lo step.
                _LOGGER.exception("Errore imprevisto durante il login")
                errors["base"] = "cannot_connect"
            else:
                return await self.async_step_otp()

        email = (
            vol.Required("email", default=self._email)
            if self._email
            else vol.Required("email")
        )
        return self.async_show_form(
            step_id="password_login",
            data_schema=vol.Schema({email: str, vol.Required("password"): str}),
            errors=errors,
            description_placeholders={"pod_correnti": self._nota_reauth()},
        )

    async def async_step_antibot(self, user_input: dict[str, Any] | None = None):
        """Spiega il blocco Imperva senza farlo sembrare un errore password.

        Un menu, invece del form credenziali con un errore rosso, evita che
        l'utente ripeta subito il login peggiorando il rate/risk score. Il
        pulsante riporta al login quando ha atteso o cambiato rete.
        """
        return self.async_show_menu(
            step_id="antibot",
            menu_options=["browser_login", "riprova_login"],
        )

    async def async_step_browser_login(self, user_input: dict[str, Any] | None = None):
        """Completa OAuth nel browser reale, fuori dal blocco anti-bot.

        Il link contiene soltanto challenge PKCE e state. Home Assistant
        conserva il verifier in memoria e riceve dall'utente il redirect
        finale ``eneldist://`` con un authorization code monouso.
        """
        if self._session is None:
            self._session = async_create_clientsession(self.hass)
        if self._auth is None:
            self._auth = AuthClient(self._session)
        if self._browser_auth_url is None:
            self._browser_auth_url = (
                self._auth.prepare_browser_token_authorization_url()
            )
        register_browser_oauth_view(self.hass)

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                tokens = self._auth.tokens_from_browser_redirect(
                    user_input["redirect_url"]
                )
            except InvalidBrowserRedirect:
                # Compatibilità con un flow iniziato dalla versione 0.2.3:
                # quel link contiene un authorization code PKCE e richiede
                # ancora il vecchio scambio tramite pagina locale.
                try:
                    exchange = self._auth.prepare_browser_token_exchange(
                        user_input["redirect_url"]
                    )
                except InvalidBrowserRedirect:
                    errors["base"] = "invalid_browser_redirect"
                else:
                    fragment = urlencode({
                        "code": exchange.code,
                        "code_verifier": exchange.code_verifier,
                    })
                    self._browser_exchange_url = self._url_scambio_browser(fragment)
                    return await self.async_step_browser_token_exchange()
            else:
                return await self._autenticazione_completata(tokens)

        return self.async_show_form(
            step_id="browser_login",
            data_schema=vol.Schema({vol.Required("redirect_url"): str}),
            errors=errors,
            description_placeholders={
                "authorization_url": self._browser_auth_url,
                "pod_correnti": self._nota_reauth(),
            },
        )

    async def async_step_browser_token_exchange(
        self, user_input: dict[str, Any] | None = None
    ):
        """Riceve la risposta OAuth creata dal token endpoint nel browser."""
        if self._browser_exchange_url is None or self._auth is None:
            return self.async_abort(reason="browser_exchange_missing")

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                tokens = self._auth.tokens_from_browser_payload(
                    user_input["oauth_response"]
                )
            except InvalidBrowserRedirect:
                errors["base"] = "invalid_browser_token"
            else:
                return await self._autenticazione_completata(tokens)

        return self.async_show_form(
            step_id="browser_token_exchange",
            data_schema=vol.Schema({
                vol.Required("oauth_response"): selector.TextSelector(
                    selector.TextSelectorConfig(multiline=True)
                )
            }),
            errors=errors,
            description_placeholders={
                "exchange_url": self._browser_exchange_url,
            },
        )

    async def async_step_riprova_login(self, user_input: dict[str, Any] | None = None):
        """Torna al login conservando l'email già inserita."""
        return await self.async_step_password_login()

    def _nota_reauth(self) -> str:
        """Frase aggiuntiva mostrata solo durante un reauth, per ricordare
        quali POD verranno riautenticati - vuota durante il setup iniziale."""
        if self._reauth_entry is None:
            return ""
        pods = ", ".join(self._reauth_entry.data.get(CONF_PODS, []))
        return f" Stai aggiornando le credenziali per i POD: {pods}."

    def _url_scambio_browser(self, fragment: str) -> str:
        """URL assoluto per evitare che il router frontend intercetti /api."""
        base_url = self.hass.config.internal_url or self.hass.config.external_url
        if not base_url:
            api = self.hass.config.api
            scheme = "https" if api is not None and api.use_ssl else "http"
            port = api.port if api is not None else 8123
            # 127.0.0.1 è intenzionalmente diverso dall'origine tipica
            # ``localhost`` del frontend: così il router SPA non intercetta
            # il link /api e il browser esegue davvero la richiesta HTTP.
            base_url = f"{scheme}://127.0.0.1:{port}"
        return f"{base_url.rstrip('/')}{OAUTH_BROWSER_EXCHANGE_PATH}#{fragment}"

    async def async_step_otp(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        avviso = self._avviso_iniziale()

        if user_input is not None:
            form_senza_codice = await self._otp_senza_codice(user_input, avviso)
            if form_senza_codice is not None:
                return form_senza_codice

            try:
                tokens = await self._auth.async_submit_otp(user_input["otp"])
            except InvalidOtp:
                errors["base"] = "invalid_otp"
            except TroppeSessioni:
                errors["base"] = "troppe_sessioni"
            except ParsingError:
                # A questo punto l'OTP è già stato accettato da Salesforce
                # (altrimenti avremmo preso InvalidOtp sopra): il
                # fallimento è nel parsing di uno step successivo, non nel
                # codice inserito. Ririmostrare il form OTP non aiuta -
                # l'OTP è monouso e il ViewState è già avanzato, un retry
                # con lo stesso codice fallirebbe di nuovo allo stesso modo.
                _LOGGER.exception("Parsing della pagina OTP fallito")
                return self.async_abort(reason="otp_exchange_failed")
            except Exception:  # noqa: BLE001 - vedi commento in async_step_user
                _LOGGER.exception("Errore imprevisto durante lo scambio del codice OTP")
                return self.async_abort(reason="otp_exchange_failed")
            else:
                return await self._autenticazione_completata(tokens)

        return self._form_otp(errors, avviso)

    async def _autenticazione_completata(self, tokens):
        """Prosegue allo stesso modo dopo OTP headless o login nel browser."""
        self._access_token = tokens.access_token
        self._refresh_token = tokens.refresh_token
        self._instance_url = getattr(tokens, "instance_url", "")
        if self._reauth_entry is not None:
            nuovi_dati = {
                **self._reauth_entry.data,
                CONF_ACCESS_TOKEN: tokens.access_token,
                CONF_REFRESH_TOKEN: tokens.refresh_token,
                CONF_INSTANCE_URL: self._instance_url,
            }
            self.hass.config_entries.async_update_entry(
                self._reauth_entry, data=nuovi_dati
            )
            await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
            return self.async_abort(reason="reauth_successful")
        return await self.async_step_pod()

    # ------------------------------------------------------------------
    # Selezione POD (solo per il setup iniziale, non per il reauth)
    # ------------------------------------------------------------------

    async def async_step_pod(self, user_input: dict[str, Any] | None = None):
        session = async_get_clientsession(self.hass)
        api = ApiClient(session, self._access_token)

        if not self._pods_disponibili:
            try:
                self._pods_disponibili = await api.async_get_supplies()
            except Exception:  # noqa: BLE001 - vedi commento in async_step_user
                # Login e OTP sono già andati a buon fine: un OTP è
                # utilizzabile una sola volta, un retry di questo step non
                # risolverebbe nulla senza rifare login+OTP da capo.
                _LOGGER.exception("Errore imprevisto nel recupero dei POD")
                return self.async_abort(reason="supplies_failed")

        if not self._pods_disponibili:
            return self.async_abort(reason="no_pods_found")

        def crea_entry(pods: list[str]):
            titolo = pods[0] if len(pods) == 1 else f"{len(pods)} POD"
            return self.async_create_entry(
                title=titolo,
                data={
                    CONF_PODS: pods,
                    CONF_ACCESS_TOKEN: self._access_token,
                    CONF_REFRESH_TOKEN: self._refresh_token,
                    CONF_INSTANCE_URL: self._instance_url,
                },
            )

        # Con un solo POD sull'account non serve far scegliere.
        if len(self._pods_disponibili) == 1:
            return crea_entry([self._pods_disponibili[0]["IdPod"]])

        if user_input is not None:
            scelti = user_input[CONF_PODS]
            if not scelti:
                return self.async_show_form(
                    step_id="pod",
                    data_schema=self._schema_multi_pod(),
                    errors={"pods": "nessun_pod_selezionato"},
                )
            return crea_entry(scelti)

        return self.async_show_form(step_id="pod", data_schema=self._schema_multi_pod())

    def _schema_multi_pod(self) -> vol.Schema:
        pod_ids = [p["IdPod"] for p in self._pods_disponibili]
        opzioni = [{"value": p["IdPod"], "label": _etichetta_pod(p)} for p in self._pods_disponibili]
        return vol.Schema({
            vol.Required(CONF_PODS, default=pod_ids): selector.SelectSelector(
                selector.SelectSelectorConfig(options=opzioni, multiple=True)
            )
        })

    # ------------------------------------------------------------------
    # Reauth: stesso login+OTP, niente selezione POD
    # ------------------------------------------------------------------

    async def async_step_reauth(self, entry_data: dict[str, Any]):
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        # La reauth è richiesta proprio quando il refresh automatico non è
        # più utilizzabile. Il browser è il percorso più affidabile contro
        # Imperva e non richiede di consegnare la password a Home Assistant.
        return await self.async_step_browser_login()

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> EdistribuzioneOptionsFlow:
        return EdistribuzioneOptionsFlow()


class EdistribuzioneOptionsFlow(config_entries.OptionsFlow):
    """Ruolo del POD, aggiungi/rimuovi POD, orario della richiesta - dopo
    la configurazione iniziale."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "riautentica",
                "tipo_pod",
                "aggiungi_pod",
                "rimuovi_pod",
                "orario",
            ],
        )

    async def async_step_riautentica(self, user_input: dict[str, Any] | None = None):
        """Avvia volontariamente il reauth senza rimuovere la config entry."""
        self.config_entry.async_start_reauth(self.hass)
        return self.async_abort(reason="reauth_started")

    async def async_step_tipo_pod(self, user_input: dict[str, Any] | None = None):
        pods = list(self.config_entry.data.get(CONF_PODS, []))
        tipi_attuali = dict(self.config_entry.options.get(CONF_TIPO_POD, {}))

        if user_input is not None:
            nuovi_tipi = {pod: user_input[f"tipo_{pod}"] for pod in pods}
            nuove_opzioni = {**self.config_entry.options, CONF_TIPO_POD: nuovi_tipi}
            # Aggiornata PRIMA del reload, cosi' i sensori vengono ricostruiti
            # con il ruolo nuovo. async_create_entry(data=...) qui sotto
            # sostituisce INTERAMENTE entry.options con 'data' quando
            # l'options flow si conclude: un data={} azzererebbe anche
            # CONF_ORA_RICHIESTA impostato in precedenza, quindi si rimanda
            # lo stesso dizionario completo gia' applicato (nessuna doppia
            # scrittura diversa, solo la conferma finale richiesta dal flow).
            self.hass.config_entries.async_update_entry(self.config_entry, options=nuove_opzioni)
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            return self.async_create_entry(title="", data=nuove_opzioni)

        opzioni_ruolo = [
            {"value": TIPO_POD_SCAMBIO, "label": "Contatore normale / scambio"},
            {"value": TIPO_POD_PRODUZIONE, "label": "Contatore fotovoltaico / produzione"},
        ]
        schema = {
            vol.Required(
                f"tipo_{pod}", default=tipi_attuali.get(pod, TIPO_POD_DEFAULT)
            ): selector.SelectSelector(selector.SelectSelectorConfig(options=opzioni_ruolo))
            for pod in pods
        }
        return self.async_show_form(step_id="tipo_pod", data_schema=vol.Schema(schema))

    async def async_step_orario(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            ora = int(user_input[CONF_ORA_RICHIESTA])
            return self.async_create_entry(
                title="", data={**self.config_entry.options, CONF_ORA_RICHIESTA: ora}
            )

        attuale = self.config_entry.options.get(CONF_ORA_RICHIESTA, ORA_MINIMA_RICHIESTA)
        return self.async_show_form(
            step_id="orario",
            data_schema=vol.Schema({
                vol.Required(CONF_ORA_RICHIESTA, default=attuale): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=23, step=1, mode=selector.NumberSelectorMode.BOX
                    )
                )
            }),
            description_placeholders={"ora_attuale": str(attuale)},
        )

    async def async_step_aggiungi_pod(self, user_input: dict[str, Any] | None = None):
        pods_attuali = list(self.config_entry.data.get(CONF_PODS, []))

        if user_input is not None:
            nuovi = user_input.get("pods_da_aggiungere", [])
            if not nuovi:
                return self.async_abort(reason="nessun_pod_selezionato")
            pods_finali = pods_attuali + [p for p in nuovi if p not in pods_attuali]
            new_data = {**self.config_entry.data, CONF_PODS: pods_finali}
            self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            # data=... qui è il dizionario di OPZIONI (non di data) con cui
            # l'options flow si conclude: passare le opzioni correnti
            # invariate, non {}, altrimenti azzererebbe CONF_TIPO_POD/
            # CONF_ORA_RICHIESTA già impostati.
            return self.async_create_entry(title="", data=dict(self.config_entry.options))

        session = async_get_clientsession(self.hass)
        access_token = self.config_entry.data.get(CONF_ACCESS_TOKEN)
        if not access_token:
            auth = AuthClient(session)
            try:
                tokens = await auth.async_refresh_access_token(
                    self.config_entry.data[CONF_REFRESH_TOKEN]
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Refresh del token fallito nelle opzioni")
                return self.async_abort(reason="refresh_failed")
            access_token = tokens.access_token

        api = ApiClient(session, access_token)
        try:
            tutti_pod = await api.async_get_supplies()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Recupero POD fallito nelle opzioni")
            return self.async_abort(reason="supplies_failed")

        pods_disponibili = [p for p in tutti_pod if p["IdPod"] not in pods_attuali]
        if not pods_disponibili:
            return self.async_abort(reason="nessun_pod_da_aggiungere")

        opzioni = [{"value": p["IdPod"], "label": _etichetta_pod(p)} for p in pods_disponibili]

        return self.async_show_form(
            step_id="aggiungi_pod",
            data_schema=vol.Schema({
                vol.Required("pods_da_aggiungere", default=[]): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=opzioni, multiple=True)
                )
            }),
            description_placeholders={"pod_correnti": ", ".join(pods_attuali) or "nessuno"},
        )

    async def async_step_rimuovi_pod(self, user_input: dict[str, Any] | None = None):
        pods = list(self.config_entry.data.get(CONF_PODS, []))
        if not pods:
            return self.async_abort(reason="nessun_pod")

        if user_input is not None:
            da_rimuovere = set(user_input.get("pods_da_rimuovere", []))
            if len(da_rimuovere) >= len(pods):
                return self.async_show_form(
                    step_id="rimuovi_pod",
                    data_schema=self._schema_rimuovi(pods),
                    errors={"pods_da_rimuovere": "non_puoi_rimuoverli_tutti"},
                )
            pods_rimasti = [p for p in pods if p not in da_rimuovere]
            new_data = {**self.config_entry.data, CONF_PODS: pods_rimasti}
            self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            # Vedi il commento in async_step_aggiungi_pod: 'data' qui sono le
            # opzioni con cui il flow si conclude, non {}.
            return self.async_create_entry(title="", data=dict(self.config_entry.options))

        return self.async_show_form(step_id="rimuovi_pod", data_schema=self._schema_rimuovi(pods))

    @staticmethod
    def _schema_rimuovi(pods: list[str]) -> vol.Schema:
        return vol.Schema({
            vol.Required("pods_da_rimuovere", default=[]): selector.SelectSelector(
                selector.SelectSelectorConfig(options=pods, multiple=True)
            )
        })
