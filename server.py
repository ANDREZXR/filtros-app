import os
import re
import json
import base64
import io
from functools import wraps
from difflib import SequenceMatcher
from PIL import Image
from flask import Flask, request, jsonify, send_from_directory, session, redirect, url_for, render_template_string
import google.genai as genai
from google.genai import types

app = Flask(__name__, static_folder="public")
app.secret_key = os.environ.get("SECRET_KEY", "adamanto-secret-2026")
gemini = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

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

TIPO_LABEL = {"oleo": "Óleo", "ar": "Ar", "combustivel": "Combustível", "cabine": "Cabine"}


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
    """Busca em todas as marcas pelo código (normalizado)."""
    codigo_norm = normalizar(codigo)
    for tipo, marcas in DB.items():
        for m, codigos in marcas.items():
            for cod, equiv in codigos.items():
                if normalizar(cod) == codigo_norm:
                    equivalentes = [{"marca": k, "codigo": v} for k, v in equiv.items()]
                    return {
                        "marca_original": m,
                        "tipo_filtro": TIPO_LABEL.get(tipo, "Outro"),
                        "equivalentes": equivalentes,
                    }
    return None


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
        imagem_part = types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg")
        resp = gemini.models.generate_content(
            model="gemini-2.0-flash",
            contents=[imagem_part, PROMPT_OCR],
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=200,
            ),
        )
        texto = resp.text or ""
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    print(f"Servidor rodando em http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
