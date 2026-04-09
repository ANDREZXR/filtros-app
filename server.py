import os
import re
import json
import base64
import io
from functools import wraps
from difflib import SequenceMatcher
from PIL import Image
from flask import Flask, request, jsonify, send_from_directory, session, redirect, url_for, render_template_string
from groq import Groq

app = Flask(__name__, static_folder="public")
app.secret_key = os.environ.get("SECRET_KEY", "adamanto-secret-2026")
groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

LOGIN_USER = os.environ.get("APP_USER", "adamanto.ismo")
LOGIN_PASS = os.environ.get("APP_PASS", "titanomachia")

LOGIN_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0"/>
  <meta name="theme-color" content="#1a1a2e"/>
  <title>ADAMANTO — Login</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Segoe UI',system-ui,sans-serif;background:#1a1a2e;min-height:100dvh;display:flex;align-items:center;justify-content:center}
    .box{background:#fff;border-radius:20px;padding:40px 32px;width:100%;max-width:360px;margin:16px;text-align:center;box-shadow:0 8px 40px rgba(0,0,0,0.4)}
    h1{font-size:1.8rem;color:#1a1a2e;letter-spacing:2px;margin-bottom:6px}
    p{color:#888;font-size:0.85rem;margin-bottom:28px}
    input{width:100%;border:1.5px solid #e2e8f0;border-radius:10px;padding:13px 14px;font-size:1rem;margin-bottom:12px;outline:none;transition:border-color .2s}
    input:focus{border-color:#1a1a2e}
    button{width:100%;background:#1a1a2e;color:#fff;border:none;border-radius:10px;padding:14px;font-size:1rem;font-weight:700;cursor:pointer;margin-top:4px;letter-spacing:1px}
    button:hover{background:#e63946}
    .erro{color:#e63946;font-size:0.85rem;margin-bottom:12px;display:{% if erro %}block{% else %}none{% endif %}}
  </style>
</head>
<body>
  <div class="box">
    <h1>ADAMANTO</h1>
    <p>Sistema de identificação de filtros</p>
    <div class="erro">{{ erro }}</div>
    <form method="POST" action="/login">
      <input type="text" name="usuario" placeholder="Usuário" autocomplete="username" required/>
      <input type="password" name="senha" placeholder="Senha" autocomplete="current-password" required/>
      <button type="submit">ENTRAR</button>
    </form>
  </div>
</body>
</html>"""


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("autenticado"):
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated

with open(os.path.join(os.path.dirname(__file__), "cross_reference.json"), encoding="utf-8") as f:
    DB = json.load(f)

MEDIDAS_PATH = os.path.join(os.path.dirname(__file__), "medidas.json")
with open(MEDIDAS_PATH, encoding="utf-8") as f:
    MEDIDAS = json.load(f)

TIPO_LABEL = {"oleo": "Óleo", "ar": "Ar", "combustivel": "Combustível", "cabine": "Cabine", "hidraulico": "Hidráulico"}


def normalizar(codigo: str) -> str:
    """Remove espaços, traços, underscores e barras; deixa maiúsculo para comparação."""
    return re.sub(r"[\s\-_/]", "", codigo).upper()


def buscar_no_db(marca: str, codigo: str):
    marca_up = marca.upper().strip()
    codigo_norm = normalizar(codigo)
    for tipo, marcas in DB.items():
        for m, codigos in marcas.items():
            if m.upper() == marca_up:
                for cod, equiv in codigos.items():
                    if normalizar(cod) == codigo_norm:
                        equivalentes = [{"marca": k, "codigo": v} for k, v in equiv.items()]
                        return {"tipo_filtro": TIPO_LABEL.get(tipo, "Outro"), "equivalentes": equivalentes}
    return None


def buscar_por_codigo(codigo: str):
    """Busca em todas as marcas pelo código (normalizado) com expansão transitiva."""
    codigo_norm = normalizar(codigo)
    for tipo, marcas in DB.items():
        for m, codigos in marcas.items():
            for cod, equiv in codigos.items():
                if normalizar(cod) == codigo_norm:
                    # Coleta equivalentes diretos
                    equiv_dict = dict(equiv)
                    # Expansão transitiva: busca equivalentes dos equivalentes
                    for marca_eq, cod_eq in list(equiv.items()):
                        if marca_eq in marcas_do_tipo(tipo) and cod_eq in DB[tipo].get(marca_eq, {}):
                            for mk2, cd2 in DB[tipo][marca_eq][cod_eq].items():
                                if mk2 not in equiv_dict and mk2 != m:
                                    equiv_dict[mk2] = cd2
                    equivalentes = [{"marca": k, "codigo": v} for k, v in equiv_dict.items()]
                    return {
                        "marca_original": m,
                        "tipo_filtro": TIPO_LABEL.get(tipo, "Outro"),
                        "equivalentes": equivalentes,
                    }
    return None


def marcas_do_tipo(tipo: str):
    return list(DB.get(tipo, {}).keys())


def sugerir_similares(codigo: str, max_resultados: int = 5):
    """Encontra códigos similares para ajudar quando há erro de OCR."""
    codigo_norm = normalizar(codigo)
    candidatos = []
    for tipo, marcas in DB.items():
        for m, codigos in marcas.items():
            for cod in codigos:
                cod_norm = normalizar(cod)
                # Calcula similaridade de strings
                ratio = SequenceMatcher(None, codigo_norm, cod_norm).ratio()
                # Bonus se começa com as mesmas letras
                if cod_norm.startswith(codigo_norm[:3]):
                    ratio += 0.2
                candidatos.append({
                    "codigo": cod,
                    "marca": m,
                    "tipo_filtro": TIPO_LABEL.get(tipo, "Outro"),
                    "similaridade": round(ratio, 3),
                })
    candidatos.sort(key=lambda x: x["similaridade"], reverse=True)
    # Filtra apenas os com similaridade razoável
    return [c for c in candidatos[:max_resultados] if c["similaridade"] > 0.5]


PROMPT_OCR = """Analise esta foto de um filtro automotivo.

Sua única tarefa é ler o texto impresso no filtro ou na embalagem.

Retorne APENAS este JSON:
{
  "codigo": "código exato como aparece impresso, ex: W712/75 ou PSL204 ou WK853/3",
  "marca": "nome da marca como aparece impresso"
}

Se não conseguir ler com clareza, use null nos campos. Não invente nada."""


@app.route("/login", methods=["GET", "POST"])
def login():
    erro = ""
    if request.method == "POST":
        usuario = request.form.get("usuario", "").strip()
        senha = request.form.get("senha", "").strip()
        if usuario == LOGIN_USER and senha == LOGIN_PASS:
            session["autenticado"] = True
            return redirect("/")
        erro = "Usuário ou senha incorretos."
    return render_template_string(LOGIN_HTML, erro=erro)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/")
@login_required
def index():
    return send_from_directory("public", "index.html")


@app.route("/ler-codigo", methods=["POST"])
@login_required
def ler_codigo():
    """Etapa 1: lê o código da foto via OCR."""
    if "foto" not in request.files:
        return jsonify({"erro": "Nenhuma foto enviada."}), 400

    arquivo = request.files["foto"]
    dados_bytes = arquivo.read()
    if not dados_bytes:
        return jsonify({"erro": "Arquivo vazio."}), 400

    try:
        img = Image.open(io.BytesIO(dados_bytes)).convert("RGB")
        img.thumbnail((1024, 1024), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        b64 = base64.standard_b64encode(buf.getvalue()).decode()
    except Exception as e:
        return jsonify({"erro": "Imagem inválida.", "detalhes": str(e)}), 400

    try:
        b64 = base64.standard_b64encode(buf.getvalue()).decode()
        resp = groq_client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": PROMPT_OCR},
            ]}],
            max_tokens=200,
            temperature=0.0,
        )
        texto = resp.choices[0].message.content or ""
        match = re.search(r"\{[\s\S]*?\}", texto)
        if not match:
            return jsonify({"codigo": None, "marca": None})
        dados = json.loads(match.group())
        return jsonify({
            "codigo": dados.get("codigo") or None,
            "marca":  dados.get("marca")  or None,
        })
    except Exception as e:
        return jsonify({"erro": "Erro ao ler a imagem.", "detalhes": str(e)}), 500


@app.route("/buscar", methods=["POST"])
@login_required
def buscar():
    """Etapa 2: busca no banco de dados pelo código confirmado."""
    body = request.get_json(force=True) or {}
    codigo = (body.get("codigo") or "").strip()
    marca  = (body.get("marca")  or "").strip()

    if not codigo:
        return jsonify({"erro": "Informe o código do filtro."}), 400

    resultado = None
    if marca:
        resultado = buscar_no_db(marca, codigo)
    if not resultado:
        resultado = buscar_por_codigo(codigo)

    if not resultado:
        similares = sugerir_similares(codigo)
        return jsonify({
            "encontrado": False,
            "codigo": codigo,
            "mensagem": f'Código "{codigo}" não encontrado na base de dados.',
            "similares": similares,
        })

    return jsonify({
        "encontrado": True,
        "codigo_original": codigo,
        "marca_original": resultado.get("marca_original", marca or "—"),
        "tipo_filtro": resultado["tipo_filtro"],
        "equivalentes": resultado["equivalentes"],
    })


MEDIDAS_PATH_JSON = os.path.join(os.path.dirname(__file__), "medidas.json")
DB_PATH = os.path.join(os.path.dirname(__file__), "cross_reference.json")

def salvar_db():
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(DB, f, ensure_ascii=False, indent=2)

def salvar_medidas():
    with open(MEDIDAS_PATH_JSON, "w", encoding="utf-8") as f:
        json.dump(MEDIDAS, f, ensure_ascii=False, indent=2)

def _to_float(v):
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None

def medidas_compatíveis(ref: dict, tolerancia_mm: int = 5) -> list:
    """Retorna filtros com medidas físicas próximas às de referência."""
    resultado = []
    ref_altura = _to_float(ref.get("altura_mm"))
    ref_diam   = _to_float(ref.get("diametro_ext_mm"))
    for cod, m in MEDIDAS.items():
        if m.get("tipo") != ref.get("tipo"):
            continue
        if normalizar(cod) == normalizar(ref.get("codigo", "")):
            continue
        score = 0
        if m.get("rosca") and ref.get("rosca") and m["rosca"] == ref["rosca"]:
            score += 3
        m_altura = _to_float(m.get("altura_mm"))
        if m_altura is not None and ref_altura is not None:
            if abs(m_altura - ref_altura) <= tolerancia_mm:
                score += 2
        m_diam = _to_float(m.get("diametro_ext_mm"))
        if m_diam is not None and ref_diam is not None:
            if abs(m_diam - ref_diam) <= tolerancia_mm:
                score += 2
        if score >= 4:
            resultado.append({"codigo": cod, "score": score, **m})
    resultado.sort(key=lambda x: x["score"], reverse=True)
    return resultado

MARCAS_CONHECIDAS = ["MANN","BOSCH","TECFIL","FRAM","MAHLE","WIX","PUROLATOR","HENGST","WEGA","VOX","CHAMPION","ACDELCO","DELPHI","INPECA","NISSAN","VOLKSWAGEN","TOYOTA","GM","UNIFILTER","RACOR","KSPG","NGK","MOTORCRAFT","FILTRON","UFI","NIPPARTS"]
TIPOS = {"oleo":"Óleo","ar":"Ar","combustivel":"Combustível","cabine":"Cabine","hidraulico":"Hidráulico"}

ADMIN_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0"/>
  <title>ADAMANTO — Cadastro</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
    body{font-family:'Segoe UI',system-ui,sans-serif;background:#f0f2f5;min-height:100dvh}
    header{background:#1a1a2e;color:#fff;padding:14px 16px;display:flex;align-items:center;gap:12px;position:sticky;top:0;z-index:10}
    header a{color:#fff;text-decoration:none;font-size:1.3rem}
    header h1{font-size:1rem;font-weight:700;letter-spacing:1px}
    header span{font-size:0.75rem;color:#9ab;margin-left:auto}
    .container{max-width:600px;margin:0 auto;padding:16px 14px 40px}
    .card{background:#fff;border-radius:16px;padding:20px;box-shadow:0 2px 16px rgba(0,0,0,0.07);margin-bottom:16px}
    h2{font-size:1rem;color:#1a1a2e;margin-bottom:16px;font-weight:700}
    label{display:block;font-size:0.75rem;font-weight:700;color:#888;text-transform:uppercase;margin-bottom:5px;margin-top:14px}
    label:first-of-type{margin-top:0}
    select,input[type=text]{width:100%;border:1.5px solid #e2e8f0;border-radius:10px;padding:11px 12px;font-size:0.95rem;outline:none;transition:border-color .2s;background:#fff}
    select:focus,input[type=text]:focus{border-color:#1a1a2e}
    .equiv-row{display:grid;grid-template-columns:1fr 1fr auto;gap:8px;margin-bottom:8px;align-items:center}
    .equiv-row select,.equiv-row input{margin:0}
    .btn-del{background:#fee2e2;border:none;border-radius:8px;color:#e63946;font-size:1.1rem;padding:8px 12px;cursor:pointer;flex-shrink:0}
    .btn-add{background:#f0f2f5;border:1.5px dashed #cbd5e1;border-radius:10px;width:100%;padding:10px;font-size:0.85rem;color:#666;cursor:pointer;margin-top:4px}
    .btn-add:hover{background:#e8f0fe;border-color:#6366f1;color:#6366f1}
    .btn-salvar{background:#1a1a2e;color:#fff;border:none;border-radius:12px;width:100%;padding:15px;font-size:1rem;font-weight:700;cursor:pointer;margin-top:8px;letter-spacing:1px}
    .btn-salvar:hover{background:#e63946}
    .msg-ok{background:#d1fae5;color:#065f46;border-radius:10px;padding:12px 16px;margin-bottom:12px;font-weight:600;display:none}
    .msg-err{background:#fee2e2;color:#991b1b;border-radius:10px;padding:12px 16px;margin-bottom:12px;font-weight:600;display:none}
    .total{font-size:0.8rem;color:#888;text-align:right;margin-bottom:12px}
  </style>
</head>
<body>
<header>
  <a href="/">←</a>
  <h1>ADAMANTO — Cadastro</h1>
  <span id="totalSpan"></span>
</header>
<div class="container">
  <div class="card">
    <h2>Adicionar novo filtro</h2>
    <div class="msg-ok" id="msgOk">✓ Filtro salvo com sucesso!</div>
    <div class="msg-err" id="msgErr"></div>

    <label>Tipo de filtro</label>
    <select id="tipo">
      <option value="oleo">Óleo</option>
      <option value="ar">Ar</option>
      <option value="combustivel">Combustível</option>
      <option value="cabine">Cabine</option>
    </select>

    <label>Marca original</label>
    <select id="marca">
      {% for m in marcas %}
      <option value="{{ m }}">{{ m }}</option>
      {% endfor %}
      <option value="__OUTRA__">Outra marca...</option>
    </select>
    <input type="text" id="marcaCustom" placeholder="Digite a marca" style="display:none;margin-top:8px"/>

    <label>Código original</label>
    <input type="text" id="codigo" placeholder="ex: PSL340" autocomplete="off" autocorrect="off" spellcheck="false"/>

    <label>Equivalentes</label>
    <div id="listaEquiv"></div>
    <div style="display:flex;gap:8px;margin-top:4px">
      <button class="btn-add" style="flex:1" onclick="addEquiv()">+ Adicionar equivalente</button>
      <button class="btn-add" style="flex:1;border-color:#6366f1;color:#6366f1" onclick="document.getElementById('inputArquivo').click()">📂 Importar arquivo</button>
    </div>
    <input type="file" id="inputArquivo" accept=".csv,.txt,.xls,.xlsx" style="display:none" onchange="importarArquivo(this)"/>
    <div id="dicaFormato" style="font-size:0.75rem;color:#888;margin-top:6px;display:none">
      Formato aceito — CSV/TXT com colunas: <b>MARCA,CODIGO</b> (uma por linha)<br>
      Exemplo: <code>MANN,W712/75</code>
    </div>

    <div style="margin-top:18px;border-top:1.5px dashed #e2e8f0;padding-top:16px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px;cursor:pointer" onclick="toggleMedidas()">
        <span style="font-size:0.75rem;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:0.5px">📐 Medidas técnicas</span>
        <span id="seta" style="font-size:0.8rem;color:#888;margin-left:auto">▼ expandir</span>
      </div>
      <div id="secaoMedidas" style="display:none">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
          <div>
            <label>Altura (mm)</label>
            <input type="number" id="altura" placeholder="ex: 93"/>
          </div>
          <div>
            <label>Diâmetro ext. (mm)</label>
            <input type="number" id="diamExt" placeholder="ex: 76"/>
          </div>
          <div>
            <label>Diâmetro int. (mm)</label>
            <input type="number" id="diamInt" placeholder="Opcional"/>
          </div>
          <div>
            <label>Rosca</label>
            <input type="text" id="rosca" placeholder="ex: 3/4-16 UNF" autocomplete="off" spellcheck="false"/>
          </div>
        </div>
        <label>Aplicação / Veículo</label>
        <input type="text" id="aplicacao" placeholder="ex: VW Gol, Ford Ka, Fiat Palio..." autocomplete="off"/>
      </div>
    </div>

    <button class="btn-salvar" onclick="salvar()">💾 Salvar filtro</button>
  </div>

  <div class="card">
    <h2>Filtros cadastrados</h2>
    <div class="total" id="totalCadastros"></div>
    <input type="text" id="filtroTexto" placeholder="Buscar por código ou marca..." oninput="filtrarTabela()" style="margin-bottom:12px"/>
    <div id="tabelaEntradas" style="overflow-x:auto"></div>
  </div>
</div>

<script>
const MARCAS = {{ marcas_json }};

document.getElementById('marca').addEventListener('change', function(){
  document.getElementById('marcaCustom').style.display = this.value === '__OUTRA__' ? 'block' : 'none';
});

function addEquiv(marca='', codigo='') {
  const div = document.createElement('div');
  div.className = 'equiv-row';
  div.innerHTML = `
    <select class="eq-marca">
      ${MARCAS.map(m=>`<option value="${m}" ${m===marca?'selected':''}>${m}</option>`).join('')}
      <option value="__OUTRA__" ${marca&&!MARCAS.includes(marca)?'selected':''}>Outra...</option>
    </select>
    <input type="text" class="eq-cod" placeholder="Código" value="${codigo}" autocomplete="off" spellcheck="false"/>
    <button class="btn-del" onclick="this.parentElement.remove()">✕</button>`;
  document.getElementById('listaEquiv').appendChild(div);
}

function toggleMedidas() {
  const sec = document.getElementById('secaoMedidas');
  const seta = document.getElementById('seta');
  const aberto = sec.style.display !== 'none';
  sec.style.display = aberto ? 'none' : 'block';
  seta.textContent = aberto ? '▼ expandir' : '▲ recolher';
}

async function salvar() {
  const tipo  = document.getElementById('tipo').value;
  const marca = document.getElementById('marca').value === '__OUTRA__'
    ? document.getElementById('marcaCustom').value.trim().toUpperCase()
    : document.getElementById('marca').value;
  const codigo = document.getElementById('codigo').value.trim().toUpperCase();

  if (!marca || !codigo) { mostrarMsg('err','Preencha a marca e o código.'); return; }

  const equiv = {};
  document.querySelectorAll('.equiv-row').forEach(row => {
    let m = row.querySelector('.eq-marca').value;
    const c = row.querySelector('.eq-cod').value.trim().toUpperCase();
    if (m === '__OUTRA__') m = c;
    if (m && c) equiv[m] = c;
  });

  const medidas = {
    altura_mm:       parseFloat(document.getElementById('altura').value)  || null,
    diametro_ext_mm: parseFloat(document.getElementById('diamExt').value) || null,
    diametro_int_mm: parseFloat(document.getElementById('diamInt').value) || null,
    rosca:           document.getElementById('rosca').value.trim().toUpperCase() || null,
    aplicacao:       document.getElementById('aplicacao').value.trim() || null,
  };
  const temMedidas = Object.values(medidas).some(v => v !== null && v !== '');

  const resp = await fetch('/admin/salvar', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({tipo, marca, codigo, equiv, medidas: temMedidas ? medidas : null})
  });
  const res = await resp.json();
  if (res.ok) {
    mostrarMsg('ok', temMedidas ? '✓ Filtro e medidas salvos!' : '✓ Filtro salvo!');
    document.getElementById('codigo').value = '';
    document.getElementById('listaEquiv').innerHTML = '';
    document.getElementById('altura').value = '';
    document.getElementById('diamExt').value = '';
    document.getElementById('diamInt').value = '';
    document.getElementById('rosca').value = '';
    document.getElementById('aplicacao').value = '';
    carregarTabela();
  } else {
    mostrarMsg('err', res.erro || 'Erro ao salvar.');
  }
}

function mostrarMsg(tipo, txt) {
  const el = document.getElementById(tipo === 'ok' ? 'msgOk' : 'msgErr');
  const outro = document.getElementById(tipo === 'ok' ? 'msgErr' : 'msgOk');
  outro.style.display = 'none';
  el.textContent = txt;
  el.style.display = 'block';
  setTimeout(() => el.style.display = 'none', 3000);
}

let todasEntradas = [];

async function carregarTabela() {
  const resp = await fetch('/admin/listar');
  const data = await resp.json();
  todasEntradas = data;
  document.getElementById('totalCadastros').textContent = `${data.length} filtros cadastrados`;
  document.getElementById('totalSpan').textContent = `${data.length} filtros`;
  filtrarTabela();
}

function filtrarTabela() {
  const q = document.getElementById('filtroTexto').value.toLowerCase();
  const filtradas = q ? todasEntradas.filter(e =>
    e.codigo.toLowerCase().includes(q) || e.marca.toLowerCase().includes(q) || e.tipo.toLowerCase().includes(q)
  ) : todasEntradas;

  const html = `<table style="width:100%;border-collapse:collapse;font-size:0.82rem">
    <thead><tr style="background:#f8f9fa">
      <th style="padding:8px;text-align:left;border-bottom:2px solid #e2e8f0">Tipo</th>
      <th style="padding:8px;text-align:left;border-bottom:2px solid #e2e8f0">Marca</th>
      <th style="padding:8px;text-align:left;border-bottom:2px solid #e2e8f0">Código</th>
      <th style="padding:8px;text-align:left;border-bottom:2px solid #e2e8f0">Equivalentes</th>
    </tr></thead>
    <tbody>${filtradas.map(e => `<tr style="border-bottom:1px solid #f2f2f2">
      <td style="padding:8px;color:#888">${e.tipo}</td>
      <td style="padding:8px;font-weight:700">${e.marca}</td>
      <td style="padding:8px;font-family:monospace;color:#e63946;font-weight:700">${e.codigo}</td>
      <td style="padding:8px;color:#555">${Object.entries(e.equiv).map(([m,c])=>`<b>${m}</b> ${c}`).join(' · ')}</td>
    </tr>`).join('')}</tbody>
  </table>`;
  document.getElementById('tabelaEntradas').innerHTML = html;
}

function importarArquivo(input) {
  const file = input.files[0];
  if (!file) return;
  input.value = '';
  document.getElementById('dicaFormato').style.display = 'block';

  const ext = file.name.split('.').pop().toLowerCase();

  if (ext === 'csv' || ext === 'txt') {
    const reader = new FileReader();
    reader.onload = e => processarCSV(e.target.result);
    reader.readAsText(file, 'UTF-8');
  } else if (ext === 'xlsx' || ext === 'xls') {
    const reader = new FileReader();
    reader.onload = e => processarExcel(e.target.result);
    reader.readAsArrayBuffer(file);
  } else {
    alert('Formato não suportado. Use CSV, TXT, XLS ou XLSX.');
  }
}

function processarCSV(texto) {
  const linhas = texto.split(/\\r?\\n/).map(l => l.trim()).filter(l => l && !l.startsWith('#'));
  let adicionados = 0;
  for (const linha of linhas) {
    // Aceita vírgula, ponto-e-vírgula ou tab como separador
    const partes = linha.split(/[,;\\t]/).map(p => p.trim().toUpperCase().replace(/^["']|["']$/g, ''));
    if (partes.length >= 2 && partes[0] && partes[1]) {
      addEquiv(partes[0], partes[1]);
      adicionados++;
    }
  }
  if (adicionados === 0) {
    mostrarMsg('err', 'Nenhuma linha válida encontrada. Formato: MARCA,CODIGO');
  } else {
    mostrarMsg('ok', `✓ ${adicionados} equivalente(s) importado(s)!`);
  }
}

async function processarExcel(buffer) {
  // Leitura manual de XLSX (formato ZIP com XML)
  try {
    // Tenta usar SheetJS se disponível, senão faz leitura básica
    if (typeof XLSX !== 'undefined') {
      const wb = XLSX.read(buffer, {type:'array'});
      const ws = wb.Sheets[wb.SheetNames[0]];
      const rows = XLSX.utils.sheet_to_json(ws, {header:1, raw:false});
      let adicionados = 0;
      for (const row of rows) {
        if (row.length >= 2 && row[0] && row[1]) {
          addEquiv(String(row[0]).trim().toUpperCase(), String(row[1]).trim().toUpperCase());
          adicionados++;
        }
      }
      mostrarMsg('ok', `✓ ${adicionados} equivalente(s) importado(s)!`);
    } else {
      mostrarMsg('err', 'Para Excel, salve como CSV e importe novamente.');
    }
  } catch(e) {
    mostrarMsg('err', 'Erro ao ler Excel. Salve como CSV e tente novamente.');
  }
}

// Carrega SheetJS para suporte a Excel
const s = document.createElement('script');
s.src = 'https://cdn.sheetjs.com/xlsx-0.20.2/package/dist/xlsx.mini.min.js';
document.head.appendChild(s);

addEquiv();
carregarTabela();
</script>
</body>
</html>"""


@app.route("/admin")
@login_required
def admin():
    return render_template_string(ADMIN_HTML,
        marcas=MARCAS_CONHECIDAS,
        marcas_json=json.dumps(MARCAS_CONHECIDAS)
    )


@app.route("/admin/salvar", methods=["POST"])
@login_required
def admin_salvar():
    body = request.get_json(force=True) or {}
    tipo   = body.get("tipo", "").strip().lower()
    marca  = body.get("marca", "").strip().upper()
    codigo = body.get("codigo", "").strip().upper()
    equiv  = {k.upper(): v.upper() for k, v in (body.get("equiv") or {}).items() if k and v}

    if not tipo or not marca or not codigo:
        return jsonify({"ok": False, "erro": "Tipo, marca e código são obrigatórios."})
    if tipo not in DB:
        return jsonify({"ok": False, "erro": f"Tipo inválido: {tipo}"})

    if marca not in DB[tipo]:
        DB[tipo][marca] = {}
    DB[tipo][marca][codigo] = equiv

    # Adiciona entrada reversa para cada equivalente
    for m_eq, c_eq in equiv.items():
        if m_eq not in DB[tipo]:
            DB[tipo][m_eq] = {}
        if c_eq not in DB[tipo][m_eq]:
            DB[tipo][m_eq][c_eq] = {}
        DB[tipo][m_eq][c_eq][marca] = codigo

    salvar_db()

    # Salva medidas se fornecidas
    medidas = body.get("medidas")
    if medidas and any(v for v in medidas.values() if v is not None and v != ""):
        MEDIDAS[codigo] = {
            "tipo":              tipo,
            "marca":             marca,
            "altura_mm":         medidas.get("altura_mm"),
            "diametro_ext_mm":   medidas.get("diametro_ext_mm"),
            "diametro_int_mm":   medidas.get("diametro_int_mm"),
            "rosca":             medidas.get("rosca") or "",
            "aplicacao":         medidas.get("aplicacao") or "",
        }
        salvar_medidas()

    return jsonify({"ok": True})


@app.route("/admin/listar")
@login_required
def admin_listar():
    entradas = []
    for tipo, marcas in DB.items():
        for m, codigos in marcas.items():
            for cod, equiv in codigos.items():
                entradas.append({
                    "tipo": TIPO_LABEL.get(tipo, tipo),
                    "marca": m,
                    "codigo": cod,
                    "equiv": equiv,
                })
    entradas.sort(key=lambda x: (x["tipo"], x["marca"], x["codigo"]))
    return jsonify(entradas)


@app.route("/pesquisa")
@login_required
def pesquisa():
    return send_from_directory("public", "pesquisa.html")


@app.route("/pesquisa/buscar", methods=["POST"])
@login_required
def pesquisa_buscar():
    body = request.get_json(force=True) or {}
    codigo = normalizar(body.get("codigo", "").strip())

    if not codigo:
        return jsonify({"erro": "Informe o código."}), 400

    # 1. Busca exata pelo código
    medidas_ref = None
    for cod, m in MEDIDAS.items():
        if normalizar(cod) == codigo:
            medidas_ref = {"codigo": cod, **m}
            break

    # 1b. Busca por prefixo: ex. "PSD450" encontra "PSD450/1", "PSD450/6"
    if not medidas_ref:
        prefixo_matches = [
            {"codigo": cod, **m}
            for cod, m in MEDIDAS.items()
            if normalizar(cod).startswith(codigo) and len(normalizar(cod)) > len(codigo)
        ]
        if len(prefixo_matches) == 1:
            medidas_ref = prefixo_matches[0]
        elif len(prefixo_matches) > 1:
            # Retorna lista de variantes
            return jsonify({
                "modo": "variantes",
                "codigo": body.get("codigo","").upper(),
                "variantes": prefixo_matches,
            })

    # 2. Busca cross-reference
    equiv_result = buscar_por_codigo(body.get("codigo", "").strip())

    if medidas_ref or equiv_result:
        equiv_list = equiv_result["equivalentes"] if equiv_result else []
        compatíveis_por_medida = medidas_compatíveis(medidas_ref) if medidas_ref else []
        return jsonify({
            "modo": "exato",
            "codigo": medidas_ref["codigo"] if medidas_ref else body.get("codigo","").upper(),
            "marca":  medidas_ref.get("marca") if medidas_ref else (equiv_result or {}).get("marca_original","—"),
            "tipo":   medidas_ref.get("tipo","") if medidas_ref else "",
            "medidas": medidas_ref if medidas_ref else None,
            "equivalentes": equiv_list,
            "compatíveis_medida": compatíveis_por_medida[:10],
        })

    # 3. Não encontrou — busca por medidas se informadas
    return jsonify({"modo": "nao_encontrado", "codigo": body.get("codigo","")})


def _norm_rosca(r):
    """Normaliza rosca para comparação: remove espaços, vírgula→ponto, uppercase."""
    if not r:
        return ""
    r = r.upper().strip()
    r = re.sub(r'\s+', '', r)          # remove todos os espaços
    r = r.replace(',', '.')            # vírgula → ponto
    r = r.replace('"', '')             # remove aspas
    r = re.sub(r'UNS$', 'UN', r)      # UNS → UN
    return r

@app.route("/pesquisa/por-medidas", methods=["POST"])
@login_required
def pesquisa_por_medidas():
    body = request.get_json(force=True) or {}
    tipo        = body.get("tipo", "").lower()
    rosca       = _norm_rosca(body.get("rosca") or "")
    altura      = body.get("altura_mm")
    diam_ext    = body.get("diametro_ext_mm")
    tolerancia  = int(body.get("tolerancia_mm", 5))

    resultados = []
    for cod, m in MEDIDAS.items():
        if tipo and m.get("tipo") != tipo:
            continue
        if rosca and _norm_rosca(m.get("rosca", "")) != rosca:
            continue
        score = 0
        if altura and m.get("altura_mm"):
            if abs(m["altura_mm"] - float(altura)) <= tolerancia:
                score += 2
            else:
                continue
        if diam_ext and m.get("diametro_ext_mm"):
            if abs(m["diametro_ext_mm"] - float(diam_ext)) <= tolerancia:
                score += 2
            else:
                if altura:
                    continue
        score += 1
        equiv = buscar_por_codigo(cod)
        resultados.append({
            "codigo": cod,
            "score": score,
            **m,
            "equivalentes": equiv["equivalentes"] if equiv else [],
        })

    resultados.sort(key=lambda x: x["score"], reverse=True)
    return jsonify({"resultados": resultados[:30]})


@app.route("/pesquisa/salvar-medidas", methods=["POST"])
@login_required
def pesquisa_salvar_medidas():
    body = request.get_json(force=True) or {}
    codigo = body.get("codigo", "").strip().upper()
    if not codigo:
        return jsonify({"ok": False, "erro": "Código obrigatório."})
    MEDIDAS[codigo] = {
        "tipo":              body.get("tipo", "oleo"),
        "marca":             body.get("marca", "").upper(),
        "altura_mm":         body.get("altura_mm"),
        "diametro_ext_mm":   body.get("diametro_ext_mm"),
        "diametro_int_mm":   body.get("diametro_int_mm"),
        "rosca":             body.get("rosca", ""),
        "aplicacao":         body.get("aplicacao", ""),
    }
    salvar_medidas()
    return jsonify({"ok": True})


APLICACOES_PATH = os.path.join(os.path.dirname(__file__), "aplicacoes.json")
try:
    with open(APLICACOES_PATH, encoding="utf-8") as _f:
        APLICACOES = json.load(_f)
except FileNotFoundError:
    APLICACOES = []


def _norm_veiculo(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().upper())


@app.route("/veiculo")
@login_required
def veiculo():
    return send_from_directory("public", "veiculo.html")


@app.route("/veiculo/montadoras")
@login_required
def veiculo_montadoras():
    montadoras = sorted(set(e["montadora"] for e in APLICACOES if e.get("montadora")))
    return jsonify(montadoras)


@app.route("/veiculo/modelos")
@login_required
def veiculo_modelos():
    montadora = _norm_veiculo(request.args.get("montadora", ""))
    modelos = sorted(set(
        e["modelo"] for e in APLICACOES
        if _norm_veiculo(e.get("montadora", "")) == montadora and e.get("modelo")
    ))
    return jsonify(modelos)


@app.route("/veiculo/motores")
@login_required
def veiculo_motores():
    montadora = _norm_veiculo(request.args.get("montadora", ""))
    modelo = _norm_veiculo(request.args.get("modelo", ""))
    motores = sorted(set(
        e["motor"] for e in APLICACOES
        if _norm_veiculo(e.get("montadora", "")) == montadora
        and _norm_veiculo(e.get("modelo", "")) == modelo
        and e.get("motor")
    ))
    return jsonify(motores)


@app.route("/veiculo/buscar")
@login_required
def veiculo_buscar():
    montadora = _norm_veiculo(request.args.get("montadora", ""))
    modelo = _norm_veiculo(request.args.get("modelo", ""))
    motor = _norm_veiculo(request.args.get("motor", ""))

    if not montadora or not modelo:
        return jsonify({"erro": "Informe montadora e modelo."}), 400

    resultados = []
    for e in APLICACOES:
        if _norm_veiculo(e.get("montadora", "")) != montadora:
            continue
        if _norm_veiculo(e.get("modelo", "")) != modelo:
            continue
        if motor and _norm_veiculo(e.get("motor", "")) != motor:
            continue
        resultados.append(e)

    if not resultados:
        return jsonify({"encontrado": False, "mensagem": "Nenhuma aplicação encontrada."})

    # Collect all filter codes from the results
    FILTER_KEYS = [
        ("ar_cabine",      "Ar Cabine"),
        ("ar_cabine_carvao", "Ar Cabine c/ Carvão"),
        ("ar1",            "Ar Motor 1"),
        ("ar2",            "Ar Motor 2"),
        ("oleo1",          "Óleo 1"),
        ("oleo2",          "Óleo 2"),
        ("combustivel1",   "Combustível 1"),
        ("combustivel2",   "Combustível 2"),
        ("cambio",         "Câmbio Automático"),
        ("sedim_blindado", "Sedimentador Blindado"),
        ("sedim_copo",     "Sedimentador c/ Copo"),
        ("sedim_sem_copo", "Sedimentador s/ Copo"),
        ("direcao",        "Direção"),
        ("transmissao",    "Transmissão"),
        ("outros1",        "Outros 1"),
        ("outros2",        "Outros 2"),
        ("outros3",        "Outros 3"),
    ]

    # Tipo fallback inferido pelo campo (para códigos não encontrados em medidas/CR)
    KEY_TIPO_FALLBACK = {
        "ar_cabine": "Cabine", "ar_cabine_carvao": "Cabine",
        "ar1": "Ar", "ar2": "Ar",
        "oleo1": "Óleo", "oleo2": "Óleo",
        "combustivel1": "Combustível", "combustivel2": "Combustível",
        "cambio": "Óleo", "transmissao": "Óleo",
        "sedim_blindado": "Combustível", "sedim_copo": "Combustível", "sedim_sem_copo": "Combustível",
        "direcao": "Hidráulico",
    }

    # Enrich filter codes with cross-reference equivalents and medidas
    def enrich(codigo, field_key=""):
        if not codigo or codigo.strip() in ("", "-", "N/D", "n/d"):
            return None
        equiv = buscar_por_codigo(codigo)
        # Tenta pegar tipo/funcao do medidas.json se cross-reference não tiver
        med = MEDIDAS.get(codigo) or {}
        tipo_filtro = (equiv["tipo_filtro"] if equiv else "") or TIPO_LABEL.get(med.get("tipo",""), "")
        # Fallback: infere pelo campo se ainda desconhecido
        if not tipo_filtro:
            tipo_filtro = KEY_TIPO_FALLBACK.get(field_key, "")
        # Fallback final: infere pelo prefixo do código
        if not tipo_filtro:
            c = codigo.upper()
            if c.startswith(("ACA", "ACP", "AP")):
                tipo_filtro = "Cabine"
            elif c.startswith(("AR", "ARL", "ARS", "ART", "AS", "GI", "AG")):
                tipo_filtro = "Ar"
            elif c.startswith(("PEL", "PEC", "FCA", "FCI", "FBA", "FC", "FI", "FBT")):
                tipo_filtro = "Combustível"
            elif c.startswith(("PSL", "PSC", "PSD", "PH", "PSH", "AP")):
                tipo_filtro = "Óleo"
        return {
            "codigo": codigo,
            "equivalentes": equiv["equivalentes"] if equiv else [],
            "tipo_filtro": tipo_filtro,
        }

    enriched_results = []
    for e in resultados:
        filtros = {}
        for key, label in FILTER_KEYS:
            val = e.get(key, "").strip()
            if val:
                filtros[label] = enrich(val, key)
        enriched_results.append({
            "motor": e.get("motor", ""),
            "ano_de": e.get("ano_de", ""),
            "ate": e.get("ate", ""),
            "descricao": e.get("descricao", ""),
            "combustivel": e.get("combustivel", ""),
            "filtros": filtros,
        })

    return jsonify({
        "encontrado": True,
        "montadora": montadora,
        "modelo": modelo,
        "resultados": enriched_results,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    print(f"Servidor rodando em http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
