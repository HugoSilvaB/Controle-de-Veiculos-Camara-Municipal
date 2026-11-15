"""Controle de Frota - Câmara Municipal de Novo Gama."""
import os
import secrets
import uuid
import zipfile
from datetime import date, datetime, timedelta
from io import BytesIO
from urllib.parse import urlparse

from dotenv import load_dotenv
from flask import (Flask, abort, flash, redirect, render_template, request, send_file, send_from_directory,
                   url_for)
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from flask_wtf.csrf import CSRFError, CSRFProtect
from sqlalchemy import and_, func, or_
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import helpers as h
from models import (STATUS_DISPONIVEL, STATUS_MANUTENCAO, TIPOS_EVENTO, TIPOS_GABINETE, AdminUser, EventoVeiculo,
                    FotoOcorrencia, Gabinete, Legislatura, LogAuditoria, MotoristaCadastrado, PresidenteAno,
                    RegistroUso, Veiculo, db)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(BASE_DIR, '.env'))


def _chave_secreta():
    """SECRET_KEY do .env; se não existir, gera uma chave forte e guarda em .secret_key."""
    chave = os.getenv('SECRET_KEY')
    if chave:
        return chave
    arquivo = os.path.join(BASE_DIR, '.secret_key')
    if os.path.exists(arquivo):
        with open(arquivo, encoding='utf-8') as f:
            return f.read().strip()
    chave = secrets.token_hex(32)
    with open(arquivo, 'w', encoding='utf-8') as f:
        f.write(chave)
    return chave


app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SECRET_KEY=_chave_secreta(),
    SQLALCHEMY_DATABASE_URI='sqlite:///database.db',
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    UPLOAD_FOLDER=os.path.join(BASE_DIR, 'uploads'),
    BACKUP_FOLDER=os.path.join(BASE_DIR, 'backups'),
    MAX_CONTENT_LENGTH=40 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    WTF_CSRF_TIME_LIMIT=None,      # o formulário no celular pode ficar aberto por horas
    WTF_CSRF_SSL_STRICT=False,     # atrás do ngrok/proxy o Referer pode não bater
)
UPLOAD = app.config['UPLOAD_FOLDER']
BACKUPS = app.config['BACKUP_FOLDER']
os.makedirs(UPLOAD, exist_ok=True)

db.init_app(app)
csrf = CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login_admin'
login_manager.login_message = 'Faça login para continuar.'
login_manager.login_message_category = 'warning'

LIMITE_LOGIN = h.LimiteTentativas(5, 15 * 60)
LIMITE_CPF = h.LimiteTentativas(10, 10 * 60)


@login_manager.user_loader
def carregar_usuario(user_id):
    usuario = db.session.get(AdminUser, int(user_id))
    return usuario if usuario is not None and usuario.ativo else None


# ------------------------------------------------------------------ Filtros, contexto, erros
def formatar_km(valor):
    if valor is None:
        return '–'
    return f'{valor:,.1f}'.replace(',', 'X').replace('.', ',').replace('X', '.')


@app.template_filter('cpf')
def filtro_cpf(valor):
    return h.formatar_cpf(valor)


@app.template_filter('km')
def filtro_km(valor):
    return formatar_km(valor)


@app.template_filter('moeda')
def filtro_moeda(valor):
    valor = valor or 0
    return 'R$ ' + f'{valor:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')


@app.template_filter('dt')
def filtro_dt(valor):
    return valor.strftime('%d/%m/%Y %H:%M') if valor else '–'


@app.template_filter('d')
def filtro_d(valor):
    return valor.strftime('%d/%m/%Y') if valor else '–'


@app.context_processor
def contexto_global():
    return {'ano_atual': date.today().year, 'hoje': date.today()}


def voltar(padrao, **kw):
    """Volta para a página anterior (só se for do próprio sistema)."""
    ref = request.referrer
    if ref and urlparse(ref).netloc == request.host:
        return redirect(ref)
    return redirect(url_for(padrao, **kw))


@app.errorhandler(CSRFError)
def erro_csrf(_erro):
    flash('O formulário expirou ou é inválido. Atualize a página e tente de novo.', 'warning')
    return voltar('index')


@app.errorhandler(403)
def erro_403(_erro):
    return render_template('erro.html', codigo=403, titulo='Acesso negado',
                           msg='Você não tem permissão para ver esta página.'), 403


@app.errorhandler(404)
def erro_404(_erro):
    return render_template('erro.html', codigo=404, titulo='Página não encontrada',
                           msg='O endereço não existe ou o QR Code foi substituído.'), 404


@app.errorhandler(413)
def erro_413(_erro):
    return render_template('erro.html', codigo=413, titulo='Arquivo muito grande',
                           msg='O envio passou do limite de 40 MB. Tente fotos menores.'), 413


@app.errorhandler(500)
def erro_500(_erro):
    db.session.rollback()
    return render_template('erro.html', codigo=500, titulo='Algo deu errado',
                           msg='O erro foi registrado no terminal. Tente novamente em instantes.'), 500


@app.before_request
def exigir_troca_de_senha():
    if current_user.is_authenticated and current_user.deve_trocar_senha:
        if request.endpoint not in ('alterar_senha', 'logout_admin', 'static'):
            return redirect(url_for('alterar_senha'))
    return None


# ------------------------------------------------------------------ Utilidades de rota
def veiculos_em_rota_ids():
    return {r.veiculo_id for r in RegistroUso.query.filter(RegistroUso.km_chegada.is_(None)).all()}


def finalizar_registro(reg, km_chegada, nome_foto_painel, observacoes, fotos_ocorrencia):
    """Fecha a viagem (usado pelo QR Code e pelo painel do gestor)."""
    reg.km_chegada = km_chegada
    reg.data_hora_chegada = datetime.now()
    reg.distancia_km = round(km_chegada - reg.km_saida, 2)
    reg.observacoes = (observacoes or '').strip()[:500] or None
    if nome_foto_painel:
        reg.foto_km_chegada = nome_foto_painel
    for arquivo in fotos_ocorrencia:
        nome = h.salvar_foto_compacta(arquivo, 'O', UPLOAD)
        if nome:
            db.session.add(FotoOcorrencia(registro_id=reg.id, caminho_foto=nome))
    reg.veiculo.km_atual = km_chegada


# ================================================================== PÚBLICO: QR CODE
@app.route('/')
def index():
    return render_template('index_publico.html')


@app.route('/viagem/<qr_token>', methods=['GET', 'POST'])
def iniciar_viagem_qrcode(qr_token):
    veiculo = Veiculo.query.filter_by(qr_token=qr_token).first_or_404()
    if not veiculo.ativo:
        return render_template('veiculo_indisponivel.html', veiculo=veiculo, motivo='desativado')
    if veiculo.em_manutencao:
        return render_template('veiculo_indisponivel.html', veiculo=veiculo, motivo='manutencao')
    em_uso = RegistroUso.query.filter_by(veiculo_id=veiculo.id, km_chegada=None).first()
    if em_uso:
        return _fim_de_viagem(veiculo, em_uso)
    return _inicio_de_viagem(veiculo)


def _inicio_de_viagem(veiculo):
    if request.method == 'GET':
        return render_template('form_motorista_qrcode.html', veiculo=veiculo, dados={})

    chave = 'cpf:' + h.ip_cliente()
    if LIMITE_CPF.bloqueado(chave):
        flash(f'Muitas tentativas. Aguarde {LIMITE_CPF.minutos_restantes(chave)} minuto(s).', 'danger')
        return render_template('form_motorista_qrcode.html', veiculo=veiculo, dados=request.form), 429

    erro = None
    cpf = h.limpar_cpf(request.form.get('cpf'))
    motorista = MotoristaCadastrado.query.filter_by(cpf=cpf).first() if cpf else None
    km = h.para_float(request.form.get('km_saida'))
    destino = ' '.join((request.form.get('destino_finalidade') or '').split())[:255]
    aberta = None
    if motorista is not None:
        aberta = RegistroUso.query.filter_by(motorista_id=motorista.id, km_chegada=None).first()

    if motorista is None or not motorista.ativo:
        LIMITE_CPF.falhou(chave)
        erro = 'CPF não autorizado. Solicite o cadastro ao gestor.'
    elif not motorista.validade_cnh or motorista.validade_cnh < date.today():
        erro = 'Liberação negada: CNH vencida ou sem validade cadastrada. Procure o gestor.'
    elif not motorista.gabinete_ativo:
        erro = 'Liberação negada: o gabinete/setor deste cadastro está inativo. Procure o gestor.'
    elif aberta is not None:
        erro = f'Você já tem uma viagem em aberto ({aberta.veiculo.modelo}). Encerre-a antes de iniciar outra.'
    elif km is None:
        erro = 'Informe a quilometragem do painel (somente números).'
    elif km < (veiculo.km_atual or 0):
        erro = (f'O KM informado é menor que o último registrado ({formatar_km(veiculo.km_atual)} km). '
                'Confira o painel ou procure o gestor.')
    elif len(destino) < 3:
        erro = 'Informe o destino / finalidade da viagem.'
    elif not request.form.get('termo_responsabilidade'):
        erro = 'É preciso aceitar o termo de uso para iniciar a viagem.'

    nome_foto = None
    if erro is None:
        nome_foto = h.salvar_foto_compacta(request.files.get('foto_km_saida'), 'S', UPLOAD)
        if nome_foto is None:
            erro = 'Envie uma foto válida do painel (JPG ou PNG).'

    if erro:
        flash(erro, 'danger')
        return render_template('form_motorista_qrcode.html', veiculo=veiculo, dados=request.form)

    LIMITE_CPF.limpar(chave)
    registro = RegistroUso(
        gabinete_vereador=motorista.gabinete_rotulo, gabinete_id=motorista.gabinete_id,
        motorista_nome=motorista.nome, motorista_id=motorista.id, veiculo_id=veiculo.id,
        km_saida=km, foto_km_saida=nome_foto, destino_finalidade=destino, data_hora_saida=datetime.now())
    db.session.add(registro)
    h.registrar_log('viagem_inicio', f'{veiculo.modelo} {veiculo.placa} - KM {km:.1f} - {destino}',
                    usuario=f'Motorista: {motorista.nome}')
    db.session.commit()
    return redirect(url_for('viagem_sucesso', qr_token=veiculo.qr_token, tipo='inicio'))


def _fim_de_viagem(veiculo, em_uso):
    primeiro_nome = (em_uso.motorista_nome or '').split(' ')[0]
    if request.method == 'GET':
        return render_template('form_fim_viagem_qrcode.html', veiculo=veiculo, em_uso=em_uso,
                               primeiro_nome=primeiro_nome, dados={})

    chave = 'cpf:' + h.ip_cliente()
    if LIMITE_CPF.bloqueado(chave):
        flash(f'Muitas tentativas. Aguarde {LIMITE_CPF.minutos_restantes(chave)} minuto(s).', 'danger')
        return render_template('form_fim_viagem_qrcode.html', veiculo=veiculo, em_uso=em_uso,
                               primeiro_nome=primeiro_nome, dados=request.form), 429

    if em_uso.motorista_id:
        motorista = db.session.get(MotoristaCadastrado, em_uso.motorista_id)
    else:  # viagens antigas, sem vínculo com o cadastro
        motorista = MotoristaCadastrado.query.filter_by(nome=em_uso.motorista_nome).first()

    erro = None
    cpf = h.limpar_cpf(request.form.get('cpf'))
    km = h.para_float(request.form.get('km_chegada'))
    if motorista is not None and cpf != motorista.cpf:
        LIMITE_CPF.falhou(chave)
        erro = 'O CPF não confere com o motorista desta viagem. Se precisar, o gestor pode encerrá-la no painel.'
    elif km is None:
        erro = 'Informe a quilometragem final do painel (somente números).'
    elif km < em_uso.km_saida:
        erro = f'O KM final não pode ser menor que o de saída ({formatar_km(em_uso.km_saida)} km).'

    nome_foto = None
    if erro is None:
        nome_foto = h.salvar_foto_compacta(request.files.get('foto_km_chegada'), 'C', UPLOAD)
        if nome_foto is None:
            erro = 'Envie uma foto válida do painel (JPG ou PNG).'

    if erro:
        flash(erro, 'danger')
        return render_template('form_fim_viagem_qrcode.html', veiculo=veiculo, em_uso=em_uso,
                               primeiro_nome=primeiro_nome, dados=request.form)

    LIMITE_CPF.limpar(chave)
    finalizar_registro(em_uso, km, nome_foto, request.form.get('observacoes'),
                       request.files.getlist('foto_ocorrencia'))
    h.registrar_log('viagem_fim', f'{veiculo.modelo} {veiculo.placa} - KM {km:.1f} - rodou {em_uso.km_rodados:.1f} km',
                    usuario=f'Motorista: {em_uso.motorista_nome}')
    db.session.commit()
    return redirect(url_for('viagem_sucesso', qr_token=veiculo.qr_token, tipo='fim'))


@app.route('/viagem/<qr_token>/ok/<tipo>')
def viagem_sucesso(qr_token, tipo):
    veiculo = Veiculo.query.filter_by(qr_token=qr_token).first_or_404()
    if tipo == 'inicio':
        return render_template('sucesso_viagem.html', veiculo=veiculo)
    registro = (RegistroUso.query.filter_by(veiculo_id=veiculo.id)
                .filter(RegistroUso.km_chegada.is_not(None))
                .order_by(RegistroUso.data_hora_chegada.desc()).first())
    return render_template('sucesso_fim_viagem.html', veiculo=veiculo, registro=registro)


@app.route('/viagem/<qr_token>/finalizar')
def finalizar_viagem_qrcode(qr_token):
    return redirect(url_for('iniciar_viagem_qrcode', qr_token=qr_token))


# ================================================================== LOGIN / SENHA
@app.route('/admin/login', methods=['GET', 'POST'])
def login_admin():
    if current_user.is_authenticated:
        return redirect(url_for('painel_admin'))
    if request.method == 'POST':
        chave = 'login:' + h.ip_cliente()
        if LIMITE_LOGIN.bloqueado(chave):
            flash(f'Muitas tentativas. Aguarde {LIMITE_LOGIN.minutos_restantes(chave)} minuto(s).', 'danger')
            return render_template('login_admin.html'), 429
        usuario = (request.form.get('usuario') or '').strip()
        senha = request.form.get('senha') or ''
        admin = AdminUser.query.filter_by(usuario=usuario).first()
        if admin and admin.ativo and check_password_hash(admin.senha, senha):
            LIMITE_LOGIN.limpar(chave)
            login_user(admin)
            admin.ultimo_acesso = datetime.now()
            h.registrar_log('login', 'Acesso ao painel', usuario=admin.usuario)
            db.session.commit()
            destino = request.args.get('next', '')
            if destino.startswith('/') and not destino.startswith('//') and '\\' not in destino:
                return redirect(destino)
            return redirect(url_for('painel_admin'))
        LIMITE_LOGIN.falhou(chave)
        h.registrar_log('login_falhou', f'Usuário informado: {usuario[:50]}', usuario='-')
        db.session.commit()
        flash('Usuário ou senha incorretos!', 'danger')
    return render_template('login_admin.html')


@app.route('/admin/logout', methods=['POST'])
@login_required
def logout_admin():
    h.registrar_log('logout')
    db.session.commit()
    logout_user()
    return redirect(url_for('login_admin'))


@app.route('/admin/senha', methods=['GET', 'POST'])
@login_required
def alterar_senha():
    forcado = bool(current_user.deve_trocar_senha)
    if request.method == 'POST':
        atual = request.form.get('senha_atual') or ''
        nova = request.form.get('senha_nova') or ''
        confirma = request.form.get('senha_confirma') or ''
        erro = h.senha_forte(nova)
        if not check_password_hash(current_user.senha, atual):
            flash('A senha atual não confere.', 'danger')
        elif erro:
            flash(erro, 'danger')
        elif nova != confirma:
            flash('A confirmação não é igual à nova senha.', 'danger')
        elif nova == atual:
            flash('A nova senha precisa ser diferente da atual.', 'danger')
        else:
            current_user.senha = generate_password_hash(nova)
            current_user.deve_trocar_senha = False
            h.registrar_log('senha_alterada', 'Troca da própria senha')
            db.session.commit()
            flash('Senha alterada com sucesso!', 'success')
            return redirect(url_for('painel_admin'))
    return render_template('alterar_senha.html', forcado=forcado)


# ================================================================== PAINEL
@app.route('/admin/monitoramento')
@login_required
def painel_admin():
    veiculos = Veiculo.query.filter_by(ativo=True).order_by(Veiculo.modelo).all()
    viagens_em_rota = (RegistroUso.query.filter(RegistroUso.km_chegada.is_(None))
                       .order_by(RegistroUso.data_hora_saida).all())
    ids_em_rota = {v.veiculo_id for v in viagens_em_rota}
    inicio_dia = datetime.combine(date.today(), datetime.min.time())
    inicio_mes = inicio_dia.replace(day=1)
    viagens_hoje = RegistroUso.query.filter(RegistroUso.data_hora_saida >= inicio_dia).count()
    km_mes = (db.session.query(func.coalesce(func.sum(RegistroUso.distancia_km), 0.0))
              .filter(RegistroUso.data_hora_saida >= inicio_mes).scalar())
    disponiveis = sum(1 for v in veiculos if v.id not in ids_em_rota and not v.em_manutencao)
    return render_template('painel_admin.html', veiculos=veiculos, viagens_em_rota=viagens_em_rota,
                           ids_em_rota=ids_em_rota, viagens_hoje=viagens_hoje, km_mes=km_mes or 0.0,
                           disponiveis=disponiveis, alertas=h.gerar_alertas())


@app.route('/admin/encerrar-viagem/<int:id>', methods=['POST'])
@login_required
def encerrar_viagem(id):
    reg = db.get_or_404(RegistroUso, id)
    if reg.km_chegada is not None:
        flash('Esta viagem já foi encerrada.', 'warning')
        return redirect(url_for('painel_admin'))
    km = h.para_float(request.form.get('km_chegada'))
    if km is None:
        flash('KM de chegada inválido.', 'danger')
        return redirect(url_for('painel_admin'))
    if km < reg.km_saida:
        flash(f'O KM de chegada não pode ser menor que o de saída ({formatar_km(reg.km_saida)} km).', 'danger')
        return redirect(url_for('painel_admin'))
    arquivo = request.files.get('foto_km_chegada')
    nome_foto = h.salvar_foto_compacta(arquivo, 'C', UPLOAD)
    if arquivo and arquivo.filename and nome_foto is None:
        flash('A foto do painel não é uma imagem válida.', 'danger')
        return redirect(url_for('painel_admin'))
    finalizar_registro(reg, km, nome_foto, request.form.get('observacoes'), request.files.getlist('foto_ocorrencia'))
    h.registrar_log('viagem_encerrada_gestor', f'{reg.veiculo.modelo} - {reg.motorista_nome} - KM {km:.1f}')
    db.session.commit()
    flash('Viagem encerrada com sucesso!', 'success')
    return redirect(url_for('painel_admin'))


# ================================================================== VEÍCULOS
def _ler_veiculo(atual=None):
    modelo = ' '.join(request.form.get('modelo', '').split())
    placa = request.form.get('placa', '').strip().upper()
    km = h.para_float(request.form.get('km_atual')) if request.form.get('km_atual', '').strip() else 0.0
    revisao_txt = request.form.get('proxima_revisao_km', '').strip()
    revisao = h.para_float(revisao_txt) if revisao_txt else None
    if len(modelo) < 2 or len(modelo) > 50:
        return None, 'Informe o modelo do veículo (até 50 letras).'
    if not h.validar_placa(placa):
        return None, 'Placa inválida. Use o formato ABC-1234 ou ABC1D23.'
    if km is None:
        return None, 'Quilometragem inválida.'
    if revisao_txt and revisao is None:
        return None, 'KM da próxima revisão inválido.'
    outro = Veiculo.query.filter(func.upper(Veiculo.placa) == placa).first()
    if outro and (atual is None or outro.id != atual.id):
        return None, f'Já existe um veículo com a placa {placa}.'
    return {'modelo': modelo, 'placa': placa, 'km_atual': km, 'proxima_revisao_km': revisao}, None


@app.route('/admin/veiculos', methods=['GET', 'POST'])
@login_required
def gestao_veiculos():
    if request.method == 'POST':
        dados, erro = _ler_veiculo()
        if erro:
            flash(erro, 'danger')
        else:
            db.session.add(Veiculo(**dados))
            h.registrar_log('veiculo_criado', f"{dados['modelo']} {dados['placa']}")
            db.session.commit()
            flash('Veículo cadastrado e QR Code gerado!', 'success')
        return redirect(url_for('gestao_veiculos'))
    veiculos = Veiculo.query.order_by(Veiculo.ativo.desc(), Veiculo.modelo).all()
    return render_template('gestao_veiculos.html', veiculos=veiculos, ids_em_rota=veiculos_em_rota_ids())


@app.route('/admin/veiculos/<int:id>/editar', methods=['GET', 'POST'])
@login_required
def veiculo_editar(id):
    veiculo = db.get_or_404(Veiculo, id)
    if request.method == 'POST':
        dados, erro = _ler_veiculo(veiculo)
        if erro:
            flash(erro, 'danger')
        else:
            antes = f'{veiculo.modelo} {veiculo.placa} {veiculo.km_atual} rev={veiculo.proxima_revisao_km}'
            for campo, valor in dados.items():
                setattr(veiculo, campo, valor)
            h.registrar_log('veiculo_editado', f'{antes} -> {veiculo.modelo} {veiculo.placa} '
                                               f'{veiculo.km_atual} rev={veiculo.proxima_revisao_km}')
            db.session.commit()
            flash('Veículo atualizado.', 'success')
            return redirect(url_for('gestao_veiculos'))
    return render_template('veiculo_editar.html', veiculo=veiculo)


@app.route('/admin/veiculo/<int:veiculo_id>/alternar_status', methods=['POST'])
@login_required
def alternar_status_veiculo(veiculo_id):
    veiculo = db.get_or_404(Veiculo, veiculo_id)
    if veiculo.em_manutencao:
        veiculo.status = STATUS_DISPONIVEL
        flash(f'{veiculo.modelo} voltou a ficar Disponível.', 'success')
        h.registrar_log('veiculo_liberado', f'{veiculo.modelo} {veiculo.placa}')
    elif veiculo.id in veiculos_em_rota_ids():
        flash('Este veículo está em viagem. Encerre a viagem antes de enviá-lo para manutenção.', 'warning')
        return voltar('gestao_veiculos')
    else:
        veiculo.status = STATUS_MANUTENCAO
        flash(f'{veiculo.modelo} colocado Em Manutenção (o QR Code fica bloqueado).', 'warning')
        h.registrar_log('veiculo_manutencao', f'{veiculo.modelo} {veiculo.placa}')
    db.session.commit()
    return voltar('gestao_veiculos')


@app.route('/admin/veiculos/<int:id>/ativo', methods=['POST'])
@login_required
def veiculo_ativo(id):
    veiculo = db.get_or_404(Veiculo, id)
    if veiculo.ativo and id in veiculos_em_rota_ids():
        flash('Este veículo está em viagem e não pode ser desativado agora.', 'warning')
        return redirect(url_for('gestao_veiculos'))
    veiculo.ativo = not veiculo.ativo
    h.registrar_log('veiculo_ativado' if veiculo.ativo else 'veiculo_desativado', f'{veiculo.modelo} {veiculo.placa}')
    db.session.commit()
    flash('Veículo reativado.' if veiculo.ativo else 'Veículo desativado (some das listas e o QR Code fica bloqueado).',
          'success')
    return redirect(url_for('gestao_veiculos'))


@app.route('/admin/veiculos/<int:id>/novo-qr', methods=['POST'])
@login_required
def veiculo_novo_qr(id):
    veiculo = db.get_or_404(Veiculo, id)
    veiculo.qr_token = str(uuid.uuid4())
    h.registrar_log('qr_regenerado', f'{veiculo.modelo} {veiculo.placa}')
    db.session.commit()
    flash('Novo QR Code gerado. O adesivo antigo deixou de funcionar: imprima e troque o do veículo.', 'warning')
    return redirect(url_for('imprimir_qr', id=veiculo.id))


@app.route('/admin/veiculos/<int:id>/excluir', methods=['POST'])
@login_required
def veiculo_excluir(id):
    veiculo = db.get_or_404(Veiculo, id)
    if RegistroUso.query.filter_by(veiculo_id=id).count() or EventoVeiculo.query.filter_by(veiculo_id=id).count():
        flash('Este veículo tem histórico e não pode ser excluído. Use "Desativar".', 'warning')
        return redirect(url_for('gestao_veiculos'))
    h.registrar_log('veiculo_excluido', f'{veiculo.modelo} {veiculo.placa}')
    db.session.delete(veiculo)
    db.session.commit()
    flash('Veículo excluído.', 'success')
    return redirect(url_for('gestao_veiculos'))


@app.route('/admin/veiculos/qr/<int:id>')
@login_required
def imprimir_qr(id):
    veiculo = db.get_or_404(Veiculo, id)
    return render_template('imprimir_qr.html', veiculo=veiculo, url_viagem=h.url_viagem(veiculo), qr_local=h.QR_LOCAL)


@app.route('/admin/veiculos/qr/<int:id>.png')
@login_required
def qr_png(id):
    if not h.QR_LOCAL:
        abort(404)
    veiculo = db.get_or_404(Veiculo, id)
    imagem = h.qrcode.make(h.url_viagem(veiculo), box_size=10, border=2)
    buf = BytesIO()
    imagem.save(buf, 'PNG')
    buf.seek(0)
    return send_file(buf, mimetype='image/png', max_age=0)


@app.route('/admin/veiculos/qr-todos')
@login_required
def imprimir_qr_todos():
    veiculos = Veiculo.query.filter_by(ativo=True).order_by(Veiculo.modelo).all()
    itens = [{'veiculo': v, 'url': h.url_viagem(v)} for v in veiculos]
    return render_template('imprimir_qr_todos.html', itens=itens, qr_local=h.QR_LOCAL)


# ================================================================== MOTORISTAS
def _ler_motorista(atual=None):
    nome = ' '.join(request.form.get('nome', '').split())
    cpf = h.limpar_cpf(request.form.get('cpf'))
    gabinete = db.session.get(Gabinete, request.form.get('gabinete_id', type=int) or 0)
    validade = h.parse_data(request.form.get('validade_cnh'))
    if len(nome) < 3:
        return None, 'Informe o nome completo.'
    if not h.cpf_valido(cpf):
        return None, 'CPF inválido. Confira os 11 números.'
    existente = MotoristaCadastrado.query.filter_by(cpf=cpf).first()
    if existente and (atual is None or existente.id != atual.id):
        return None, f'Este CPF já está cadastrado ({existente.nome}).'
    if gabinete is None:
        return None, 'Selecione o gabinete / setor.'
    if not gabinete.ativo and (atual is None or atual.gabinete_id != gabinete.id):
        return None, 'Este gabinete está inativo. Escolha um gabinete ativo.'
    if validade is None:
        return None, 'Informe a validade da CNH.'
    return {'nome': nome, 'cpf': cpf, 'gabinete': gabinete, 'validade_cnh': validade}, None


def _aplicar_motorista(m, dados):
    m.nome = dados['nome']
    m.cpf = dados['cpf']
    m.gabinete_id = dados['gabinete'].id
    m.gabinete = dados['gabinete'].rotulo
    m.validade_cnh = dados['validade_cnh']


@app.route('/admin/motoristas', methods=['GET', 'POST'])
@login_required
def gestao_motoristas():
    if request.method == 'POST':
        dados, erro = _ler_motorista()
        if erro:
            flash(erro, 'danger')
        else:
            novo = MotoristaCadastrado(ativo=True)
            _aplicar_motorista(novo, dados)
            db.session.add(novo)
            h.registrar_log('motorista_criado', f"{dados['nome']} - {dados['gabinete'].rotulo}")
            db.session.commit()
            flash('Motorista cadastrado com sucesso!', 'success')
        return redirect(url_for('gestao_motoristas'))

    q = MotoristaCadastrado.query
    busca = request.args.get('q', '').strip()
    status = request.args.get('status', 'ativos')
    gabinete = request.args.get('gabinete', type=int)
    if busca:
        digitos = h.limpar_cpf(busca)
        condicoes = [MotoristaCadastrado.nome.ilike(f'%{busca}%')]
        if digitos:
            condicoes.append(MotoristaCadastrado.cpf.like(f'%{digitos}%'))
        q = q.filter(or_(*condicoes))
    if status == 'ativos':
        q = q.filter_by(ativo=True)
    elif status == 'inativos':
        q = q.filter_by(ativo=False)
    if gabinete:
        q = q.filter_by(gabinete_id=gabinete)
    motoristas = q.order_by(MotoristaCadastrado.nome).all()
    return render_template('gestao_motoristas.html', motoristas=motoristas, gabinetes=h.gabinetes_ativos(),
                           todos_gabinetes=h.ordenar_gabinetes(Gabinete.query.all()),
                           busca=busca, status=status, filtro_gabinete=gabinete,
                           dias_alerta=h.cfg_int('dias_alerta_cnh'))


@app.route('/admin/motoristas/<int:id>/editar', methods=['GET', 'POST'])
@login_required
def motorista_editar(id):
    motorista = db.get_or_404(MotoristaCadastrado, id)
    if request.method == 'POST':
        dados, erro = _ler_motorista(motorista)
        if erro:
            flash(erro, 'danger')
        else:
            antes = f'{motorista.nome} / {motorista.gabinete_rotulo} / CNH {motorista.validade_cnh}'
            _aplicar_motorista(motorista, dados)
            h.registrar_log('motorista_editado', f"{antes} -> {dados['nome']} / {dados['gabinete'].rotulo} / "
                                                 f"CNH {dados['validade_cnh']}")
            db.session.commit()
            flash('Motorista atualizado.', 'success')
            return redirect(url_for('gestao_motoristas'))
    gabinetes_lista = h.gabinetes_ativos()
    if motorista.gabinete_obj and motorista.gabinete_obj not in gabinetes_lista:
        gabinetes_lista.append(motorista.gabinete_obj)
    return render_template('motorista_editar.html', motorista=motorista, gabinetes=gabinetes_lista)


@app.route('/admin/motoristas/<int:id>/ativo', methods=['POST'])
@login_required
def motorista_ativo(id):
    motorista = db.get_or_404(MotoristaCadastrado, id)
    if motorista.ativo and RegistroUso.query.filter_by(motorista_id=id, km_chegada=None).first():
        flash('Este motorista tem uma viagem em aberto. Encerre-a antes de desativá-lo.', 'warning')
        return voltar('gestao_motoristas')
    motorista.ativo = not motorista.ativo
    h.registrar_log('motorista_ativado' if motorista.ativo else 'motorista_desativado', motorista.nome)
    db.session.commit()
    flash('Motorista reativado.' if motorista.ativo else 'Motorista desativado (não consegue mais iniciar viagens).',
          'success')
    return voltar('gestao_motoristas')


@app.route('/admin/motoristas/<int:id>/excluir', methods=['POST'])
@login_required
def motorista_excluir(id):
    motorista = db.get_or_404(MotoristaCadastrado, id)
    if RegistroUso.query.filter(or_(RegistroUso.motorista_id == id,
                                    RegistroUso.motorista_nome == motorista.nome)).count():
        flash('Este motorista tem viagens no histórico e não pode ser excluído. Use "Desativar".', 'warning')
        return voltar('gestao_motoristas')
    h.registrar_log('motorista_excluido', motorista.nome)
    db.session.delete(motorista)
    db.session.commit()
    flash('Motorista excluído.', 'success')
    return redirect(url_for('gestao_motoristas'))


# ================================================================== GABINETES / LEGISLATURA / MESA
@app.route('/admin/gabinetes')
@login_required
def gabinetes():
    mostrar = request.args.get('mostrar', 'ativos')
    lista = Gabinete.query.all()
    if mostrar != 'todos':
        lista = [g for g in lista if g.ativo]
    contagem = dict(db.session.query(MotoristaCadastrado.gabinete_id, func.count(MotoristaCadastrado.id))
                    .filter(MotoristaCadastrado.ativo.is_(True)).group_by(MotoristaCadastrado.gabinete_id).all())
    legislaturas = Legislatura.query.order_by(Legislatura.inicio.desc()).all()
    leg_ativa = next((x for x in legislaturas if x.ativa), None)
    return render_template('gabinetes.html', gabinetes=h.ordenar_gabinetes(lista), contagem=contagem,
                           legislaturas=legislaturas, leg_ativa=leg_ativa, mostrar=mostrar,
                           ativos=h.ordenar_gabinetes(Gabinete.query.filter_by(ativo=True).all()),
                           tipos=TIPOS_GABINETE, ano_sugerido=date.today().year)


def _ler_gabinete(atual=None):
    tipo = request.form.get('tipo') if atual is None else atual.tipo
    vereador = ' '.join(request.form.get('vereador', '').split())
    nome = ' '.join(request.form.get('nome', '').split())
    leg_id = request.form.get('legislatura_id', type=int)
    if tipo not in TIPOS_GABINETE:
        return None, 'Tipo inválido.'
    if tipo == 'Gabinete':
        if len(vereador) < 3:
            return None, 'Informe o nome do vereador.'
        outro = Gabinete.query.filter(Gabinete.tipo == 'Gabinete', Gabinete.ativo.is_(True),
                                      func.lower(Gabinete.vereador) == vereador.lower()).first()
        if outro and (atual is None or outro.id != atual.id):
            return None, f'Já existe um gabinete ativo para {vereador}.'
        if leg_id and db.session.get(Legislatura, leg_id) is None:
            return None, 'Legislatura inválida.'
        return {'tipo': tipo, 'vereador': vereador, 'nome': None, 'legislatura_id': leg_id}, None
    if tipo == 'Administrativo':
        if len(nome) < 3:
            return None, 'Informe o nome do setor.'
        return {'tipo': tipo, 'vereador': None, 'nome': nome, 'legislatura_id': None}, None
    existente = Gabinete.query.filter_by(tipo='Presidência').first()
    if existente and (atual is None or existente.id != atual.id):
        return None, 'A Presidência já está cadastrada. O presidente de cada ano é definido em "Mesa Diretora".'
    return {'tipo': tipo, 'vereador': None, 'nome': 'Presidência', 'legislatura_id': None}, None


@app.route('/admin/gabinetes/novo', methods=['POST'])
@login_required
def gabinete_novo():
    dados, erro = _ler_gabinete()
    if erro:
        flash(erro, 'danger')
    else:
        if dados['tipo'] == 'Gabinete' and not dados['legislatura_id']:
            ativa = Legislatura.query.filter_by(ativa=True).order_by(Legislatura.inicio.desc()).first()
            dados['legislatura_id'] = ativa.id if ativa else None
        novo = Gabinete(ativo=True, **dados)
        db.session.add(novo)
        db.session.flush()
        h.registrar_log('gabinete_criado', novo.rotulo)
        db.session.commit()
        flash(f'{novo.rotulo} cadastrado.', 'success')
    return redirect(url_for('gabinetes'))


@app.route('/admin/gabinetes/<int:id>/editar', methods=['GET', 'POST'])
@login_required
def gabinete_editar(id):
    gabinete = db.get_or_404(Gabinete, id)
    if request.method == 'POST':
        dados, erro = _ler_gabinete(gabinete)
        if erro:
            flash(erro, 'danger')
        else:
            antes = gabinete.rotulo
            for campo, valor in dados.items():
                setattr(gabinete, campo, valor)
            h.registrar_log('gabinete_editado', f'{antes} -> {gabinete.rotulo}')
            db.session.commit()
            flash('Gabinete atualizado. As viagens antigas continuam com o nome da época.', 'success')
            return redirect(url_for('gabinetes'))
    return render_template('gabinete_editar.html', gabinete=gabinete,
                           legislaturas=Legislatura.query.order_by(Legislatura.inicio.desc()).all())


@app.route('/admin/gabinetes/<int:id>/ativo', methods=['POST'])
@login_required
def gabinete_ativo(id):
    gabinete = db.get_or_404(Gabinete, id)
    gabinete.ativo = not gabinete.ativo
    gabinete.inativado_em = None if gabinete.ativo else date.today()
    afetados = MotoristaCadastrado.query.filter_by(gabinete_id=id, ativo=True).count()
    h.registrar_log('gabinete_ativado' if gabinete.ativo else 'gabinete_inativado', gabinete.rotulo)
    db.session.commit()
    if gabinete.ativo:
        flash(f'{gabinete.rotulo} reativado.', 'success')
    else:
        msg = f'{gabinete.rotulo} inativado.'
        if afetados:
            msg += f' {afetados} motorista(s) ficaram bloqueados até serem remanejados.'
        flash(msg, 'warning' if afetados else 'success')
    return voltar('gabinetes')


@app.route('/admin/gabinetes/<int:id>/excluir', methods=['POST'])
@login_required
def gabinete_excluir(id):
    gabinete = db.get_or_404(Gabinete, id)
    if (MotoristaCadastrado.query.filter_by(gabinete_id=id).count()
            or RegistroUso.query.filter_by(gabinete_id=id).count()):
        flash('Este gabinete tem motoristas ou viagens vinculados. Use "Inativar".', 'warning')
        return redirect(url_for('gabinetes'))
    h.registrar_log('gabinete_excluido', gabinete.rotulo)
    db.session.delete(gabinete)
    db.session.commit()
    flash('Gabinete excluído.', 'success')
    return redirect(url_for('gabinetes'))


@app.route('/admin/gabinetes/<int:id>/remanejar', methods=['POST'])
@login_required
def gabinete_remanejar(id):
    origem = db.get_or_404(Gabinete, id)
    destino = db.session.get(Gabinete, request.form.get('destino_id', type=int) or 0)
    if destino is None or not destino.ativo or destino.id == origem.id:
        flash('Escolha um gabinete de destino ativo e diferente do atual.', 'danger')
        return redirect(url_for('gabinetes'))
    motoristas = MotoristaCadastrado.query.filter_by(gabinete_id=origem.id).all()
    for m in motoristas:
        m.gabinete_id = destino.id
        m.gabinete = destino.rotulo
    h.registrar_log('motoristas_remanejados', f'{len(motoristas)} motorista(s): {origem.rotulo} -> {destino.rotulo}')
    db.session.commit()
    flash(f'{len(motoristas)} motorista(s) remanejado(s) para {destino.rotulo}.', 'success')
    return redirect(url_for('gabinetes'))


@app.route('/admin/legislaturas/nova', methods=['POST'])
@login_required
def legislatura_nova():
    inicio = request.form.get('inicio', type=int)
    fim = request.form.get('fim', type=int) or (inicio + 3 if inicio else None)
    if not inicio or not (2000 <= inicio <= 2100) or not fim or fim < inicio:
        flash('Informe o ano de início e de fim da legislatura (ex.: 2029 a 2032).', 'danger')
        return redirect(url_for('gabinetes'))
    for outra in Legislatura.query.all():
        if not (fim < outra.inicio or inicio > outra.fim):
            flash(f'O período {inicio}–{fim} se sobrepõe à legislatura {outra.rotulo}.', 'danger')
            return redirect(url_for('gabinetes'))
    anteriores = Legislatura.query.filter_by(ativa=True).all()
    ids_anteriores = [x.id for x in anteriores]
    for x in anteriores:
        x.ativa = False
    inativados, bloqueados = 0, 0
    if request.form.get('inativar_anteriores') and ids_anteriores:
        for g in Gabinete.query.filter(Gabinete.tipo == 'Gabinete', Gabinete.ativo.is_(True),
                                       Gabinete.legislatura_id.in_(ids_anteriores)).all():
            g.ativo = False
            g.inativado_em = date.today()
            inativados += 1
            bloqueados += MotoristaCadastrado.query.filter_by(gabinete_id=g.id, ativo=True).count()
    nova = Legislatura(inicio=inicio, fim=fim, ativa=True)
    db.session.add(nova)
    h.registrar_log('legislatura_nova',
                    f'{inicio}–{fim}: {inativados} gabinete(s) inativado(s), {bloqueados} motorista(s) bloqueado(s)')
    db.session.commit()
    flash(f'Legislatura {inicio}–{fim} criada. {inativados} gabinete(s) antigo(s) inativado(s); '
          f'{bloqueados} motorista(s) aguardam remanejamento. Agora cadastre os novos vereadores.', 'success')
    return redirect(url_for('gabinetes'))


@app.route('/admin/mesa', methods=['GET', 'POST'])
@login_required
def mesa_diretora():
    if request.method == 'POST':
        ano = request.form.get('ano', type=int)
        nome = ' '.join(request.form.get('nome', '').split())
        if not ano or not (2000 <= ano <= 2100) or len(nome) < 3:
            flash('Informe o ano e o nome do presidente.', 'danger')
        else:
            registro = PresidenteAno.query.filter_by(ano=ano).first()
            if registro:
                h.registrar_log('presidente_alterado', f'{ano}: {registro.nome} -> {nome}')
                registro.nome = nome
            else:
                db.session.add(PresidenteAno(ano=ano, nome=nome))
                h.registrar_log('presidente_definido', f'{ano}: {nome}')
            db.session.commit()
            flash(f'Presidente de {ano}: {nome}.', 'success')
        return redirect(url_for('mesa_diretora'))
    presidentes = PresidenteAno.query.order_by(PresidenteAno.ano.desc()).all()
    vereadores = sorted({g.vereador for g in Gabinete.query.filter_by(tipo='Gabinete').all() if g.vereador})
    return render_template('mesa_diretora.html', presidentes=presidentes, vereadores=vereadores)


@app.route('/admin/mesa/<int:id>/excluir', methods=['POST'])
@login_required
def mesa_excluir(id):
    registro = db.get_or_404(PresidenteAno, id)
    h.registrar_log('presidente_removido', f'{registro.ano}: {registro.nome}')
    db.session.delete(registro)
    db.session.commit()
    flash('Registro removido.', 'success')
    return redirect(url_for('mesa_diretora'))


# ================================================================== HISTÓRICO / VIAGENS / OCORRÊNCIAS
def _opcoes_filtro():
    return {'veiculos': Veiculo.query.order_by(Veiculo.modelo).all(),
            'gabinetes': h.ordenar_gabinetes(Gabinete.query.all())}


@app.route('/admin/historico')
@login_required
def historico():
    q = h.filtrar_viagens(request.args)
    pagina = request.args.get('pagina', 1, type=int)
    pag = q.paginate(page=pagina, per_page=25, error_out=False)
    total, km = (q.order_by(None).with_entities(func.count(RegistroUso.id),
                                                func.coalesce(func.sum(RegistroUso.distancia_km), 0.0)).one())
    filtros = {k: v for k, v in request.args.items() if k != 'pagina' and v}
    return render_template('historico.html', pag=pag, total=total, km_total=km, filtros=filtros,
                           f=request.args, **_opcoes_filtro())


@app.route('/admin/historico/exportar')
@login_required
def historico_exportar():
    registros = h.filtrar_viagens(request.args).limit(20000).all()
    linhas = []
    for r in registros:
        linhas.append([
            r.data_hora_saida.strftime('%d/%m/%Y %H:%M'),
            r.data_hora_chegada.strftime('%d/%m/%Y %H:%M') if r.data_hora_chegada else 'Em andamento',
            r.veiculo.modelo, r.veiculo.placa, r.motorista_nome, r.gabinete_vereador, r.destino_finalidade or '',
            r.km_saida, r.km_chegada, r.km_rodados, r.duracao_texto if r.concluida else '',
            'Sim' if r.tem_ocorrencia else 'Não', r.observacoes or ''])
    cabecalho = ['Saída', 'Chegada', 'Veículo', 'Placa', 'Motorista', 'Gabinete / Setor', 'Destino', 'KM saída',
                 'KM chegada', 'KM rodados', 'Duração', 'Ocorrência', 'Observações']
    buf = h.montar_xlsx([('Viagens', cabecalho, linhas)])
    h.registrar_log('exportacao', f'Histórico: {len(linhas)} viagens')
    db.session.commit()
    return send_file(buf, as_attachment=True, download_name=f'viagens_{date.today():%Y%m%d}.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/admin/viagem/<int:id>')
@login_required
def viagem_detalhe(id):
    return render_template('viagem_detalhe.html', r=db.get_or_404(RegistroUso, id))


@app.route('/admin/viagem/<int:id>/editar', methods=['POST'])
@login_required
def viagem_editar(id):
    reg = db.get_or_404(RegistroUso, id)
    destino = ' '.join(request.form.get('destino_finalidade', '').split())[:255]
    km_saida = h.para_float(request.form.get('km_saida'))
    km_cheg_txt = request.form.get('km_chegada', '').strip()
    km_chegada = h.para_float(km_cheg_txt) if km_cheg_txt else None
    if km_saida is None:
        flash('KM de saída inválido.', 'danger')
    elif reg.concluida and (km_chegada is None or km_chegada < km_saida):
        flash('O KM de chegada é obrigatório e não pode ser menor que o de saída.', 'danger')
    elif not reg.concluida and km_cheg_txt:
        flash('Para encerrar uma viagem em aberto use o botão "Encerrar" do painel.', 'warning')
    else:
        antes = (f'destino={reg.destino_finalidade!r} saida={reg.km_saida} chegada={reg.km_chegada} '
                 f'obs={reg.observacoes!r}')
        reg.destino_finalidade = destino or None
        reg.km_saida = km_saida
        reg.observacoes = (request.form.get('observacoes', '').strip()[:500]) or None
        if reg.concluida:
            reg.km_chegada = km_chegada
            reg.distancia_km = round(km_chegada - km_saida, 2)
            ultima = (RegistroUso.query.filter_by(veiculo_id=reg.veiculo_id)
                      .filter(RegistroUso.km_chegada.is_not(None))
                      .order_by(RegistroUso.data_hora_saida.desc()).first())
            if ultima is not None and ultima.id == reg.id:
                reg.veiculo.km_atual = km_chegada
        depois = (f'destino={reg.destino_finalidade!r} saida={reg.km_saida} chegada={reg.km_chegada} '
                  f'obs={reg.observacoes!r}')
        h.registrar_log('viagem_corrigida', f'#{reg.id} {reg.veiculo.modelo}: {antes} -> {depois}')
        db.session.commit()
        flash('Dados da viagem corrigidos (a alteração ficou registrada na auditoria).', 'success')
    return redirect(url_for('viagem_detalhe', id=id))


@app.route('/admin/ocorrencias')
@login_required
def ocorrencias():
    q = h.filtrar_viagens(request.args, status_padrao='concluidas')
    tem_obs = and_(RegistroUso.observacoes.is_not(None), RegistroUso.observacoes != '')
    q = q.filter(or_(tem_obs, RegistroUso.fotos_ocorrencia_multiplas.any()))
    pagina = request.args.get('pagina', 1, type=int)
    pag = q.paginate(page=pagina, per_page=12, error_out=False)
    filtros = {k: v for k, v in request.args.items() if k != 'pagina' and v}
    return render_template('ocorrencias.html', pag=pag, filtros=filtros, f=request.args, **_opcoes_filtro())


# ================================================================== RELATÓRIOS
def _dados_relatorio():
    de, ate, rotulo = h.resolver_periodo(request.args)
    return h.compilar_relatorio(de, ate), rotulo


@app.route('/admin/relatorios')
@login_required
def relatorios_frotas():
    rel, rotulo = _dados_relatorio()
    primeira = db.session.query(func.min(RegistroUso.data_hora_saida)).scalar()
    anos = list(range(date.today().year, (primeira.year if primeira else date.today().year) - 1, -1))
    meses = [f"{m['nome'][5:]}/{m['nome'][:4]}" for m in rel['por_mes']]
    graficos = {
        'meses': meses,
        'viagens_mes': [m['viagens'] for m in rel['por_mes']],
        'km_mes': [m['km'] for m in rel['por_mes']],
        'gab_nomes': [g['nome'] for g in rel['por_gabinete'][:12]],
        'gab_km': [g['km'] for g in rel['por_gabinete'][:12]],
        'vei_nomes': [v['nome'] for v in rel['por_veiculo'][:12]],
        'vei_km': [v['km'] for v in rel['por_veiculo'][:12]],
        'custo_rotulos': list(rel['custos'].keys()),
        'custo_valores': list(rel['custos'].values()),
    }
    return render_template('relatorios.html', rel=rel, rotulo=rotulo, graficos=graficos, anos=anos, f=request.args,
                           legislaturas=Legislatura.query.order_by(Legislatura.inicio.desc()).all(),
                           filtros={k: v for k, v in request.args.items() if v})


@app.route('/admin/relatorios/exportar')
@login_required
def relatorios_exportar():
    rel, rotulo = _dados_relatorio()
    resumo = [['Período', rotulo], ['Viagens concluídas', rel['viagens']], ['KM rodados', rel['km_total']],
              ['KM médio por viagem', rel['km_medio']], ['Viagens em andamento', rel['em_andamento']],
              ['Viagens com ocorrência', rel['ocorrencias']], ['Abastecimento (R$)', rel['custos']['Abastecimento']],
              ['Manutenção (R$)', rel['custos']['Manutenção']], ['Multas (R$)', rel['custos']['Multa']],
              ['Custo total (R$)', rel['custo_total']]]

    def tabela(lista, extra=False):
        cab = ['Nome', 'Viagens', 'KM'] + (['Custos (R$)'] if extra else [])
        return cab, [[i['nome'], i['viagens'], i['km']] + ([i.get('custo', 0)] if extra else []) for i in lista]

    abas = [('Resumo', ['Indicador', 'Valor'], resumo)]
    for titulo, lista, extra in (('Por gabinete', rel['por_gabinete'], False), ('Por mês', rel['por_mes'], False),
                                 ('Por veículo', rel['por_veiculo'], True),
                                 ('Por motorista', rel['por_motorista'], False)):
        cab, linhas = tabela(lista, extra)
        abas.append((titulo, cab, linhas))
    abas.append(('Viagens', ['Saída', 'Veículo', 'Motorista', 'Gabinete', 'Destino', 'KM rodados'],
                 [[r.data_hora_saida.strftime('%d/%m/%Y %H:%M'), f'{r.veiculo.modelo} ({r.veiculo.placa})',
                   r.motorista_nome, r.gabinete_vereador, r.destino_finalidade or '', r.km_rodados]
                  for r in rel['registros']]))
    h.registrar_log('exportacao', f'Relatório consolidado - {rotulo}')
    db.session.commit()
    return send_file(h.montar_xlsx(abas), as_attachment=True,
                     download_name=f'relatorio_frota_{date.today():%Y%m%d}.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ================================================================== CUSTOS (abastecimento, manutenção, multas)
def _ler_evento():
    veiculo = db.session.get(Veiculo, request.form.get('veiculo_id', type=int) or 0)
    tipo = request.form.get('tipo')
    data_hora = h.parse_datahora(request.form.get('data_hora'))
    valor_txt = request.form.get('valor', '').strip()
    valor = h.para_float(valor_txt) if valor_txt else 0.0
    litros_txt = request.form.get('litros', '').strip()
    litros = h.para_float(litros_txt) if litros_txt else None
    km_txt = request.form.get('km', '').strip()
    km = h.para_float(km_txt) if km_txt else None
    revisao_txt = request.form.get('proxima_revisao_km', '').strip()
    revisao = h.para_float(revisao_txt) if revisao_txt else None
    if veiculo is None:
        return None, 'Selecione o veículo.'
    if tipo not in TIPOS_EVENTO:
        return None, 'Selecione o tipo de lançamento.'
    if data_hora is None:
        return None, 'Informe a data e a hora.'
    if valor is None or (litros_txt and litros is None) or (km_txt and km is None) or (revisao_txt and revisao is None):
        return None, 'Confira os números informados (valor, litros, KM).'
    motorista = ' '.join(request.form.get('motorista_nome', '').split())[:100] or None
    if tipo == 'Multa' and not motorista:  # tenta descobrir quem estava com o carro
        viagem = (RegistroUso.query.filter(RegistroUso.veiculo_id == veiculo.id,
                                           RegistroUso.data_hora_saida <= data_hora,
                                           or_(RegistroUso.data_hora_chegada >= data_hora,
                                               RegistroUso.data_hora_chegada.is_(None)))
                  .order_by(RegistroUso.data_hora_saida.desc()).first())
        motorista = viagem.motorista_nome if viagem else None
    return {'veiculo': veiculo, 'tipo': tipo, 'data_hora': data_hora, 'valor': valor, 'litros': litros, 'km': km,
            'local': ' '.join(request.form.get('local', '').split())[:100] or None,
            'descricao': ' '.join(request.form.get('descricao', '').split())[:300] or None,
            'motorista_nome': motorista, 'pago': bool(request.form.get('pago')), 'revisao': revisao}, None


def _aplicar_evento(ev, dados):
    for campo in ('tipo', 'data_hora', 'valor', 'litros', 'km', 'local', 'descricao', 'motorista_nome', 'pago'):
        setattr(ev, campo, dados[campo])
    ev.veiculo_id = dados['veiculo'].id
    if dados['tipo'] == 'Manutenção' and dados['revisao']:
        dados['veiculo'].proxima_revisao_km = dados['revisao']


@app.route('/admin/custos', methods=['GET', 'POST'])
@login_required
def custos():
    if request.method == 'POST':
        dados, erro = _ler_evento()
        if erro:
            flash(erro, 'danger')
        else:
            ev = EventoVeiculo(criado_por=current_user.usuario)
            _aplicar_evento(ev, dados)
            db.session.add(ev)
            h.registrar_log('custo_lancado', f"{dados['tipo']} {dados['veiculo'].modelo} R$ {dados['valor']:.2f}")
            db.session.commit()
            flash('Lançamento salvo.', 'success')
        return redirect(url_for('custos'))

    q = EventoVeiculo.query
    tipo = request.args.get('tipo', '')
    veiculo = request.args.get('veiculo', type=int)
    de, ate = h.parse_data(request.args.get('de')), h.parse_data(request.args.get('ate'))
    ini = datetime.combine(de, datetime.min.time()) if de else None
    fim = datetime.combine(ate + timedelta(days=1), datetime.min.time()) if ate else None
    if tipo in TIPOS_EVENTO:
        q = q.filter(EventoVeiculo.tipo == tipo)
    if veiculo:
        q = q.filter(EventoVeiculo.veiculo_id == veiculo)
    if ini:
        q = q.filter(EventoVeiculo.data_hora >= ini)
    if fim:
        q = q.filter(EventoVeiculo.data_hora < fim)
    q = q.order_by(EventoVeiculo.data_hora.desc())
    pag = q.paginate(page=request.args.get('pagina', 1, type=int), per_page=25, error_out=False)

    por_veiculo, totais = {}, {t: 0.0 for t in TIPOS_EVENTO}
    for e in q.all():
        item = por_veiculo.setdefault(e.veiculo_id, {'veiculo': e.veiculo, 'total': 0.0, 'litros': 0.0,
                                                     **{t: 0.0 for t in TIPOS_EVENTO}})
        item[e.tipo] += e.valor or 0.0
        item['total'] += e.valor or 0.0
        item['litros'] += e.litros or 0.0
        totais[e.tipo] += e.valor or 0.0
    qv = db.session.query(RegistroUso.veiculo_id, func.coalesce(func.sum(RegistroUso.distancia_km), 0.0)).filter(
        RegistroUso.km_chegada.is_not(None))
    if ini:
        qv = qv.filter(RegistroUso.data_hora_saida >= ini)
    if fim:
        qv = qv.filter(RegistroUso.data_hora_saida < fim)
    km_por_veiculo = dict(qv.group_by(RegistroUso.veiculo_id).all())
    for vid, item in por_veiculo.items():
        km = km_por_veiculo.get(vid) or 0.0
        item['km'] = km
        item['custo_km'] = (item['total'] / km) if km else None
    resumo = sorted(por_veiculo.values(), key=lambda i: i['total'], reverse=True)
    filtros = {k: v for k, v in request.args.items() if k != 'pagina' and v}
    return render_template('custos.html', pag=pag, resumo=resumo, totais=totais, total_geral=sum(totais.values()),
                           veiculos=Veiculo.query.order_by(Veiculo.modelo).all(), tipos=TIPOS_EVENTO,
                           filtros=filtros, f=request.args, agora_input=datetime.now().strftime('%Y-%m-%dT%H:%M'))


@app.route('/admin/custos/<int:id>/editar', methods=['GET', 'POST'])
@login_required
def custo_editar(id):
    ev = db.get_or_404(EventoVeiculo, id)
    if request.method == 'POST':
        dados, erro = _ler_evento()
        if erro:
            flash(erro, 'danger')
        else:
            _aplicar_evento(ev, dados)
            h.registrar_log('custo_editado',
                            f"#{ev.id} {dados['tipo']} {dados['veiculo'].modelo} R$ {dados['valor']:.2f}")
            db.session.commit()
            flash('Lançamento atualizado.', 'success')
            return redirect(url_for('custos'))
    return render_template('custo_editar.html', ev=ev, veiculos=Veiculo.query.order_by(Veiculo.modelo).all(),
                           tipos=TIPOS_EVENTO)


@app.route('/admin/custos/<int:id>/excluir', methods=['POST'])
@login_required
def custo_excluir(id):
    ev = db.get_or_404(EventoVeiculo, id)
    h.registrar_log('custo_excluido', f'#{ev.id} {ev.tipo} {ev.veiculo.modelo} R$ {(ev.valor or 0):.2f}')
    db.session.delete(ev)
    db.session.commit()
    flash('Lançamento excluído.', 'success')
    return voltar('custos')


@app.route('/admin/custos/<int:id>/pago', methods=['POST'])
@login_required
def custo_pago(id):
    ev = db.get_or_404(EventoVeiculo, id)
    ev.pago = not ev.pago
    h.registrar_log('multa_paga' if ev.pago else 'multa_em_aberto', f'#{ev.id} {ev.veiculo.modelo}')
    db.session.commit()
    return voltar('custos')


# ================================================================== SISTEMA
@app.route('/admin/administradores', methods=['GET', 'POST'])
@login_required
def administradores():
    if request.method == 'POST':
        nome = ' '.join(request.form.get('nome', '').split())
        usuario = request.form.get('usuario', '').strip().lower()
        senha = request.form.get('senha', '')
        erro = h.senha_forte(senha)
        if len(nome) < 3 or len(usuario) < 3 or ' ' in usuario:
            flash('Informe o nome e um usuário (sem espaços, mínimo 3 letras).', 'danger')
        elif AdminUser.query.filter_by(usuario=usuario).first():
            flash('Já existe um administrador com este usuário.', 'danger')
        elif erro:
            flash(erro, 'danger')
        else:
            db.session.add(AdminUser(nome=nome, usuario=usuario, senha=generate_password_hash(senha), ativo=True,
                                     deve_trocar_senha=True))
            h.registrar_log('admin_criado', f'{nome} ({usuario})')
            db.session.commit()
            flash(f'Administrador {nome} criado. Ele deverá trocar a senha no primeiro acesso.', 'success')
        return redirect(url_for('administradores'))
    return render_template('administradores.html', admins=AdminUser.query.order_by(AdminUser.nome).all())


def _admin_ou_aviso(id):
    admin = db.get_or_404(AdminUser, id)
    if admin.id == current_user.id:
        flash('Você não pode fazer isso com a sua própria conta.', 'warning')
        return None
    return admin


@app.route('/admin/administradores/<int:id>/ativo', methods=['POST'])
@login_required
def administrador_ativo(id):
    admin = _admin_ou_aviso(id)
    if admin is None:
        return redirect(url_for('administradores'))
    if admin.ativo and AdminUser.query.filter_by(ativo=True).count() <= 1:
        flash('É preciso manter pelo menos um administrador ativo.', 'warning')
        return redirect(url_for('administradores'))
    admin.ativo = not admin.ativo
    h.registrar_log('admin_ativado' if admin.ativo else 'admin_desativado', admin.usuario)
    db.session.commit()
    flash('Administrador reativado.' if admin.ativo else 'Administrador desativado.', 'success')
    return redirect(url_for('administradores'))


@app.route('/admin/administradores/<int:id>/senha', methods=['POST'])
@login_required
def administrador_senha(id):
    admin = db.get_or_404(AdminUser, id)
    provisoria = h.senha_provisoria()
    admin.senha = generate_password_hash(provisoria)
    admin.deve_trocar_senha = True
    h.registrar_log('admin_senha_resetada', admin.usuario)
    db.session.commit()
    flash(f'Senha provisória de {admin.usuario}: {provisoria}  (anote e entregue a ele; será exigida a troca no '
          'primeiro acesso. Esta mensagem não aparece de novo.)', 'warning')
    return redirect(url_for('administradores'))


@app.route('/admin/administradores/<int:id>/excluir', methods=['POST'])
@login_required
def administrador_excluir(id):
    admin = _admin_ou_aviso(id)
    if admin is None:
        return redirect(url_for('administradores'))
    h.registrar_log('admin_excluido', f'{admin.nome} ({admin.usuario})')
    db.session.delete(admin)
    db.session.commit()
    flash('Administrador excluído.', 'success')
    return redirect(url_for('administradores'))


@app.route('/admin/auditoria')
@login_required
def auditoria():
    q = LogAuditoria.query
    usuario = request.args.get('usuario', '').strip()
    acao = request.args.get('acao', '').strip()
    de, ate = h.parse_data(request.args.get('de')), h.parse_data(request.args.get('ate'))
    if usuario:
        q = q.filter(LogAuditoria.usuario.ilike(f'%{usuario}%'))
    if acao:
        q = q.filter(or_(LogAuditoria.acao.ilike(f'%{acao}%'), LogAuditoria.detalhe.ilike(f'%{acao}%')))
    if de:
        q = q.filter(LogAuditoria.data_hora >= datetime.combine(de, datetime.min.time()))
    if ate:
        q = q.filter(LogAuditoria.data_hora < datetime.combine(ate + timedelta(days=1), datetime.min.time()))
    pag = q.order_by(LogAuditoria.id.desc()).paginate(page=request.args.get('pagina', 1, type=int), per_page=40,
                                                      error_out=False)
    return render_template('auditoria.html', pag=pag, f=request.args,
                           filtros={k: v for k, v in request.args.items() if k != 'pagina' and v})


@app.route('/admin/configuracoes', methods=['GET', 'POST'])
@login_required
def configuracoes():
    if request.method == 'POST':
        base = request.form.get('base_url', '').strip()
        if not base.lower().startswith(('http://', 'https://')) or ' ' in base:
            flash('O endereço do sistema precisa começar com http:// ou https://', 'danger')
            return redirect(url_for('configuracoes'))
        numeros = {}
        for chave in ('dias_alerta_cnh', 'km_alerta_revisao', 'horas_viagem_aberta'):
            valor = request.form.get(chave, type=int)
            if valor is None or valor < 0 or valor > 100000:
                flash('Os alertas precisam ser números inteiros positivos.', 'danger')
                return redirect(url_for('configuracoes'))
            numeros[chave] = valor
        h.definir_cfg('base_url', base.rstrip('/'))
        for chave, valor in numeros.items():
            h.definir_cfg(chave, valor)
        h.registrar_log('configuracoes_alteradas', f'base_url={base}')
        db.session.commit()
        flash('Configurações salvas.', 'success')
        return redirect(url_for('configuracoes'))
    valores = {k: h.cfg(k) for k in h.CONFIG_PADRAO}
    return render_template('configuracoes.html', v=valores, exemplo=h.cfg('base_url').rstrip('/') + '/viagem/xxxx')


@app.route('/admin/backup')
@login_required
def backup():
    return render_template('backup.html', backups=h.listar_backups(BACKUPS), banco=h.caminho_banco(),
                           total_fotos=len(os.listdir(UPLOAD)) if os.path.isdir(UPLOAD) else 0)


@app.route('/admin/backup/gerar', methods=['POST'])
@login_required
def backup_gerar():
    nome = h.fazer_backup(BACKUPS)
    h.registrar_log('backup_gerado', nome)
    db.session.commit()
    flash(f'Backup criado: {nome}', 'success')
    return redirect(url_for('backup'))


@app.route('/admin/backup/baixar')
@login_required
def backup_baixar():
    tipo = request.args.get('tipo', 'banco')
    if tipo == 'arquivo':
        nome = os.path.basename(request.args.get('nome', ''))
        if not (nome.startswith('database_') and nome.endswith('.db')):
            abort(404)
        return send_from_directory(BACKUPS, nome, as_attachment=True)
    nome = h.fazer_backup(BACKUPS)
    h.registrar_log('backup_baixado', f'{tipo}: {nome}')
    db.session.commit()
    if tipo == 'banco':
        return send_from_directory(BACKUPS, nome, as_attachment=True)
    buf = BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.write(os.path.join(BACKUPS, nome), 'database.db')
        for arquivo in os.listdir(UPLOAD):
            caminho = os.path.join(UPLOAD, arquivo)
            if os.path.isfile(caminho):
                z.write(caminho, f'uploads/{arquivo}')
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f'backup_completo_{datetime.now():%Y%m%d_%H%M}.zip',
                     mimetype='application/zip')


# ================================================================== ARQUIVOS ENVIADOS (só para logados)
@app.route('/uploads/<path:filename>')
@login_required
def uploaded_file(filename):
    return send_from_directory(UPLOAD, filename, as_attachment=bool(request.args.get('baixar')))


# ================================================================== INICIALIZAÇÃO
with app.app_context():
    h.preparar_banco()
    if os.getenv('DEMO', '0').lower() in ('1', 'true', 'sim'):
        import demo
        demo.semear(UPLOAD)


if __name__ == '__main__':
    h.iniciar_backup_automatico(app, BACKUPS)
    host = os.getenv('HOST', '0.0.0.0')
    porta = int(os.getenv('PORT', '5000'))
    if os.getenv('FLASK_DEBUG', '0').lower() in ('1', 'true', 'sim'):
        app.run(host=host, port=porta, debug=True, use_reloader=False)
    else:
        try:
            from waitress import serve
            print(f'Sistema no ar: http://localhost:{porta}  (Ctrl+C para parar)')
            serve(app, host=host, port=porta, threads=8)
        except ImportError:
            print('Dica: instale o waitress (pip install waitress) para um servidor mais robusto.')
            app.run(host=host, port=porta, debug=False)
