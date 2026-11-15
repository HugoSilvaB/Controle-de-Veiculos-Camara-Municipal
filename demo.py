"""Dados FICTÍCIOS para demonstração (só roda com DEMO=1 e banco sem veículos)."""
import os
import random
from datetime import datetime, timedelta, date

from PIL import Image, ImageDraw

from models import db, Veiculo, MotoristaCadastrado, Gabinete, RegistroUso


def _cpf(base9):
    d = [int(c) for c in base9]
    for n in (9, 10):
        soma = sum(v * (n + 1 - i) for i, v in enumerate(d[:n]))
        d.append((soma * 10 % 11) % 10)
    return ''.join(map(str, d))


def _foto(pasta, nome, km):
    img = Image.new('RGB', (640, 360), (30, 40, 55))
    ImageDraw.Draw(img).text((40, 150), f'PAINEL (DEMO)  KM {km:,.0f}'.replace(',', '.'), fill=(240, 240, 240))
    img.save(os.path.join(pasta, nome), 'JPEG', quality=70)
    return nome


def semear(pasta):
    if Veiculo.query.count() > 0:
        return
    os.makedirs(pasta, exist_ok=True)
    rnd = random.Random(7)
    carros = [('Toyota Corolla (demo)', 'DEM-1A23', 42100), ('Fiat Strada (demo)', 'DEM-2B34', 18750),
              ('Chevrolet Spin (demo)', 'DEM-3C45', 66300)]
    veiculos = []
    for modelo, placa, km in carros:
        v = Veiculo(modelo=modelo, placa=placa, km_atual=km, proxima_revisao_km=km + 900, ativo=True)
        db.session.add(v)
        veiculos.append(v)
    unidades = Gabinete.query.filter_by(ativo=True).limit(4).all()
    nomes = ['Motorista Exemplo Um', 'Motorista Exemplo Dois', 'Motorista Exemplo Tres', 'Motorista Exemplo Quatro']
    motoristas = []
    for i, nome in enumerate(nomes):
        u = unidades[i % len(unidades)]
        m = MotoristaCadastrado(nome=nome, cpf=_cpf(f'{111222330 + i * 7:09d}'), gabinete=u.rotulo, gabinete_id=u.id,
                                validade_cnh=date.today() + timedelta(days=(20 if i == 0 else 400 + i * 50)), ativo=True)
        db.session.add(m)
        motoristas.append(m)
    db.session.flush()
    destinos = ['Reunião na Secretaria de Obras', 'Audiência em Brasília', 'Visita a escola municipal',
                'Entrega de documentos', 'Evento no bairro']
    for v in veiculos:
        km = v.km_atual - 1500
        t = datetime.now() - timedelta(days=45)
        while t < datetime.now() - timedelta(days=1):
            m = rnd.choice(motoristas)
            rod = rnd.randint(12, 80)
            reg = RegistroUso(
                gabinete_vereador=m.gabinete, motorista_nome=m.nome, veiculo_id=v.id,
                data_hora_saida=t, km_saida=km, foto_km_saida=_foto(pasta, f'demo_s_{v.id}_{int(km)}.jpg', km),
                data_hora_chegada=t + timedelta(hours=rnd.randint(1, 5)), km_chegada=km + rod,
                foto_km_chegada=_foto(pasta, f'demo_c_{v.id}_{int(km)}.jpg', km + rod),
                destino_finalidade=rnd.choice(destinos), distancia_km=rod, gabinete_id=m.gabinete_id, motorista_id=m.id)
            db.session.add(reg)
            km += rod
            t += timedelta(days=rnd.randint(1, 4), hours=rnd.randint(0, 3))
    db.session.commit()
    print('Dados de demonstração criados.')
