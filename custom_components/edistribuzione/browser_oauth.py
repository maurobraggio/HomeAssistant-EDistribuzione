"""Pagina locale per completare lo scambio OAuth dal browser.

Imperva consente il flusso interattivo nel browser ma può bloccare lo stesso
POST ``/oauth2/token`` quando parte da aiohttp nel container. Questa view
serve soltanto HTML/JavaScript statico: code e PKCE verifier restano nel
fragment ``#`` dell'URL (mai inviato a Home Assistant) e vengono inseriti in
un normale form POST diretto all'endpoint OAuth. Una navigazione form non è
soggetta a CORS, a differenza di ``fetch``.
"""
from __future__ import annotations

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import OAUTH_CLIENT_ID, OAUTH_REDIRECT_URI, OAUTH_TOKEN_URL

OAUTH_BROWSER_EXCHANGE_PATH = "/api/edistribuzione/oauth_browser_exchange"
_REGISTRATION_KEY = "edistribuzione_browser_oauth_view_registered"


def _html() -> str:
    """Documento autosufficiente; nessun segreto viene interpolato lato server."""
    return f"""<!doctype html>
<html lang="it">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>E-Distribuzione - scambio OAuth</title>
  <style>body {{ font: 16px system-ui, sans-serif; max-width: 760px;
    margin: 3rem auto; padding: 0 1rem; color: #202124; }}</style>
</head>
<body>
  <h1>Scambio OAuth E-Distribuzione</h1>
  <p id="stato">Apro lo scambio token su E-Distribuzione…</p>
  <script>
    const fragment = new URLSearchParams(location.hash.slice(1));
    history.replaceState(null, "", location.pathname);
    const code = fragment.get("code");
    const verifier = fragment.get("code_verifier");
    if (!code || !verifier) {{
      document.getElementById("stato").textContent =
        "Code o PKCE verifier mancanti dal link.";
    }} else {{
      const form = document.createElement("form");
      form.method = "POST";
      form.action = "{OAUTH_TOKEN_URL}";
      form.hidden = true;
      const fields = {{
        grant_type: "authorization_code",
        code,
        code_verifier: verifier,
        redirect_uri: "{OAUTH_REDIRECT_URI}",
        client_id: "{OAUTH_CLIENT_ID}"
      }};
      for (const [name, value] of Object.entries(fields)) {{
        const input = document.createElement("input");
        input.type = "hidden";
        input.name = name;
        input.value = value;
        form.appendChild(input);
      }}
      document.body.appendChild(form);
      form.submit();
    }}
  </script>
</body>
</html>"""


class EdistribuzioneBrowserOAuthView(HomeAssistantView):
    """Serve la pagina di scambio senza richiedere un bearer token HA."""

    url = OAUTH_BROWSER_EXCHANGE_PATH
    name = "api:edistribuzione:oauth_browser_exchange"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        return web.Response(
            text=_html(),
            content_type="text/html",
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'none'; script-src 'unsafe-inline'; "
                    "style-src 'unsafe-inline'; form-action "
                    "https://private.e-distribuzione.it; base-uri 'none'"
                ),
                "Referrer-Policy": "no-referrer",
            },
        )


def register_browser_oauth_view(hass: HomeAssistant) -> None:
    """Registra la route una sola volta, anche durante config flow/reauth."""
    if hass.data.get(_REGISTRATION_KEY):
        return
    # Durante alcuni test del config flow il componente HTTP non viene
    # inizializzato. Nell'istanza reale è disponibile già da async_setup;
    # questo controllo rende innocuo il richiamo difensivo dal flow.
    if hass.http is None:
        return
    hass.http.register_view(EdistribuzioneBrowserOAuthView())
    hass.data[_REGISTRATION_KEY] = True
