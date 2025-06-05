# despesas/signals.py
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from decimal import Decimal, ROUND_HALF_UP
from django.db import transaction
from collections import defaultdict
from django.db.models import Sum
from .models import (
    Despesa,
    FundoReserva,
    TipoDespesa,
    Rateio,
    FracaoPorTipoDespesa,
    Unidade,
    LeituraEnergia
)

BASE_TIPOS = [
    'Reparos/Reforma',
    'Reparo/Reforma (Sem a Sala)',
    'Salário - Síndico',
    'Elevador',
    'Serviço - Faxina',
    'Material Consumo (Sem Sala Comercial)',
    'Material/Serviço de Consumo',
    'Seguro 6x',
    'Energia Áreas Comuns',
    'Taxa Lixo',
    'Água',
    'Honorários Contábeis',
]

@receiver(post_save, sender=Despesa)
@receiver(post_delete, sender=Despesa)
def recalc_fundo_reserva(sender, instance, **kwargs):
    """
    1) Recalcula valor_total do FundoReserva (10% dos BASE_TIPOS)
    2) Apaga e recria os Rateio desse FundoReserva
    """
    # só recalcula se for um tipo-base
    if instance.tipo.nome not in BASE_TIPOS:
        return

    # pega ou cria o tipo 'Fundo de Reserva'
    try:
        tipo_fundo = TipoDespesa.objects.get(nome__iexact='Fundo de Reserva')
    except TipoDespesa.DoesNotExist:
        return

    # obtém ou cria o FundoReserva do mês/ano
    fr, _ = FundoReserva.objects.get_or_create(
        tipo=tipo_fundo,
        mes=instance.mes,
        ano=instance.ano,
        defaults={'valor_total': Decimal('0.00')}
    )

    # 1) soma todas as despesas-base e calcula 10%
    total_base = Despesa.objects.filter(
        tipo__nome__in=BASE_TIPOS,
        mes=instance.mes,
        ano=instance.ano
    ).aggregate(soma=Sum('valor_total'))['soma'] or Decimal('0')

    fr.valor_total = (total_base * Decimal('0.1')).quantize(Decimal('0.01'))
    fr.save()

    # 2) refaz os Rateio para esse FundoReserva
    #   -> primeiro apaga os antigos
    Rateio.objects.filter(despesa=fr).delete()

    #   -> pega todas as fracões
    frac_qs = FracaoPorTipoDespesa.objects.filter(tipo_despesa=tipo_fundo)

    # monta map unidade → percentual (normalizado 0–1)
    pct_map = {}
    for f in frac_qs:
        pct = Decimal(f.percentual)
        if pct > 1:
            pct /= Decimal('100')
        pct_map[f.unidade] = pct

    # identifica unidade “Sala” para dividir a cota
    sala = next((u for u in pct_map if 'sala' in u.nome.lower()), None)
    share_sala = Decimal('0')
    if sala:
        share_sala = (fr.valor_total * pct_map[sala] / 2).quantize(Decimal('0.01'))

    restante = (fr.valor_total - share_sala).quantize(Decimal('0.01'))

    # cria novo rateio para cada unidade
    for unidade, pct in pct_map.items():
        if unidade == sala:
            valor = share_sala
        else:
            valor = (restante * pct).quantize(Decimal('0.01'))
        Rateio.objects.create(despesa=fr, unidade=unidade, valor=valor)

@receiver(post_save, sender=FundoReserva)
def criar_rateios_para_fundo(sender, instance, **kwargs):
    # limpa rateios antigos
    Rateio.objects.filter(despesa=instance).delete()

    valor_base = instance.valor_total
    # busca todas as frações para este tipo-de-fundo
    frac_qs = FracaoPorTipoDespesa.objects.filter(tipo_despesa=instance.tipo)

    # monta dicionário unidade → percentual normalizado (0–1)
    frac_map = {}
    for f in frac_qs:
        pct = Decimal(f.percentual)
        if pct > 1:
            pct = pct / Decimal('100')
        frac_map[f.unidade] = pct

    # identifica se há “sala” e calcula meia-cota
    sala = None
    for un, pct in frac_map.items():
        if un.nome.lower().startswith('sala'):
            sala = un
            break

    if sala:
        pct_sala = frac_map[sala]
        share_sala = (valor_base * pct_sala / 2).quantize(Decimal('0.01'))
        restante   = (valor_base - share_sala).quantize(Decimal('0.01'))
    else:
        share_sala = Decimal('0')
        restante   = valor_base

    # cria um Rateio para cada unidade
    for un, pct in frac_map.items():
        if sala and un == sala:
            valor = share_sala
        else:
            valor = (restante * pct).quantize(Decimal('0.01'))
        Rateio.objects.create(
            despesa=instance,
            unidade=un,
            valor=valor
        )

@receiver(post_save, sender=FundoReserva)
def sync_fundo_reserva(sender, instance, **kwargs):
    # 1) pega (ou cria) o Despesa do tipo “Fundo de Reserva”
    td = TipoDespesa.objects.get(nome__iexact='Fundo de Reserva')
    desp, _ = Despesa.objects.get_or_create(
        tipo=td,
        mes=instance.mes,
        ano=instance.ano,
        defaults={'valor_total': instance.valor_total}
    )
    # 2) atualiza o valor_total se mudou
    desp.valor_total = instance.valor_total
    desp.save()

    # 3) limpa rateios antigos
    Rateio.objects.filter(despesa=desp).delete()

    # 4) monta mapa de percentuais normalizados (0–1)
    frac_qs = FracaoPorTipoDespesa.objects.filter(tipo_despesa=td)
    frac_map = {}
    for f in frac_qs:
        pct = Decimal(f.percentual)
        if pct > 1:
            pct /= Decimal('100')
        frac_map[f.unidade] = pct

    # 5) identifica se há unidade "Sala" para dividir a cota
    sala = next((u for u in frac_map if 'sala' in u.nome.lower()), None)
    share_sala = Decimal('0')
    if sala:
        share_sala = (instance.valor_total * frac_map[sala] / 2).quantize(Decimal('0.01'))

    restante = (instance.valor_total - share_sala).quantize(Decimal('0.01'))

    # 6) recria os rateios exatamente como no share_por_unidade
    for unidade, pct in frac_map.items():
        if unidade == sala:
            valor = share_sala
        else:
            valor = (restante * pct).quantize(Decimal('0.01'))
        Rateio.objects.create(despesa=desp, unidade=unidade, valor=valor)

@receiver(post_save, sender=Despesa)
def criar_energia_areas_comuns(sender, instance, created, **kwargs):
    if instance.tipo.nome.lower() != 'energia salão':
        return

    # 2) Converte mes/ano para int (caso dê erro, aborta)
    try:
        mes_int = int(instance.mes)
        ano_int = int(instance.ano)
    except (TypeError, ValueError):
        return

    # 3) Garante que exista o TipoDespesa “Energia Áreas Comuns”
    #    (busca por __iexact para não duplicar)
    tipo_ac, _ = TipoDespesa.objects.get_or_create(
        nome__iexact='Energia Áreas Comuns',
        defaults={'nome': 'Energia Áreas Comuns'}
    )

    # 4) Extrai do JSONField de Energia Salão os parâmetros “fatura” e “custo_kwh”
    params = instance.energia_leituras.get('params', {}) if instance.energia_leituras else {}
    try:
        fatura = Decimal(str(params.get('fatura', 0)))
    except:
        fatura = Decimal('0')
    try:
        custo_kwh = Decimal(str(params.get('custo_kwh', 0)))
    except:
        custo_kwh = Decimal('0')

    # 5) Soma todas as “leituras” do mês atual (cada leitura.leitura já é o consumo daquele medidor)
    total_leituras = Decimal('0')
    # Basta somar cada objeto LeituraEnergia deste mes/ano:
    for leitura in LeituraEnergia.objects.filter(mes=mes_int, ano=ano_int):
        total_leituras += Decimal(leitura.leitura)

    # Arredonda o total de leituras para 2 casas
    total_leituras = total_leituras.quantize(Decimal('0.01'), ROUND_HALF_UP)

    # 6) Calcula o valor total de Áreas Comuns:
    #    valor_ac = fatura – (custo_kwh × total_leituras)
    valor_ac = (
        (fatura - (total_leituras * custo_kwh))
        .quantize(Decimal('0.01'), ROUND_HALF_UP)
    )

    # 7) Cria ou atualiza a Despesa “Energia Áreas Comuns” deste mês/ano
    with transaction.atomic():
        desp_ac, criado_ac = Despesa.objects.update_or_create(
            tipo=tipo_ac,
            mes=instance.mes,
            ano=instance.ano,
            defaults={
                'descricao':   f"Áreas Comuns — {instance.mes}/{instance.ano}",
                'valor_total': valor_ac,
            }
        )
        # (opcional) armazena total_leituras no JSONField da própria desp_ac,
        #    para histórico/facilitar debug:
        desp_ac.energia_leituras = {
            'params': {
                'fatura':         float(fatura),
                'custo_kwh':      float(custo_kwh),
                'total_leituras': float(total_leituras),
            }
        }
        desp_ac.save(update_fields=['energia_leituras'])

        # 8) Limpa quaisquer Rateios antigos desta despesa de Áreas Comuns
        Rateio.objects.filter(despesa=desp_ac).delete()

        # 9) Busca TODAS as frações cadastradas para “Energia Áreas Comuns”:
        #    cada FracaoPorTipoDespesa tem `.unidade` e `.percentual`
        frac_qs = FracaoPorTipoDespesa.objects.filter(tipo_despesa=tipo_ac)

        # 10) Monta mapa { unidade: percentual_normalizado }
        pct_map = {}
        for f in frac_qs:
            pct = Decimal(f.percentual)
            # se o admin guardou “10” em vez de “0.10”, divide por 100
            if pct > 1:
                pct /= Decimal('100')
            pct_map[f.unidade] = pct

        # 11) Identifica se há alguma “Sala” para aplicar meia-cota
        sala = next((u for u in pct_map if u.nome.lower().startswith('sala')), None)

        # 12) Calcula quanto a Sala deve pagar (metade da parte dela):
        if sala:
            pct_sala = pct_map[sala]
            # metade da cota normal da Sala
            share_sala = (valor_ac * pct_sala / Decimal('2')).quantize(Decimal('0.01'), ROUND_HALF_UP)
            restante = (valor_ac - share_sala).quantize(Decimal('0.01'), ROUND_HALF_UP)
        else:
            share_sala = Decimal('0')
            restante = valor_ac

        # 13) Agora cria Rateio para cada unidade de pct_map:
        for unidade_obj, pct in pct_map.items():
            if sala and unidade_obj == sala:
                valor_unitario = share_sala
            else:
                valor_unitario = (restante * pct).quantize(Decimal('0.01'), ROUND_HALF_UP)

            Rateio.objects.create(
                despesa=desp_ac,
                unidade=unidade_obj,
                valor=valor_unitario
            )