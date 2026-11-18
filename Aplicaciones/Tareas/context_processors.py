from .models import Tarea


def menu(request):
    """Contador del menú: órdenes que todavía no terminan."""
    if not request.user.is_authenticated:
        return {}
    return {"menu_tareas": Tarea.objects.filter(estado__in=("en_cola", "ejecutando", "reintento")).count()}
