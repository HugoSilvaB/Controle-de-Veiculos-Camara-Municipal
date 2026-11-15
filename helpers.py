"""Funções auxiliares: CPF, fotos, configurações, alertas, backup, migração do banco."""
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from io import BytesIO

from flask import request, url_for
from flask_login import current_user
from PIL import Image, ImageOps
from sqlalchemy import func, or_
from werkzeug.security import check_password_hash, generate_password_hash

from models import (AdminUser, Configuracao, EventoVeiculo, Gabinete, Legislatura, LogAuditoria,
                    MotoristaCadastrado, PresidenteAno, RegistroUso, Veiculo, db, presidente_do_ano)

Image.MAX_IMAGE_PIXELS = 60_000_000

try:  # QR Code gerado no próprio sistema (pip install "qrcode[pil]")
    import qrcode
    QR_LOCAL = True
except ImportError:  # sem a biblioteca, cai no serviço público como antes
    qrcode = None
    QR_LOCAL = False

# ------------------------------------------------------------------ Configurações
CONFIG_PADRAO = {
    'base_url': os.getenv('BASE_URL') or os.getenv('RENDER_EXTERNAL_URL') or 'https://snub-riddance-strangely.ngrok-free.dev',
    'dias_alerta_cnh': '30',
    'km_alerta_revisao': '1000',
    'horas_viagem_aberta': '12',
}

GABINETES_INICIAIS = [
    'Presidência - Charles do Oceano', 'Gabinete - André Logos', 'Gabinete - Chico Civil',
    'Gabinete - Cleiton Lucio', 'Gabinete - Emilio Santiago', 'Gabinete - Ewerton Vidal',
    'Gabinete - Fabricio Chaves', 'Gabinete - Luana Marques', 'Gabinete - Marcos Oliveira',
    'Gabinete - Marquim do Baxim', 'Gabinete - Pacífico', 'Gabinete - Paulo Jordão',
    'Gabinete - Renato Caldas', 'Gabinete - Willian Faleiro', 'Gabinete - Zé Lopes',
]


def cfg(chave):
    c = db.session.get(Configuracao, chave)
    if c is not None and c.valor not in (None, ''):
        return c.valor
    return CONFIG_PADRAO[chave]


def cfg_int(chave):
    try:
        return int(float(cfg(chave)))
    except (TypeError, ValueError):
        return int(CONFIG_PADRAO[chave])


def definir_cfg(chave, valor):
    c = db.session.get(Configuracao, chave)
    if c is None:
        db.session.add(Configuracao(chave=chave, valor=str(valor)))
    else:
        c.valor = str(valor)


def url_viagem(veiculo):
    return cfg('base_url').rstrip('/') + url_for('iniciar_viagem_qrcode', qr_token=veiculo.qr_token)


# ------------------------------------------------------------------ Validação / conversão
def limpar_cpf(txt):
    return re.sub(r'\D', '', txt or '')


def cpf_valido(txt):
    cpf = limpar_cpf(txt)
    if len(cpf) != 11:
        return False
    if os.getenv('VALIDAR_CPF', '1').lower() in ('0', 'false', 'nao', 'não', 'no'):
        return True  # só confere o formato (útil em testes)
    if cpf == cpf[0] * 11:
        return False
    for n in (9, 10):
        soma = sum(int(cpf[i]) * (n + 1 - i) for i in range(n))
        if (soma * 10 % 11) % 10 != int(cpf[n]):
            return False
    return True


def formatar_cpf(valor):
    c = limpar_cpf(valor)
    return f'{c[:3]}.{c[3:6]}.{c[6:9]}-{c[9:]}' if len(c) == 11 else (valor or '')


def para_float(txt):
    """Converte '1.234,5' / '1234.5' em float. Devolve None se inválido ou negativo."""
    if txt is None:
        return None
    t = str(txt).strip().replace(' ', '')
    if not t:
        return None
    if ',' in t:
        t = t.replace('.', '').replace(',', '.')
    try:
        v = float(t)
    except ValueError:
        return None
    if not math.isfinite(v) or v < 0:
        return None
    return v


def parse_data(txt):
    try:
        return datetime.strptime((txt or '').strip(), '%Y-%m-%d').date()
    except ValueError:
        return None


def parse_datahora(txt):
    for formato in ('%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime((txt or '').strip(), formato)
        except ValueError:
            continue
    return None


def validar_placa(placa):
    return bool(re.fullmatch(r'[A-Z]{3}-?\d[A-Z0-9]\d{2}', placa or ''))


def senha_forte(senha):
    """Devolve uma mensagem de erro ou None."""
    if len(senha or '') < 8:
        return 'A senha precisa ter pelo menos 8 caracteres.'
    if senha.lower() in ('admin123', '12345678', 'senha123', 'password'):
        return 'Escolha uma senha menos óbvia.'
    return None


def senha_provisoria():
    alfabeto = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    return ''.join(secrets.choice(alfabeto) for _ in range(10))


# ------------------------------------------------------------------ Fotos
def salvar_foto_compacta(arquivo, prefixo, pasta):
    """Valida a imagem, corrige a rotação e grava um JPEG leve. Devolve o nome ou None."""
    if arquivo is None or not getattr(arquivo, 'filename', ''):
        return None
    try:
        img = Image.open(arquivo.stream)
        img = ImageOps.exif_transpose(img)
        if img.mode != 'RGB':
            img = img.convert('RGB')
        img.thumbnail((1280, 1280))
        os.makedirs(pasta, exist_ok=True)
        nome = f"{prefixo}_{datetime.now():%Y%m%d%H%M%S}_{uuid.uuid4().hex[:8]}.jpg"
        img.save(os.path.join(pasta, nome), 'JPEG', quality=75, optimize=True)
        return nome
    except Exception as exc:  # imagem corrompida, formato inválido etc.
        print(f'[ERRO salvar_foto_compacta]: {exc}')
        return None


# ------------------------------------------------------------------ Limite de tentativas
class LimiteTentativas:
    """Bloqueia temporariamente depois de várias falhas seguidas (login, CPF...)."""

    def __init__(self, maximo, janela_segundos):
        self.maximo = maximo
        self.janela = janela_segundos
        self._falhas = {}
        self._lock = threading.Lock()

    def _recentes(self, chave):
        agora = time.time()
        lista = [t for t in self._falhas.get(chave, []) if agora - t < self.janela]
        self._falhas[chave] = lista
        return lista

    def bloqueado(self, chave):
        with self._lock:
            return len(self._recentes(chave)) >= self.maximo

    def falhou(self, chave):
        with self._lock:
            self._recentes(chave).append(time.time())

    def limpar(self, chave):
        with self._lock:
            self._falhas.pop(chave, None)

    def minutos_restantes(self, chave):
        with self._lock:
            lista = self._recentes(chave)
            if not lista:
                return 0
            return max(1, math.ceil((self.janela - (time.time() - min(lista))) / 60))


def ip_cliente():
    return request.remote_addr or '-'


# ------------------------------------------------------------------ Auditoria
def registrar_log(acao, detalhe='', usuario=None):
    """Adiciona uma linha de auditoria à sessão (o commit fica por conta da rota)."""
    if usuario is None:
        usuario = current_user.usuario if current_user.is_authenticated else 'público'
    db.session.add(LogAuditoria(usuario=usuario, acao=acao, detalhe=(detalhe or '')[:2000], ip=ip_cliente()))


# ------------------------------------------------------------------ Gabinetes
ORDEM_TIPO = {'Presidência': 0, 'Gabinete': 1, 'Administrativo': 2}


def ordenar_gabinetes(lista):
    return sorted(lista, key=lambda g: (ORDEM_TIPO.get(g.tipo, 9), (g.rotulo or '').casefold()))


def gabinetes_ativos():
    return ordenar_gabinetes(Gabinete.query.filter_by(ativo=True).all())


# ------------------------------------------------------------------ Consultas de viagens
def filtrar_viagens(args, status_padrao='todas'):
    """Monta a consulta de viagens a partir dos filtros da tela (de, ate, veiculo, gabinete, motorista, status)."""
    q = RegistroUso.query
    de = parse_data(args.get('de'))
    ate = parse_data(args.get('ate'))
    if de:
        q = q.filter(RegistroUso.data_hora_saida >= datetime.combine(de, datetime.min.time()))
    if ate:
        q = q.filter(RegistroUso.data_hora_saida < datetime.combine(ate + timedelta(days=1), datetime.min.time()))
    veiculo = args.get('veiculo', type=int)
    if veiculo:
        q = q.filter(RegistroUso.veiculo_id == veiculo)
    gabinete = args.get('gabinete', type=int)
    if gabinete:
        q = q.filter(RegistroUso.gabinete_id == gabinete)
    motorista = (args.get('motorista') or '').strip()
    if motorista:
        q = q.filter(RegistroUso.motorista_nome.ilike(f'%{motorista}%'))
    status = args.get('status') or status_padrao
    if status == 'andamento':
        q = q.filter(RegistroUso.km_chegada.is_(None))
    elif status == 'concluidas':
        q = q.filter(RegistroUso.km_chegada.is_not(None))
    return q.order_by(RegistroUso.data_hora_saida.desc())


def resolver_periodo(args):
    """Devolve (de, ate, rotulo) a partir de ano / legislatura / datas livres."""
    de, ate = parse_data(args.get('de')), parse_data(args.get('ate'))
    if de or ate:
        rotulo = f"{de.strftime('%d/%m/%Y') if de else 'início'} a {ate.strftime('%d/%m/%Y') if ate else 'hoje'}"
        return de, ate, rotulo
    leg_id = args.get('legislatura', type=int)
    if leg_id:
        leg = db.session.get(Legislatura, leg_id)
        if leg:
            return leg.data_inicio, leg.data_fim, f'Legislatura {leg.rotulo}'
    ano = args.get('ano')
    if ano == 'todos':
        return None, None, 'Todo o período'
    ano = args.get('ano', type=int) or date.today().year
    return date(ano, 1, 1), date(ano, 12, 31), f'Ano de {ano}'


def _soma(d, chave, viagens, km):
    item = d.setdefault(chave, {'nome': chave, 'viagens': 0, 'km': 0.0})
    item['viagens'] += viagens
    item['km'] = round(item['km'] + km, 1)


def compilar_relatorio(de, ate):
    """Totais e agrupamentos de viagens concluídas e custos no período."""
    q = RegistroUso.query.filter(RegistroUso.km_chegada.is_not(None))
    qa = RegistroUso.query.filter(RegistroUso.km_chegada.is_(None))
    qe = EventoVeiculo.query
    if de:
        ini = datetime.combine(de, datetime.min.time())
        q = q.filter(RegistroUso.data_hora_saida >= ini)
        qa = qa.filter(RegistroUso.data_hora_saida >= ini)
        qe = qe.filter(EventoVeiculo.data_hora >= ini)
    if ate:
        fim = datetime.combine(ate + timedelta(days=1), datetime.min.time())
        q = q.filter(RegistroUso.data_hora_saida < fim)
        qa = qa.filter(RegistroUso.data_hora_saida < fim)
        qe = qe.filter(EventoVeiculo.data_hora < fim)

    por_gab, por_mes, por_vei, por_mot = {}, {}, {}, {}
    total_km, ocorrencias, registros = 0.0, 0, q.all()
    for r in registros:
        km = r.km_rodados or 0.0
        total_km += km
        gab = r.gabinete_unidade.nome_relatorio if r.gabinete_unidade else r.gabinete_vereador
        _soma(por_gab, gab, 1, km)
        _soma(por_mes, r.data_hora_saida.strftime('%Y-%m'), 1, km)
        _soma(por_vei, f'{r.veiculo.modelo} ({r.veiculo.placa})', 1, km)
        _soma(por_mot, r.motorista_nome, 1, km)
        if r.tem_ocorrencia:
            ocorrencias += 1

    custos = {'Abastecimento': 0.0, 'Manutenção': 0.0, 'Multa': 0.0}
    custo_vei = {}
    for e in qe.all():
        custos[e.tipo] = custos.get(e.tipo, 0.0) + (e.valor or 0.0)
        nome = f'{e.veiculo.modelo} ({e.veiculo.placa})'
        custo_vei[nome] = custo_vei.get(nome, 0.0) + (e.valor or 0.0)
    for nome, item in por_vei.items():
        item['custo'] = round(custo_vei.get(nome, 0.0), 2)

    def ordenar(d, por='km'):
        return sorted(d.values(), key=lambda i: i[por], reverse=True)

    n = len(registros)
    return {
        'viagens': n,
        'km_total': round(total_km, 1),
        'km_medio': round(total_km / n, 1) if n else 0.0,
        'em_andamento': qa.count(),
        'ocorrencias': ocorrencias,
        'por_gabinete': ordenar(por_gab),
        'por_mes': sorted(por_mes.values(), key=lambda i: i['nome']),
        'por_veiculo': ordenar(por_vei),
        'por_motorista': ordenar(por_mot),
        'custos': {k: round(v, 2) for k, v in custos.items()},
        'custo_total': round(sum(custos.values()), 2),
        'registros': registros,
    }


# ------------------------------------------------------------------ Alertas do painel
def gerar_alertas():
    hoje = date.today()
    alertas = []

    def add(nivel, icone, titulo, detalhes=None, endpoint=None, link='Abrir'):
        alertas.append({'nivel': nivel, 'icone': icone, 'titulo': titulo, 'detalhes': detalhes or [],
                        'url': url_for(endpoint) if endpoint else None, 'link': link})

    if not presidente_do_ano():
        add('warning', 'user-tie', f'O presidente da Câmara de {hoje.year} ainda não foi definido.',
            endpoint='mesa_diretora', link='Definir presidente')

    leg = Legislatura.query.filter_by(ativa=True).order_by(Legislatura.inicio.desc()).first()
    if leg is None or hoje.year > leg.fim:
        add('danger', 'landmark', 'A legislatura terminou: cadastre a nova e atualize os gabinetes.',
            [f'Última legislatura ativa: {leg.rotulo}'] if leg else [], 'gabinetes', 'Gabinetes e legislatura')

    dias = cfg_int('dias_alerta_cnh')
    motoristas = MotoristaCadastrado.query.filter_by(ativo=True).all()
    vencidas, a_vencer, sem_data, setor_inativo = [], [], [], []
    for m in motoristas:
        if not m.validade_cnh:
            sem_data.append(m.nome)
        elif m.validade_cnh < hoje:
            vencidas.append(f'{m.nome} (venceu em {m.validade_cnh:%d/%m/%Y})')
        elif m.validade_cnh <= hoje + timedelta(days=dias):
            a_vencer.append(f'{m.nome} (vence em {m.dias_cnh} dia(s))')
        if not m.gabinete_ativo:
            setor_inativo.append(f'{m.nome} → {m.gabinete_rotulo}')
    if vencidas:
        add('danger', 'id-card', f'{len(vencidas)} motorista(s) com CNH vencida (bloqueados).', vencidas,
            'gestao_motoristas', 'Ver motoristas')
    if a_vencer:
        add('warning', 'id-card', f'{len(a_vencer)} CNH vencendo nos próximos {dias} dias.', a_vencer,
            'gestao_motoristas', 'Ver motoristas')
    if sem_data:
        add('warning', 'id-card', f'{len(sem_data)} motorista(s) sem validade de CNH cadastrada (bloqueados).',
            sem_data, 'gestao_motoristas', 'Ver motoristas')
    if setor_inativo:
        add('warning', 'users-slash', f'{len(setor_inativo)} motorista(s) vinculados a gabinete inativo (bloqueados).',
            setor_inativo, 'gabinetes', 'Remanejar')

    margem = cfg_int('km_alerta_revisao')
    rev_vencida, rev_proxima, manutencao = [], [], []
    for v in Veiculo.query.filter_by(ativo=True).all():
        if v.em_manutencao:
            manutencao.append(f'{v.modelo} ({v.placa})')
        if v.proxima_revisao_km:
            if (v.km_atual or 0) >= v.proxima_revisao_km:
                rev_vencida.append(f'{v.modelo} ({v.placa}) - {v.km_atual:.0f} km, revisão em {v.proxima_revisao_km:.0f} km')
            elif (v.km_atual or 0) >= v.proxima_revisao_km - margem:
                faltam = v.proxima_revisao_km - (v.km_atual or 0)
                rev_proxima.append(f'{v.modelo} ({v.placa}) - faltam {faltam:.0f} km')
    if rev_vencida:
        add('danger', 'wrench', f'{len(rev_vencida)} veículo(s) com revisão vencida.', rev_vencida, 'gestao_veiculos', 'Ver veículos')
    if rev_proxima:
        add('warning', 'wrench', f'{len(rev_proxima)} veículo(s) perto da revisão.', rev_proxima, 'gestao_veiculos', 'Ver veículos')
    if manutencao:
        add('info', 'screwdriver-wrench', f'{len(manutencao)} veículo(s) em manutenção.', manutencao, 'gestao_veiculos', 'Ver veículos')

    horas = cfg_int('horas_viagem_aberta')
    limite = datetime.now() - timedelta(hours=horas)
    abertas = RegistroUso.query.filter(RegistroUso.km_chegada.is_(None), RegistroUso.data_hora_saida < limite).all()
    if abertas:
        add('warning', 'hourglass-half', f'{len(abertas)} viagem(ns) abertas há mais de {horas}h.',
            [f'{r.veiculo.modelo} - {r.motorista_nome} (há {r.duracao_texto})' for r in abertas], 'painel_admin', '')
    return alertas


# ------------------------------------------------------------------ Planilhas
def montar_xlsx(abas):
    """abas = [(titulo, [cabeçalhos], [linhas])] -> BytesIO pronto para enviar."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)
    for titulo, cabecalho, linhas in abas:
        ws = wb.create_sheet(titulo[:31])
        ws.append(cabecalho)
        for cel in ws[1]:
            cel.font = Font(bold=True, color='FFFFFF')
            cel.fill = PatternFill('solid', fgColor='003366')
            cel.alignment = Alignment(vertical='center')
        for linha in linhas:
            ws.append(linha)
        ws.freeze_panes = 'A2'
        for i, titulo_col in enumerate(cabecalho, 1):
            largura = max([len(str(titulo_col))] + [len(str(l[i - 1])) for l in linhas[:500]
                                                  if len(l) >= i and l[i - 1] is not None])
            ws.column_dimensions[get_column_letter(i)].width = min(max(10, largura + 2), 60)
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# ------------------------------------------------------------------ Backup
def caminho_banco():
    return db.engine.url.database


def fazer_backup(pasta, manter=14):
    os.makedirs(pasta, exist_ok=True)
    nome = f'database_{datetime.now():%Y%m%d_%H%M%S}.db'
    destino = os.path.join(pasta, nome)
    origem_con = sqlite3.connect(caminho_banco())
    destino_con = sqlite3.connect(destino)
    try:
        origem_con.backup(destino_con)
    finally:
        destino_con.close()
        origem_con.close()
    antigos = sorted(f for f in os.listdir(pasta) if f.startswith('database_') and f.endswith('.db'))
    for velho in antigos[:-manter]:
        try:
            os.remove(os.path.join(pasta, velho))
        except OSError:
            pass
    return nome


def listar_backups(pasta):
    if not os.path.isdir(pasta):
        return []
    itens = []
    for f in sorted(os.listdir(pasta), reverse=True):
        if f.startswith('database_') and f.endswith('.db'):
            p = os.path.join(pasta, f)
            itens.append({'nome': f, 'tamanho_kb': round(os.path.getsize(p) / 1024, 1),
                          'data': datetime.fromtimestamp(os.path.getmtime(p))})
    return itens


def iniciar_backup_automatico(app, pasta):
    """Uma cópia do banco por dia, enquanto o sistema estiver ligado (guarda as 14 últimas)."""
    def laco():
        while True:
            try:
                with app.app_context():
                    os.makedirs(pasta, exist_ok=True)
                    prefixo = f'database_{date.today():%Y%m%d}'
                    if not any(f.startswith(prefixo) for f in os.listdir(pasta)):
                        fazer_backup(pasta)
            except Exception as exc:
                print(f'[backup automático] {exc}')
            time.sleep(3600)

    threading.Thread(target=laco, daemon=True, name='backup-automatico').start()


# ------------------------------------------------------------------ Preparação / migração do banco
def _colunas(tabela):
    return {linha[1] for linha in db.session.execute(db.text(f'PRAGMA table_info({tabela})'))}


def _adicionar_coluna(tabela, coluna, definicao):
    if coluna not in _colunas(tabela):
        db.session.execute(db.text(f'ALTER TABLE {tabela} ADD COLUMN {coluna} {definicao}'))
        db.session.commit()


def _migrar_colunas():
    novas = [
        ('admin_users', 'ativo', 'BOOLEAN DEFAULT 1'),
        ('admin_users', 'deve_trocar_senha', 'BOOLEAN DEFAULT 0'),
        ('admin_users', 'ultimo_acesso', 'DATETIME'),
        ('admin_users', 'criado_em', 'DATETIME'),
        ('motoristas_cadastrados', 'validade_cnh', 'DATE'),
        ('motoristas_cadastrados', 'ativo', 'BOOLEAN DEFAULT 1'),
        ('motoristas_cadastrados', 'gabinete_id', 'INTEGER'),
        ('veiculos', 'status', "VARCHAR(30) DEFAULT 'Disponível'"),
        ('veiculos', 'proxima_revisao_km', 'FLOAT'),
        ('veiculos', 'ativo', 'BOOLEAN DEFAULT 1'),
        ('registro_uso', 'gabinete_id', 'INTEGER'),
        ('registro_uso', 'motorista_id', 'INTEGER'),
    ]
    for tabela, coluna, definicao in novas:
        _adicionar_coluna(tabela, coluna, definicao)


def _semear_gabinetes():
    if Gabinete.query.count() > 0:
        return
    leg = Legislatura.query.filter_by(ativa=True).first()
    if leg is None:
        leg = Legislatura(inicio=2025, fim=2028, ativa=True)
        db.session.add(leg)
        db.session.flush()
    for rotulo in GABINETES_INICIAIS:
        tipo, _, nome = rotulo.partition(' - ')
        if tipo == 'Presidência':
            db.session.add(Gabinete(tipo='Presidência', nome='Presidência', ativo=True))
            if nome and not PresidenteAno.query.filter_by(ano=date.today().year).first():
                db.session.add(PresidenteAno(ano=date.today().year, nome=nome))
        else:
            db.session.add(Gabinete(tipo='Gabinete', vereador=nome, legislatura_id=leg.id, ativo=True))
    db.session.commit()


def _vincular_historico():
    unidades = Gabinete.query.all()
    mapa = {u.rotulo.casefold(): u.id for u in unidades}
    presidencia = next((u for u in unidades if u.tipo == 'Presidência'), None)

    def achar(texto):
        if not texto:
            return None
        t = texto.strip().casefold()
        if t in mapa:
            return mapa[t]
        if t.startswith('presidência') and presidencia:
            return presidencia.id
        return None

    for m in MotoristaCadastrado.query.filter(MotoristaCadastrado.gabinete_id.is_(None)).all():
        m.gabinete_id = achar(m.gabinete)
    nomes = {}
    for m in MotoristaCadastrado.query.all():
        nomes.setdefault(m.nome.casefold(), []).append(m.id)
    for r in RegistroUso.query.filter(or_(RegistroUso.gabinete_id.is_(None), RegistroUso.motorista_id.is_(None))).all():
        if r.gabinete_id is None:
            r.gabinete_id = achar(r.gabinete_vereador)
        if r.motorista_id is None:
            ids = nomes.get((r.motorista_nome or '').casefold(), [])
            if len(ids) == 1:
                r.motorista_id = ids[0]
    db.session.commit()


def _garantir_admin():
    if AdminUser.query.count() == 0:
        db.session.add(AdminUser(nome='Administrador Master', usuario='admin',
                                 senha=generate_password_hash('admin123'), ativo=True, deve_trocar_senha=True))
    else:
        for a in AdminUser.query.all():
            if a.ativo is None:
                a.ativo = True
            if check_password_hash(a.senha, 'admin123'):
                a.deve_trocar_senha = True  # senha padrão: obriga a trocar no próximo acesso
    db.session.commit()


def _acertar_dados_antigos():
    for r in RegistroUso.query.filter(RegistroUso.km_chegada.is_not(None), RegistroUso.distancia_km.is_(None)).all():
        r.distancia_km = round(r.km_chegada - r.km_saida, 2)
    for v in Veiculo.query.all():
        if v.status is None:
            v.status = 'Disponível'
        if v.ativo is None:
            v.ativo = True
        ultimo = db.session.query(func.max(RegistroUso.km_chegada)).filter(RegistroUso.veiculo_id == v.id).scalar()
        if ultimo and ultimo > (v.km_atual or 0):
            v.km_atual = ultimo
    for m in MotoristaCadastrado.query.filter(MotoristaCadastrado.ativo.is_(None)).all():
        m.ativo = True
    db.session.commit()


def preparar_banco():
    """Cria tabelas novas, adiciona colunas que faltam e ajusta dados antigos (seguro para rodar sempre)."""
    db.create_all()
    _migrar_colunas()
    _semear_gabinetes()
    _vincular_historico()
    _garantir_admin()
    _acertar_dados_antigos()
