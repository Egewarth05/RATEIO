from django.urls import path
from .views import nova_despesa, lista_despesas, ver_rateio, excluir_despesa, editar_rateio, ajax_ultima_agua, limpar_rateio, limpar_tudo

urlpatterns = [
    path('', lista_despesas, name='lista_despesas'),
    path('nova/', nova_despesa, name='nova_despesa'),
    path('rateio/<int:despesa_id>/', ver_rateio, name='ver_rateio'),
    path('limpar_rateio/<int:despesa_id>/', limpar_rateio, name='limpar_rateio'),
    path('limpar_tudo/', limpar_tudo, name='limpar_tudo'),
    path('excluir/<int:despesa_id>/', excluir_despesa, name='excluir_despesa'),
    path('editar_rateio/<int:rateio_id>/', editar_rateio, name='editar_rateio'),
    path('ajax/ultima_agua/', ajax_ultima_agua, name='ajax_ultima_agua'),

]
