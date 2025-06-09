import io
import zipfile
from datetime import datetime
from django.db.models import Q
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import os, json
from django.conf import settings
from xlsxwriter.utility import xl_rowcol_to_cell
import xlsxwriter
import numpy as np
from .forms import DespesaGasForm, DespesaAguaForm, DespesaEnergiaForm
from django import forms
from django.contrib import admin
from django.db.models.signals import post_save
from django.dispatch import receiver
import pandas as pd
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.template.response import TemplateResponse
from django.urls import path
from weasyprint import HTML
from django.utils.html import format_html
from django.db.models import OuterRef, Subquery, CharField, F
from django.db.models.functions import Cast
from django.utils import timezone
from django.db.models.signals import post_delete
from django.db.models import Sum
import csv
from django.shortcuts import get_object_or_404

from .models import (
    Unidade,
    TipoDespesa,
    Despesa,
    Rateio,
    LeituraGas,
    LeituraAgua,
    ParametroAgua,
    ParametroGas,
    ParametroEnergia,
    FracaoPorTipoDespesa,
    Boleto,
    DespesaGas,
    DespesaAgua,
    DespesaEnergia,
    MESES_CHOICES,
    LeituraEnergia,
    ExportarXlsx,
    FundoReserva,
    DespesaAreasComuns
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

# Inline para mostrar Frações dentro de TipoDespesa
class FracaoPorTipoDespesaInline(admin.TabularInline):
    model = FracaoPorTipoDespesa
    extra = 0


@admin.register(Unidade)
class UnidadeAdmin(admin.ModelAdmin):
    list_display = ('id', 'nome')
    search_fields = ('nome',)

@admin.register(Despesa)
class DespesaAdmin(admin.ModelAdmin):
    list_display  = ('id', 'tipo', 'mes', 'ano', 'get_valor_total', 'descricao', 'total_leituras',)
    list_filter   = ('tipo', 'mes', 'ano')
    search_fields = ('descricao',)
    readonly_fields = ('mes', 'ano', 'get_valor_total', 'total_leituras')

    default_fieldsets = (
        (None, {
            'fields': ('tipo', 'mes', 'ano', 'get_valor_total', 'descricao'),
        }),
    )

    energia_fieldsets = (
        (None, {
            'fields': ('tipo', 'mes', 'ano', 'get_valor_total'),
        }),
        ('Parâmetros de Energia Salão', {
            'fields': ('fatura', 'kwh_total', 'custo_kwh', 'uso_kwh'),
            'description': 'Preencha os parâmetros de fatura, kWh total, custo por kWh e uso.',
        }),
    )

    def get_fieldsets(self, request, obj=None):
        if obj and obj.tipo and obj.tipo.nome.lower() == 'energia salão':
            return self.energia_fieldsets
        return self.default_fieldsets

    def get_form(self, request, obj=None, **kwargs):
        if obj and obj.tipo and obj.tipo.nome.lower() == 'energia salão':
            kwargs['form'] = DespesaEnergiaForm
            return super().get_form(request, obj, **kwargs)
        return super().get_form(request, obj, **kwargs)

    def get_valor_total(self, obj):
        nome_tipo = (obj.tipo.nome or "").strip().lower()
        if nome_tipo == 'energia áreas comuns':
            # 1) pega a última despesa “Energia Salão” para este mês/ano
            energia = DespesaEnergia.objects.filter(
                mes=obj.mes,
                ano=obj.ano,
                tipo__nome__iexact='Energia Salão'
            ).order_by('-id').first()

            if not energia or not energia.energia_leituras:
                return obj.valor_total  # cai para o valor bruto, se não existir parâmetro

            # 2) extrai fatura e custo_kwh do JSON
            params = energia.energia_leituras.get('params', {})
            raw_fatura = params.get('fatura', 0)
            raw_custo  = params.get('custo_kwh', 0)
            try:
                fatura = Decimal(str(raw_fatura))
            except:
                fatura = Decimal('0')
            try:
                custo = Decimal(str(raw_custo))
            except:
                custo = Decimal('0')

            # 3) soma todas as leituras do mês para obter total_kwh
            try:
                mes_int = int(obj.mes)
                ano_int = int(obj.ano)
                agregado = LeituraEnergia.objects.filter(
                    mes=mes_int,
                    ano=ano_int
                ).aggregate(total=Sum('leitura'))
                total_kwh = agregado.get('total') or Decimal('0')
            except:
                total_kwh = Decimal('0')

            # 4) faz o cálculo: fatura − (custo × total_kwh)
            resultado = (fatura - (custo * total_kwh)).quantize(Decimal('0.01'), ROUND_HALF_UP)
            return resultado

        # para qualquer outro tipo, devolve o valor bruto
        return obj.valor_total

    get_valor_total.short_description = 'Valor Total'
    get_valor_total.admin_order_field = 'valor_total'
    def total_leituras(self, obj):
        """
        Mostra a soma dos kWh lidos se o tipo for 'Energia Salão'.
        Caso contrário, exibe “—”.
        """
        if not obj.tipo or obj.tipo.nome.lower() != 'energia salão':
            return "—"
        try:
            mes_int = int(obj.mes)
            ano_int = int(obj.ano)
        except (TypeError, ValueError):
            return "—"

        aggregate_data = (
            LeituraEnergia.objects
            .filter(mes=mes_int, ano=ano_int)
            .aggregate(total=Sum('leitura'))
        )
        total_kwh = aggregate_data.get('total') or Decimal('0')
        if total_kwh == 0:
            return "—"
        return f"{total_kwh:.4f}"
    total_leituras.short_description = 'Total Leituras (kWh)'

@admin.register(Rateio)
class RateioAdmin(admin.ModelAdmin):
    list_display = ('id', 'despesa', 'unidade', 'valor')
    list_filter = ('despesa', 'unidade')

@admin.register(LeituraEnergia)
class LeituraEnergiaAdmin(admin.ModelAdmin):
    list_display = ('id','unidade','mes','ano','leitura','medidor','consumo',)
    list_filter   = ('ano', 'mes', 'unidade', 'medidor')
    search_fields = ('unidade__nome',)
    ordering      = ('-ano', '-mes', 'unidade', 'medidor')

    def consumo(self, obj):
        # igual ao LeituraGasAdmin, mas filtrando pelo mesmo medidor
        if obj.mes > 1:
            mes_ant, ano_ant = obj.mes - 1, obj.ano
        else:
            mes_ant, ano_ant = 12, obj.ano - 1

        anterior = LeituraEnergia.objects.filter(
            unidade=obj.unidade,
            medidor=obj.medidor,
            mes=mes_ant,
            ano=ano_ant
        ).first()

        if anterior:
            diff = obj.leitura - anterior.leitura
            return f"{max(diff, 0):.3f}"
        return "—"
    consumo.short_description = 'Consumo (kWh)'

@admin.register(DespesaEnergia)
class DespesaEnergiaAdmin(admin.ModelAdmin):
    form = DespesaEnergiaForm
    list_display = (
        'id', 'mes', 'ano', 'valor_total',
        'energia_fatura', 'kwh_total', 'custo_kwh', 'uso_kwh', 'total_leituras'
    )
    readonly_fields = ('mes', 'ano', 'valor_total', 'total_leituras')

    # Métodos para exibir cada parâmetro vindo do JSONField
    def fatura_param(self, obj):
        return obj.energia_leituras.get('params', {}).get('fatura', 0)
    fatura_param.short_description = 'R$ Fatura'

    def kwh_total_param(self, obj):
        return obj.energia_leituras.get('params', {}).get('kwh_total', 0)
    kwh_total_param.short_description = 'kWh Total'

    def custo_kwh_param(self, obj):
        return obj.energia_leituras.get('params', {}).get('custo_kwh', 0)
    custo_kwh_param.short_description = 'R$ Custo kWh'

    def uso_kwh_param(self, obj):
        return obj.energia_leituras.get('params', {}).get('uso_kwh', 0)
    uso_kwh_param.short_description = 'R$ Uso kWh'

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if not qs.exists():
            return qs

        # Função que computa quantos campos “úteis” cada objeto possui no JSON de params
        def contar_campos(obj):
            # Pega o dicionário de parâmetros ou {} se estiver nulo
            params = (obj.energia_leituras or {}).get('params', {})
            # Conta quantos valores em params são diferentes de 0 ou None
            return sum(
                1
                for valor in params.values()
                if valor not in (None, 0)
            )

        # Encontra o objeto com pontuação máxima (maior número de campos preenchidos)
        melhor_obj = max(qs, key=contar_campos)
        return qs.filter(pk=melhor_obj.pk)

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        from django.utils import timezone

        mes = int(request.GET.get('mes', timezone.now().month))
        ano = int(request.GET.get('ano', timezone.now().year))

        ultima = DespesaEnergia.objects.filter(mes=mes, ano=ano).order_by('-id').first()
        if ultima and ultima.energia_leituras:
            params = ultima.energia_leituras.get('params', {})
        else:
            params = {}

        initial.update({
            'fatura':    params.get('fatura',    0),
            'kwh_total': params.get('kwh_total', 1),
            'custo_kwh': params.get('custo_kwh', 0),
            'uso_kwh':   params.get('uso_kwh',   0),
        })
        return initial

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if obj and obj.energia_leituras:
            params = obj.energia_leituras.get('params', {})
            for fld in ('fatura', 'kwh_total', 'custo_kwh', 'uso_kwh'):
                form.base_fields[fld].initial = params.get(fld, 0)
        return form

    def total_leituras(self, obj):
        """
        Retorna o total de kWh lidos no mês/ano de obj.
        """
        try:
            mes_int = int(obj.mes)
            ano_int = int(obj.ano)
        except (TypeError, ValueError):
            return "—"

        aggregate_data = (
            LeituraEnergia.objects
            .filter(mes=mes_int, ano=ano_int)
            .aggregate(total=Sum('leitura'))
        )
        total_kwh = aggregate_data.get('total') or Decimal('0')
        if total_kwh == 0:
            return "—"
        return f"{total_kwh:.4f}"
    total_leituras.short_description = 'Total Leituras (kWh)'

    def save_model(self, request, obj, form, change):
        # 1) Remove quaisquer “Energia Áreas Comuns” já existentes no mesmo mês/ano
        Despesa.objects.filter(
            tipo__nome__iexact='Energia Áreas Comuns',
            mes=obj.mes,
            ano=obj.ano
        ).delete()

        # 2) Força o tipo para “Energia Salão”
        obj.tipo = TipoDespesa.objects.get(nome__iexact='Energia Salão')

        # 3) Guarda os parâmetros vindos do formulário no JSONField
        obj.energia_leituras = {
            'params': {
                'fatura':    float(form.cleaned_data.get('fatura')    or 0),
                'kwh_total': float(form.cleaned_data.get('kwh_total') or 0),
                'custo_kwh': float(form.cleaned_data.get('custo_kwh') or 0),
                'uso_kwh':   float(form.cleaned_data.get('uso_kwh')   or 0),
            }
        }

        # 4) Calcula `valor_total` para “Energia Salão”
        #    (por exemplo, você usa aqui total_kwh * custo_kwh)
        try:
            mes_int = int(obj.mes)
            ano_int = int(obj.ano)
        except (TypeError, ValueError):
            mes_int = None
            ano_int = None

        if mes_int and ano_int:
            agregado = (
                LeituraEnergia.objects
                .filter(mes=mes_int, ano=ano_int)
                .aggregate(total=Sum('leitura'))
            )
            total_kwh = agregado.get('total') or Decimal('0')
        else:
            total_kwh = Decimal('0')

        raw_custo = form.cleaned_data.get('custo_kwh') or 0
        try:
            custo = Decimal(str(raw_custo))
        except Exception:
            custo = Decimal('0')

        raw_fatura = form.cleaned_data.get('fatura') or 0
        try:
            fatura = Decimal(str(raw_fatura))
        except Exception:
            fatura = Decimal('0')

        # Neste ponto, `obj.valor_total` conterá o valor que você quer que apareça
        # em “Energia Salão” (por exemplo, rateio interno baseado em consumo).
        valor_energia_salao = (Decimal(total_kwh) * custo).quantize(Decimal('0.01'), ROUND_HALF_UP)
        obj.valor_total = valor_energia_salao

        # 5) Salva o objeto “Energia Salão” no banco
        super().save_model(request, obj, form, change)

        # ------------------------------------
        # 6) Agora, CRIA (ou ATUALIZA) a despesa “Energia Áreas Comuns” para o mesmo mês/ano
        # ------------------------------------
        tipo_areas = TipoDespesa.objects.get(nome__iexact='Energia Áreas Comuns')
        # Se porventura já existir (embora tenhamos apagado acima), podemos usar get_or_create
        eac_obj, created = Despesa.objects.get_or_create(
            tipo=tipo_areas,
            mes=obj.mes,
            ano=obj.ano,
            defaults={
                'descricao': f"Áreas Comuns — {obj.mes}/{obj.ano}",
                # preenchemos abaixo o valor_total
                'valor_total': Decimal('0.00'),
            }
        )

        # Recalcula o valor de “Energia Áreas Comuns” = total_kwh * custo_kwh
        # Se você quiser que “Energia Áreas Comuns” retenha arquitetura semelhante ao proxy,
        # basta usar o mesmo cálculo:
        valor_areas = (
            Decimal(str(fatura)) - (custo * Decimal(total_kwh))
        ).quantize(Decimal('0.01'), ROUND_HALF_UP)

        eac_obj.valor_total = valor_areas
        eac_obj.save()

@admin.register(DespesaAreasComuns)
class DespesaAreasComunsAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'mes', 'ano', 'valor_calculado',
        'energia_fatura',
        'custo_kwh',
        'total_leituras',
    )
    readonly_fields = (
        'mes', 'ano', 'valor_exibido_admin',
        'energia_fatura', 'custo_kwh', 'total_leituras', 'rateio_html',
    )
    fieldsets = (
        (None, {
            'fields': ('mes', 'ano', 'valor_exibido_admin',),
        }),
        ('Parâmetros de Áreas Comuns', {
            'fields': ('energia_fatura', 'custo_kwh', 'total_leituras'),
            'description': 'Dados vindos da Energia Salão + cálculo de consumo',
        }),
        ('Rateio por Fração (só leitura)', {
            'fields': ('rateio_html',),
        }),
    )

    @admin.display(description='Valor Total (Áreas Comuns)')
    def valor_bruto_db(self, obj):
        """
        Retorna exatamente o que está gravado em obj.valor_total (o valor “319,55”).
        """
        # Se quiser formatar em “R$ 319,55”:
        return f"R$ {obj.valor_total:.2f}".replace('.', ',')

    @admin.display(description='Valor Total (Áreas Comuns)')
    def valor_calculado(self, obj):
        """
        Continua aqui apenas para o change form (se você quiser recalcular),
        mas **não** será usado no list_display.
        """
        # 1) pega fatura e custo do JSON
        raw_fatura = obj.energia_fatura     or 0
        raw_custo  = obj.custo_kwh          or 0
        try:
            fatura = Decimal(str(raw_fatura))
        except:
            fatura = Decimal('0')
        try:
            custo = Decimal(str(raw_custo))
        except:
            custo = Decimal('0')

        # 2) soma total kWh do mês/ano
        try:
            mes_int = int(obj.mes)
            ano_int = int(obj.ano)
            agregado = LeituraEnergia.objects.filter(
                mes=mes_int, ano=ano_int
            ).aggregate(total=Sum('leitura'))
            total_kwh = agregado.get('total') or Decimal('0')
        except:
            total_kwh = Decimal('0')

        # 3) faz o cálculo
        valor_corrigido = (fatura - (custo * total_kwh)).quantize(Decimal('0.01'), ROUND_HALF_UP)
        return valor_corrigido


    def valor_exibido_admin(self, obj):
        """
        Exibe, no change form, o valor corrigido (fatura - custo × total_kwh), formatado como “R$ xx,xx”
        """
        # Repetimos o cálculo do método anterior e só formatamos
        raw_fatura = obj.energia_fatura     or 0
        raw_custo  = obj.custo_kwh          or 0
        try:
            fatura = Decimal(str(raw_fatura))
        except:
            fatura = Decimal('0')
        try:
            custo = Decimal(str(raw_custo))
        except:
            custo = Decimal('0')

        try:
            mes_int = int(obj.mes)
            ano_int = int(obj.ano)
            agregado = LeituraEnergia.objects.filter(
                mes=mes_int, ano=ano_int
            ).aggregate(total=Sum('leitura'))
            total_kwh = agregado.get('total') or Decimal('0')
        except:
            total_kwh = Decimal('0')

        valor_corrigido = (fatura - (custo * total_kwh)).quantize(Decimal('0.01'), ROUND_HALF_UP)
        texto = f"R$ {valor_corrigido:.2f}".replace('.', ',')
        return format_html("<strong>{}</strong>", texto)

    valor_exibido_admin.short_description = "Valor Total"

    def rateio_html(self, obj):
        """
        Retorna em HTML a tabela de Rateio por Fração, DISTRIBUINDO
        sobre o MESMO valor corrigido usado em `valor_exibido_admin`.
        """
        # 1) Primeiro, calculamos novamente o "valor corrigido":
        raw_fatura = obj.energia_fatura or 0
        raw_custo  = obj.custo_kwh    or 0

        try:
            fatura = Decimal(str(raw_fatura))
        except:
            fatura = Decimal('0')
        try:
            custo = Decimal(str(raw_custo))
        except:
            custo = Decimal('0')

        try:
            mes_int = int(obj.mes)
            ano_int = int(obj.ano)
            agregado = (
                LeituraEnergia.objects
                .filter(mes=mes_int, ano=ano_int)
                .aggregate(total=Sum('leitura'))
            )
            total_kwh = agregado.get('total') or Decimal('0')
        except:
            total_kwh = Decimal('0')

        valor_corrigido = (fatura - (custo * total_kwh)).quantize(Decimal('0.01'), ROUND_HALF_UP)

        # 2) Carrega todas as frações para este tipo de despesa:
        fracoes_qs = FracaoPorTipoDespesa.objects.filter(tipo_despesa=obj.tipo)

        # 3) Monta um dicionário { unidade.id: percentual_decimal }
        pct_map = {
            f.unidade.id: Decimal(str(f.percentual))
            for f in fracoes_qs
        }

        # 4) Identifica a “Sala” (unidade com nome contendo 'Sala')
        try:
            sala = Unidade.objects.get(nome__icontains='Sala')
            pct_sala = pct_map.get(sala.id, Decimal('0'))
        except Unidade.DoesNotExist:
            sala = None
            pct_sala = Decimal('0')

        # Se o percentual estiver em '20' (exemplo de 20%), converta para 0.20:
        if pct_sala > 1:
            pct_sala = pct_sala / Decimal('100')

        # 5) Calcula metade da cota da Sala:
        sala_share = (valor_corrigido * pct_sala / Decimal('2')).quantize(Decimal('0.01'), ROUND_HALF_UP)

        # 6) O restante a dividir entre as outras unidades:
        restante = (valor_corrigido - sala_share).quantize(Decimal('0.01'), ROUND_HALF_UP)

        # 7) Monta lista de linhas:
        linhas = []

        # 7a) Sala primeiro, se existir:
        if sala:
            linhas.append({
                'nome': sala.nome,
                'valor': sala_share,
            })

        # 7b) Para cada outra fração:
        for f in fracoes_qs:
            u = f.unidade
            if sala and u.id == sala.id:
                continue
            pct_i = Decimal(str(f.percentual))
            if pct_i > 1:
                pct_i = pct_i / Decimal('100')
            share_i = (restante * pct_i).quantize(Decimal('0.01'), ROUND_HALF_UP)
            linhas.append({
                'nome': u.nome,
                'valor': share_i,
            })

        # 8) Constrói uma mini-tabela HTML:
        html = ['<table style="width:100%; border-collapse: collapse; margin-top:8px;">']
        html.append(
            '<thead>'
            '  <tr>'
            '    <th style="border: 1px solid #444; padding: 4px; text-align:left;">Unidade</th>'
            '    <th style="border: 1px solid #444; padding: 4px; text-align:right;">Valor (R$)</th>'
            '  </tr>'
            '</thead>'
            '<tbody>'
        )

        for linha in linhas:
            nome = linha['nome']
            val  = linha['valor']
            val_str = f"{val:.2f}".replace('.', ',')
            html.append(
                f'<tr>'
                f'  <td style="border: 1px solid #444; padding: 4px;">{nome}</td>'
                f'  <td style="border: 1px solid #444; padding: 4px; text-align:right;">R$ {val_str}</td>'
                f'</tr>'
            )

        html.append('</tbody></table>')
        return format_html(''.join(html))

    rateio_html.short_description = "Rateio por Fração"

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.filter(tipo__nome__iexact='Energia Áreas Comuns')

    @admin.display(description='Energia Fatura')
    def energia_fatura(self, obj):
        energia = DespesaEnergia.objects.filter(
            mes=str(int(obj.mes)),
            ano=obj.ano,
            tipo__nome__iexact='Energia Salão'
        ).order_by('-id').first()

        if not energia or not energia.energia_leituras:
            return Decimal('0.00')
        return Decimal(
            str(energia.energia_leituras['params'].get('fatura', 0))
        ).quantize(Decimal('0.01'))

    @admin.display(description='Custo por kWh (R$)')
    def custo_kwh(self, obj):
        # força buscar sempre a despesa "Energia Salão" daquele mês/ano:
        energia = DespesaEnergia.objects.filter(
            mes=obj.mes,
            ano=obj.ano,
            tipo__nome__iexact='Energia Salão'
        ).order_by('-id').first()

        if not energia or not energia.energia_leituras:
            return Decimal('0.00')
        raw = energia.energia_leituras['params'].get('custo_kwh', 0)
        try:
            return Decimal(str(raw)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        except:
            return Decimal('0.00')

    @admin.display(description='Total Leituras (kWh)')
    def total_leituras(self, obj):
        """
        Retorna a soma de (leitura_atual − leitura_anterior) para cada medidor,
        de cada unidade, no mês/ano de obj. Se não houver leitura, retorna “0,00”.
        """
        try:
            mes_int = int(obj.mes)
            ano_int = int(obj.ano)
        except (TypeError, ValueError):
            return Decimal('0.00')

        aggregate_data = (
            LeituraEnergia.objects
            .filter(mes=mes_int, ano=ano_int)
            .aggregate(total=Sum('leitura'))
        )
        total_kwh = aggregate_data.get('total') or Decimal('0')

        if total_kwh == 0:
            return Decimal('0.00')  # Ou “—”, se preferir
        return total_kwh.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @admin.display(description='Valor Total (Áreas Comuns)')
    def valor_calculado(self, obj):
        """
        Retorna: Fatura da Energia Salão − (Custo kWh × Total Leituras),
        sem formatação (para que possamos exibir em list_display se quisermos).
        """

        # 1) consulta o parâmetro "Energia Salão" daquele mês/ano
        energia = DespesaEnergia.objects.filter(
            mes=obj.mes,
            ano=obj.ano,
            tipo__nome__iexact='Energia Salão'
        ).order_by('-id').first()

        if not energia or not energia.energia_leituras:
            return Decimal('0.00')

        params = energia.energia_leituras.get('params', {})
        try:
            fatura = Decimal(str(params.get('fatura', 0))).quantize(Decimal('0.01'), ROUND_HALF_UP)
        except:
            fatura = Decimal('0.00')
        try:
            custo = Decimal(str(params.get('custo_kwh', 0))).quantize(Decimal('0.01'), ROUND_HALF_UP)
        except:
            custo = Decimal('0.00')

        # 2) soma total kWh do mês/ano
        try:
            mes_int = int(obj.mes)
            ano_int = int(obj.ano)
            agregado = (
                LeituraEnergia.objects
                .filter(mes=mes_int, ano=ano_int)
                .aggregate(total=Sum('leitura'))
            )
            total_kwh = agregado.get('total') or Decimal('0')
        except:
            total_kwh = Decimal('0')

        # 3) faz o cálculo: fatura − (custo × total_kwh)
        resultado = (fatura - (custo * total_kwh)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        return resultado

    def save_model(self, request, obj, form, change):
        """
        Ao salvar “Energia Áreas Comuns”, definimos:
          valor_total = fatura − (custo_kwh × total_consumo_do_mes)
        """
        energia = DespesaEnergia.objects.filter(
            mes=obj.mes, ano=obj.ano
        ).order_by('-id').first()
        if not energia or not energia.energia_leituras:
            obj.valor_total = Decimal('0.00')
        else:
            params  = energia.energia_leituras['params']
            raw_fat = params.get('fatura', 0)
            raw_cus = params.get('custo_kwh', 0)

            try:
                fatura = Decimal(str(raw_fat))
            except:
                fatura = Decimal('0')
            try:
                custo = Decimal(str(raw_cus))
            except:
                custo = Decimal('0')

            # reutiliza total_leituras para obter o consumo
            total_consumo = self.total_leituras(obj)
            valor_areas = fatura - (custo * total_consumo)
            obj.valor_total = valor_areas.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        # força o tipo correto
        obj.tipo = TipoDespesa.objects.get(nome__iexact='Energia Áreas Comuns')
        super().save_model(request, obj, form, change)

@admin.register(LeituraGas)
class LeituraGasAdmin(admin.ModelAdmin):
    list_display = ('id', 'unidade', 'mes', 'ano', 'leitura', 'consumo')
    list_filter = ('ano', 'mes', 'unidade')
    search_fields = ('unidade__nome',)
    ordering = ('-ano', '-mes', 'unidade')

    def consumo(self, obj):
        # calcula mês/ano anterior
        if obj.mes > 1:
            mes_ant = obj.mes - 1
            ano_ant = obj.ano
        else:
            mes_ant = 12
            ano_ant = obj.ano - 1

        anterior = LeituraGas.objects.filter(
            unidade=obj.unidade,
            mes=mes_ant,
            ano=ano_ant
        ).first()

        if anterior:
            # diferença entre a leitura atual e a anterior
            diff = obj.leitura - anterior.leitura
            # formata com 4 casas decimais, se quiser:
            return f"{max(diff, 0):.4f}"
        else:
            return "—"  # ou "0.0000", como preferir

    consumo.short_description = 'Consumo (m³)'
    consumo.admin_order_field = 'leitura'

class DespesaBaseAdmin(admin.ModelAdmin):
    list_display = ('id','mes','ano','valor_total')
    list_filter  = ('mes','ano')

# ––––– Despesas de GÁS –––––
@admin.register(DespesaGas)
class DespesaGasAdmin(DespesaBaseAdmin):
    form = DespesaGasForm
    list_display = (
        'id',
        'mes',
        'ano',
        'recarga_param',
        'kg_param',
        'm3_kg_param',
        'valor_m3_param',
        'valor_total',
    )
    fieldsets = (
        (None, {'fields': ('mes', 'ano', 'valor_total')}),
        ('Parâmetros de Gás', {
            'fields': ('recarga', 'kg', 'm3_kg', 'valor_m3'),
            'description': 'Pré-populado com o mês anterior ou com o JSON',
        }),
    )

    # 2) Métodos para ler cada parâmetro do JSONField
    def recarga_param(self, obj):
        return obj.gas_leituras.get('params', {}).get('recarga', 0)
    recarga_param.short_description = 'Recarga (R$)'

    def kg_param(self, obj):
        return obj.gas_leituras.get('params', {}).get('kg', 0)
    kg_param.short_description = 'KG'

    def m3_kg_param(self, obj):
        return obj.gas_leituras.get('params', {}).get('m3_kg', 0)
    m3_kg_param.short_description = 'm³/kg'

    def valor_m3_param(self, obj):
        return obj.gas_leituras.get('params', {}).get('valor_m3', 0)
    valor_m3_param.short_description = 'R$ por m³'

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.filter(tipo__nome__iexact='Gás')

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        from django.utils import timezone

        # determina mês/ano da URL ou hoje
        mes = int(request.GET.get('mes', timezone.now().month))
        ano = int(request.GET.get('ano', timezone.now().year))

        # busca o último objeto salvo para este mês/ano
        ultima = DespesaGas.objects.filter(mes=mes, ano=ano).order_by('-id').first()
        if ultima and ultima.gas_leituras:
            params = ultima.gas_leituras.get('params', {})
        else:
            params = {}

        initial.update({
            'recarga':  params.get('recarga',  0),
            'kg':       params.get('kg',       1),
            'm3_kg':    params.get('m3_kg',    1),
            'valor_m3': params.get('valor_m3', 0),
        })
        return initial

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if obj and obj.gas_leituras:
            params = obj.gas_leituras.get('params', {})
            for fld in ('recarga', 'kg', 'm3_kg', 'valor_m3'):
                form.base_fields[fld].initial = params.get(fld, 0)
        return form


    def save_model(self, request, obj, form, change):
        # (1) garante o tipo e grava os params no JSONField
        obj.tipo = TipoDespesa.objects.get(nome__iexact='Gás')
        obj.gas_leituras = {
            'params': {
                'recarga':  float(form.cleaned_data['recarga']  or 0),
                'kg':       float(form.cleaned_data['kg']       or 1),
                'm3_kg':    float(form.cleaned_data['m3_kg']    or 1),
                'valor_m3': float(form.cleaned_data['valor_m3'] or 0),
            },
            # opcional: preserve leituras antigas se já existirem
            'leituras': obj.gas_leituras.get('leituras', {}) if obj.gas_leituras else {}
        }

        # (2) antes de salvar, recalcula valor_total = consumo * R$/m³
        #     supondo que o JSONField 'leituras' já tenha {'anterior': x, 'atual': y}
        leituras = obj.gas_leituras.get('leituras', {})
        anterior = Decimal(leituras.get('anterior', 0))
        atual    = Decimal(leituras.get('atual', 0))
        consumo  = (atual - anterior).quantize(Decimal('0.0001'))
        valor_m3 = Decimal(str(form.cleaned_data['valor_m3'] or 0))
        obj.valor_total = (consumo * valor_m3).quantize(Decimal('0.01'))

        # (3) salva tudo normalmente
        super().save_model(request, obj, form, change)


@admin.register(DespesaAgua)
class DespesaAguaAdmin(DespesaBaseAdmin):
    form = DespesaAguaForm
    fieldsets = (
        (None, {'fields': ('mes','ano','valor_total')}),
        ('Parâmetros de Água', {
            'fields': ('fatura','m3_total','valor_m3_agua'),
            'description': 'Pré-povoado com o mês anterior',
        }),
    )

    def save_model(self, request, obj, form, change):
        # 1) Marca o tipo como “Água”
        obj.tipo = TipoDespesa.objects.get(nome__iexact='Água')

        # 2) Converte a fatura do form para Decimal e armazena em valor_total
        raw_fatura = form.cleaned_data.get('fatura') or 0
        try:
            fatura_dec = Decimal(str(raw_fatura))
        except (InvalidOperation, TypeError):
            fatura_dec = Decimal('0')
        obj.valor_total = fatura_dec.quantize(Decimal('0.01'), ROUND_HALF_UP)

        # 3) Armazena também os parâmetros no JSONField (se você for usar depois)
        obj.agua_leituras = {
            'params': {
                'fatura':      float(raw_fatura),
                'm3_total':    float(form.cleaned_data.get('m3_total')   or 0),
                'valor_m3_agua': float(form.cleaned_data.get('valor_m3_agua') or 0),
            }
        }

        duplicatas = DespesaAgua.objects.filter(
            mes=obj.mes,
            ano=obj.ano
        ).exclude(pk=obj.pk)
        for dup in duplicatas:
            Rateio.objects.filter(despesa=dup).delete()
            dup.delete()

        # 4) Salva o objeto “DespesaÁgua” no banco, para termos obj.id disponível
        super().save_model(request, obj, form, change)

        # --------------------------------------------------------
        # 5) Agora precisamos recriar o Rateio de água para TODAS as unidades
        # --------------------------------------------------------

        # 5.1) Delete todos os Rateio antigos dessa despesa,
        #      caso já existam (evita herdar valores negativos).
        Rateio.objects.filter(despesa=obj).delete()

        LeituraAgua.objects.filter(mes=obj.mes, ano=obj.ano).delete()

        # Caso o JSON de parâmetros traga um dicionário de leituras no formato
        # {unidade_id: leitura}, cria novas entradas automaticamente. Caso
        # contrário, orienta o usuário a cadastrá-las manualmente.
        leituras_novas = (obj.agua_leituras or {}).get('leituras')
        if leituras_novas:
            for unidade_id, valor in leituras_novas.items():
                unidade = Unidade.objects.filter(id=unidade_id).first()
                if unidade:
                    LeituraAgua.objects.create(
                        unidade=unidade,
                        mes=obj.mes,
                        ano=obj.ano,
                        leitura=Decimal(str(valor))
                    )
        else:
            self.message_user(
                request,
                'Registre as leituras de água para este mês na seção "Leituras de Água".'
            )

        # Determina mês/ano anterior para buscar a leitura de maio/2025
        mes_atual = int(obj.mes)
        ano_atual = int(obj.ano)
        if mes_atual > 1:
            mes_ant = mes_atual - 1
            ano_ant = ano_atual
        else:
            mes_ant = 12
            ano_ant = ano_atual - 1

        # Dicionário que vai guardar { unidade_obj: consumo_em_m3 }
        consumos = {}

        # Primeiro, percorre todas as unidades cadastradas no condomínio
        for unidade in Unidade.objects.all():
            # Tenta pegar a leitura de junho de 2025 e a de maio de 2025:
            leit_atual = LeituraAgua.objects.filter(
                unidade=unidade,
                mes=mes_atual,
                ano=ano_atual
            ).first()
            leit_ant = LeituraAgua.objects.filter(
                unidade=unidade,
                mes=mes_ant,
                ano=ano_ant
            ).first()

            if leit_atual and leit_ant:
                diff = leit_atual.leitura - leit_ant.leitura
                consumo_m3 = diff if diff > 0 else Decimal("0")
            elif leit_atual:
                consumo_m3 = leit_atual.leitura
            else:
                consumo_m3 = Decimal("0")

            consumos[unidade] = consumo_m3

        # 5.3) Soma o consumo total de todas as unidades neste mês
        total_consumo_geral = sum(consumos.values())  # soma só Decimals

        # 5.4) Se houver consumo, calcula o R$ por m³; senão, todo rateio será zero
        if total_consumo_geral > 0:
            valor_por_m3 = (fatura_dec / total_consumo_geral).quantize(Decimal('0.0001'), ROUND_HALF_UP)
        else:
            # se não houver leitura anterior ou se todas as unidades zeraram,
            # forçamos rateio zero para todas
            valor_por_m3 = Decimal('0')

        # 5.5) Cria um Rateio para cada unidade: consumo_unidade × valor_por_m3
        for unidade, consumo_m3 in consumos.items():
            # Se quisesse tratar unis que não têm rateio (por exemplo, valor zero),
            # poderia condicionar:
            # if consumo_m3 > 0:
            #     rateio_val = (consumo_m3 * valor_por_m3).quantize(Decimal('0.01'), ROUND_HALF_UP)
            # else:
            #     rateio_val = Decimal('0')
            rateio_val = (consumo_m3 * valor_por_m3).quantize(Decimal('0.01'), ROUND_HALF_UP)

            Rateio.objects.create(
                despesa=obj,
                unidade=unidade,
                valor=rateio_val
            )

    def fatura(self, obj):     return obj.fatura_agua or 0
    def m3_total(self, obj):   return obj.m3_total_agua or 0
    def valor_m3(self, obj):   return obj.valor_m3_agua or 0

    def get_queryset(self, request):
        return super().get_queryset(request).filter(tipo__nome__iexact='Água')

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        mes = int(request.GET.get('mes', timezone.now().month))
        ano = int(request.GET.get('ano', timezone.now().year))
        if mes > 1:
            mes_ant, ano_ant = mes - 1, ano
        else:
            mes_ant, ano_ant = 12, ano - 1
        ultima = DespesaAgua.objects.filter(
            mes=str(mes_ant), ano=ano_ant
        ).order_by('-id').first()
        if ultima and ultima.agua_leituras:
            params = ultima.agua_leituras.get('params', {})
            initial.update({
                'fatura'       : params.get('fatura',    0),
                'm3_total'     : params.get('m3_total',  1),
                'valor_m3_agua': params.get('valor_m3',  0),
            })
        return initial

@admin.register(LeituraAgua)
class LeituraAguaAdmin(admin.ModelAdmin):
    list_display = ('id', 'unidade', 'mes', 'ano', 'leitura', 'consumo')
    list_filter = ('ano', 'mes', 'unidade')
    search_fields = ('unidade__nome',)
    ordering = ('-ano', '-mes', 'unidade')

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        # Correção: usando OuterRef para referenciar campos de LeituraAgua
        rateio_sq = Rateio.objects.filter(
            despesa__tipo__nome__iexact='Água',
            despesa__mes=OuterRef('mes'),
            despesa__ano=OuterRef('ano'),
            unidade=OuterRef('unidade')
        ).values('valor')[:1]

        return qs.annotate(
            _valor_rateado=Subquery(rateio_sq)
        )

    def consumo(self, obj):
        """Calcula o consumo baseado na leitura anterior"""
        if obj.mes > 1:
            mes_ant, ano_ant = obj.mes - 1, obj.ano
        else:
            mes_ant, ano_ant = 12, obj.ano - 1

        anterior = LeituraAgua.objects.filter(
            unidade=obj.unidade,
            mes=mes_ant,
            ano=ano_ant
        ).first()

        if anterior:
            diff = obj.leitura - anterior.leitura
            return f"{max(diff, 0):.4f}"
        return "0.0000"
    consumo.short_description = 'Consumo (m³)'

@admin.register(FracaoPorTipoDespesa)
class FracaoPorTipoDespesaAdmin(admin.ModelAdmin):
    list_display = ('tipo_despesa', 'unidade', 'percentual')
    list_filter = ('tipo_despesa',)
    search_fields = ('tipo_despesa__nome', 'unidade__nome')
    class Meta:
        model = FracaoPorTipoDespesa
        fields = '__all__'
        widgets = {
            'percentual': forms.NumberInput(attrs={'step': '0.000001'}),
        }

@admin.register(TipoDespesa)
class TipoDespesaAdmin(admin.ModelAdmin):
    list_display = ('id', 'nome')
    inlines = [FracaoPorTipoDespesaInline]

# --- Formulário para escolher mês/ano ---
MESES_CHOICES = [
    (str(i), nome) for i, nome in enumerate([
        "Janeiro","Fevereiro","Março","Abril","Maio","Junho",
        "Julho","Agosto","Setembro","Outubro","Novembro","Dezembro"
    ], start=1)
]

class GerarBoletosForm(forms.Form):
    mes = forms.ChoiceField(label="Mês", choices=MESES_CHOICES)
    ano = forms.ChoiceField(label="Ano", choices=[(str(y), str(y)) for y in range(2025, 2030)])

class RateioFundoInline(admin.TabularInline):
    model = Rateio
    fk_name = 'despesa'
    fields = ('unidade', 'valor')
    readonly_fields = ('unidade', 'valor')
    extra = 0
    can_delete = False
    verbose_name = "Parcela do Fundo"
    verbose_name_plural = "Parcelas do Fundo"

@admin.register(FundoReserva)
class FundoReservaAdmin(admin.ModelAdmin):
    inlines       = [RateioFundoInline]
    list_display   = ('mes', 'ano', 'tipo', 'valor_total', 'valor_fundo')
    list_filter  = ('mes', 'ano', 'tipo')
    readonly_fields = ('valor_fundo', 'share_por_unidade')
    fields = (
        'tipo','mes','ano','valor_total',
        'share_por_unidade',
    )

    def valor_fundo(self, obj):
        # simplesmente mostra o valor_total já gravado
        return f"R$ {obj.valor_total:.2f}"
    valor_fundo.short_description = "Valor Fundo (10%)"

    def share_por_unidade(self, obj):

        qs = Rateio.objects.filter(despesa=obj).order_by('unidade__nome')
        linhas = []
        for r in qs:
            # r.valor já é Decimal
            linhas.append(f"{r.unidade.nome}: R$ {r.valor.quantize(Decimal('0.01'))}")
        return format_html("<br>".join(linhas))
    share_por_unidade.short_description = "Parte por Unidade"

    # bloquear alterações, se quiser:
    def has_add_permission(self, request):    return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False

    def save_model(self, request, obj, form, change):
        # monta o dict de parâmetros a partir do que veio no formulário
        params = {
            'fatura':        form.cleaned_data['fatura'],
            'm3_total':      form.cleaned_data['m3_total'],
            'valor_m3':      form.cleaned_data['valor_m3_agua'],
        }
        # grava no JSONField antes de salvar o objeto
        obj.agua_leituras = {
            'params': params,
            'leituras': obj.agua_leituras.get('leituras', {}) if obj.agua_leituras else {}
        }
        super().save_model(request, obj, form, change)

        # 1) busca a soma dos tipos-base no mesmo mês/ano
        base_tipos = [
            'Reparos/Reforma', 'Reparo/Reforma (Sem a Sala)', 'Salário - Síndico', 'Elevador', 'Serviço - Faxina',
            'Material Consumo Sem Sala Comercial', 'Material/Serviço de Consumo',
            'Seguro 6x', 'Energia Áreas Comuns', 'Taxa Lixo',
            'Água', 'Honorários Contábeis'
        ]
        total_base = Despesa.objects.filter(
            tipo__nome__in=base_tipos,
            mes=obj.mes,
            ano=obj.ano
        ).aggregate(soma=Sum('valor_total'))['soma'] or Decimal('0')

        # 2) 10% desse total
        valor_fundo = total_base * Decimal('0.1')

        # 3) determina meia-fatia para Sala Comercial
        f_sala = FracaoPorTipoDespesa.objects.filter(
            tipo_despesa__nome__iexact='Fundo de Reserva',
            unidade__nome__icontains='sala'
        ).select_related('unidade').first()

        if f_sala:
            sala = f_sala.unidade
        else:
            sala = None
        pct = FracaoPorTipoDespesa.objects.get(
            tipo_despesa=obj.tipo, unidade=sala
        ).percentual
        pct = Decimal(pct)
        if pct > 1:
            pct /= Decimal('100')
        share_sala = (valor_fundo * pct) / Decimal('2')

        # 4) monta rateio para todas as unidades
        frac_map = {
            f.unidade.id: (Decimal(f.percentual) / (Decimal('100') if f.percentual > 1 else Decimal('1')))
            for f in FracaoPorTipoDespesa.objects.filter(
                tipo_despesa__nome__iexact='Fundo de Reserva'
            )
        }

        f_sala = FracaoPorTipoDespesa.objects.filter(
            tipo_despesa__nome__iexact='Fundo de Reserva',
            unidade__nome__icontains='sala'
        ).select_related('unidade').first()

        if f_sala:
            sala = f_sala.unidade
            sala_pct   = frac_map.get(sala.id, Decimal('0'))
            bruto_sala = (valor_fundo * sala_pct).quantize(Decimal('0.01'))
            share_sala = (bruto_sala / Decimal('2')).quantize(Decimal('0.01'))
            restante   = (valor_fundo - share_sala).quantize(Decimal('0.01'))
        else:
            sala       = None
            share_sala = Decimal('0')
            restante   = valor_fundo

        valores = { sala: share_sala }
        restante = valor_fundo - share_sala
        for unid, pct in frac_map.items():
            if unid == sala.id:
                continue
            pctd = pct if pct <= 1 else pct / Decimal('100')
            u = Unidade.objects.get(id=unid)
            valores[u] = restante * pctd

        # 5) salva o objeto e recria os Rateio
        obj.valor_total = valor_fundo
        super().save_model(request, obj, form, change)
        Rateio.objects.filter(despesa=obj).delete()
        for unidade, v in valores.items():
            Rateio.objects.create(despesa=obj, unidade=unidade, valor=v)

@admin.register(Boleto)
class BoletoAdmin(admin.ModelAdmin):
    change_list_template = "admin/despesas/boletos_changelist.html"

    # bloqueia add/change/delete
    def has_add_permission(self, request):    return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                'gerar/',
                self.admin_site.admin_view(self.gerar_boletos_view),
                name='despesas_boleto_gerar'
            ),
        ]
        return custom + urls

    def boletos_button(self, request):
        return {
            'title':       "Gerar boletos por mês",
            'button_url':  "gerar/",
            'button_text': "Gerar boletos",
        }

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context.update(self.boletos_button(request))
        return super().changelist_view(request, extra_context=extra_context)

    def gerar_boletos_view(self, request):
        if request.method == 'POST':
            form = GerarBoletosForm(request.POST)
            if form.is_valid():
                mes = int(form.cleaned_data['mes'])
                ano = int(form.cleaned_data['ano'])
                return self._gerar_zip_de_boletos(mes, ano)
        else:
            form = GerarBoletosForm(initial={
                'mes': str(datetime.now().month),
                'ano': str(datetime.now().year),
            })

        context = self.admin_site.each_context(request)
        context.update({
            'form': form,
            'title': "Gerar boletos",
        })
        return TemplateResponse(request, "admin/despesas/gerar_boletos.html", context)

    def _gerar_zip_de_boletos(self, mes, ano):
        buffer = io.BytesIO()
        zf = zipfile.ZipFile(buffer, 'w')

        if mes > 1:
            mes_ant = mes - 1
            ano_ant = ano
        else:
            mes_ant = 12
            ano_ant = ano - 1

        # 1) soma as despesas-base e calcula 10%
        BASE = [
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
        soma = Despesa.objects.filter(
            tipo__nome__in=BASE,
            mes=str(mes),
            ano=ano
        ).aggregate(total=Sum('valor_total'))['total'] or Decimal('0')
        valor_fundo_total = (soma * Decimal('0.1')).quantize(Decimal('0.01'))

        # 2) mapa de frações normalizadas (0–1) para o Fundo de Reserva
        frac_qs = FracaoPorTipoDespesa.objects.filter(
            tipo_despesa__nome__iexact='Fundo de Reserva'
        )
        frac_map = {
            f.unidade.id: (Decimal(f.percentual) / Decimal('100'))
            if Decimal(f.percentual) > 1 else Decimal(f.percentual)
            for f in frac_qs
        }

        # 3) identifica a Sala e calcula metade da sua cota
        try:
            f_sala = frac_qs.get(unidade__nome__icontains='Sala')
            sala = f_sala.unidade
            pct_sala = frac_map[sala.id]
            share_sala = (valor_fundo_total * pct_sala / Decimal('2')).quantize(Decimal('0.01'))
        except FracaoPorTipoDespesa.DoesNotExist:
            sala = None
            share_sala = Decimal('0')

        # 4) restante após a sala
        restante_fundo = (valor_fundo_total - share_sala).quantize(Decimal('0.01'))

        # 5) monta o mapa de valores do Fundo para cada unidade
        valores_fundo = {}
        if sala:
            valores_fundo[sala.id] = share_sala
        for unid, pct in frac_map.items():
            if unid == sala.id:
                continue
            valores_fundo[unid] = (restante_fundo * pct).quantize(Decimal('0.01'))

        tipos = (
            TipoDespesa.objects
            .exclude(nome__iexact='Fundo de Reserva')
            .exclude(nome__iexact='Fatura Energia Elétrica')
            .order_by('nome')
        )

        existe_despesa_agua = Despesa.objects.filter(
            tipo__nome__iexact='Água',
            mes=str(mes),
            ano=ano,
            valor_total__gt=0
        ).exists()

        # 6) gera um PDF por unidade
        for unidade in Unidade.objects.order_by('nome'):
            lancamentos = []

            # percorre cada tipo (exceto Fundo de Reserva)
            for tipo in tipos:
                desp = (Despesa.objects
                    .filter(tipo=tipo,
                            mes=str(mes),
                            ano=ano,
                            valor_total__gt=0)
                    .order_by('-id')
                    .first())

                if desp:
                    rateio = Rateio.objects.filter(
                        despesa=desp,
                        unidade=unidade
                    ).first()

                    # só calcula consumo se existir rateio e valor > 0
                    if rateio and rateio.valor > Decimal('0'):
                        atual = LeituraGas.objects.filter(
                            unidade=unidade, mes=mes, ano=ano
                        ).first()
                        anterior = LeituraGas.objects.filter(
                            unidade=unidade, mes=mes_ant, ano=ano_ant
                        ).first()

                        if atual and anterior:
                            diff = atual.leitura - anterior.leitura
                            consumo_gas = diff if diff > 0 else 0
                        else:
                            consumo_gas = 0
                    else:
                        consumo_gas = 0

                    valor = rateio.valor if rateio else Decimal('0.00')

                else:
                    # não existe despesa cadastrada: exibe “–”
                    consumo_gas = 0
                    valor = None

                lancamentos.append({
                    'descricao': tipo.nome,
                    'valor':     valor,
                })

            valor_fundo_un = valores_fundo.get(unidade.id, Decimal('0'))

            # por fim, insere o Fundo de Reserva
            if valor_fundo_un > 0:
                lancamentos.append({
                    'descricao': 'Fundo de Reserva',
                    'valor':     valor_fundo_un,
                })

            # 6.4) consumo de gás
            rateio_gas = Rateio.objects.filter(
                despesa__tipo__nome__iexact='Gás',
                despesa__mes=str(mes),
                despesa__ano=ano,
                unidade=unidade
            ).first()

            if rateio_gas and rateio_gas.valor > Decimal('0'):
                atual_gas = LeituraGas.objects.filter(unidade=unidade,
                                       mes=str(mes),
                                       ano=str(ano)
                                      ).first()
                anterior  = LeituraGas.objects.filter(unidade=unidade,
                                       mes=str(mes_ant),
                                       ano=str(ano_ant)
                                      ).first()
                if atual_gas and anterior:
                    diff = atual_gas.leitura - anterior.leitura
                    consumo_gas = diff if diff > 0 else 0
                else:
                    consumo_gas = 0
            else:
                consumo_gas = 0

            if existe_despesa_agua:
                atual_agua = LeituraAgua.objects.filter(unidade=unidade,
                                                         mes=str(mes),
                                                         ano=str(ano)
                                                        ).first()
                ant_agua   = LeituraAgua.objects.filter(unidade=unidade,
                                                         mes=str(mes_ant),
                                                         ano=str(ano_ant)
                                                        ).first()
                if atual_agua and ant_agua:
                    diff_wa = atual_agua.leitura - ant_agua.leitura
                    consumo_agua = diff_wa if diff_wa > 0 else 0
                else:
                    consumo_agua = 0

                rateio_agua = Rateio.objects.filter(
                    despesa__tipo__nome__iexact='Água',
                    despesa__mes=str(mes),
                    despesa__ano=ano,
                    unidade=unidade
                ).first()
                valor_agua = rateio_agua.valor if (rateio_agua and rateio_agua.valor > 0) else None
            else:
                consumo_agua = None
                valor_agua = None

            # 6.6) soma final e geração do PDF
            total_boleto = sum(
                item['valor'] if item['valor'] is not None else Decimal('0.00')
                for item in lancamentos
            )
            html = render_to_string("despesas/boletos/boleto.html", {
                'unidade':          unidade,
                'mes':              mes,
                'ano':              ano,
                'lancamentos':      lancamentos,
                'total':            total_boleto,
                'gas_consumption':  consumo_gas,
                'water_consumption': consumo_agua,
            })
            pdf = HTML(string=html, base_url=f"file://{settings.STATIC_ROOT}/").write_pdf()
            zf.writestr(f"boleto_{unidade.nome}_{mes:02d}-{ano}.pdf", pdf)

        # 7) devolve o ZIP
        zf.close()
        resp = HttpResponse(buffer.getvalue(), content_type="application/zip")
        resp["Content-Disposition"] = f'attachment; filename="boletos_{mes:02d}-{ano}.zip"'
        return resp

@receiver(post_delete, sender=Despesa)
def recalc_fundo_reserva(sender, instance, **kwargs):

    if instance.tipo.nome in BASE_TIPOS:
        from .models import FundoReserva, TipoDespesa
        fundo_tipo = TipoDespesa.objects.get(nome__iexact='Fundo de Reserva')
        fr, created = FundoReserva.objects.get_or_create(
            tipo=fundo_tipo,
            mes=instance.mes,
            ano=instance.ano,
            defaults={'valor_total': Decimal('0.00'), 'descricao': ''}
        )
        # forçando o save para chamar seu save_model e recomputar
        fr.save()

@receiver(post_save, sender=Despesa)
def recalc_fundo_reserva_on_save(sender, instance, **kwargs):

    if instance.tipo.nome not in BASE_TIPOS:
        return

    # busca (ou cria) o Fundo de Reserva para o mês/ano
    try:
        tipo_fundo = TipoDespesa.objects.get(nome__iexact='Fundo de Reserva')
    except TipoDespesa.DoesNotExist:
        return

    fr, created = FundoReserva.objects.get_or_create(
        tipo=tipo_fundo,
        mes=instance.mes,
        ano=instance.ano,
        defaults={'valor_total': Decimal('0.00')}
    )

    # soma todas as despesas-base e calcula 10%
    total_base = Despesa.objects.filter(
        tipo__nome__in=BASE_TIPOS,
        mes=instance.mes,
        ano=instance.ano
    ).aggregate(soma=Sum('valor_total'))['soma'] or Decimal('0')
    fr.valor_total = (total_base * Decimal('0.1')).quantize(Decimal('0.01'))
    fr.save()

@admin.register(ExportarXlsx)
class ExportarXlsxAdmin(admin.ModelAdmin):
    change_list_template = "admin/despesas/exports_changelist.html"

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                'exportar-xlsx/',
                self.admin_site.admin_view(self.exportar_excel_view),
                name='despesas_exportar_xlsx'
            ),
        ]
        return custom + urls

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context['meses_choices'] = MESES_CHOICES
        extra_context['anos_choices']   = [
            str(y) for y,_ in Despesa._meta.get_field('ano').choices
        ]
        return super().changelist_view(request, extra_context=extra_context)

    def exportar_excel_view(self, request):
        mes_str = request.GET.get('mes')
        ano_str = request.GET.get('ano')
        if not mes_str or not ano_str:
            return HttpResponse('Selecione mês e ano.', status=400)

        mes = int(mes_str)
        ano = int(ano_str)

            # --- 1) buscar últimos registros de cada despesa direto do banco ---
        wa = DespesaAgua.objects.filter(mes=mes, ano=ano).order_by('-id').first()
        if wa and wa.agua_leituras:
            p = wa.agua_leituras['params']
            agua_fat    = Decimal(p.get('fatura', 0)).quantize(Decimal('0.01'), ROUND_HALF_UP)
            agua_m3tot  = Decimal(p.get('m3_total', 0)).quantize(Decimal('0.01'), ROUND_HALF_UP)
            agua_val_m3 = Decimal(p.get('valor_m3_agua', 0)).quantize(Decimal('0.01'), ROUND_HALF_UP)
        else:
            agua_fat = agua_m3tot = agua_val_m3 = Decimal('0')

        ga = DespesaGas.objects.filter(mes=mes, ano=ano).order_by('-id').first()
        if ga and ga.gas_leituras:
            p = ga.gas_leituras['params']
            gas_rec    = Decimal(p.get('recarga', 0)).quantize(Decimal('0.01'), ROUND_HALF_UP)
            gas_kg     = Decimal(p.get('kg',      1)).quantize(Decimal('0.0001'), ROUND_HALF_UP)
            gas_m3kg   = Decimal(p.get('m3_kg',   1)).quantize(Decimal('0.0001'), ROUND_HALF_UP)
            gas_val_m3 = Decimal(p.get('valor_m3',0)).quantize(Decimal('0.01'), ROUND_HALF_UP)
        else:
            gas_rec = gas_kg = gas_m3kg = gas_val_m3 = Decimal('0')

        en = DespesaEnergia.objects.filter(mes=mes, ano=ano).order_by('-id').first()
        if en and en.energia_leituras:
            p = en.energia_leituras['params']
            en_fat     = Decimal(p.get('fatura',   0)).quantize(Decimal('0.01'), ROUND_HALF_UP)
            en_kwh_tot = Decimal(p.get('kwh_total',1)).quantize(Decimal('0.0001'), ROUND_HALF_UP)
            en_custo   = Decimal(p.get('custo_kwh',0)).quantize(Decimal('0.01'), ROUND_HALF_UP)
            en_uso     = Decimal(p.get('uso_kwh',  0)).quantize(Decimal('0.01'), ROUND_HALF_UP)
        else:
            en_fat = en_kwh_tot = en_custo = en_uso = Decimal('0')


        if agua_m3tot != 0:
            valor_por_m3   = (agua_fat / agua_m3tot).quantize(Decimal('0.01'), ROUND_HALF_UP)
        else:
            valor_por_m3   = Decimal('0.00')

        # 1) busca dados
        despesas = Despesa.objects.filter(mes=mes, ano=int(ano))
        rateios  = Rateio.objects.filter(despesa__mes=mes, despesa__ano=int(ano))

        # filtra todos os rateios do período
        rateios = Rateio.objects.filter(
            despesa__mes=mes,
            despesa__ano=int(ano)
        )

        # pega apenas o FUNDOSERVA mais recente
        fundo_tipo = TipoDespesa.objects.get(nome__iexact='Fundo de Reserva')
        latest_fr = FundoReserva.objects.filter(
            mes=mes,
            ano=int(ano),
            tipo=fundo_tipo
        ).order_by('-id').first()

        shares = {}

        if latest_fr:
            # mantém todos os rateios que NÃO são de Fundo de Reserva
            # + apenas os rateios desse único Fundo de Reserva
            rateios = rateios.filter(
                Q(despesa__tipo__nome__iexact='Fundo de Reserva', despesa=latest_fr) |
                ~Q(despesa__tipo__nome__iexact='Fundo de Reserva')
            )

        # 3) monta DataFrame RATEIO achatado
        rows = []
        for r in rateios:
            rows.append({
                'Unidade':      r.unidade.nome,
                'Tipo':         r.despesa.tipo.nome,
                'Valor Rateio': float(Decimal(r.valor).quantize(Decimal('0.01'))),
            })
        df_rateio = pd.DataFrame(rows)

        # 4) pivot para ter colunas de cada tipo
        df_rateio_pivot = (
            df_rateio
            .pivot_table(
                index='Unidade',
                columns='Tipo',
                values='Valor Rateio',
                aggfunc='sum',
                fill_value=0
            )
            .reset_index()
        )
        df_rateio_pivot.replace(0, pd.NA, inplace=True)
        # tenta carregar o proxy de FundoReserva para este mês/ano
        if latest_fr:
            rateios_fundo = Rateio.objects.filter(despesa=latest_fr)
            shares = {r.unidade.nome: float(r.valor) for r in rateios_fundo}
            df_rateio_pivot['Fundo de Reserva'] = df_rateio_pivot['Unidade'].map(shares).fillna(0)
        else:
            df_rateio_pivot['Fundo de Reserva'] = 0

        # injeta na pivot — todas as linhas terão valor (zero se a unidade não existir no mapa)
        df_rateio_pivot['Fundo de Reserva'] = (
            df_rateio_pivot['Unidade']
            .map(shares)
            .fillna(Decimal('0'))
            .astype(float)
        )
        tipos = list(
            TipoDespesa.objects
            .order_by('id')
            .values_list('nome', flat=True)
        )

        for t in tipos:
            if t not in df_rateio_pivot.columns:
                df_rateio_pivot[t] = 0

        # só agora você sabe que 'Gás' existe
        gas_rateio_map = df_rateio_pivot.set_index('Unidade')['Gás'].fillna(0).to_dict()

        # reordena as colunas: primeiro 'Unidade', depois todas as despesas
        df_rateio_pivot = df_rateio_pivot[['Unidade'] + tipos]

        # ––– 5) EXIBIÇÃO POR UNIDADE –––
        # calcula mês/ano anterior corretamente
        prev_mes = mes - 1 if mes > 1 else 12
        prev_ano = ano     if mes > 1 else ano - 1

        # pivot com índice = Tipo e colunas = Unidade
        df_exib_un = (
            df_rateio
            .pivot_table(
                index='Tipo',
                columns='Unidade',
                values='Valor Rateio',
                aggfunc='sum',
                fill_value=0
            )
        )
        df_exib_un.replace(0, pd.NA, inplace=True)
        # garante todas as despesas, na ordem do TipoDespesa
        tipos = list(
            TipoDespesa.objects
                .order_by('id')
                .values_list('nome', flat=True)
        )
        df_exib_un = df_exib_un.reindex(tipos).fillna(0)

        # calcula consumo de gás e água por unidade
        energia_map = {}
        for un in df_exib_un.columns:
            # só calcula se tiver rateio > 0
            rateio_en = Rateio.objects.filter(
                despesa__tipo__nome__iexact='Energia Salão',
                despesa__mes=str(mes),
                despesa__ano=ano,
                unidade__nome=un
            ).first()
            if rateio_en and rateio_en.valor > Decimal('0'):
                # leituras anteriores e atuais dos 2 medidores
                ant1 = LeituraEnergia.objects.filter(
                    unidade__nome=un, medidor=1,
                    mes=prev_mes, ano=prev_ano
                ).first()
                atu1 = LeituraEnergia.objects.filter(
                    unidade__nome=un, medidor=1,
                    mes=mes, ano=ano
                ).first()
                ant2 = LeituraEnergia.objects.filter(
                    unidade__nome=un, medidor=2,
                    mes=prev_mes, ano=prev_ano
                ).first()
                atu2 = LeituraEnergia.objects.filter(
                    unidade__nome=un, medidor=2,
                    mes=mes, ano=ano
                ).first()

                if ant1 and atu1 and ant2 and atu2:
                    diff1 = atu1.leitura - ant1.leitura
                    diff2 = atu2.leitura - ant2.leitura
                    energia = (diff1 + diff2) if (diff1 + diff2) > 0 else 0
                else:
                    energia = 0
            else:
                energia = 0

            energia_map[un] = energia

        gas_map = {}
        agua_map = {}
        for un in df_exib_un.columns:

            rateio_gas = Rateio.objects.filter(
                despesa__tipo__nome__iexact='Gás',
                despesa__mes=str(mes),
                despesa__ano=ano,
                unidade__nome=un
            ).first()

            if rateio_gas and rateio_gas.valor > Decimal('0'):
                atual_gas = LeituraGas.objects.filter(unidade__nome=un, mes=mes, ano=ano).first()
                ant_gas   = LeituraGas.objects.filter(unidade__nome=un, mes=prev_mes, ano=prev_ano).first()
                if atual_gas and ant_gas:
                    diff_g = atual_gas.leitura - ant_gas.leitura
                    gas_map[un] = diff_g if diff_g > 0 else 0
                else:
                    gas_map[un] = 0
            else:
                gas_map[un] = 0

            rateio_agua = Rateio.objects.filter(
                despesa__tipo__nome__iexact='Água',
                despesa__mes=str(mes),
                despesa__ano=ano,
                unidade__nome=un
            ).first()

            if rateio_agua and rateio_agua.valor > Decimal('0'):
                atual_agua = LeituraAgua.objects.filter(unidade__nome=un, mes=mes,     ano=ano).first()
                ant_agua   = LeituraAgua.objects.filter(unidade__nome=un, mes=prev_mes, ano=prev_ano).first()
                # Só subtrai se TIVER leituras atual e anterior
                if atual_agua and ant_agua:
                    diff_wa = atual_agua.leitura - ant_agua.leitura
                    agua_map[un] = diff_wa if diff_wa > 0 else 0
                else:
                    # Se não existir leitura atual OU não existir anterior, zera
                    agua_map[un] = 0
            else:
                # Se não há rateio de água para esta unidade, força 0
                agua_map[un] = 0

            # água
            atual_a = LeituraAgua.objects.filter(unidade__nome=un, mes=mes,     ano=ano).first()
            ant_a   = LeituraAgua.objects.filter(unidade__nome=un, mes=prev_mes, ano=prev_ano).first()
            if atual_a and ant_a:
                diff = atual_a.leitura - ant_a.leitura
                agua_map[un] = diff if diff > 0 else 0
            else:
                agua_map[un] = 0

        # adiciona as linhas no final
        df_exib_un.loc['TOTAL BOLETO']    = df_exib_un.sum(axis=0)
        df_exib_un.loc['Consumo Gás m³']  = pd.Series(gas_map)
        df_exib_un.loc['Consumo Água m³'] = pd.Series(agua_map)
        df_exib_un.loc['Consumo Energia Salão'] = pd.Series(energia_map)

        df_exib_un.index.name = 'Tipo'

        # 2) reseta índice e renomeia a coluna 'Tipo' para 'Despesas Condomínio'
        df_exib_un = df_exib_un.reset_index().rename(columns={'Tipo': 'Despesas Condomínio'})

        piv = df_exib_un.set_index('Despesas Condomínio')

        agua_rateio_map = piv.loc['Água'].to_dict()
        gas_rateio_map  = {
            uni: (val or 0)
            for uni, val in df_rateio_pivot.set_index('Unidade')['Gás'].to_dict().items()
        }
        # 7) DESPESAS × RATEIO
#        wa = DespesaAgua.objects.filter(mes=mes, ano=ano).order_by('-id').first()
#        if wa and wa.agua_leituras:
#            params = wa.agua_leituras.get('params', {})
#            agua_fat    = Decimal(params.get('fatura',    0))
#            agua_m3tot  = Decimal(params.get('m3_total',  0))
#            agua_val_m3 = Decimal(params.get('valor_m3_agua',  0))
#        else:
#            agua_fat = agua_m3tot = agua_val_m3 = Decimal('0')

#        if agua_m3tot != Decimal("0"):
#            valor_por_m3 = (agua_fat / agua_m3tot).quantize(
#                Decimal("0.01"),
#                rounding=ROUND_HALF_UP
#            )
#        else:
#            valor_por_m3 = Decimal("0.00")



        en = DespesaEnergia.objects.filter(mes=mes, ano=ano).order_by('-id').first()
        if en and en.energia_leituras:
            p = en.energia_leituras['params']
            en_fat = Decimal(p.get('fatura',   0))
            en_uso = Decimal(p.get('uso_kwh',  0))
        else:
            en_kwh_tot = en_custo = Decimal('0')
        rows = []

        for un in Unidade.objects.order_by('nome'):
            # consumo você já calcula normalmente
            ant_wa = LeituraAgua.objects.filter(unidade=un, mes=prev_mes, ano=prev_ano).first()
            atu_wa = LeituraAgua.objects.filter(unidade=un, mes=mes,     ano=ano    ).first()
            if ant_wa and atu_wa:
                diff_wa = atu_wa.leitura - ant_wa.leitura
                cons_wa = diff_wa if diff_wa > 0 else 0
            else:
                cons_wa = 0

            ant_ga = LeituraGas.objects.filter(unidade=un, mes=prev_mes, ano=prev_ano).first()
            atu_ga = LeituraGas.objects.filter(unidade=un, mes=mes,     ano=ano    ).first()
            if ant_ga and atu_ga:
                diff_ga = atu_ga.leitura - ant_ga.leitura
                cons_ga = diff_ga if diff_ga > 0 else 0
            else:
                cons_ga = 0

            ant1 = LeituraEnergia.objects.filter(unidade=un, mes=prev_mes, ano=prev_ano, medidor=1).first()
            atu1 = LeituraEnergia.objects.filter(unidade=un, mes=mes,     ano=ano,     medidor=1).first()
            ant2 = LeituraEnergia.objects.filter(unidade=un, mes=prev_mes, ano=prev_ano, medidor=2).first()
            atu2 = LeituraEnergia.objects.filter(unidade=un, mes=mes,     ano=ano,     medidor=2).first()
            if ant1 and atu1 and ant2 and atu2:
                diff1 = atu1.leitura - ant1.leitura
                diff2 = atu2.leitura - ant2.leitura
                total_diff = diff1 + diff2
                cons_en = total_diff if total_diff > 0 else 0
            else:
                cons_en = 0


            la1 = getattr(ant1, 'leitura', 0)
            lk1 = getattr(atu1, 'leitura', 0)
            la2 = getattr(ant2, 'leitura', 0)
            lk2 = getattr(atu2, 'leitura', 0)

            cons_en = (Decimal(str(lk1)) - Decimal(str(la1))) + (Decimal(str(lk2)) - Decimal(str(la2)))
            # multiplica Decimal * Decimal → OK, já quantizando para duas casas
            valor_dec = (cons_en * en_uso).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            # e, se você quiser armazenar como float para o DataFrame:
            valor = float(valor_dec)

            if lk1 is None and lk2 is None:
                cons_en = None
                valor   = None
            else:
                diff1   = (lk1 or 0) - (la1 or 0)
                diff2   = (lk2 or 0) - (la2 or 0)
                cons_en = max(diff1 + diff2, 0)
                valor   = cons_en * Decimal(en_uso)  # ou Decimal(en_uso)

            # 1) pega os valores prontos vindos do admin
            wa = DespesaAgua.objects.filter(mes=mes, ano=ano).order_by('-id').first()
            if wa and wa.agua_leituras:
                params_agua = wa.agua_leituras.get('params', {})
                agua_fat    = Decimal(params_agua.get('fatura',    0))
                agua_m3tot  = Decimal(params_agua.get('m3_total',  0))
                agua_val_m3 = Decimal(params_agua.get('valor_m3_agua',  0))
            else:
                agua_fat = agua_m3tot = agua_val_m3 = Decimal('0')

            # faça o mesmo para m3_total e valor_m3_agua:
            if wa and wa.m3_total is not None:
                agua_m3tot = Decimal(str(wa.m3_total))
            else:
                agua_m3tot = Decimal('0')


            if wa and wa.valor_m3_agua is not None:
                agua_val_m3 = Decimal(str(wa.valor_m3_agua))
            else:
                agua_val_m3 = Decimal('0')

            # repita o guard para gás e energia…
            ga = DespesaGas.objects.filter(mes=mes, ano=ano).order_by('-id').first()
            if ga and ga.gas_leituras:
                p = ga.gas_leituras.get('params', {})
                gas_rec  = Decimal(p.get('recarga', 0))
                gas_kg   = Decimal(p.get('kg',      0))
                gas_m3kg = Decimal(p.get('m3_kg',   0))
            else:
                gas_rec = gas_kg = gas_m3kg = Decimal('0')

            en = DespesaEnergia.objects.filter(mes=mes, ano=ano).order_by('-id').first()
            if en and en.energia_leituras:
                p = en.energia_leituras.get('params', {})
                en_kwh_tot = Decimal(p.get('kwh_total', 0))
                en_custo   = Decimal(p.get('custo_kwh', 0))
            else:
                en_kwh_tot = en_custo = Decimal('0')

            cons_en = (lk1 - la1) + (lk2 - la2)
            valor   = en_uso * cons_en

            # 2) só depois disso você monta o dicionário:
            rows.append({
                'Unidade':            un.nome,
                'Água – R$ Fatura':   float(agua_fat),
                'Água – m³ total':    float(agua_m3tot),
                'Água – R$/m³':       float(agua_val_m3),
                'Água – Leitura Ant.':   getattr(ant_wa, 'leitura', ''),
                'Água – Leitura Atu.':   getattr(atu_wa, 'leitura', ''),
                'Água – Consumo m³':     cons_wa,
                'Água – R$':           float(agua_rateio_map.get(un.nome, 0)),

                'Gás – R$ Recarga':    float(gas_rec),
                'Gás – KG':            float(gas_kg),
                'Gás – m³/kg':         float(gas_m3kg),
                'Gás – R$ por m³': float(gas_val_m3),
                'Gás – Leitura Ant.':  getattr(ant_ga, 'leitura', ''),
                'Gás – Leitura Atu.':  getattr(atu_ga, 'leitura', ''),
                'Gás – Consumo m³':     cons_ga,
                'Gás – R$':  float(gas_rateio_map .get(un.nome, 0)),

                'Energia – Fatura': float(en_fat),
                'Energia – kWh Total':         float(en_kwh_tot),
                'Energia – R$ por kWh':        float(en_custo),
                'Energia – Uso kWh': float(en_uso),
                'Energia – Med1 Ant.': la1,
                'Energia – Med1 Atu.': lk1,
                'Energia – Med2 Ant.': la2,
                'Energia – Med2 Atu.': lk2,
                'Energia – Consumo kWh': cons_en,
                'Energia – R$':          float(valor),
            })

        df_leituras = pd.DataFrame(rows)

        df_leituras['Energia – Med1 Atu.'] = (
            df_leituras['Energia – Med1 Atu.']
              .replace('-', np.nan)
              .replace('', np.nan)
              .astype(float)
        )
        df_leituras['Energia – Med2 Atu.'] = (
            df_leituras['Energia – Med2 Atu.']
              .replace('-', np.nan)
              .replace('', np.nan)
              .astype(float)
        )

        # máscara: linhas onde as duas colunas são NaN
        mask_sem_leitura = (
            df_leituras[['Energia – Med1 Atu.', 'Energia – Med2 Atu.']]
              .isna()
              .all(axis=1)
        )

        # para essas linhas, zera consumo e valor
        df_leituras.loc[mask_sem_leitura, 'Energia – Consumo kWh'] = 0.0
        df_leituras.loc[mask_sem_leitura, 'Energia – R$']           = 0.0

        # garante que não haja Decimal na divisão
        df_leituras['Água – R$'] = df_leituras['Água – R$'].astype(float)
        df_leituras['Água – Consumo m³'] = df_leituras['Água – Consumo m³'].astype(float)

        # agora sim, float ÷ float
        df_leituras['Água – R$/m³'] = (
            df_leituras['Água – R$'] /
            df_leituras['Água – Consumo m³'].replace(0, pd.NA)  # evita divisão por zero
        ).fillna(0).round(4)

        # calcula a coluna "Água – R$/m³" linha a linha
        df_leituras['Água – R$/m³'] = (
            df_leituras['Água – R$'] / df_leituras['Água – Consumo m³']
        ).round(4)

        # primeiro converte as duas colunas para float
        serie_valr   = df_leituras['Água – R$'].astype(float)
        serie_cons   = df_leituras['Água – Consumo m³'].astype(float)

        # aí faz a divisão, cuidando do zero
        df_leituras['Água – R$/m³'] = (
            serie_valr.div(serie_cons.replace(0, pd.NA))  # troca 0 por NA para não dar divisão por zero
                   .fillna(0)                              # coloca 0 onde era NA ou infinidade
        )

        df_leituras.replace(0, pd.NA, inplace=True)

        buffer = io.BytesIO()
        # É aqui que abre o único 'with'
        with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
            df_leituras.to_excel(writer,
                                 sheet_name='DESPESAS RATEIO',
                                 index=False)
        # lista completa na ordem que você quer
        colunas = [
            'Unidade',
            'Água – R$ Fatura',
            'Água – m³ total',
            'Água – R$/m³',
            'Água – Leitura Ant.',
            'Água – Leitura Atu.',
            'Água – Consumo m³',
            'Água – R$',
            # agora os campos de gás
            'Gás – R$ Recarga',
            'Gás – KG',
            'Gás – m³/kg',
            'Gás – R$ por m³',
            'Gás – Leitura Ant.',
            'Gás – Leitura Atu.',
            'Gás – Consumo m³',
            'Gás – R$',
            # e finalmente os de energia
            'Energia – Fatura',
            'Energia – kWh Total',
            'Energia – R$ por kWh',
            'Energia – Uso kWh',
            'Energia – kWh total',
            'Energia – Custo kWh',
            'Energia – Med1 Ant.',
            'Energia – Med1 Atu.',
            'Energia – Med2 Ant.',
            'Energia – Med2 Atu.',
            'Energia – Consumo kWh',
            'Energia – R$',
        ]
        # filtra apenas as colunas existentes (evita KeyError caso falte alguma)
        colunas_existentes = [c for c in colunas if c in df_leituras.columns]
        df_leituras = df_leituras[[c for c in colunas if c in df_leituras.columns]]

        if agua_m3tot:
            valor_por_m3 = (agua_fat / agua_m3tot).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            valor_por_m3 = Decimal('0.00')

        df_leituras['Água – R$/m³'] = float(valor_por_m3)

        df_params_agua = pd.DataFrame([
            {"Parâmetro": "R$ Fatura",      "Valor": float(agua_fat)},
            {"Parâmetro": "m³ Total",        "Valor": float(agua_m3tot)},
            {"Parâmetro": "R$ por m³ Água", "Valor": float(valor_por_m3)},
        ])
        df_params_gas = pd.DataFrame([
            {"Parâmetro": "R$ Recarga",      "Valor": float(gas_rec)},
            {"Parâmetro": "KG",              "Valor": float(gas_kg)},
            {"Parâmetro": "m³/kg",           "Valor": float(gas_m3kg)},
            {"Parâmetro": "R$ por m³ Gás",   "Valor": float(gas_val_m3)},
        ])
        df_params_energia = pd.DataFrame([
            {"Parâmetro": "R$ Fatura",      "Valor": float(en_fat)},
            {"Parâmetro": "kWh Total",      "Valor": float(en_kwh_tot)},
            {"Parâmetro": "R$ por kWh",     "Valor": float(en_custo)},
            {"Parâmetro": "Uso kWh",        "Valor": float(en_uso)},
        ])

        # 3) gravar tudo no Excel, incluindo a aba de parâmetros
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
            df_params_gas    .to_excel(writer, sheet_name="Parâmetros Gás",    index=False)
            df_params_energia.to_excel(writer, sheet_name="Parâmetros Energia",index=False)

            # suas demais abas
            df_rateio_pivot.to_excel(
                writer,
                sheet_name="RATEIO",
                index=False,
                na_rep='-'
            )

            unit_cols = [c for c in df_exib_un.columns if c != 'Despesas Condomínio']
            df_exib_un['Total Geral'] = ''
            if 'TOTAL BOLETO' in df_exib_un['Despesas Condomínio'].values:
                soma_geral = (
                    df_exib_un.loc[
                        df_exib_un['Despesas Condomínio']=='TOTAL BOLETO',
                        unit_cols
                    ]
                    .sum(axis=1)
                    .iat[0]
                )
                df_exib_un.loc[
                    df_exib_un['Despesas Condomínio']=='TOTAL BOLETO',
                    'Total Geral'
                ] = float(soma_geral)
            # reordena para garantir que fique no fim
            cols = [c for c in df_exib_un.columns if c!='Total Geral'] + ['Total Geral']
            df_exib_un = df_exib_un[cols]

            # reordena para garantir que 'Total Geral' fique na última coluna
            cols_ordenadas = [c for c in df_exib_un.columns if c != 'Total Geral'] + ['Total Geral']
            df_exib_un = df_exib_un[cols_ordenadas]

            df_exib_un.to_excel(
                writer,
                sheet_name="EXIBIÇÃO POR UNIDADE",
                index=False,
                na_rep='-'
            )
            workbook       = writer.book
            bold_currency_fmt = workbook.add_format({
                'bold':      True,
                'num_format':'R$ #,##0.00;R$ -#,##0.00;"–";@',
                'align':     'center',
                'valign':    'vcenter',
            })

            # 2) Pega a planilha “Exibição por Unidade”
            ws2 = writer.sheets['EXIBIÇÃO POR UNIDADE']

            # 3) Descobre o índice da nova coluna
            col_idx = {name: idx for idx, name in enumerate(df_exib_un.columns)}
            if 'Total Geral' in col_idx:
                idx_tot = col_idx['Total Geral']
                # 4) Aplica largura + negrito moeda
                ws2.set_column(idx_tot, idx_tot, 15, bold_currency_fmt)

            df_leituras.to_excel(
                writer,
                sheet_name="DESPESAS RATEIO",
                index=False,
                na_rep='-'
            )

            nrows   = len(df_leituras)
            cols    = list(df_leituras.columns)
            idx     = {c:i for i,c in enumerate(cols)}

            c_med1  = idx["Energia – Med1 Atu."]
            c_med2  = idx["Energia – Med2 Atu."]
            c_ant1  = idx["Energia – Med1 Ant."]
            c_ant2  = idx["Energia – Med2 Ant."]
            c_uso   = idx["Energia – Uso kWh"]
            c_cons  = idx["Energia – Consumo kWh"]
            c_val   = idx["Energia – R$"]

            # formatos
            number4_fmt  = writer.book.add_format({'num_format':'0.0000','align':'center'})
            currency_fmt = writer.book.add_format({'num_format':'R$ #,##0.00','align':'center'})

            # tenta obter a aba 'DESPESAS RATEIO'
            ws3 = writer.sheets.get('DESPESAS RATEIO')
            if ws3:
                for row in range(1, nrows+1):
                    m1 = xl_rowcol_to_cell(row, c_med1)
                    m2 = xl_rowcol_to_cell(row, c_med2)
                    a1 = xl_rowcol_to_cell(row, c_ant1)
                    a2 = xl_rowcol_to_cell(row, c_ant2)
                    uso= xl_rowcol_to_cell(row, c_uso)
                    cons_cell = xl_rowcol_to_cell(row, c_cons)

                    # só calcula consumo se existir leitura atual em pelo menos um medidor
                    f_cons = f'=IF(AND({m1}="",{m2}=""),0,({m1}-{a1})+({m2}-{a2}))'
                    ws3.write_formula(row, c_cons, f_cons, number4_fmt)

                    # só calcula valor se houver consumo
                    f_val  = f'=IF(AND({m1}="",{m2}=""),0,{cons_cell}*{uso})'
                    ws3.write_formula(row, c_val, f_val, currency_fmt)

            # --- 8.2) crie seus formatos ---
            workbook    = writer.book
            currency_fmt = workbook.add_format({
                'num_format': 'R$ #,##0.00;R$ -#,##0.00;"–";@',
                'align':      'center',
                'valign':     'vcenter',
            })
            header_fmt = workbook.add_format({
                'bold':   True,
                'align':  'center',
                'valign': 'vcenter',
                'bg_color':'#D3D3D3',
            })
            number4_fmt = workbook.add_format({
                'num_format': '0.0000',
                'align':      'center',
                'valign':     'vcenter',
            })
            # negrito + moeda para a linha TOTAL BOLETO
            bold_currency_fmt = workbook.add_format({
                'bold':      True,
                'num_format':'R$ #,##0.00;R$ -#,##0.00;"–";@',
                'align':     'center',
                'valign':    'vcenter',
            })

            # --- 8.3) formate apenas a aba RATEIO, se existir ---
            if 'RATEIO' in writer.sheets:
                ws = writer.sheets['RATEIO']
                first_col = 1
                last_col  = df_rateio_pivot.shape[1] - 1
                ws.set_column(first_col, last_col, 15, currency_fmt)
                ws.set_row(0, None, header_fmt)

                # escreve a linha TOTAL logo abaixo dos dados
                n = len(df_rateio_pivot)
                total_row = n + 1
                ws.write(total_row, 0, 'TOTAL:', header_fmt)
                for col_idx in range(first_col, last_col + 1):
                    col_letter = xlsxwriter.utility.xl_col_to_name(col_idx)
                    formula = f"=SUM({col_letter}2:{col_letter}{n+1})"
                    ws.write_formula(total_row, col_idx, formula, currency_fmt)

                # --- 8.4) formata aba EXIBIÇÃO POR UNIDADE ---
                if 'EXIBIÇÃO POR UNIDADE' in writer.sheets:
                    ws2 = writer.sheets['EXIBIÇÃO POR UNIDADE']

                    # cabeçalho
                    ws2.set_row(0, None, header_fmt)

                    # monta número de linhas até o TOTAL BOLETO
                    total_row_exib = len(tipos) + 1

                    # TOTAL BOLETO em negrito e moeda
                    ws2.set_row(total_row_exib, None, bold_currency_fmt)

                    total_cols = len(df_exib_un.columns)

                    # 1) todas as colunas de unidade (1 até última) como R$
                    ws2.set_column(1, total_cols-1, 15, currency_fmt)

                    # 2) apenas as linhas de consumo (duas após o TOTAL BOLETO) com 4 casas e dash no zero
                    # consumo Gás
                    ws2.set_row(total_row_exib + 1, None, number4_fmt)
                    # consumo Água
                    ws2.set_row(total_row_exib + 2, None, number4_fmt)
                    # consumo Energia Salão
                    ws2.set_row(total_row_exib + 3, None, number4_fmt)

                # --- 8.5) formata aba DESPESAS RATEIO ---
                if 'DESPESAS RATEIO' in writer.sheets:
                    ws3 = writer.sheets['DESPESAS RATEIO']
                    # cabeçalho em negrito
                    ws3.set_row(0, None, header_fmt)

                    col_idx = {col: i for i, col in enumerate(df_leituras.columns)}

                # 1) formata Unidade em negrito
                bold_fmt = workbook.add_format({'bold': True})

                fmt_agua = workbook.add_format({
                    'bg_color': '#E8F6F3',
                    'align':    'center',
                    'valign':   'vcenter',
                })
                fmt_gas  = workbook.add_format({
                    'bg_color': '#FEF9E7',
                    'align':    'center',
                    'valign':   'vcenter',
                })
                fmt_ener = workbook.add_format({
                    'bg_color': '#D6EAF8',  # cor nova
                    'align':    'center',
                    'valign':   'vcenter',
                })
                fmt_cons = workbook.add_format({
                    'bg_color': '#E8DAEF',
                    'align':    'center',
                    'valign':   'vcenter',
                })

                # formato de consumo com 4 casas e centralizado
                number4_fmt = workbook.add_format({
                    'num_format': '0.0000',
                    'align':      'center',
                    'valign':     'vcenter',
                })
                # cria um format só com alinhamento central
                center_fmt = workbook.add_format({
                    'align':  'center',
                    'valign': 'vcenter',
                })

                for sheet in ("Parâmetros Água", "Parâmetros Gás", "Parâmetros Energia"):
                    ws = writer.sheets.get(sheet)
                    if ws:
                        ws.hide()

                ws_rateio = writer.sheets.get("RATEIO")
                if ws_rateio:
                    ws_rateio.activate()

                # encontra colunas que contêm 'Leitura' no nome
                leitura_cols = [i for i, c in enumerate(df_leituras.columns) if 'Leitura' in c]

                # aplica centralização nessas colunas, mantendo a largura automática (ou escolha uma)
                for col_idx in range(len(df_leituras.columns)):
                    # largura “None” mantém o padrão/autofit
                    ws3.set_column(col_idx, col_idx, None, center_fmt)

                last_col = len(df_leituras.columns) - 1
                ws3.set_column(last_col, last_col, 15, currency_fmt)

                # determina até onde colorir (linha de dados começa em 1; limite = min(24, total))
                n_linhas = df_leituras.shape[0]
                last_row = min(n_linhas, 24)

                cols = df_leituras.columns.tolist()
                idx  = {c: i for i, c in enumerate(cols)}

                # coluna Unidade em negrito
                bold_fmt = workbook.add_format({'bold': True, 'align':'center','valign':'vcenter'})
                if 'Unidade' in idx:
                    ws3.set_column(idx['Unidade'], idx['Unidade'], None, bold_fmt)

                # agrupa índices por prefixo
                agua_idx = [idx[c] for c in cols if c.startswith('Água')]
                gas_idx  = [idx[c] for c in cols if c.startswith('Gás')]
                ener_idx = [idx[c] for c in cols if c.startswith('Energia')]
                cons_idx = [idx[c] for c in cols if 'Consumo' in c]

                # aplica o number4_fmt nas colunas de consumo
                for ci in cons_idx:
                    ws3.set_column(ci, ci, 12, number4_fmt)

                # helper para pintar bloco
                def paint(cols_idx, fmt):
                    if not cols_idx: return
                    c1, c2 = min(cols_idx), max(cols_idx)
                    ws3.conditional_format(1, c1, last_row, c2, {
                        'type':   'no_errors',
                        'format': fmt
                    })

                # pinta cada bloco
                paint(agua_idx, fmt_agua)
                paint(gas_idx,  fmt_gas)
                paint(ener_idx, fmt_ener)
                paint(cons_idx, fmt_cons)

        buffer.seek(0)
        filename = f'RATEIOS DESPESAS {mes:02d}_{ano}.xlsx'
        response = HttpResponse(
            buffer.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
