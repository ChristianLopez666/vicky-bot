"""Cierre de cortesia post-cuestionario (Vicky SECOM).

Modulo puro: textos y clasificadores del tramo final de la conversacion,
despues de que el cliente termino un embudo y su solicitud quedo registrada.
Sin I/O ni estado global -- el wiring (envio, contexto, temporizador) vive en
app.py.

Es el gemelo del modulo del mismo nombre en bot-vicky (Vicky Redes), con los
textos adaptados a SECOM: aqui el cierre ya manda el menu principal solito y
la cartera no es solo de pensionados, asi que la mencion de la tarifa
preferencial del seguro de auto solo sale cuando el embudo cerrado fue el de
prestamo IMSS.

Secuencia que implementa:

  1. El cliente termina el cuestionario y su solicitud queda registrada.
     Vicky agradece y avisa que Christian lo contactara, SIN esperar otro
     mensaje del cliente (acuse).
  2. Si el cliente agradece -> cortesia_final(), sin marca de genero.
  3. Si el cliente responde que no -> DESPEDIDA_NEGATIVA y se cierra.
  4. Si no responde en una hora -> NUDGE, una sola vez.
"""

from __future__ import annotations

import re
import unicodedata


# ── Textos ────────────────────────────────────────────────────────────────────

ACUSE = (
    "🙏 *Gracias por su tiempo.*\n"
    "Christian López ya recibió su solicitud y se pondrá en contacto con usted "
    "personalmente."
)

DESPEDIDA_NEGATIVA = (
    "Le agradezco su tiempo y su atención 🙏\n"
    "Quedo a sus órdenes cuando lo necesite."
)

NUDGE = "Quedo atenta para cualquier otra consulta, saludos 😊"

_CORTESIA_BASE = "Es un gusto atenderle 😊\n\nSi requiere algún otro servicio, escriba *menú*."
_CORTESIA_AUTO_PENSIONADO = (
    " Por ejemplo, el *seguro para su auto con tarifa preferencial por ser pensionado*."
)
_CORTESIA_AUTO_GENERAL = (
    " Por ejemplo, el *seguro para su auto con tarifa preferencial*."
)


def cortesia_final(producto: str | None = None) -> str:
    """Respuesta al agradecimiento del cliente. Sin marca de genero.

    La tarifa preferencial "por ser pensionado" solo se ofrece cuando el
    embudo que se acaba de cerrar fue el de prestamo IMSS: en SECOM el resto
    de la cartera no necesariamente esta pensionada, y prometerselo a quien no
    lo es seria una oferta que el asesor no puede sostener. Tampoco se ofrece
    el seguro de auto a quien acaba de cerrar justamente ese embudo."""
    producto = (producto or "").strip().lower()
    if producto == "auto":
        return _CORTESIA_BASE
    if producto == "imss":
        return _CORTESIA_BASE + _CORTESIA_AUTO_PENSIONADO
    return _CORTESIA_BASE + _CORTESIA_AUTO_GENERAL


# ── Clasificadores ────────────────────────────────────────────────────────────

def normalizar(texto: str) -> str:
    """minusculas, sin acentos y sin puntuacion -- SECOM no tenia un
    normalizador propio y interpret_response() no sirve aqui: clasifica
    cualquier texto como positivo/negativo/neutro, no distingue una cortesia
    pura de una respuesta con contenido."""
    if not texto:
        return ""
    t = unicodedata.normalize("NFD", str(texto).lower().strip())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = t.replace("ñ", "n")
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


_CORTESIA_KW = {"gracias", "ok", "okay", "perfecto", "sale", "vale", "genial",
                "excelente", "listo", "entendido", "va", "correcto"}
_CORTESIA_FRASES = ("de acuerdo", "esta bien", "muy bien", "mil gracias")
_CORTESIA_FILLER = {"muchas", "muy", "amable", "tambien", "y", "ademas",
                    "porfavor", "por", "favor", "super", "vicky", "quedo",
                    "pendiente", "espero", "su", "llamada", "de", "nada"}


def es_cortesia_pura(texto: str) -> bool:
    """True solo si el mensaje, quitando cortesia y relleno, no deja nada
    sustantivo -- evita que "gracias, tambien quiero cotizar auto" se trague
    como agradecimiento en vez de rutearse como intencion nueva."""
    n = normalizar(texto)
    if not n:
        return False
    trabajo = n
    for frase in _CORTESIA_FRASES:
        trabajo = trabajo.replace(frase, " ")
    toks = set(trabajo.split())
    tiene_cortesia = bool(toks & _CORTESIA_KW) or any(f in n for f in _CORTESIA_FRASES)
    if not tiene_cortesia:
        return False
    return not (toks - _CORTESIA_KW - _CORTESIA_FILLER)


_NEG_KW = {"no", "nel", "nop", "nope", "negativo", "ninguno", "ninguna",
           "nada", "tampoco", "nunca", "jamas"}
_NEG_FRASES = ("asi esta bien", "esta bien asi", "asi lo dejamos", "queda asi",
               "asi le dejamos")
_NEG_FILLER = {"gracias", "muchas", "muy", "amable", "ok", "okay", "por",
               "ahora", "el", "momento", "de", "todo", "bien", "ya", "es",
               "eso", "seria", "sera", "todos", "modos", "igual", "mas",
               "esta", "asi", "vicky", "solo", "era", "esto", "hoy"}


def es_respuesta_negativa(texto: str) -> bool:
    """True si el mensaje es una negativa de cierre pura ("no gracias", "asi
    esta bien"). "No entiendo" o "no, mejor el seguro de auto" NO lo son."""
    n = normalizar(texto)
    if not n:
        return False
    trabajo = n
    tiene_frase = False
    for frase in _NEG_FRASES:
        if frase in trabajo:
            tiene_frase = True
            trabajo = trabajo.replace(frase, " ")
    toks = set(trabajo.split())
    if not (tiene_frase or (toks & _NEG_KW)):
        return False
    return not (toks - _NEG_KW - _NEG_FILLER)
