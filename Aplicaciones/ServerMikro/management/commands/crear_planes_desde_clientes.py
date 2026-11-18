"""
Crea un Plan por cada velocidad distinta que ya tienen tus clientes y se lo asigna.
Solo toca clientes SIN plan. No habla con el MikroTik (la velocidad ya es la misma).

    py manage.py crear_planes_desde_clientes            -> muestra lo que haría
    py manage.py crear_planes_desde_clientes --aplicar  -> lo hace
Después pon los precios en Planes > Editar.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from Aplicaciones.ServerMikro.models import Cliente, Plan


class Command(BaseCommand):
    help = "Crea planes a partir de las velocidades de los clientes que no tienen plan"

    def add_arguments(self, parser):
        parser.add_argument("--aplicar", action="store_true", help="Guardar los cambios")

    def handle(self, *args, aplicar=False, **opts):
        velocidades = (Cliente.objects.filter(plan__isnull=True)
                       .values_list("velocidad", flat=True).distinct().order_by("velocidad"))
        if not velocidades:
            self.stdout.write(self.style.SUCCESS("Todos los clientes ya tienen plan."))
            return
        with transaction.atomic():
            for vel in velocidades:
                if "/" not in vel:
                    self.stdout.write(self.style.WARNING(f"  Velocidad rara '{vel}', se omite"))
                    continue
                subida, bajada = vel.split("/", 1)
                plan = Plan.objects.filter(subida=subida, bajada=bajada).first()
                nuevo = plan is None
                if nuevo:
                    plan = Plan(nombre=f"Plan {bajada}", subida=subida, bajada=bajada)
                    if Plan.objects.filter(nombre=plan.nombre).exists():
                        plan.nombre = f"Plan {vel}"
                n = Cliente.objects.filter(plan__isnull=True, velocidad=vel).count()
                self.stdout.write(f"  {vel:<14} -> {'NUEVO ' if nuevo else ''}{plan.nombre}  ({n} clientes)")
                if aplicar:
                    if nuevo:
                        plan.save()
                    Cliente.objects.filter(plan__isnull=True, velocidad=vel).update(plan=plan)
        if aplicar:
            self.stdout.write(self.style.SUCCESS("Listo. Ahora pon los precios en Planes > Editar."))
        else:
            self.stdout.write(self.style.WARNING("Solo vista previa. Repite con --aplicar para guardar."))
