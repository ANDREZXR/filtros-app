import os
import re
import json
import base64
import io
from difflib import SequenceMatcher
from PIL import Image
from flask import Flask, request, jsonify, send_from_directory
from groq import Groq

app = Flask(__name__, static_folder="public")
client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

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


@app.route("/")
def index():
    return send_from_directory("public", "index.html")


@app.route("/ler-codigo", methods=["POST"])
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
        resp = client.chat.completions.create(
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
