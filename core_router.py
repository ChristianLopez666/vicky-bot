"""
core_router.py

Router principal de mensajes para Vicky Bot (WhatsApp).
Exponer la función `route_message(wa_id, wa_e164_no_plus, text_in) -> str`.

Requisitos implementados:
- Manejo de menú principal (opciones 1-8).
- Consulta libre: delega a ask_gpt() en integrations_gpt.py.
- Validaciones: mensaje vacío, opción fuera de rango, 'menu' para mostrar opciones.
- Robustez: logging y manejo de excepciones para evitar que errores rompan el flujo.
- Siempre devuelve un string.
"""

from typing import Optional
import logging
import re

# Configuración básica de logging; en producción podría configurarse desde Flask/Render env.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("core_router")

# Texto del menú principal (en español)
MENU_TEXT = (
    "Vicky Bot - Menú principal:\n"
    "1: Asesoría en pensiones IMSS.\n"
    "2: Seguros de auto.\n"
    "3: Seguros de vida y salud.\n"
    "4: Tarjetas médicas VRIM.\n"
    "5: Préstamos a pensionados IMSS.\n"
    "6: Financiamiento empresarial.\n"
    "7: Nómina empresarial.\n"
    "8: Contactar con Christian (notificación interna al asesor).\n\n"
    "Escribe el número de la opción (1-8) o escribe tu consulta libremente."
)

# Mensajes de validación / errores
ERROR_INVALID_OPTION = "⚠️ Opción no válida. Por favor selecciona un número del 1 al 8 o escribe *menu* para ver las opciones."
ERROR_EMPTY_MESSAGE = "⚠️ No recibí ningún mensaje. Intenta de nuevo."
ERROR_GPT_UNAVAILABLE = "⚠️ En este momento no puedo conectarme con GPT, por favor intenta más tarde."

# Respuestas fijas por opción
FIXED_RESPONSES = {
    1: (
        "Asesoría en pensiones IMSS:\n"
        "- Podemos ayudarte a revisar requisitos, montos y trámites.\n"
        "Por favor comparte tu duda específica o escribe 'menu' para volver."
    ),
    2: (
        "Seguros de auto:\n"
        "- Ofrecemos planes con cobertura amplia y asistencia en carretera.\n"
        "¿Quieres cotizar un vehículo? Indica marca, modelo y año."
    ),
    3: (
        "Seguros de vida y salud:\n"
        "- Contamos con opciones para cobertura individual y familiar.\n"
        "Dime si buscas protección de vida, gastos médicos o ambos."
    ),
    4: (
        "Tarjetas médicas VRIM:\n"
        "- Información sobre beneficios, afiliación y uso.\n"
        "Escribe tu pregunta específica para que te asesoremos."
    ),
    5: (
        "Préstamos a pensionados IMSS:\n"
        "- Te explicamos requisitos, tasas y tiempos de pago.\n"
        "Comparte tu pensión mensual (aprox.) para darte una estimación."
    ),
    6: (
        "Financiamiento empresarial:\n"
        "- Opciones para capital de trabajo, inversión y expansión.\n"
        "Indica el tamaño de tu empresa y el monto aproximado requerido."
    ),
    7: (
        "Nómina empresarial:\n"
        "- Servicios de administración de nómina, cumplimiento y pagos.\n"
        "¿Quieres cotización o detalles del servicio?"
    ),
    8: (
        "He notificado internamente a Christian. Un asesor se pondrá en contacto contigo pronto.\n"
        "Si quieres, puedes dejar un mensaje adicional y lo reenviaré."
    ),
}


def _is_empty(text: Optional[str]) -> bool:
    return text is None or (isinstance(text, str) and text.strip() == "")


def route_message(wa_id: str, wa_e164_no_plus: str, text_in: Optional[str]) -> str:
    """
    Router principal que decide la respuesta para un mensaje entrante de WhatsApp.

    Parámetros:
    - wa_id: ID del mensaje entrante en WhatsApp.
    - wa_e164_no_plus: número de teléfono en formato E164 sin el '+'.
    - text_in: texto recibido del usuario.

    Retorna:
    - str: respuesta en texto plano que se enviará al usuario.
    """
    try:
        logger.info("route_message called - wa_id=%s wa_e164=%s text_in=%r", wa_id, wa_e164_no_plus, text_in)

        # Validación: mensaje vacío o nulo
        if _is_empty(text_in):
            logger.warning("Mensaje vacío recibido - wa_id=%s wa_e164=%s", wa_id, wa_e164_no_plus)
            return ERROR_EMPTY_MESSAGE

        # Normalizar texto
        text = text_in.strip()
        text_lower = text.lower()

        # Si el usuario pide el menú explícitamente
        if text_lower in ("menu", "menú"):
            logger.info("Usuario solicitó el menú - wa_id=%s wa_e164=%s", wa_id, wa_e164_no_plus)
            return MENU_TEXT

        # Si el texto es un número (p. ej. "1", " 2 ")
        # Usamos regex para permitir signos de espacio y evitar que palabras que contengan números coincidan.
        if re.fullmatch(r"\d+", text):
            try:
                option = int(text)
            except ValueError:
                # Esto no debería ocurrir por la regex, pero lo capturamos por seguridad.
                logger.error("Error convirtiendo opción a entero - text=%r wa_id=%s", text, wa_id)
                return ERROR_INVALID_OPTION

            # Validar rango 1-8
            if option < 1 or option > 8:
                logger.info("Opción fuera de rango seleccionada: %d - wa_id=%s wa_e164=%s", option, wa_id, wa_e164_no_plus)
                return ERROR_INVALID_OPTION

            # Log de la opción seleccionada
            logger.info("Usuario seleccionó opción %d - wa_id=%s wa_e164=%s", option, wa_id, wa_e164_no_plus)

            # Acción para la opción 8: notificar internamente al asesor Christian
            if option == 8:
                # Aquí solo hacemos logging para representar la notificación interna.
                logger.info("Notificación interna: contacto solicitado con Christian - wa_id=%s wa_e164=%s", wa_id, wa_e164_no_plus)
                # Si hay otros side-effects (webhook, DB, etc.), integrarlos en esta sección.
                return FIXED_RESPONSES[8]

            # Respuesta fija para opciones 1-7
            response = FIXED_RESPONSES.get(option)
            if response:
                return response
            else:
                # Fallback (no debería pasar)
                logger.error("No hay respuesta fija definida para la opción %d - wa_id=%s", option, wa_id)
                return ERROR_INVALID_OPTION

        # Si no coincide con opciones fijas, usamos GPT (consulta libre)
        logger.info("Consulta libre detectada - delegando a GPT - wa_id=%s wa_e164=%s", wa_id, wa_e164_no_plus)

        # Importar la función ask_gpt dinámicamente para evitar fallos en import globals
        try:
            from integrations_gpt import ask_gpt  # type: ignore
        except Exception as e:
            # Si no se puede importar la integración, no romper el flujo.
            logger.exception("No se pudo importar integrations_gpt.ask_gpt: %s", e)
            return ERROR_GPT_UNAVAILABLE

        # Llamada a la función ask_gpt - proteger con try/except
        try:
            # Se asume que ask_gpt acepta (user_message: str, wa_id: str, wa_e164_no_plus: str) o al menos (text).
            # Para compatibilidad, primero intentamos llamarlo con un solo argumento (texto).
            # Si eso falla por firma, intentamos pasar más contexto.
            try:
                gpt_response = ask_gpt(text)
            except TypeError:
                # Intentar con parámetros ampliados si la firma lo soporta
                gpt_response = ask_gpt(text, wa_id=wa_id, wa_e164_no_plus=wa_e164_no_plus)
        except Exception as e:
            logger.exception("Error al llamar a ask_gpt: %s", e)
            return ERROR_GPT_UNAVAILABLE

        # Validar que GPT devolvió algo razonable
        if gpt_response is None:
            logger.warning("ask_gpt devolvió None - wa_id=%s", wa_id)
            return ERROR_GPT_UNAVAILABLE

        # Asegurar que devolvemos un string
        if not isinstance(gpt_response, str):
            try:
                gpt_text = str(gpt_response)
                logger.debug("Conversión de respuesta GPT a string realizada - wa_id=%s", wa_id)
            except Exception:
                logger.exception("No se pudo convertir la respuesta de GPT a string - wa_id=%s", wa_id)
                return ERROR_GPT_UNAVAILABLE
        else:
            gpt_text = gpt_response

        logger.info("Respuesta GPT entregada correctamente - wa_id=%s", wa_id)
        return gpt_text

    except Exception as outer_exc:
        # Captura cualquier excepción inesperada y devuelve un mensaje seguro.
        logger.exception("Error inesperado en route_message: %s", outer_exc)
        return "⚠️ Ocurrió un error procesando tu mensaje. Por favor intenta de nuevo más tarde."