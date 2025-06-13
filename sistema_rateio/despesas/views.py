from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.http import JsonResponse
from .forms import DespesaForm
from .models import (
    Despesa, Unidade, Rateio, TipoDespesa,
    LeituraGas, LeituraAgua, FracaoPorTipoDespesa, LeituraEnergia
)
from django.db import transactiona
from django.db.models.signals import post_delete
from .signals import recalc_fundo_reserva
from datetime import datetime
import json
from decimal import Decimal, ROUND_HALF_UP
from django.db.models import Q, Sum
from django.views.decorators.csrf import csrf_exempt

MESES_CHOICES = [
    (1,  'Janeiro'),
    (2,  'Fevereiro'),
    (3,  'Março'),
    (4,  'Abril'),
    (5,  'Maio'),
    (6,  'Junho'),
    (7,  'Julho'),
    (8,  'Agosto'),
    (9,  'Setembro'),
    (10, 'Outubro'),
    (11, 'Novembro'),
    (12, 'Dezembro'),
]

ANOS_DISPONIVEIS = [2025, 2026, 2027]

def lista_despesas(request):
    current_sort = request.GET.get('sort', 'recentes')
    qs = Despesa.objects.exclude(tipo__nome__iexact='Fundo de Reserva')

    # filtros de tipo / mês / ano
    tipo = request.GET.get('tipo')
    mes  = request.GET.get('mes')
    ano  = request.GET.get('ano')

    if tipo:
        qs = qs.filter(tipo_id=tipo)
    if mes:
        qs = qs.filter(mes=mes)
    if ano:
        qs = qs.filter(ano=ano)

    # ordenação
    if current_sort == 'alpha':
        qs = qs.order_by('tipo__nome', 'ano', 'mes')
    else:
        qs = qs.order_by('-id')

    despesas = (
        qs
        .filter(valor_total__gt=0)
    )

    # AQUI: para **todos** os objetos, usamos valor_total como valor_exibido.
    for d in despesas:
        if d.tipo and d.tipo.nome.lower() == 'energia áreas comuns':
            # 1) pegar a fatura e o custo_kwh da despesa "Energia Salão"
            energia = (
                Despesa.objects
                .filter(
                    mes=str(int(d.mes)),
                    ano=d.ano,
                    tipo__nome__iexact='Energia Salão'
                )
                .order_by('-id')
                .first()
            )

            if energia and energia.energia_leituras:
                params = energia.energia_leituras.get('params', {})
                raw_fatura = params.get('fatura', 0)
                raw_custo = params.get('custo_kwh', 0)
            else:
                raw_fatura = 0
                raw_custo = 0

            try:
                fatura = Decimal(str(raw_fatura))
            except Exception:
                fatura = Decimal('0')
            try:
                custo = Decimal(str(raw_custo))
            except Exception:
                custo = Decimal('0')

            # 2) total de kWh consumidos no mês
            try:
                mes_int = int(d.mes)
                ano_int = int(d.ano)
                agregado = (
                    LeituraEnergia.objects
                    .filter(mes=mes_int, ano=ano_int)
                    .aggregate(total=Sum('leitura'))
                )
                total_kwh = agregado.get('total') or Decimal('0')
            except Exception:
                total_kwh = Decimal('0')

            # 3) cálculo final
            d.valor_exibido = (
                fatura - (custo * total_kwh)
            ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            d.valor_exibido = d.valor_total

    anos_distintos = despesas.values_list('ano', flat=True).distinct()
    anos_distintos = sorted(int(a) for a in anos_distintos)

    meses_distintos = despesas.values_list('mes', flat=True).distinct()
    meses_distintos = sorted(int(m) for m in meses_distintos)

    tipos_distintos = TipoDespesa.objects.filter(
        id__in=despesas.values_list('tipo_id', flat=True).distinct()
    )

    return render(request, 'despesas/lista_despesas.html', {
        'despesas':         despesas,
        'MESES_CHOICES':    MESES_CHOICES,
        'MESES_DISTINTOS':  meses_distintos,
        'ANOS_DISPONIVEIS': ANOS_DISPONIVEIS,
        'ANOS_DISTINTOS':   anos_distintos,
        'tipos_distintos':  tipos_distintos,
        'current_sort':     current_sort,
    })


def nova_despesa(request):
    tipos = TipoDespesa.objects.exclude(nome__iexact='Fundo de Reserva')
    leituras_anteriores = {}
    leituras_agua_anteriores = {}
    form = DespesaForm()
    form.fields['tipo'].queryset = tipos
    unidades = Unidade.objects.order_by('nome')

    # iniciais (gás e água)
    recarga_initial = kg_initial = m3kg_initial = valor_m3_initial = 0
    fatura_initial = m3total_initial = agua_valor_m3_initial = 0

    # --- GET: tipo, mês e ano ---
    mes_str     = request.GET.get('mes')
    ano_str     = request.GET.get('ano')
    tipo_id_str = request.GET.get('tipo')
    mes = int(mes_str) if mes_str and mes_str.isdigit() else datetime.now().month
    ano = int(ano_str) if ano_str and ano_str.isdigit() else datetime.now().year

    # pega o objeto TipoDespesa
    if tipo_id_str and tipo_id_str.isdigit():
        tipo = TipoDespesa.objects.filter(id=int(tipo_id_str)).first() or TipoDespesa.objects.first()
    else:
        tipo = TipoDespesa.objects.first()
    if not tipo:
        messages.error(request, "Nenhum TipoDespesa cadastrado.")
        return redirect("lista_despesas")
    tipo_id = tipo.id

    # mapeia frações
    fracoes_map = {
        f.unidade.id: float(f.percentual)
        for f in FracaoPorTipoDespesa.objects.filter(tipo_despesa=tipo)
    }

    # calcula mês/ano anterior
    if mes > 1:
        mes_ant, ano_ant = mes - 1, ano
    else:
        mes_ant, ano_ant = 12, ano - 1

    # buscar parâmetros de energia do mês anterior
    fatura_energy_initial = kwh_initial = custo_kwh_initial = 0
    ultima_energia = Despesa.objects.filter(
        tipo__nome__iexact="energia salão",
        mes=str(mes_ant),
        ano=ano_ant
    ).order_by('-id').first()
    if ultima_energia and ultima_energia.energia_leituras:
        params = ultima_energia.energia_leituras.get('params', {})
        fatura_energy_initial  = params.get('fatura', 0)
        kwh_initial             = params.get('kwh_total', 1)
        custo_kwh_initial       = params.get('custo_kwh', 0)

    fatura_eletrica = (
        Despesa.objects
        .filter(
            tipo__nome__iexact='Fatura Energia Elétrica',
            mes=str(mes),
            ano=ano
        )
        .order_by('-id')
        .first()
    )
    if fatura_eletrica:
        try:
            fatura_energy_initial = float(fatura_eletrica.valor_total)
        except Exception:
            fatura_energy_initial = 0
    else:
        fatura_energy_initial = 0

    # parâmetros de GÁS do mês anterior
    ultima_gas = Despesa.objects.filter(
        tipo=tipo, mes=str(mes_ant), ano=ano_ant
    ).order_by('-id').first()
    if ultima_gas and ultima_gas.gas_leituras and 'params' in ultima_gas.gas_leituras:
        params = ultima_gas.gas_leituras['params']
        recarga_initial   = params.get('recarga', 0)
        kg_initial        = params.get('kg',      1)
        m3kg_initial      = params.get('m3_kg',   1)
        valor_m3_initial  = params.get('valor_m3',0)

    # parâmetros de ÁGUA do mês anterior
    ultima_agua = Despesa.objects.filter(
        tipo=tipo, mes=str(mes_ant), ano=ano_ant
    ).order_by('-id').first()
    if ultima_agua and ultima_agua.agua_leituras and 'params' in ultima_agua.agua_leituras:
        params = ultima_agua.agua_leituras['params']
        fatura_initial        = params.get('fatura',   fatura_initial)
        m3total_initial       = params.get('m3_total', m3total_initial)
        agua_valor_m3_initial = params.get('valor_m3', agua_valor_m3_initial)

    # leituras anteriores de GÁS
    leituras_anteriores = {}
    for u in unidades:
        lec = LeituraGas.objects.filter(
            unidade=u, mes=mes_ant, ano=ano_ant
        ).first()
        leituras_anteriores[u.id] = float(lec.leitura) if lec else 0

    # 2) popula leituras anteriores de ÁGUA
    leituras_agua_anteriores = {}
    for u in unidades:
        lac = LeituraAgua.objects.filter(
            unidade=u, mes=mes_ant, ano=ano_ant
        ).first()
        leituras_agua_anteriores[u.id] = float(lac.leitura) if lac else 0

    # 3) popula leituras anteriores de ENERGIA (medidor 1 e 2)
    leituras_anteriores_energia1 = {}
    leituras_anteriores_energia2 = {}
    for u in unidades:
        lec1 = LeituraEnergia.objects.filter(
            unidade=u, mes=mes_ant, ano=ano_ant, medidor=1
        ).first()
        lec2 = LeituraEnergia.objects.filter(
            unidade=u, mes=mes_ant, ano=ano_ant, medidor=2
        ).first()
        leituras_anteriores_energia1[u.id] = float(lec1.leitura) if lec1 else 0
        leituras_anteriores_energia2[u.id] = float(lec2.leitura) if lec2 else 0

    uso_kwh_initial = {
        uid: leituras_anteriores_energia2.get(uid, 0)
             - leituras_anteriores_energia1.get(uid, 0)
        for uid in leituras_anteriores_energia1
    }

    tipo_obj = None
    if tipo_id:
        try:
            tipo_obj = TipoDespesa.objects.get(pk=tipo_id)
        except TipoDespesa.DoesNotExist:
            tipo_obj = None

    # se for GET, renderiza o form com todos os iniciais
    if request.method != 'POST':
        return render(request, 'despesas/nova_despesa.html', {
            'form':                          form,
            'unidades':                     unidades,
            'leituras_anteriores':          leituras_anteriores,
            'leituras_agua_anteriores':     leituras_agua_anteriores,
            'recarga_initial':              recarga_initial,
            'kg_initial':                   kg_initial,
            'm3kg_initial':                 m3kg_initial,
            'valor_m3_initial':             valor_m3_initial,
            'agua_fatura_initial':          fatura_initial,
            'agua_m3_total_initial':        m3total_initial,
            'agua_valor_m3_initial':        agua_valor_m3_initial,
            'leituras_energia_anteriores_med1': leituras_anteriores_energia1,
            'leituras_energia_anteriores_med2': leituras_anteriores_energia2,
            'energia_fatura_initial':       fatura_energy_initial,
            'energia_kwh_total_initial':    kwh_initial,
            'energia_custo_kwh_initial':    custo_kwh_initial,
            'energia_uso_kwh_initial':      uso_kwh_initial,
            'mes':                           mes,
            'ano':                           ano,
            'tipo_obj':                     tipo_obj,
            'tipo_id':                       tipo_id,
            'tipo':                          tipo,
            'fracoes_map':                   fracoes_map,
        })

    # === processamento do POST ===
    form = DespesaForm(request.POST)
    tipo = get_object_or_404(TipoDespesa, id=int(request.POST.get('tipo', tipo_id)))

    if form.is_valid():
        despesa = form.save(commit=False)
        despesa.tipo = tipo

        if tipo.nome.lower() == 'água':
            antigas = Despesa.objects.filter(
                tipo__nome__iexact='água',
                mes=despesa.mes,
                ano=despesa.ano,
            )
            if antigas.exists():
                Rateio.objects.filter(despesa__in=antigas).delete()
                antigas.delete()
            antigas_leituras = LeituraAgua.objects.filter(
                mes=int(despesa.mes), ano=despesa.ano
            )
            leituras_atual_existentes = {
                l.unidade_id: float(l.leitura) for l in antigas_leituras
            }
            antigas_leituras.delete()

        total = 0
        despesa.descricao = request.POST.get('descricao_unico', '').strip()
        valores_por_unidade = {}
        consumos_por_unidade = {}

        def parse_float(v, default=0):
            try:
                return float(str(v).replace(',', '.'))
            except:
                return default

        # === MATERIAL/SERVIÇO DE CONSUMO ===
        nf_entries = []
        if tipo.nome.lower() in ["material/serviço de consumo", "reparos/reforma"]:
            idx = 0
            while True:
                key = f'nf_valor_{idx}'
                if key not in request.POST:
                    break
                val = parse_float(request.POST.get(key))
                forn = request.POST.get(f'nf_fornecedor_{idx}', '').strip()
                hist = request.POST.get(f'nf_historico_{idx}', '').strip()
                num  = request.POST.get(f'nf_numero_{idx}', '').strip()
                tipo_nf = request.POST.get(f'nf_tipo_{idx}', 'com')
                if forn or hist or num or val:
                    nf_entries.append({
                        'fornecedor': forn,
                        'historico': hist,
                        'numero': num,
                        'tipo': tipo_nf,
                        'valor': val,
                    })
                idx += 1

            nf_com = [e for e in nf_entries if (e.get('tipo') or 'com') != 'sem']
            nf_sem = [e for e in nf_entries if (e.get('tipo') or 'com') == 'sem']
            total_com = sum(e['valor'] for e in nf_com)
            total_sem = sum(e['valor'] for e in nf_sem)

            despesa.nf_info = nf_com
            despesa.valor_total = total_com
            despesa.save()

            if tipo.nome.lower() == 'material/serviço de consumo':
                tipo_sem_nome = 'Material Consumo (Sem Sala Comercial)'
            else:
                tipo_sem_nome = 'Reparo/Reforma (Sem a Sala)'

            tipo_sem = TipoDespesa.objects.filter(
                nome__iexact=tipo_sem_nome
            ).first()
            despesa_sem = None
            if tipo_sem:
                despesa_sem, _ = Despesa.objects.update_or_create(
                    tipo=tipo_sem,
                    mes=despesa.mes,
                    ano=despesa.ano,
                    defaults={
                        'descricao': despesa.descricao,
                        'valor_total': total_sem,
                    }
                )
                despesa_sem.nf_info = nf_sem
                despesa_sem.valor_total = total_sem
                despesa_sem.save()

            fracoes_sem_map = {
                f.unidade.id: float(f.percentual)
                for f in FracaoPorTipoDespesa.objects.filter(tipo_despesa=tipo_sem)
            } if tipo_sem else {}

            valores_com = {}
            valores_sem = {}

            if fracoes_map:
                for u in unidades:
                    pct_com = fracoes_map.get(u.id, 0)
                    pct_sem = fracoes_sem_map.get(u.id, 0)
                    valores_com[u] = total_com * pct_com
                    valores_sem[u] = total_sem * pct_sem
            else:
                share_com = total_com / len(unidades) if unidades else 0
                share_sem = total_sem / len(unidades) if unidades else 0

                for u in unidades:
                    valores_com[u] = share_com
                    valores_sem[u] = share_sem

            Rateio.objects.filter(despesa=despesa).delete()
            if despesa_sem:
                Rateio.objects.filter(despesa=despesa_sem).delete()

            for u in unidades:
                Rateio.objects.create(despesa=despesa, unidade=u, valor=valores_com.get(u, 0))
                if despesa_sem:
                    Rateio.objects.create(despesa=despesa_sem, unidade=u, valor=valores_sem.get(u, 0))
            messages.success(request, 'Despesa cadastrada com sucesso!')
            return redirect('lista_despesas')

        # === GÁS ===
        if tipo.nome.lower() == "gás":
            recarga = parse_float(request.POST.get('recarga'))
            kg      = parse_float(request.POST.get('kg'), 1)
            m3_kg   = parse_float(request.POST.get('m3_kg'), 1)
            preco   = parse_float(request.POST.get('valor_m3'))

            LeituraGas.objects.filter(
                mes=int(despesa.mes), ano=despesa.ano
            ).delete()

            for u in unidades:
                raw = request.POST.get(f'atual_{u.id}', '').strip()
                if raw:
                    atual = parse_float(raw)
                    ant   = leituras_anteriores.get(u.id, 0)
                    c = max(atual - ant, 0)
                    LeituraGas.objects.update_or_create(
                        unidade=u, mes=int(despesa.mes), ano=despesa.ano,
                        defaults={'leitura': atual}
                    )
                else:
                    c = 0

                v = c * preco
                valores_por_unidade[u]     = v
                consumos_por_unidade[u.id] = c
                total += v

            despesa.gas_leituras = {
                'params': {
                    'recarga':   recarga,
                    'kg':        kg,
                    'm3_kg':     m3_kg,
                    'valor_m3':  preco,
                },
                'leituras': leituras_anteriores,
            }

        # === ÁGUA ===
        elif tipo.nome.lower() == "água":

            fatura   = parse_float(request.POST.get('agua_fatura'))
            m3_total = parse_float(request.POST.get('agua_m3_total'), 1)
            valor_m3 = (fatura / m3_total) if m3_total else 0
            for u in unidades:
                raw = request.POST.get(f'agua_atual_{u.id}', '').strip()
                if raw == "":
                    atual = parse_float(leituras_atual_existentes.get(u.id, 0))
                else:
                    atual = parse_float(raw)
                ant   = leituras_agua_anteriores.get(u.id, 0)
                c     = max(atual - ant, 0)
                v     = c * valor_m3
                valores_por_unidade[u]     = v
                consumos_por_unidade[u.id] = c
                total += v
                LeituraAgua.objects.update_or_create(
                    unidade=u, mes=int(despesa.mes), ano=despesa.ano,
                    defaults={'leitura': atual}
                )
            despesa.agua_leituras = {
                'params': {
                    'fatura':    fatura,
                    'm3_total':  m3_total,
                    'valor_m3':  valor_m3,
                },
                'leituras': leituras_agua_anteriores,
            }

        # === FUNDO DE RESERVA ===
        elif tipo.nome.lower() == "fundo de reserva":
            base_tipos = [
                'Reparos/Reforma', 'Salário - síndico', 'Elevador',
                'Material Consumo Sem Sala Comercial', 'Material/Serviço de Consumo',
                'Seguro 6x', 'Energia Áreas Comuns', 'Taxa Lixo',
                'Água', 'Honorários Contábeis'
            ]
            soma = Despesa.objects.filter(
                tipo__nome__in=base_tipos,
                mes=despesa.mes,
                ano=despesa.ano
            ).aggregate(total=Sum('valor_total'))['total'] or Decimal('0')
            valor_fundo = soma * Decimal('0.1')

            sala = Unidade.objects.get(nome__icontains='Sala Comercial')
            pct_sala = Decimal(fracoes_map.get(sala.id, 0))
            if pct_sala > 1:
                pct_sala /= Decimal('100')
            share_sala = (valor_fundo * pct_sala) / Decimal('2')

            valores_por_unidade = { sala: share_sala }
            restante = valor_fundo - share_sala
            for unid, pct in fracoes_map.items():
                if unid == sala.id:
                    continue
                pct_dec = Decimal(pct)
                if pct_dec > 1:
                    pct_dec /= Decimal('100')
                unidade = Unidade.objects.get(id=unid)
                valores_por_unidade[unidade] = restante * pct_dec

            despesa.valor_total = valor_fundo
            despesa.save()
            for un, val in valores_por_unidade.items():
                Rateio.objects.create(despesa=despesa, unidade=un, valor=val)

            return redirect('lista_despesas')

        # === ENERGIA SALÃO ===
        elif tipo.nome.lower() == "energia salão":
            fatura    = parse_float(request.POST.get('energia_fatura'))
            kwh_total = parse_float(request.POST.get('energia_kwh_total'), 1)
            custo_kwh = parse_float(request.POST.get('energia_custo_kwh'))
            uso_kwh   = parse_float(request.POST.get('energia_uso_kwh'))

            LeituraEnergia.objects.filter(
                mes=int(despesa.mes), ano=despesa.ano
            ).delete()

            valores_por_unidade = {}
            for u in unidades:
                raw1 = request.POST.get(f'energia_atual1_{u.id}', '').strip()
                raw2 = request.POST.get(f'energia_atual2_{u.id}', '').strip()
                cons = 0

                if raw1:
                    cur1 = parse_float(raw1)
                    ant1 = leituras_anteriores_energia1[u.id]
                    cons += cur1 - ant1
                    LeituraEnergia.objects.update_or_create(
                        unidade=u, mes=int(despesa.mes), ano=despesa.ano, medidor=1,
                        defaults={'leitura': cur1}
                    )

                if raw2:
                    cur2 = parse_float(raw2)
                    ant2 = leituras_anteriores_energia2[u.id]
                    cons += cur2 - ant2
                    LeituraEnergia.objects.update_or_create(
                        unidade=u, mes=int(despesa.mes), ano=despesa.ano, medidor=2,
                        defaults={'leitura': cur2}
                    )

                cons = max(cons, 0)
                val      = cons * uso_kwh
                valores_por_unidade[u] = val

            despesa.energia_leituras = {
                'params': {
                    'fatura':    fatura,
                    'kwh_total': kwh_total,
                    'custo_kwh': custo_kwh,
                    'uso_kwh':    uso_kwh,
                },
                'leituras': {
                    'anteriores1': leituras_anteriores_energia1,
                    'anteriores2': leituras_anteriores_energia2,
                }
            }
            despesa.valor_total = sum(valores_por_unidade.values())
            despesa.save()
            for u, v in valores_por_unidade.items():
                if v > 0:
                    Rateio.objects.create(despesa=despesa, unidade=u, valor=v)

            return redirect('lista_despesas')

        # --- ENERGIA ÁREAS COMUNS (único bloco) ---
        elif tipo.nome.lower() == "energia áreas comuns":
            # pega o valor exato que o usuário digitou no form (ou no Admin)
            despesa.valor_total = parse_float(request.POST.get('valor_unico', 0))
            despesa.save()

            # busca frações e cria rateio usando esse valor exato
            fracoes_qs = FracaoPorTipoDespesa.objects.filter(tipo_despesa=despesa.tipo)
            pct_map = {
                f.unidade.id: Decimal(str(f.percentual))
                for f in fracoes_qs
            }

            sala = Unidade.objects.get(nome__icontains="Sala")
            pct_sala = pct_map.get(sala.id, Decimal('0'))
            if pct_sala > 1:
                pct_sala = pct_sala / Decimal('100')

            # metade da cota da sala
            sala_share = (despesa.valor_total * pct_sala / Decimal('2')).quantize(
                Decimal('0.01'),
                rounding=ROUND_HALF_UP
            )
            restante = despesa.valor_total - sala_share

            Rateio.objects.create(despesa=despesa, unidade=sala, valor=sala_share)
            for f in fracoes_qs:
                if f.unidade.id == sala.id:
                    continue
                pct = Decimal(str(f.percentual))
                if pct > 1:
                    pct = pct / Decimal('100')
                share = (restante * pct).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                Rateio.objects.create(despesa=despesa, unidade=f.unidade, valor=share)

            return redirect('lista_despesas')

        # === FATURA ENERGIA ELÉTRICA ===
        elif tipo.nome.lower() == "fatura energia elétrica":
            despesa.valor_total = parse_float(request.POST.get('valor_unico', 0))
            despesa.save()
            return redirect('lista_despesas')

        # === TAXA BOLETO ===
        elif tipo.nome.lower() == "taxa boleto":
            valor_boleto = parse_float(request.POST.get('valor_unico'))
            for u in unidades:
                valores_por_unidade[u] = valor_boleto
                total += valor_boleto

        # === FRAÇÃO (por tipo de despesa) ===
        elif fracoes_map:
            valor_unico = parse_float(request.POST.get('valor_unico'))
            for u in unidades:
                pct = fracoes_map.get(u.id, 0)
                v   = valor_unico * pct
                valores_por_unidade[u] = v
                total += v

        # === PADRÃO ===
        else:
            for u in unidades:
                v = parse_float(request.POST.get(f'valor_{u.id}'))
                valores_por_unidade[u] = v
                total += v

        despesa.valor_total = total
        despesa.save()
        for u, v in valores_por_unidade.items():
            Rateio.objects.create(despesa=despesa, unidade=u, valor=v)

        messages.success(request, 'Despesa cadastrada com sucesso!')
        return redirect('lista_despesas')

    return render(request, 'despesas/nova_despesa.html', {
        'form':                         form,
        'unidades':                    unidades,
        'leituras_anteriores':         leituras_anteriores,
        'leituras_agua_anteriores':    leituras_agua_anteriores,
        'recarga_initial':             recarga_initial,
        'kg_initial':                  kg_initial,
        'm3kg_initial':                m3kg_initial,
        'valor_m3_initial':            valor_m3_initial,
        'agua_fatura_initial':         fatura_initial,
        'agua_m3_total_initial':       m3total_initial,
        'agua_valor_m3_initial':       agua_valor_m3_initial,
        'fatura_energy_initial':       fatura_energy_initial,
        'kwh_initial':                 kwh_initial,
        'custo_kwh_initial':           custo_kwh_initial,
        'leituras_energia_anteriores_med1': leituras_anteriores_energia1,
        'leituras_energia_anteriores_med2': leituras_anteriores_energia2,
        'energia_fatura_initial':      fatura_energy_initial,
        'energia_kwh_total_initial':   kwh_initial,
        'energia_custo_kwh_initial':   custo_kwh_initial,
        'energia_uso_kwh_initial':     uso_kwh_initial,
        'mes':                          mes,
        'ano':                          ano,
        'tipo_id':                      tipo_id,
        'tipo':                         tipo,
        'fracoes_map':                  fracoes_map,
    })

@csrf_exempt
def limpar_rateio(request, despesa_id):
    if request.method == 'POST':
        try:
            desp = Despesa.objects.get(id=despesa_id)
            Rateio.objects.filter(despesa=desp).delete()
            desp.valor_total = 0
            desp.save()
            return JsonResponse({'success': True})
        except Despesa.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'Despesa não encontrada'})
    return JsonResponse({'success': False, 'error': 'Método não permitido'})

@csrf_exempt
def editar_rateio(request, rateio_id):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            novo_valor = float(data.get('valor', 0))
            rateio = Rateio.objects.get(id=rateio_id)
            rateio.valor = novo_valor
            rateio.save()
            total = Rateio.objects.filter(despesa=rateio.despesa).aggregate(Sum('valor'))['valor__sum'] or 0
            rateio.despesa.valor_total = total
            rateio.despesa.save()
            return JsonResponse({'success': True, 'novo_total': round(total, 2)})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})
    return JsonResponse({'success': False, 'error': 'Método não permitido'})

@csrf_exempt
def excluir_despesa(request, despesa_id):
    if request.method == 'POST':
        try:
            desp = Despesa.objects.get(id=despesa_id)
            if desp.tipo.nome.lower() == "água":
                LeituraAgua.objects.filter(
                    mes=int(desp.mes), ano=desp.ano
                ).delete()
            desp.delete()
            return JsonResponse({'success': True})
        except Despesa.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'Despesa não encontrada'})
    return JsonResponse({'success': False, 'error': 'Método não permitido'})

def editar_despesa(request, despesa_id):
    despesa = get_object_or_404(Despesa, id=despesa_id)

    tipo_nome = despesa.tipo.nome.lower()
    if tipo_nome not in ['material/serviço de consumo', 'reparos/reforma']:
        messages.error(
            request,
            'Despesa não é do tipo Material/Serviço de Consumo ou Reparos/Reforma.'
        )
        return redirect('lista_despesas')

    unidades = Unidade.objects.order_by('nome')
    fracoes_map = {
        f.unidade.id: float(f.percentual)
        for f in FracaoPorTipoDespesa.objects.filter(tipo_despesa=despesa.tipo)
    }

    def parse_float(v, default=0):
        try:
            return float(str(v).replace(',', '.'))
        except Exception:
            return default

    if request.method == 'POST':
        nf_entries = []
        idx = 0
        while True:
            key = f'nf_valor_{idx}'
            if key not in request.POST:
                break
            val = parse_float(request.POST.get(key))
            forn = request.POST.get(f'nf_fornecedor_{idx}', '').strip()
            hist = request.POST.get(f'nf_historico_{idx}', '').strip()
            num  = request.POST.get(f'nf_numero_{idx}', '').strip()
            tipo_nf = request.POST.get(f'nf_tipo_{idx}', 'com')
            if forn or hist or num or val:
                nf_entries.append({
                    'fornecedor': forn,
                    'historico': hist,
                    'numero': num,
                    'tipo': tipo_nf,
                    'valor': val,
                })
            idx += 1

        nf_com = [e for e in nf_entries if (e.get('tipo') or 'com') != 'sem']
        nf_sem = [e for e in nf_entries if (e.get('tipo') or 'com') == 'sem']
        total_com = sum(e['valor'] for e in nf_com)
        total_sem = sum(e['valor'] for e in nf_sem)

        despesa.nf_info = nf_com
        despesa.valor_total = total_com
        despesa.save()

        if tipo_nome == 'reparos/reforma':
            sem_nome = 'Reparo/Reforma (Sem a Sala)'
        else:
            sem_nome = 'Material Consumo (Sem Sala Comercial)'

        tipo_sem = TipoDespesa.objects.filter(
            nome__iexact=sem_nome
        ).first()
        despesa_sem = None
        if tipo_sem:
            despesa_sem, _ = Despesa.objects.update_or_create(
                tipo=tipo_sem,
                mes=despesa.mes,
                ano=despesa.ano,
                defaults={
                    'descricao': despesa.descricao,
                    'valor_total': total_sem,
                }
            )
            despesa_sem.nf_info = nf_sem
            despesa_sem.valor_total = total_sem
            despesa_sem.save()

        fracoes_sem_map = {
            f.unidade.id: float(f.percentual)
            for f in FracaoPorTipoDespesa.objects.filter(tipo_despesa=tipo_sem)
        } if tipo_sem else {}

        valores_com = {}
        valores_sem = {}
        if fracoes_map:
            for u in unidades:
                pct_com = fracoes_map.get(u.id, 0)
                pct_sem = fracoes_sem_map.get(u.id, 0)
                valores_com[u] = total_com * pct_com
                valores_sem[u] = total_sem * pct_sem
        else:
            share_com = total_com / len(unidades) if unidades else 0
            share_sem = total_sem / len(unidades) if unidades else 0
            for u in unidades:
                valores_com[u] = share_com
                valores_sem[u] = share_sem

        Rateio.objects.filter(despesa=despesa).delete()
        if despesa_sem:
            Rateio.objects.filter(despesa=despesa_sem).delete()

        for u in unidades:
            Rateio.objects.create(despesa=despesa, unidade=u, valor=valores_com.get(u, 0))
            if despesa_sem:
                Rateio.objects.create(despesa=despesa_sem, unidade=u, valor=valores_sem.get(u, 0))

        messages.success(request, 'Despesa atualizada com sucesso!')
        return redirect('lista_despesas')

    if tipo_nome == 'reparos/reforma':
        sem_nome = 'Reparo/Reforma (Sem a Sala)'
    else:
        sem_nome = 'Material Consumo (Sem Sala Comercial)'

    tipo_sem = TipoDespesa.objects.filter(
        nome__iexact=sem_nome
    ).first()
    nf_info_sem = []
    if tipo_sem:
        despesa_sem = Despesa.objects.filter(
            tipo=tipo_sem,
            mes=despesa.mes,
            ano=despesa.ano
        ).first()
        if despesa_sem and despesa_sem.nf_info:
            nf_info_sem = despesa_sem.nf_info

    nf_info_total = (despesa.nf_info or []) + nf_info_sem

    return render(request, 'despesas/editar_despesa.html', {
        'despesa': despesa,
        'nf_info': nf_info_total,
    })

def ver_rateio(request, despesa_id):
    despesa = get_object_or_404(Despesa, id=despesa_id)
    valor_exibido = despesa.valor_total
    rateios = Rateio.objects.filter(despesa=despesa)
    total_rateio = rateios.aggregate(total=Sum('valor'))['total'] or 0

    valor_com_sala = Decimal('0')
    valor_sem_sala = Decimal('0')
    rateio_com_sala = {}
    rateio_sem_sala = {}

    pairings = [
        (
            'Material/Serviço de Consumo',
            'Material Consumo (Sem Sala Comercial)'
        ),
        (
            'Reparos/Reforma',
            'Reparo/Reforma (Sem a Sala)'
        ),
    ]

    pair_com = pair_sem = None
    for com_name, sem_name in pairings:
        if despesa.tipo.nome.lower() in [com_name.lower(), sem_name.lower()]:
            pair_com, pair_sem = com_name, sem_name
            break

    if pair_com:
        tipo_com = TipoDespesa.objects.filter(
            nome__iexact=pair_com
        ).first()
        tipo_sem = TipoDespesa.objects.filter(
            nome__iexact=pair_sem
        ).first()

        despesa_com = despesa if despesa.tipo == tipo_com else None
        despesa_sem = despesa if despesa.tipo == tipo_sem else None
        if not despesa_com and tipo_com:
            despesa_com = Despesa.objects.filter(
                tipo=tipo_com, mes=despesa.mes, ano=despesa.ano
            ).first()
        if not despesa_sem and tipo_sem:
            despesa_sem = Despesa.objects.filter(
                tipo=tipo_sem, mes=despesa.mes, ano=despesa.ano
            ).first()

        rateios_com = Rateio.objects.filter(despesa=despesa_com) if despesa_com else []
        rateios_sem = Rateio.objects.filter(despesa=despesa_sem) if despesa_sem else []

        rateios = list(rateios_com) if rateios_com else list(rateios_sem)
        valor_com_sala = despesa_com.valor_total if despesa_com else Decimal('0')
        valor_sem_sala = despesa_sem.valor_total if despesa_sem else Decimal('0')

        valor_exibido = valor_com_sala + valor_sem_sala

        rateio_com_sala = {r.id: r.valor for r in rateios_com}
        sem_map = {r.unidade_id: r.valor for r in rateios_sem}

        for r in rateios:
            rateio_sem_sala[r.id] = sem_map.get(r.unidade_id, Decimal('0'))
        return render(request, 'despesas/ver_rateio.html', {
            'despesa':           despesa,
            'rateios':           rateios,
            'valor_com_sala':    float(valor_com_sala),
            'valor_sem_sala':    float(valor_sem_sala),
            'rateio_com_sala':   { k: float(v) for k,v in rateio_com_sala.items() },
            'rateio_sem_sala':   { k: float(v) for k,v in rateio_sem_sala.items() },
        })

    if despesa.tipo.nome.lower() == 'energia áreas comuns':
        # 1) pega fatura e custo_kwh da última “Energia Salão” desse mês/ano
        energia = (
            Despesa.objects
            .filter(
                mes=str(int(despesa.mes)),
                ano=despesa.ano,
                tipo__nome__iexact='Energia Salão'
            )
            .order_by('-id')
            .first()
        )

        if energia and energia.energia_leituras:
            params = energia.energia_leituras.get('params', {})
            raw_fatura = params.get('fatura', 0)
            raw_custo  = params.get('custo_kwh', 0)
        else:
            raw_fatura = 0
            raw_custo  = 0

        try:
            fatura = Decimal(str(raw_fatura))
        except Exception:
            fatura = Decimal('0')
        try:
            custo = Decimal(str(raw_custo))
        except Exception:
            custo = Decimal('0')

        # 2) soma todas as leituras (kWh) do mês/ano em LeituraEnergia
        try:
            mes_int = int(despesa.mes)
            ano_int = int(despesa.ano)
            agregado = (
                LeituraEnergia.objects
                .filter(mes=mes_int, ano=ano_int)
                .aggregate(total=Sum('leitura'))
            )
            total_kwh = agregado.get('total') or Decimal('0')
        except Exception:
            total_kwh = Decimal('0')

        # 3) calcula o valor que será rateado:
        valor_exibido = (
            fatura - (custo * total_kwh)
        ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        # 4) busca todas as frações cadastradas para “Energia Áreas Comuns”
        fracoes_qs = FracaoPorTipoDespesa.objects.filter(
            tipo_despesa=despesa.tipo
        )

        # 5) cria um dicionário { unidade.id: percentual_decimal }
        #    assumindo que o campo “percentual” está em formato 0.xx ou xx (%, que dividiremos por 100 abaixo)
        pct_map = {
            f.unidade.id: Decimal(str(f.percentual))
            for f in fracoes_qs
        }

        # 6) identifica a Sala (unidade cujo nome contenha “Sala”)
        try:
            sala = Unidade.objects.get(nome__icontains='Sala')
            pct_sala = pct_map.get(sala.id, Decimal('0'))
        except Unidade.DoesNotExist:
            sala = None
            pct_sala = Decimal('0')

        # Se o percentual estiver armazenado como “26.40” em vez de “0.2640”, converta:
        if pct_sala > 1:
            pct_sala = pct_sala / Decimal('100')

        # 7) faz a metade da cota da Sala
        sala_share = (valor_exibido * pct_sala / Decimal('2')).quantize(
            Decimal('0.01'),
            rounding=ROUND_HALF_UP
        )

        # 8) o que resta para distribuir entre os demais
        restante = (valor_exibido - sala_share).quantize(
            Decimal('0.01'),
            rounding=ROUND_HALF_UP
        )

        # 9) monta a lista de resultados
        fracoes_valores = []

        # 9a) primeiro adiciona a Sala com “metade da fração”
        if sala:
            fracoes_valores.append({
                'unidade': sala,
                'valor': float(sala_share),
            })

        # 9b) para cada outra unidade (que não seja a Sala), dá o share proporcional
        for f in fracoes_qs:
            u = f.unidade
            if sala and u.id == sala.id:
                continue

            pct_i = Decimal(str(f.percentual))
            if pct_i > 1:
                pct_i = pct_i / Decimal('100')

            share_i = (restante * pct_i).quantize(
                Decimal('0.01'),
                rounding=ROUND_HALF_UP
            )
            fracoes_valores.append({
                'unidade': u,
                'valor': float(share_i),
            })

        # 10) renderiza usando “fracoes_valores” em vez dos Rateio já gravados
        return render(request, 'despesas/ver_rateio.html', {
            'despesa':         despesa,
            'fracoes_valores': fracoes_valores,
            'valor_exibido':   valor_exibido,
        })

    # --- GÁS ---
    if despesa.tipo.nome.lower() == "gás":
        gas_leituras = getattr(despesa, 'gas_leituras', {}) or {}
        gas_params   = gas_leituras.get('params', {})
        gas_info     = {}
        mes_atual, ano_atual = int(despesa.mes), despesa.ano
        if mes_atual > 1:
            mes_ant, ano_ant = mes_atual - 1, ano_atual
        else:
            mes_ant, ano_ant = 12, ano_atual - 1
        for r in rateios:
            u   = r.unidade
            ant = LeituraGas.objects.filter(unidade=u, mes=mes_ant, ano=ano_ant).first()
            atu = LeituraGas.objects.filter(unidade=u, mes=mes_atual, ano=ano_atual).first()
            la  = float(ant.leitura) if ant else 0
            lk  = float(atu.leitura) if atu else 0
            consumo = max(lk - la, 0)
            gas_info[u.id] = {
                'leitura_anterior': la,
                'leitura_atual':    lk,
                'consumo':          consumo,
            }
        diferenca = float(despesa.valor_total) - float(despesa.recarga or 0)
        return render(request, 'despesas/ver_rateio.html', {
            'despesa':    despesa,
            'rateios':    rateios,
            'gas_params': gas_params,
            'gas_info':   gas_info,
            'diferenca':  diferenca,
            'valor_exibido':  valor_exibido,

        })

    # --- ÁGUA ---
    if despesa.tipo.nome.lower() == "água":
        agua_leituras = getattr(despesa, 'agua_leituras', {}) or {}
        agua_params   = agua_leituras.get('params', {})
        agua_info     = {}
        mes_atual, ano_atual = int(despesa.mes), despesa.ano
        if mes_atual > 1:
            mes_ant, ano_ant = mes_atual - 1, ano_atual
        else:
            mes_ant, ano_ant = 12, ano_atual - 1
        for r in rateios:
            u   = r.unidade
            ant = LeituraAgua.objects.filter(unidade=u, mes=mes_ant, ano=ano_ant).first()
            atu = LeituraAgua.objects.filter(unidade=u, mes=mes_atual, ano=ano_atual).first()
            la  = float(ant.leitura) if ant else 0
            lk  = float(atu.leitura) if atu else 0
            agua_info[u.id] = {
                'leitura_anterior': la,
                'leitura_atual':    lk,
                'consumo':          max(lk - la, 0),
            }
        return render(request, 'despesas/ver_rateio.html', {
            'despesa':    despesa,
            'rateios':    rateios,
            'agua_params': agua_params,
            'agua_info':   agua_info,
            'valor_exibido':  valor_exibido,
        })

    # --- ENERGIA SALÃO ---
    elif despesa.tipo.nome.lower() == "energia salão":
        energia_leituras = getattr(despesa, 'energia_leituras', {}) or {}
        energia_params   = energia_leituras.get('params', {})
        energia_info     = {}
        mes_atual, ano_atual = int(despesa.mes), despesa.ano
        if mes_atual > 1:
            mes_ant, ano_ant = mes_atual - 1, ano_atual
        else:
            mes_ant, ano_ant = 12, ano_atual - 1

        total_leituras = 0

        for rateio in rateios:
            u = rateio.unidade
            ant1 = LeituraEnergia.objects.filter(
                unidade=u, mes=mes_ant, ano=ano_ant, medidor=1
            ).first()
            atu1 = LeituraEnergia.objects.filter(
                unidade=u, mes=mes_atual, ano=ano_atual, medidor=1
            ).first()
            ant2 = LeituraEnergia.objects.filter(
                unidade=u, mes=mes_ant, ano=ano_ant, medidor=2
            ).first()
            atu2 = LeituraEnergia.objects.filter(
                unidade=u, mes=mes_atual, ano=ano_atual, medidor=2
            ).first()

            la1 = float(ant1.leitura) if ant1 else 0
            lk1 = float(atu1.leitura) if atu1 else 0
            la2 = float(ant2.leitura) if ant2 else 0
            lk2 = float(atu2.leitura) if atu2 else 0

            consumo = (lk1 - la1) + (lk2 - la2)
            total_leituras += consumo
            uso   = energia_params.get('uso_kwh', 0.0)
            valor   = consumo * uso

            energia_info[u.id] = {
                'unidade': u,
                'anteriores1': la1,
                'atuais1':     lk1,
                'anteriores2': la2,
                'atuais2':     lk2,
                'consumo':     consumo,
                'valor':       valor,
            }

        energia_total = sum(info['valor'] for info in energia_info.values())
        despesa.valor_total = energia_total

        return render(request, 'despesas/ver_rateio.html', {
            'despesa':        despesa,
            'rateios':        rateios,
            'total_rateio':   total_rateio,
            'energia_params': energia_params,
            'energia_total':  energia_total,
            'energia_info':   energia_info,
            'total_leituras': total_leituras,
            'valor_exibido': valor_exibido,
        })



    # --- FRAÇÃO ---
    fracoes_qs = FracaoPorTipoDespesa.objects.filter(tipo_despesa=despesa.tipo)
    if fracoes_qs.exists():
        fracoes_valores = [
            {
                'unidade': f.unidade,
                'valor': round(float(valor_exibido) * float(f.percentual), 2),
            }
            for f in fracoes_qs
        ]
        return render(request, 'despesas/ver_rateio.html', {
            'despesa':         despesa,
            'fracoes_valores': fracoes_valores,
            'valor_exibido':  valor_exibido,
            'total_rateio' : total_rateio,
        })

    # --- PADRÃO ---
    context = {
        'despesa': despesa,
        'rateios': rateios,
        'valor_exibido': valor_exibido,
    }
    if pair_com:
        context.update({
            'valor_com_sala': float(valor_com_sala),
            'valor_sem_sala': float(valor_sem_sala),
            'rateio_com_sala': {
                k: float(v) for k, v in rateio_com_sala.items()
            },
            'rateio_sem_sala': {
                k: float(v) for k, v in rateio_sem_sala.items()
            },
        })
    return render(request, 'despesas/ver_rateio.html', context)


def ajax_ultima_agua(request):
    """
    Retorna via JSON os params de água do mês anterior para o tipo/mes/ano enviados.
    """
    try:
        tipo_id = int(request.GET.get('tipo'))
        mes     = int(request.GET.get('mes'))
        ano     = int(request.GET.get('ano'))
    except (TypeError, ValueError):
        return JsonResponse({'error': 'parâmetros inválidos'}, status=400)

    if mes > 1:
        mes_ant, ano_ant = mes - 1, ano
    else:
        mes_ant, ano_ant = 12, ano - 1

    desp = Despesa.objects.filter(
        tipo_id=tipo_id,
        mes=str(mes_ant),
        ano=ano_ant
    ).order_by('-id').first()

    data = {'fatura': 0, 'm3_total': 0, 'valor_m3': 0}
    if desp and desp.agua_leituras and 'params' in desp.agua_leituras:
        data = desp.agua_leituras['params']
    return JsonResponse(data)


def limpar_tudo(request):
    # apaga todas as despesas (e cascata todos os rateios)
    post_delete.disconnect(recalc_fundo_reserva, sender=Despesa)
    try:
        with transaction.atomic():
            Despesa.objects.all().delete()
    finally:
        # Garante que os sinais voltem a estar conectados
        post_delete.connect(recalc_fundo_reserva, sender=Despesa)
    messages.success(request, "Todas as despesas foram excluídas com sucesso!")
    return redirect('lista_despesas')
