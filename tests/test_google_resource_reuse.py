from types import SimpleNamespace
from unittest.mock import patch

import app as vicky


class _FakeSpreadsheets:
    def __init__(self, rows):
        self.values_built = 0
        self._values = SimpleNamespace(
            get=lambda **_: SimpleNamespace(execute=lambda: {"values": rows})
        )

    def values(self):
        self.values_built += 1
        return self._values


class _FakeService:
    def __init__(self, rows):
        self.built = 0
        self.ss = _FakeSpreadsheets(rows)

    def spreadsheets(self):
        self.built += 1
        return self.ss


def _entorno(service):
    return (
        patch.object(vicky, "google_ready", True),
        patch.object(vicky, "sheets_svc", service),
        patch.object(vicky, "SHEETS_ID_LEADS", "sheet-id"),
        patch.object(vicky, "SHEETS_TITLE_LEADS", "Hoja1"),
    )


def test_el_recurso_de_sheets_se_construye_una_sola_vez():
    service = _FakeService([["Nombre", "WhatsApp"], ["Ana", "6681234567"]])
    a, b, c, d = _entorno(service)
    with a, b, c, d:
        for _ in range(5):
            headers, rows = vicky._sheet_get_rows()

    assert headers == ["Nombre", "WhatsApp"]
    assert rows == [["Ana", "6681234567"]]
    assert service.built == 1
    assert service.ss.values_built == 1


def test_un_servicio_nuevo_no_reutiliza_el_recurso_del_anterior():
    viejo = _FakeService([["Nombre"], ["Viejo"]])
    nuevo = _FakeService([["Nombre"], ["Nuevo"]])

    a, b, c, d = _entorno(viejo)
    with a, b, c, d:
        vicky._sheet_get_rows()

    a, b, c, d = _entorno(nuevo)
    with a, b, c, d:
        _, rows = vicky._sheet_get_rows()

    assert rows == [["Nuevo"]]
    assert nuevo.built == 1
