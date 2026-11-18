from django.urls import path

from . import views

app_name = "olt"

urlpatterns = [
    path("olts/", views.olts, name="olts"),
    path("olts/nueva/", views.olt_form, name="olt_nueva"),
    path("olts/<int:pk>/", views.detalle_olt, name="detalle_olt"),
    path("olts/<int:pk>/editar/", views.olt_form, name="olt_editar"),
    path("olts/<int:pk>/info/", views.info_olt, name="info_olt"),
    path("olts/<int:pk>/liberar/", views.olt_liberar, name="olt_liberar"),
    path("olts/<int:pk>/pon/", views.puertos_pon, name="puertos_pon"),
    path("olts/<int:pk>/vlans/", views.vlans, name="vlans"),
    path("olts/<int:pk>/perfiles/", views.perfiles_ont, name="perfiles_ont"),
    path("olts/<int:pk>/vlans/accion/", views.vlan_accion, name="vlan_accion"),
    path("olts/<int:pk>/pon/accion/", views.accion_puertos, name="accion_puertos"),
    path("planes-olt/", views.planes_olt, name="planes_olt"),
    path("planes-olt/nuevo/", views.plan_olt_form, name="plan_olt_nuevo"),
    path("planes-olt/<int:pk>/", views.plan_olt_form, name="plan_olt_editar"),
    path("planes-olt/<int:pk>/eliminar/", views.plan_olt_eliminar, name="plan_olt_eliminar"),
    path("velocidades/", views.velocidades, name="velocidades"),
    path("velocidades/nuevo/", views.velocidad_form, name="velocidad_nueva"),
    path("velocidades/<int:pk>/", views.velocidad_form, name="velocidad_editar"),
    path("velocidades/<int:pk>/eliminar/", views.velocidad_eliminar, name="velocidad_eliminar"),
    path("sin-configurar/", views.sin_configurar, name="sin_configurar"),
    path("configuradas/", views.configuradas, name="configuradas"),
    path("buscar/", views.buscar, name="buscar_todas"),
    path("buscar/<int:olt_id>/", views.buscar, name="buscar"),
    path("onu/<int:pk>/autorizar/", views.autorizar, name="autorizar"),
    path("onu/<int:pk>/", views.detalle_onu, name="detalle_onu"),
    path("onu/<int:pk>/velocidad/", views.onu_velocidad, name="onu_velocidad"),
    path("onu/<int:pk>/<str:accion>/", views.accion_onu, name="accion_onu"),
]
