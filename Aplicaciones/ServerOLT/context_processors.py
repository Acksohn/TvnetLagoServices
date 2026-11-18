"""Datos que necesitan TODAS las páginas del panel (por ejemplo, el contador del menú)."""
from .models import ONU


def menu(request):
    if not request.user.is_authenticated:
        return {}
    return {"menu_pendientes": ONU.objects.filter(estado="sin_autorizar").count()}
