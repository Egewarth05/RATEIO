from django.db import migrations, models
import django.db.models.deletion

class Migration(migrations.Migration):

    dependencies = [
        ('despesas', '0005_alter_leituragas_leitura'),
    ]

    operations = [
        migrations.CreateModel(
            name='LeituraAgua',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('mes', models.IntegerField()),
                ('ano', models.IntegerField()),
                ('leitura', models.DecimalField(max_digits=10, decimal_places=4)),
                ('unidade', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='despesas.unidade')),
            ],
            options={
                'unique_together': {('unidade', 'mes', 'ano')},
            },
        ),
        migrations.CreateModel(
            name='LeituraEnergia',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('mes', models.IntegerField()),
                ('ano', models.IntegerField()),
                ('leitura', models.DecimalField(max_digits=10, decimal_places=4)),
                ('medidor', models.IntegerField(choices=[(1, 'Medidor 1'), (2, 'Medidor 2')])),
                ('unidade', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='despesas.unidade')),
            ],
            options={
                'unique_together': {('unidade', 'mes', 'ano', 'medidor')},
            },
        ),
    ]
