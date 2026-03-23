from __future__ import annotations

from tsp_email_optimizer.domain.models import ExtractedTrip, OptimizedRoute


class ReplyBuilder:
    def build_success_reply(
        self,
        trip: ExtractedTrip,
        route: OptimizedRoute,
        map_path: str | None = None,
    ) -> str:
        lines: list[str] = []
        lines.append("Hola,")
        lines.append("")
        lines.append("Ya optimice la ruta solicitada.")
        lines.append(f"Fecha: {trip.trip_date}")
        lines.append(f"Hora de salida: {trip.departure_time} ({trip.timezone})")
        if trip.start_address:
            lines.append(f"Salida desde: {trip.start_address}")
        if route.start_location:
            lines.append(
                "Punto de inicio usado para optimizar: "
                f"{route.start_location.address}"
            )
        lines.append("")
        lines.append("Orden optimo de paradas:")
        for idx, stop in enumerate(route.ordered_stops, start=1):
            lines.append(f"{idx}. {stop.address}")

        lines.append("")
        lines.append("Resumen:")
        lines.append(f"- Total de paradas: {len(route.ordered_stops)}")
        if route.total_distance_m is not None:
            lines.append(f"- Distancia total estimada: {route.total_distance_m / 1000:.2f} km")
        if route.total_duration_s is not None:
            lines.append(f"- Duracion total estimada: {route.total_duration_s / 60:.1f} minutos")
        if map_path:
            lines.append(f"- Mapa generado en: {map_path}")
        if trip.warnings:
            lines.append("- Observaciones de extraccion:")
            for warning in trip.warnings:
                lines.append(f"  - {warning}")
        if route.notes:
            lines.append("- Observaciones de ruteo:")
            for note in route.notes:
                lines.append(f"  - {note}")

        lines.append("")
        lines.append("Saludos")
        return "\n".join(lines)

    def build_clarification_reply(self, reason: str) -> str:
        friendly_reason, guidance = self._friendly_reason_and_guidance(reason)
        return (
            "Hola,\n\n"
            "No pude procesar completamente tu solicitud para optimizar la ruta.\n"
            f"Detalle: {friendly_reason}\n\n"
            f"{guidance}\n\n"
            "Por favor, reenvia el correo incluyendo:\n"
            "- Fecha\n"
            "- Hora de salida\n"
            "- Salida desde\n"
            "- Lista de direcciones (una por linea)\n\n"
            "Saludos"
        )

    @staticmethod
    def _friendly_reason_and_guidance(reason: str) -> tuple[str, str]:
        lowered = reason.lower()
        if "no se encontro punto enrutable para la direccion" in lowered:
            return (
                reason,
                (
                    "Sugerencia: confirma esa direccion con mayor detalle "
                    "(comuna, region, pais, codigo postal o referencia de esquina)."
                ),
            )
        if "could not find routable point" in lowered:
            return (
                "La direccion de salida no se pudo ubicar sobre una via transitable.",
                (
                    "Sugerencia: corrige la direccion de salida o usa un punto cercano y conocido "
                    "(por ejemplo, una interseccion o una direccion con numero)."
                ),
            )
        if "no start or end specified" in lowered:
            return (
                "No se pudo definir el punto de inicio/termino para el vehiculo.",
                "Sugerencia: incluye claramente la linea 'Salida desde: <direccion completa>'.",
            )
        return reason, "Sugerencia: revisa el formato del correo e intenta nuevamente."

