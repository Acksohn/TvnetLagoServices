from django.urls import path

from . import views

app_name = "clientes"

urlpatterns = [
    path("", views.lista, name="lista"),
    path("nuevo/", views.nuevo, name="nuevo"),
    path("planes/", views.planes, name="planes"),
    path("planes/nuevo/", views.plan_form, name="plan_nuevo"),
    path("planes/<int:pk>/", views.plan_form, name="plan_editar"),
    path("routers/", views.routers, name="routers"),
    path("routers/nuevo/", views.router_form, name="router_nuevo"),
    path("routers/<int:pk>/", views.router_detalle, name="router_detalle"),
    path("routers/<int:pk>/editar/", views.router_form, name="router_editar"),
    path("routers/<int:pk>/probar/", views.router_info, name="router_info"),
    path("routers/<int:pk>/eliminar/", views.router_eliminar, name="router_eliminar"),
    path("routers/<int:pk>/fijar-huella/", views.router_fijar_huella, name="router_fijar_huella"),
    path("redes/", views.redes, name="redes"),
    path("redes/nueva/", views.red_form, name="red_nueva"),
    path("redes/siguiente-ip/", views.siguiente_ip, name="siguiente_ip"),
    path("redes/<int:pk>/", views.red_detalle, name="red_detalle"),
    path("redes/<int:pk>/editar/", views.red_form, name="red_editar"),
    path("redes/<int:pk>/eliminar/", views.red_eliminar, name="red_eliminar"),
    path("buscar/", views.buscar_clientes, name="buscar"),
    path("naps/", views.naps, name="naps"),
    path("naps/nueva/", views.nap_form, name="nap_nueva"),
    path("naps/<int:pk>/", views.nap_form, name="nap_editar"),
    path("naps/<int:pk>/eliminar/", views.nap_eliminar, name="nap_eliminar"),
    path("<int:pk>/nap/", views.cliente_nap, name="cliente_nap"),
    path("<int:pk>/cambiar-plan/", views.cambiar_plan, name="cambiar_plan"),
    path("<int:pk>/", views.detalle, name="detalle"),
    path("<int:pk>/<str:accion>/", views.accion, name="accion"),
]
