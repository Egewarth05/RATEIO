from django.db import models
from django.db.models import Sum
from decimal import Decimal
from django.db.models import JSONField

MESES_CHOICES = [
    ('1', 'Janeiro'), ('2', 'Fevereiro'), ('3', 'Março'),
    ('4', 'Abril'), ('5', 'Maio'), ('6', 'Junho'),
    ('7', 'Julho'), ('8', 'Agosto'), ('9', 'Setembro'),
    ('10', 'Outubro'), ('11', 'Novembro'), ('12', 'Dezembro'),
]

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

class Unidade(models.Model):
    nome   = models.CharField(max_length=100)
    fracao = models.DecimalField(max_digits=10, decimal_places=8, null=True, blank=True)

    def __str__(self):
        return self.nome

class LeituraGas(models.Model):
    unidade = models.ForeignKey(Unidade, on_delete=models.CASCADE)
    mes     = models.IntegerField()
    ano     = models.IntegerField()
    leitura = models.DecimalField(max_digits=10, decimal_places=4)

    class Meta:
        unique_together = ('unidade', 'mes', 'ano')

    def __str__(self):
        return f"{self.unidade.nome} - {self.mes}/{self.ano} - {self.leitura}"

class LeituraAgua(models.Model):
    unidade = models.ForeignKey(Unidade, on_delete=models.CASCADE)
    mes     = models.IntegerField()
    ano     = models.IntegerField()
    leitura = models.DecimalField(max_digits=10, decimal_places=4)

    class Meta:
        unique_together = ('unidade','mes','ano')

    def __str__(self):
        return f"{self.unidade.nome} - {self.mes}/{self.ano} - {self.leitura}"

class TipoDespesa(models.Model):
    nome = models.CharField(max_length=100)
    # adiciona este campo:
    ordem = models.PositiveIntegerField(
        default=100,
        help_text="Número para definir a posição (menor→aparece primeiro)."
    )

    def __str__(self):
        return self.nome

    class Meta:
        # aqui definimos que a ordenação padrão usará o campo 'ordem', depois 'nome'
        ordering = ['ordem', 'nome']

class FracaoPorTipoDespesa(models.Model):
    tipo_despesa = models.ForeignKey(
        TipoDespesa, on_delete=models.CASCADE, related_name='fracoes'
    )
    unidade    = models.ForeignKey(Unidade, on_delete=models.CASCADE)
    percentual = models.DecimalField(max_digits=10, decimal_places=9)

    def __str__(self):
        return f"{self.unidade.nome} — {self.percentual}%"

class Despesa(models.Model):
    tipo         = models.ForeignKey(TipoDespesa, on_delete=models.CASCADE)
    valor_total  = models.DecimalField(max_digits=10, decimal_places=2)
    mes          = models.CharField(max_length=2, choices=MESES_CHOICES)
    ano          = models.IntegerField(choices=[(y, y) for y in range(2025, 2028)])
    descricao    = models.CharField("Descrição única", max_length=350, blank=True, null=True)

    fatura_agua    = models.DecimalField(
        "R$ Fatura", max_digits=12, decimal_places=2,
        null=True, blank=True
    )
    m3_total_agua  = models.DecimalField(
        "m³ Total", max_digits=10, decimal_places=4,
        null=True, blank=True
    )
    valor_m3_agua  = models.DecimalField(
        "R$/m³ Água", max_digits=10, decimal_places=4,
        null=True, blank=True
    )

    # campos JSON
    gas_leituras = JSONField(blank=True, null=True)
    agua_leituras= JSONField("Leituras de Água", blank=True, null=True)
    energia_leituras = JSONField("Leituras de Energia", blank=True, null=True)
    nf_info = JSONField("Notas Fiscais", blank=True, null=True)

    @property
    def total_leituras(self):
        if self.tipo.nome.lower() != 'energia salão':
            return None

        try:
            mes_int = int(self.mes)
            ano_int = int(self.ano)
        except (TypeError, ValueError):
            return None

        # calcula mês/ano anterior
        if mes_int > 1:
            mes_ant, ano_ant = mes_int - 1, ano_int
        else:
            mes_ant, ano_ant = 12, ano_int - 1

        total = 0
        from .models import LeituraEnergia, Unidade

        for u in Unidade.objects.all():
            for medidor in (1, 2):
                ant = LeituraEnergia.objects.filter(
                    unidade=u, medidor=medidor,
                    mes=mes_ant, ano=ano_ant
                ).first()
                atu = LeituraEnergia.objects.filter(
                    unidade=u, medidor=medidor,
                    mes=mes_int, ano=ano_int
                ).first()
                la = float(ant.leitura) if ant else 0
                lk = float(atu.leitura) if atu else 0
                total += (lk - la)

        return round(total, 4)

    def __str__(self):
        # Exemplo: "Energia Salão – 05/2025"
        return f"{self.tipo.nome} – {self.mes}/{self.ano}"

    @property
    def energia_fatura(self):
        params = (self.energia_leituras or {}).get('params', {})
        return params.get('fatura', 0)

    @property
    def kwh_total(self):
        params = (self.energia_leituras or {}).get('params', {})
        return params.get('kwh_total', 0)

    @property
    def custo_kwh(self):
        params = (self.energia_leituras or {}).get('params', {})
        return params.get('custo_kwh', 0)

    @property
    def uso_kwh(self):
        params = (self.energia_leituras or {}).get('params', {})
        return params.get('uso_kwh', 0)

    @property
    def recarga(self):
        if self.gas_leituras and 'params' in self.gas_leituras:
            return self.gas_leituras['params'].get('recarga')
        return None

    @property
    def kg(self):
        if self.gas_leituras and 'params' in self.gas_leituras:
            return self.gas_leituras['params'].get('kg')
        return None

    @property
    def m3_kg(self):
        if self.gas_leituras and 'params' in self.gas_leituras:
            return self.gas_leituras['params'].get('m3_kg')
        return None

    @property
    def valor_m3(self):
        if self.gas_leituras and 'params' in self.gas_leituras:
            return self.gas_leituras['params'].get('valor_m3')
        return None

    @property
    def fatura(self):
        if self.agua_leituras and 'params' in self.agua_leituras:
            return (self.agua_leituras or {}).get('params', {}).get('fatura')
        return None

    @property
    def m3_total(self):
        if self.agua_leituras and 'params' in self.agua_leituras:
            return (self.agua_leituras or {}).get('params', {}).get('m3_total')
        return None

    @property
    def valor_m3_agua(self):
        if self.agua_leituras and 'params' in self.agua_leituras:
            return (self.agua_leituras or {}).get('params', {}).get('valor_m3_agua')
        return None

class DespesaComSala(Despesa):
    class Meta:
        proxy = True
        verbose_name = "Material/Serviço de Consumo (Com Sala)"
        verbose_name_plural = "Material/Serviço de Consumo (Com Sala)"

class DespesaSemSala(Despesa):
    class Meta:
        proxy = True
        verbose_name = "Material Consumo (Sem Sala Comercial)"
        verbose_name_plural = "Material Consumo (Sem Sala Comercial)"

class DespesaReparoComSala(Despesa):
    class Meta:
        proxy = True
        verbose_name = "Reparos/Reforma (Com Sala)"
        verbose_name_plural = "Reparos/Reforma (Com Sala)"

class DespesaReparoSemSala(Despesa):
    class Meta:
        proxy = True
        verbose_name = "Reparos/Reforma (Sem Sala)"
        verbose_name_plural = "Reparos/Reforma (Sem Sala)"

class DespesaAreasComuns(Despesa):
    class Meta:
        proxy = True
        verbose_name = "Energia Áreas Comuns"
        verbose_name_plural = "Energia Áreas Comuns"

    def save(self, *args, **kwargs):
        """
        Antes de salvar, busca a última DespesaEnergia
        (que contém o fatura e o custo_kwh) e recalcula valor_total.
        """
        # 1) pega a última DespesaEnergia para o mesmo mês/ano
        en = DespesaEnergia.objects.filter(
            mes=self.mes,
            ano=self.ano
        ).order_by('-id').first()

        if en and en.energia_leituras:
            params = en.energia_leituras.get('params', {})
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
            # 2) soma todas as leituras de energia do mês/ano
            total_kwh = (
                LeituraEnergia.objects
                .filter(mes=int(self.mes), ano=int(self.ano))
                .aggregate(total=Sum('leitura'))
                .get('total') or Decimal('0')
            )
            total_kwh = Decimal(total_kwh)
            # 3) recalcula o valor_total
            self.valor_total = (fatura - (custo * total_kwh)).quantize(Decimal('0.01'))
        else:
            # Se não houver registro de DespesaEnergia, zera
            self.valor_total = Decimal('0.00')

        # 4) força o tipo correto (caso tenha criado manualmente)
        from .models import TipoDespesa
        self.tipo = TipoDespesa.objects.get(nome__iexact='Energia Áreas Comuns')

        # 5) chame o save padrão do Django
        super().save(*args, **kwargs)

class DespesaEnergia(Despesa):
    class Meta:
        proxy = True
        verbose_name = "Parâmetro de Energia"
        verbose_name_plural = "Parâmetros de Energia"

class DespesaGas(Despesa):
    class Meta:
        proxy = True
        verbose_name = "Parâmetro de Gás"
        verbose_name_plural = "Parâmetros de Gás"

class DespesaAgua(Despesa):
    class Meta:
        proxy = True
        verbose_name = "Parâmetro de Água"
        verbose_name_plural = "Parâmetros de Água"

class Rateio(models.Model):
    despesa = models.ForeignKey(Despesa, on_delete=models.CASCADE)
    unidade = models.ForeignKey(Unidade, on_delete=models.CASCADE)
    valor   = models.DecimalField(max_digits=10, decimal_places=2)
    consumo = models.DecimalField(
        "Consumo (m³)", max_digits=10, decimal_places=3,
        null=True, blank=True,
        help_text="Só para despesas de gás, a diferença de leituras"
    )

    def __str__(self):
        return f"{self.despesa.tipo.nome} — {self.unidade.nome} — R$ {self.valor:.2f}"

class LeituraEnergia(models.Model):
    MEDIDOR_CHOICES = (
        (1, "Medidor 1"),
        (2, "Medidor 2"),
    )

    unidade = models.ForeignKey(Unidade, on_delete=models.CASCADE)
    mes     = models.IntegerField()
    ano     = models.IntegerField()
    leitura = models.DecimalField(max_digits=10, decimal_places=4)
    medidor = models.IntegerField(choices=MEDIDOR_CHOICES)

    class Meta:
        unique_together = ('unidade', 'mes', 'ano', 'medidor')

    def __str__(self):
        return f"{self.unidade.nome} - M{self.medidor}: {self.leitura}"

class Boleto(Unidade):
    class Meta:
        proxy = True
        verbose_name = "Boleto"
        verbose_name_plural = "Boletos"

class FundoReserva(Despesa):
    class Meta:
        proxy = True
        verbose_name = "Fundo de Reserva"
        verbose_name_plural = "Fundos de Reserva"

class FundosCsv(models.Model):
    """
    Modelo vazio apenas para servir de base à proxy ExportarXlsx.
    """
    # nenhum campo necessário

    class Meta:
        managed = False    # não cria tabela no banco
        verbose_name = "Fundos CSV"
        verbose_name_plural = "Fundos CSV"

class ParametroAgua(models.Model):
    mes     = models.IntegerField(choices=[(i, i) for i in range(1,13)])
    ano     = models.IntegerField()
    fatura      = models.DecimalField("R$ Fatura",      max_digits=10, decimal_places=2)
    m3_total    = models.DecimalField("m³ Total",       max_digits=10, decimal_places=4)
    valor_m3    = models.DecimalField("R$ por m³ Água", max_digits=10, decimal_places=4)

    class Meta:
        unique_together = ("mes","ano")
        verbose_name = "Parâmetro de Água"
        verbose_name_plural = "Parâmetros de Água"

    def __str__(self):
        return f"Água {self.mes}/{self.ano}"

class ParametroGas(models.Model):
    mes      = models.IntegerField()
    ano      = models.IntegerField()
    recarga  = models.DecimalField("R$ Recarga", max_digits=12, decimal_places=2)
    kg       = models.DecimalField("KG",          max_digits=12, decimal_places=3)
    m3_kg    = models.DecimalField("m³/kg",       max_digits=12, decimal_places=4)
    valor_m3 = models.DecimalField("R$ por m³ Gás", max_digits=12, decimal_places=4)

    class Meta:
        verbose_name = "Parâmetro de Gás"
        verbose_name_plural = "Parâmetros de Gás"

class ParametroEnergia(models.Model):
    mes        = models.IntegerField()
    ano        = models.IntegerField()
    fatura     = models.DecimalField("R$ Fatura", max_digits=12, decimal_places=2)
    kwh_total  = models.DecimalField("kWh Total", max_digits=12, decimal_places=2)
    custo_kwh  = models.DecimalField("R$ Custo kWh", max_digits=12, decimal_places=4)
    uso_kwh    = models.DecimalField("R$ Uso kWh",   max_digits=12, decimal_places=4)

    class Meta:
        unique_together = ('mes', 'ano')
        verbose_name = "Parâmetro de Energia"
        verbose_name_plural = "Parâmetros de Energia"

class ExportarXlsx(Despesa):
    """
    Proxy para mostrar o menu 'Exportar XLSX' no Admin.
    """
    class Meta:
        proxy = True
        verbose_name = "Exportar XLSX"
        verbose_name_plural = "Exportar XLSX"
