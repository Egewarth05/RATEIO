from django import forms
from .models import Despesa, DespesaGas
from decimal import Decimal, ROUND_HALF_UP
from .models import DespesaAgua

class DespesaForm(forms.ModelForm):
    class Meta:
        model = Despesa
        fields = ['tipo', 'mes', 'ano', 'descricao']

class DespesaEnergiaForm(forms.ModelForm):
    fatura      = forms.DecimalField(label="R$ Fatura",    max_digits=10, decimal_places=2, required=False)
    kwh_total   = forms.DecimalField(label="kWh Total",    max_digits=12, decimal_places=4, required=False)
    custo_kwh   = forms.DecimalField(label="R$ Custo kWh", max_digits=12, decimal_places=4, required=False)
    uso_kwh     = forms.DecimalField(label="R$ Uso kWh",   max_digits=12, decimal_places=2, required=False)

    class Meta:
        model = Despesa
        fields = ('mes','ano','valor_total')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        params = getattr(self.instance, 'energia_leituras', {}) or {}
        params = params.get('params', {})

        for fld in ('fatura','kwh_total','custo_kwh','uso_kwh'):
            if fld in self.fields:
                self.fields[fld].initial = params.get(fld, 0 if fld!='kwh_total' else 1)

    def save(self, commit=True):
        inst = super().save(commit=False)
        inst.energia_leituras = {
            'params': {
                'fatura':    float(self.cleaned_data.get('fatura',    0) or 0),
                'kwh_total': float(self.cleaned_data.get('kwh_total', 0) or 0),
                'custo_kwh': float(self.cleaned_data.get('custo_kwh', 0) or 0),
                'uso_kwh':   float(self.cleaned_data.get('uso_kwh',   0) or 0),
            },
            # aqui é que mudamos:
            'leituras': (inst.energia_leituras or {}).get('leituras', {}),
        }
        if commit:
            inst.save()
        return inst

class DespesaGasForm(forms.ModelForm):
    recarga   = forms.DecimalField(label="R$ Recarga", max_digits=10, decimal_places=2)
    kg        = forms.DecimalField(label="KG", max_digits=10, decimal_places=4)
    m3_kg     = forms.DecimalField(label="M³ por KG", max_digits=10, decimal_places=4)
    valor_m3  = forms.DecimalField(label="R$ por M³", max_digits=10, decimal_places=2)

    class Meta:
        model = DespesaGas
        fields = ('mes','ano','valor_total','recarga','kg','m3_kg','valor_m3')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # se já existe uma instância com dados salvos em gas_leituras, usamos eles
        leituras = getattr(self.instance, 'gas_leituras', {}) or {}
        params = leituras.get('params', {})
        for fld in ('recarga','kg','m3_kg','valor_m3'):
            # se o campo está no form, inicializa com o valor salvo (ou mantém o default)
            if fld in self.fields:
                self.fields[fld].initial = params.get(fld, self.fields[fld].initial)

    def save(self, commit=True):
        inst = super().save(commit=False)
        inst.gas_leituras = {
            'params': {
                'recarga':  float(self.cleaned_data.get('recarga', 0)),
                'kg':       float(self.cleaned_data.get('kg', 1)),
                'm3_kg':    float(self.cleaned_data.get('m3_kg', 1)),
                'valor_m3': float(self.cleaned_data.get('valor_m3',0)),
            },
            'leituras': inst.gas_leituras.get('leituras', {}) if inst.gas_leituras else {}
        }
        if commit:
            inst.save()
        return inst

class DespesaAguaForm(forms.ModelForm):
    fatura         = forms.DecimalField(label="R$ Fatura",    max_digits=10, decimal_places=2, required=False)
    m3_total       = forms.DecimalField(label="m³ Total",     max_digits=10, decimal_places=4, required=False)
    valor_m3_agua  = forms.DecimalField(label="R$ por m³ Água", max_digits=10, decimal_places=4, required=False)

    class Meta:
        model = Despesa
        fields = ('mes','ano','valor_total','fatura','m3_total','valor_m3_agua')

    def clean(self):
        cd = super().clean()
        val = cd.get('valor_m3_agua')
        if val is not None:
            cd['valor_m3_agua'] = Decimal(val).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        return cd

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if hasattr(self, 'fields') and self.instance and getattr(self.instance, 'agua_leituras', None):
            params = self.instance.agua_leituras.get('params', {})
            for fld, key in [('fatura','fatura'), ('m3_total','m3_total'), ('valor_m3_agua','valor_m3')]:
                if fld in self.fields:
                    self.fields[fld].initial = params.get(key, self.fields[fld].initial or 0)

    def save(self, commit=True):
        inst = super().save(commit=False)
        inst.agua_leituras = {
            'params': {
                'fatura':    float(self.cleaned_data.get('fatura',    0)),
                'm3_total':  float(self.cleaned_data.get('m3_total',  1)),
                'valor_m3':  float(self.cleaned_data.get('valor_m3_agua',0)),
            },
            'leituras': inst.agua_leituras.get('leituras', {}) if inst.agua_leituras else {}
        }
        if commit:
            inst.save()
        return inst
