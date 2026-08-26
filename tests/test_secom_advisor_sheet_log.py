"""Bitacora en Sheets de los avisos al asesor (SECOM).

El monitor de correos (Apps Script) no ve WhatsApp: solo lee hojas. Sin esta
bitacora los avisos de SECOM eran invisibles para el, que es por lo que Redes
mandaba correos de prospecto y SECOM no.
"""

import contextlib
from unittest.mock import Mock, patch

import app as vicky


def _sheets_mock():
    fake_append = Mock()
    fake_ss = Mock()
    fake_ss.return_value.values.return_value.append = fake_append
    return fake_ss, fake_append


@contextlib.contextmanager
def _entorno(fake_ss):
    parches = (
        patch.object(vicky, "google_ready", True),
        patch.object(vicky, "sheets_svc", Mock(spreadsheets=fake_ss)),
        patch.object(vicky, "SHEETS_ID_LEADS", "sheet-id"),
        patch.object(vicky, "ADVISOR_NUMBER", "5216682478005"),
        patch.object(vicky, "CONVERSACIONES_TAB", "CONVERSACIONES"),
        patch.object(vicky, "_ensure_conversaciones_tab"),
    )
    with contextlib.ExitStack() as stack:
        for parche in parches:
            stack.enter_context(parche)
        yield


def test_notify_advisor_registra_el_aviso_en_la_hoja():
    fake_ss, fake_append = _sheets_mock()
    with patch.object(vicky, "send_message") as send, _entorno(fake_ss):
        vicky._notify_advisor("PROSPECTO - IMSS Ley 73")

    send.assert_called_once()
    fila = fake_append.call_args.kwargs["body"]["values"][0]
    assert fila[0] == "5216682478005"
    assert fila[1] == "Asesor"
    assert fila[2] == "PROSPECTO - IMSS Ley 73"
    assert fila[4] == "saliente"
    # El monitor filtra por este campo; sin el, el aviso no genera correo.
    assert fila[5] == "asesor"


def test_el_rango_apunta_a_la_pestana_de_conversaciones():
    fake_ss, fake_append = _sheets_mock()
    with patch.object(vicky, "send_message"), _entorno(fake_ss):
        vicky._notify_advisor("hola")

    assert fake_append.call_args.kwargs["range"] == "CONVERSACIONES!A:F"


def test_se_registra_aunque_falle_el_envio_por_whatsapp():
    """Si el aviso no llego al telefono, dejar rastro importa mas."""
    fake_ss, fake_append = _sheets_mock()
    fallo = RuntimeError("meta caida")
    with patch.object(vicky, "send_message", side_effect=fallo), _entorno(fake_ss):
        vicky._notify_advisor("PROSPECTO - VRIM")

    fake_append.assert_called_once()


def test_un_fallo_de_sheets_no_rompe_el_aviso():
    fake_ss, fake_append = _sheets_mock()
    fake_append.return_value.execute.side_effect = RuntimeError("sheets caido")
    with patch.object(vicky, "send_message") as send, _entorno(fake_ss):
        vicky._notify_advisor("PROSPECTO - Empresarial")

    send.assert_called_once()


def test_no_escribe_si_sheets_no_esta_configurado():
    fake_ss, fake_append = _sheets_mock()
    with (
        patch.object(vicky, "google_ready", False),
        patch.object(vicky, "sheets_svc", Mock(spreadsheets=fake_ss)),
    ):
        vicky._log_conversacion("PROSPECTO - suelto")

    fake_append.assert_not_called()


def test_el_mensaje_se_trunca_para_no_reventar_la_celda():
    fake_ss, fake_append = _sheets_mock()
    with patch.object(vicky, "send_message"), _entorno(fake_ss):
        vicky._notify_advisor("x" * 900)

    assert len(fake_append.call_args.kwargs["body"]["values"][0][2]) == 500
