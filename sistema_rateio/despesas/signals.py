# despesas/signals.py
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from decimal import Decimal, ROUND_HALF_UP
from django.db import transaction
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

    try:
        mes_int = int(instance.mes)
        ano_int = int(instance.ano)
    except (TypeError, ValueError):
        # Se não conseguir converter mês/ano, aborta
        return

    # 2) Garante que exista o tipo 'Energia Áreas Comuns'
    tipo_ac, _ = TipoDespesa.objects.get_or_create(
        nome__iexact='Energia Áreas Comuns',
        defaults={'nome': 'Energia Áreas Comuns'}
    )

    # 3) Extrai do JSONField 'energia_leituras' o valor de fatura e o custo_kwh
    params = instance.energia_leituras.get('params', {}) if instance.energia_leituras else {}
    try:
        fatura = Decimal(str(params.get('fatura', 0)))
    except:
        fatura = Decimal('0')
    try:
        custo_kwh = Decimal(str(params.get('custo_kwh', 0)))
    except:
        custo_kwh = Decimal('0')

    # 4) Calcula o total de consumo (kWh) no mês/ano: para cada LeituraEnergia,
    #    faz (leitura_atual – leitura_anterior) e soma tudo.
    total_consumo = Decimal('0')
    leituras_atuais = LeituraEnergia.objects.filter(mes=mes_int, ano=ano_int)

    for leitura in leituras_atuais:
        # determina mês/ano anterior
        if mes_int > 1:
            mes_ant, ano_ant = mes_int - 1, ano_int
        else:
            mes_ant, ano_ant = 12, ano_int - 1

        anterior = LeituraEnergia.objects.filter(
            unidade=leitura.unidade,
            medidor=leitura.medidor,
            mes=mes_ant,
            ano=ano_ant
        ).first()

        if anterior:
            # diferença entre leitura atual e anterior
            diff = Decimal(leitura.leitura) - Decimal(anterior.leitura)
            # se houver leitura anterior, soma a diferença
            total_consumo += max(diff, Decimal('0'))

    # quantiza para duas casas decimais
    total_consumo = total_consumo.quantize(Decimal('0.01'), ROUND_HALF_UP)

    # 5) Aplica a fórmula: Valor Áreas Comuns = fatura – (total_consumo * custo_kwh)
    valor_ac = (fatura - (total_consumo * custo_kwh)).quantize(Decimal('0.01'), ROUND_HALF_UP)

    # 6) Cria ou atualiza o registro de Despesa 'Energia Áreas Comuns' para o mesmo mês/ano
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

        # 7) (Opcional) Guarda os parâmetros no JSONField 'energia_leituras' de 'desp_ac'
        desp_ac.energia_leituras = {
            'params': {
                'fatura':         float(fatura),
                'custo_kwh':      float(custo_kwh),
                'total_leituras': float(total_consumo),
            }
        }
        desp_ac.save(update_fields=['energia_leituras'])