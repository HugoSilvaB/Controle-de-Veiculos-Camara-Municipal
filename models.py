"""Modelos do banco de dados - Controle de Frota da Câmara Municipal de Novo Gama."""
import uuid
from datetime import date, datetime

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

TIPOS_GABINETE = ('Presidência', 'Gabinete', 'Administrativo')
TIPOS_EVENTO = ('Abastecimento', 'Manutenção', 'Multa')
STATUS_DISPONIVEL = 'Disponível'
STATUS_MANUTENCAO = 'Em Manutenção'


def agora():
    return datetime.now()


# ---------------------------------------------------------------- Legislatura / Gabinetes
class Legislatura(db.Model):
    """Mandato de 4 anos dos vereadores (ex.: 2025-2028)."""
    __tablename__ = 'legislaturas'
    id = db.Column(db.Integer, primary_key=True)
    inicio = db.Column(db.Integer, nullable=False)
    fim = db.Column(db.Integer, nullable=False)
    ativa = db.Column(db.Boolean, default=True, nullable=False)

    gabinetes = db.relationship('Gabinete', back_populates='legislatura')

    @property
    def rotulo(self):
        return f'{self.inicio}–{self.fim}'

    @property
    def data_inicio(self):
        return date(self.inicio, 1, 1)

    @property
    def data_fim(self):
        return date(self.fim, 12, 31)


class PresidenteAno(db.Model):
    """Quem preside a Câmara em cada ano (muda todo ano)."""
    __tablename__ = 'presidentes_ano'
    id = db.Column(db.Integer, primary_key=True)
    ano = db.Column(db.Integer, unique=True, nullable=False)
    nome = db.Column(db.String(100), nullable=False)


def presidente_do_ano(ano=None):
    return PresidenteAno.query.filter_by(ano=ano or date.today().year).first()


class Gabinete(db.Model):
    """Unidade que usa os veículos: Presidência, gabinete de vereador ou setor administrativo."""
    __tablename__ = 'gabinetes'
    id = db.Column(db.Integer, primary_key=True)
    tipo = db.Column(db.String(20), nullable=False, default='Gabinete')
    vereador = db.Column(db.String(100), nullable=True)
    nome = db.Column(db.String(100), nullable=True)
    ativo = db.Column(db.Boolean, default=True, nullable=False)
    legislatura_id = db.Column(db.Integer, db.ForeignKey('legislaturas.id'), nullable=True)
    inativado_em = db.Column(db.Date, nullable=True)

    legislatura = db.relationship('Legislatura', back_populates='gabinetes')
    motoristas = db.relationship('MotoristaCadastrado', back_populates='gabinete_obj')

    @property
    def rotulo(self):
        """Nome exibido e gravado nas viagens (a Presidência mostra o presidente do ano)."""
        if self.tipo == 'Gabinete':
            return f'Gabinete - {self.vereador or self.nome or "?"}'
        if self.tipo == 'Presidência':
            p = presidente_do_ano()
            return f'Presidência - {p.nome}' if p else 'Presidência'
        return self.nome or 'Setor'

    @property
    def nome_relatorio(self):
        """Nome estável para relatórios (a Presidência não muda de nome todo ano)."""
        return 'Presidência' if self.tipo == 'Presidência' else self.rotulo


# ---------------------------------------------------------------- Pessoas
class AdminUser(UserMixin, db.Model):
    __tablename__ = 'admin_users'
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    usuario = db.Column(db.String(50), unique=True, nullable=False)
    senha = db.Column(db.String(200), nullable=False)
    ativo = db.Column(db.Boolean, default=True)
    deve_trocar_senha = db.Column(db.Boolean, default=False)
    ultimo_acesso = db.Column(db.DateTime, nullable=True)
    criado_em = db.Column(db.DateTime, default=agora)

    @property
    def is_active(self):
        return bool(self.ativo)


class MotoristaCadastrado(db.Model):
    __tablename__ = 'motoristas_cadastrados'
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    cpf = db.Column(db.String(11), unique=True, nullable=False)
    gabinete = db.Column(db.String(100), nullable=False)  # texto (mantido por compatibilidade)
    validade_cnh = db.Column(db.Date, nullable=True)
    ativo = db.Column(db.Boolean, default=True)
    gabinete_id = db.Column(db.Integer, db.ForeignKey('gabinetes.id'), nullable=True)

    gabinete_obj = db.relationship('Gabinete', back_populates='motoristas')

    @property
    def dias_cnh(self):
        if not self.validade_cnh:
            return None
        return (self.validade_cnh - date.today()).days

    @property
    def gabinete_rotulo(self):
        return self.gabinete_obj.rotulo if self.gabinete_obj else self.gabinete

    @property
    def gabinete_ativo(self):
        return self.gabinete_obj.ativo if self.gabinete_obj else True


# ---------------------------------------------------------------- Frota
class Veiculo(db.Model):
    __tablename__ = 'veiculos'
    id = db.Column(db.Integer, primary_key=True)
    modelo = db.Column(db.String(50), nullable=False)
    placa = db.Column(db.String(10), default='OFICIAL')
    km_atual = db.Column(db.Float, default=0.0)
    km_revisao_proxima = db.Column(db.Integer, default=10000)  # legado, não usado
    qr_token = db.Column(db.String(36), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))
    status = db.Column(db.String(30), default=STATUS_DISPONIVEL)
    proxima_revisao_km = db.Column(db.Float, nullable=True)
    ativo = db.Column(db.Boolean, default=True)

    @property
    def em_manutencao(self):
        return self.status == STATUS_MANUTENCAO


class RegistroUso(db.Model):
    __tablename__ = 'registro_uso'
    id = db.Column(db.Integer, primary_key=True)
    gabinete_vereador = db.Column(db.String(100), nullable=False)  # texto da época da viagem
    motorista_nome = db.Column(db.String(100), nullable=False)
    veiculo_id = db.Column(db.Integer, db.ForeignKey('veiculos.id'), nullable=False)
    data_hora_saida = db.Column(db.DateTime, nullable=False, default=agora)
    km_saida = db.Column(db.Float, nullable=False)
    foto_km_saida = db.Column(db.String(255), nullable=False)
    data_hora_chegada = db.Column(db.DateTime, nullable=True)
    km_chegada = db.Column(db.Float, nullable=True)
    foto_km_chegada = db.Column(db.String(255), nullable=True)
    destino_finalidade = db.Column(db.String(255), nullable=True)
    observacoes = db.Column(db.String(500), nullable=True)
    distancia_km = db.Column(db.Float, nullable=True)
    gabinete_id = db.Column(db.Integer, db.ForeignKey('gabinetes.id'), nullable=True)
    motorista_id = db.Column(db.Integer, db.ForeignKey('motoristas_cadastrados.id'), nullable=True)

    veiculo = db.relationship('Veiculo', backref='registros', lazy=True)
    gabinete_unidade = db.relationship('Gabinete', foreign_keys=[gabinete_id])
    fotos_ocorrencia_multiplas = db.relationship(
        'FotoOcorrencia', back_populates='registro', cascade='all, delete-orphan')

    @property
    def concluida(self):
        return self.km_chegada is not None

    @property
    def km_rodados(self):
        if self.distancia_km is not None:
            return self.distancia_km
        if self.km_chegada is not None:
            return round(self.km_chegada - self.km_saida, 2)
        return None

    @property
    def tem_ocorrencia(self):
        return bool((self.observacoes or '').strip()) or bool(self.fotos_ocorrencia_multiplas)

    @property
    def duracao_texto(self):
        fim = self.data_hora_chegada or datetime.now()
        minutos = max(int((fim - self.data_hora_saida).total_seconds() // 60), 0)
        horas, mins = divmod(minutos, 60)
        dias, horas = divmod(horas, 24)
        return f'{dias}d {horas}h' if dias else f'{horas}h{mins:02d}'

    @property
    def horas_aberta(self):
        return (datetime.now() - self.data_hora_saida).total_seconds() / 3600


class FotoOcorrencia(db.Model):
    __tablename__ = 'foto_ocorrencia'
    id = db.Column(db.Integer, primary_key=True)
    registro_id = db.Column(db.Integer, db.ForeignKey('registro_uso.id'), nullable=False)
    caminho_foto = db.Column(db.String(255), nullable=False)

    registro = db.relationship('RegistroUso', back_populates='fotos_ocorrencia_multiplas')


class EventoVeiculo(db.Model):
    """Abastecimentos, manutenções e multas de cada veículo."""
    __tablename__ = 'eventos_veiculo'
    id = db.Column(db.Integer, primary_key=True)
    veiculo_id = db.Column(db.Integer, db.ForeignKey('veiculos.id'), nullable=False)
    tipo = db.Column(db.String(20), nullable=False)
    data_hora = db.Column(db.DateTime, nullable=False, default=agora)
    km = db.Column(db.Float, nullable=True)
    valor = db.Column(db.Float, default=0.0)
    litros = db.Column(db.Float, nullable=True)
    local = db.Column(db.String(100), nullable=True)
    descricao = db.Column(db.String(300), nullable=True)
    motorista_nome = db.Column(db.String(100), nullable=True)
    pago = db.Column(db.Boolean, default=False)
    criado_por = db.Column(db.String(50), nullable=True)
    criado_em = db.Column(db.DateTime, default=agora)

    veiculo = db.relationship('Veiculo', backref='eventos')


# ---------------------------------------------------------------- Sistema
class LogAuditoria(db.Model):
    __tablename__ = 'log_auditoria'
    id = db.Column(db.Integer, primary_key=True)
    data_hora = db.Column(db.DateTime, default=agora, nullable=False)
    usuario = db.Column(db.String(100))
    acao = db.Column(db.String(50))
    detalhe = db.Column(db.Text)
    ip = db.Column(db.String(60))


class Configuracao(db.Model):
    __tablename__ = 'configuracoes'
    chave = db.Column(db.String(50), primary_key=True)
    valor = db.Column(db.Text)
