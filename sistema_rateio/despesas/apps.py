import json
from pathlib import Path
from django.apps import AppConfig
from django.conf import settings
import logging

logger = logging.getLogger(__name__)

class DespesasConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'despesas'

    def ready(self):
        # garante que a pasta exista
        settings.PARAMETROS_AGUA_JSON.parent.mkdir(parents=True, exist_ok=True)
        settings.PARAMETROS_GAS_JSON .parent.mkdir(parents=True, exist_ok=True)
        settings.PARAMETROS_ENERGIA_JSON.parent.mkdir(parents=True, exist_ok=True)

        # valores iniciais padrão
        defaults = {
            settings.PARAMETROS_AGUA_JSON: {
                "params": {"fatura": 0, "m3_total": 0, "valor_m3": 0}
            },
            settings.PARAMETROS_GAS_JSON: {
                "params": {"recarga": 0, "kg": 0, "m3_kg": 0, "valor_m3": 0}
            },
            settings.PARAMETROS_ENERGIA_JSON: {
                "params": {"fatura": 0, "kwh_total": 0, "custo_kwh": 0, "uso_kwh": 0}
            },
        }

        for path, data in defaults.items():
            if not Path(path).exists():
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
