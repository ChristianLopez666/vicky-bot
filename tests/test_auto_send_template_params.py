"""Parametros de plantilla de /ext/auto-send-one dirigidos por el cron.

Cubre la reparacion de la regresion introducida en a1481f1 (el endpoint dejo
de mandar {"nombre": nombre} y el cron no mandaba params, por lo que las
plantillas con {{nombre}} viajaban sin parametros de body), y la retirada del
"auto-heal" que deducia la estructura de la plantilla a partir de los errores
de Meta.

Ninguna prueba llama a Meta, Google Sheets, Render ni a ningun servicio
externo real: requests.post y los helpers de Sheets estan mockeados.
"""

import json
from unittest.mock import patch

import pytest

import app as vicky


HEADERS = ["Nombre", "WhatsApp", "ESTATUS", "LAST_MESSAGE_AT", "Monto"]
ROW_PENDING = ["chiwy", "6681620521", "", "", "15000"]

IMAGE_URL = "https://example.test/img.png"


@pytest.fixture
def client():
    vicky.app.config["TESTING"] = True
    with vicky.app.test_client() as c:
        yield c


@pytest.fixture
def auto_send_token():
    with patch.object(vicky, "AUTO_SEND_TOKEN", "auto-secret"):
        yield


class FakeResp:
    """Respuesta minima de la Graph API, suficiente para send_template_message."""

    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"messages": [{"id": "wamid.TEST"}]}
        self.text = text if text is not None else json.dumps(self._payload)

    def json(self):
        return self._payload


def _meta_error(code, details):
    return json.dumps({
        "error": {
            "message": f"(#{code}) error",
            "code": code,
            "type": "OAuthException",
            "error_data": {"messaging_product": "whatsapp", "details": details},
            "fbtrace_id": "TRACE",
        }
    })


ERR_132000 = _meta_error(
    132000,
    "body: number of localizable_params (0) does not match the expected number of params (1)",
)
ERR_100 = _meta_error(100, "Parameter name is missing or empty")


@pytest.fixture
def sheet_and_wpp():
    """Aisla el endpoint: Sheets falso, WhatsApp falso, sin red.

    Devuelve la lista de payloads enviados a Meta y las escrituras a Sheets.
    """
    posts = []
    row_updates = []

    def fake_post(url, headers=None, json=None, timeout=None):
        posts.append(json)
        return FakeResp()

    with patch.object(vicky, "META_TOKEN", "token-de-prueba"), \
            patch.object(vicky, "WPP_API_URL", "https://graph.test/v20.0/1/messages"), \
            patch.object(vicky, "_is_campaign_paused", return_value=False), \
            patch.object(vicky, "_sheet_get_rows", return_value=(HEADERS, [list(ROW_PENDING)])), \
            patch.object(vicky, "_update_row_cells",
                         side_effect=lambda rn, updates, hdrs: row_updates.append((rn, updates))), \
            patch.object(vicky, "append_envio_status"), \
            patch.object(vicky, "_register_send_result", return_value=False), \
            patch.object(vicky.requests, "post", side_effect=fake_post) as post_mock:
        yield {"posts": posts, "row_updates": row_updates, "post_mock": post_mock}


def _send(client, body):
    return client.post(
        "/ext/auto-send-one",
        json=body,
        headers={"X-AUTO-TOKEN": "auto-secret"},
    )


def _components(payload):
    return payload["template"].get("components", [])


def _body_component(payload):
    for c in _components(payload):
        if c.get("type") == "body":
            return c
    return None


# --------------------------------------------------------------------------
# 1. Plantilla sin parametros
# --------------------------------------------------------------------------

def test_template_without_params_sends_no_body_component(client, auto_send_token, sheet_and_wpp):
    resp = _send(client, {"template": "promo_vrim_prestamo", "image_url": IMAGE_URL})

    assert resp.status_code == 200
    assert resp.get_json()["sent"] is True
    assert len(sheet_and_wpp["posts"]) == 1, "una sola llamada a Meta"
    assert _body_component(sheet_and_wpp["posts"][0]) is None


# --------------------------------------------------------------------------
# 2. Parametro nombrado
# --------------------------------------------------------------------------

def test_named_param_from_row_uses_exact_parameter_name(client, auto_send_token, sheet_and_wpp):
    resp = _send(client, {
        "template": "vida_temporal",
        "image_url": IMAGE_URL,
        "params_from_row": {"nombre": "Nombre"},
    })

    assert resp.status_code == 200
    assert resp.get_json()["sent"] is True
    assert len(sheet_and_wpp["posts"]) == 1

    body = _body_component(sheet_and_wpp["posts"][0])
    assert body["parameters"] == [
        {"type": "text", "parameter_name": "nombre", "text": "chiwy"}
    ], "debe viajar parameter_name='nombre', nunca '1'"


# --------------------------------------------------------------------------
# 3. Dos parametros nombrados
# --------------------------------------------------------------------------

def test_two_named_params_from_row(client, auto_send_token, sheet_and_wpp):
    resp = _send(client, {
        "template": "cualquier_plantilla",
        "params_from_row": {"nombre": "Nombre", "monto": "Monto"},
    })

    assert resp.status_code == 200
    params = _body_component(sheet_and_wpp["posts"][0])["parameters"]
    assert {p["parameter_name"]: p["text"] for p in params} == {
        "nombre": "chiwy",
        "monto": "15000",
    }


# --------------------------------------------------------------------------
# 4. Parametros posicionales
# --------------------------------------------------------------------------

def test_positional_params_from_row_keep_order(client, auto_send_token, sheet_and_wpp):
    resp = _send(client, {
        "template": "cualquier_plantilla",
        "params_from_row": ["Nombre", "Monto"],
    })

    assert resp.status_code == 200
    params = _body_component(sheet_and_wpp["posts"][0])["parameters"]
    assert params == [
        {"type": "text", "text": "chiwy"},
        {"type": "text", "text": "15000"},
    ], "posicionales: sin parameter_name y en el orden declarado"


# --------------------------------------------------------------------------
# 5. Columna inexistente
# --------------------------------------------------------------------------

def test_missing_column_aborts_before_meta(client, auto_send_token, sheet_and_wpp):
    resp = _send(client, {
        "template": "vida_temporal",
        "params_from_row": {"nombre": "ColumnaQueNoExiste"},
    })

    assert resp.status_code == 400
    assert "ColumnaQueNoExiste" in resp.get_json()["error"]
    assert sheet_and_wpp["posts"] == [], "cero llamadas a Meta"
    assert sheet_and_wpp["row_updates"] == [], "la fila no se modifica"


# --------------------------------------------------------------------------
# 6. Valor vacio
# --------------------------------------------------------------------------

def test_empty_value_aborts_before_meta(client, auto_send_token):
    row_updates = []
    row_sin_monto = ["chiwy", "6681620521", "", "", "   "]

    with patch.object(vicky, "META_TOKEN", "token-de-prueba"), \
            patch.object(vicky, "WPP_API_URL", "https://graph.test/v20.0/1/messages"), \
            patch.object(vicky, "_is_campaign_paused", return_value=False), \
            patch.object(vicky, "_sheet_get_rows", return_value=(HEADERS, [row_sin_monto])), \
            patch.object(vicky, "_update_row_cells",
                         side_effect=lambda rn, u, h: row_updates.append((rn, u))), \
            patch.object(vicky.requests, "post") as post_mock:
        resp = _send(client, {
            "template": "cualquier_plantilla",
            "params_from_row": {"monto": "Monto"},
        })

    assert resp.status_code == 400
    assert "Monto" in resp.get_json()["error"]
    post_mock.assert_not_called()
    assert row_updates == []


# --------------------------------------------------------------------------
# 7. y 8. Conflictos de configuracion
# --------------------------------------------------------------------------

def test_params_and_params_from_row_are_mutually_exclusive(client, auto_send_token, sheet_and_wpp):
    resp = _send(client, {
        "template": "vida_temporal",
        "params": {"nombre": "literal"},
        "params_from_row": {"nombre": "Nombre"},
    })

    assert resp.status_code == 400
    assert "mutuamente excluyentes" in resp.get_json()["error"]
    assert sheet_and_wpp["posts"] == []


def test_components_cannot_be_combined_with_params(client, auto_send_token, sheet_and_wpp):
    resp = _send(client, {
        "template": "vida_temporal",
        "components": [{"type": "body", "parameters": []}],
        "params_from_row": {"nombre": "Nombre"},
    })

    assert resp.status_code == 400
    assert "components" in resp.get_json()["error"]
    assert sheet_and_wpp["posts"] == []


# --------------------------------------------------------------------------
# 9. y 10. El auto-heal quedo retirado
# --------------------------------------------------------------------------

def test_meta_error_132000_does_not_retry_with_invented_params(client, auto_send_token):
    """Un 400 de Meta se reporta; no se reintenta con un payload distinto."""
    posts = []

    def fake_post(url, headers=None, json=None, timeout=None):
        posts.append(json)
        return FakeResp(status_code=400, text=ERR_132000, payload={})

    with patch.object(vicky, "META_TOKEN", "token-de-prueba"), \
            patch.object(vicky, "WPP_API_URL", "https://graph.test/v20.0/1/messages"), \
            patch.object(vicky, "_is_campaign_paused", return_value=False), \
            patch.object(vicky, "_sheet_get_rows", return_value=(HEADERS, [list(ROW_PENDING)])), \
            patch.object(vicky, "_update_row_cells"), \
            patch.object(vicky, "append_envio_status"), \
            patch.object(vicky, "_register_send_result", return_value=False), \
            patch.object(vicky.requests, "post", side_effect=fake_post):
        resp = _send(client, {"template": "vida_temporal", "image_url": IMAGE_URL})

    assert resp.status_code == 200
    assert resp.get_json()["sent"] is False
    assert len(posts) == 1, "una sola llamada: sin auto-heal"


def test_meta_error_100_does_not_invent_parameter_name(client, auto_send_token):
    posts = []

    def fake_post(url, headers=None, json=None, timeout=None):
        posts.append(json)
        return FakeResp(status_code=400, text=ERR_100, payload={})

    with patch.object(vicky, "META_TOKEN", "token-de-prueba"), \
            patch.object(vicky, "WPP_API_URL", "https://graph.test/v20.0/1/messages"), \
            patch.object(vicky, "_is_campaign_paused", return_value=False), \
            patch.object(vicky, "_sheet_get_rows", return_value=(HEADERS, [list(ROW_PENDING)])), \
            patch.object(vicky, "_update_row_cells"), \
            patch.object(vicky, "append_envio_status"), \
            patch.object(vicky, "_register_send_result", return_value=False), \
            patch.object(vicky.requests, "post", side_effect=fake_post):
        resp = _send(client, {
            "template": "vida_temporal",
            "image_url": IMAGE_URL,
            "params_from_row": {"nombre": "Nombre"},
        })

    assert resp.status_code == 200
    assert resp.get_json()["sent"] is False
    assert len(posts) == 1, "una sola llamada: sin auto-heal"

    params = _body_component(posts[0])["parameters"]
    assert all(p.get("parameter_name") != "1" for p in params), "nunca inventar parameter_name numerico"
    assert params[0]["parameter_name"] == "nombre"


def test_auto_heal_helpers_no_longer_exist():
    for helper in (
        "_expected_body_param_count",
        "_needs_named_body_params",
        "_META_MISSING_BODY_PARAMS_RE",
    ):
        assert not hasattr(vicky, helper), f"{helper} deberia haber sido retirado"


# --------------------------------------------------------------------------
# 11. Estatus de exito
# --------------------------------------------------------------------------

def test_success_status_from_cron_is_used(client, auto_send_token, sheet_and_wpp):
    resp = _send(client, {
        "template": "vida_temporal",
        "image_url": IMAGE_URL,
        "params_from_row": {"nombre": "Nombre"},
        "success_status": "ENVIADO_VIDA_TEMPORAL",
    })

    assert resp.status_code == 200
    _, updates = sheet_and_wpp["row_updates"][0]
    assert updates["ESTATUS"] == "ENVIADO_VIDA_TEMPORAL"
    assert updates["LAST_MESSAGE_AT"]


def test_without_success_status_falls_back_to_status_for_template(client, auto_send_token, sheet_and_wpp):
    resp = _send(client, {"template": "promo_vrim_prestamo"})

    assert resp.status_code == 200
    _, updates = sheet_and_wpp["row_updates"][0]
    assert updates["ESTATUS"] == vicky._status_for_template("promo_vrim_prestamo")


def test_invalid_success_status_is_rejected(client, auto_send_token, sheet_and_wpp):
    resp = _send(client, {"template": "vida_temporal", "success_status": "no minusculas!"})

    assert resp.status_code == 400
    assert sheet_and_wpp["posts"] == []


# --------------------------------------------------------------------------
# 12. y 13. image_url e idioma
# --------------------------------------------------------------------------

def test_image_url_stays_in_header(client, auto_send_token, sheet_and_wpp):
    resp = _send(client, {
        "template": "vida_temporal",
        "image_url": IMAGE_URL,
        "params_from_row": {"nombre": "Nombre"},
    })

    assert resp.status_code == 200
    header = next(c for c in _components(sheet_and_wpp["posts"][0]) if c["type"] == "header")
    assert header["parameters"][0]["image"]["link"] == IMAGE_URL


def test_language_defaults_and_can_be_overridden(client, auto_send_token, sheet_and_wpp):
    _send(client, {"template": "promo_vrim_prestamo"})
    assert sheet_and_wpp["posts"][0]["template"]["language"]["code"] == "es_MX"

    _send(client, {"template": "promo_vrim_prestamo", "language": "en_US"})
    assert sheet_and_wpp["posts"][1]["template"]["language"]["code"] == "en_US"


def test_invalid_language_is_rejected(client, auto_send_token, sheet_and_wpp):
    resp = _send(client, {"template": "vida_temporal", "language": "../etc/passwd"})

    assert resp.status_code == 400
    assert sheet_and_wpp["posts"] == []


# --------------------------------------------------------------------------
# 14. Compatibilidad con el campo literal params
# --------------------------------------------------------------------------

def test_literal_params_dict_still_supported(client, auto_send_token, sheet_and_wpp):
    resp = _send(client, {
        "template": "vida_temporal",
        "image_url": IMAGE_URL,
        "params": {"nombre": "Valor Literal"},
    })

    assert resp.status_code == 200
    assert _body_component(sheet_and_wpp["posts"][0])["parameters"] == [
        {"type": "text", "parameter_name": "nombre", "text": "Valor Literal"}
    ]


def test_literal_params_list_still_supported(client, auto_send_token, sheet_and_wpp):
    resp = _send(client, {
        "template": "vida_temporal",
        "image_url": IMAGE_URL,
        "params": ["uno", "dos"],
    })

    assert resp.status_code == 200
    assert _body_component(sheet_and_wpp["posts"][0])["parameters"] == [
        {"type": "text", "text": "uno"},
        {"type": "text", "text": "dos"},
    ]


# --------------------------------------------------------------------------
# Resolvedor: sin conocimiento de plantillas
# --------------------------------------------------------------------------

def test_resolver_rejects_non_dict_non_list():
    with pytest.raises(vicky.ParamsFromRowError):
        vicky._resolve_params_from_row("Nombre", HEADERS, list(ROW_PENDING))


def test_resolver_column_lookup_is_case_and_space_insensitive():
    resolved = vicky._resolve_params_from_row(
        {"nombre": "  nOmBrE  "}, HEADERS, list(ROW_PENDING)
    )
    assert resolved == {"nombre": "chiwy"}
