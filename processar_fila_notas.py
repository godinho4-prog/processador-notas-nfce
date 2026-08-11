from pathlib import Path
from datetime import datetime, timedelta, timezone
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.request

import firebase_admin
from firebase_admin import credentials, firestore
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from importar_notas_csv import (
    abrir_nota_como_celular,
    processar_texto_da_nota,
)


ARQUIVO_CHAVE = Path("serviceAccountKey.json")
PASTA_DEBUG = Path("debug_fila_notas")

COLECAO_FILA = "filaNotas"
COLECAO_NOTAS = "notasCapturadas"
MINUTOS_PROCESSAMENTO_TRAVADO = 30

MAX_TENTATIVAS_SEFAZ = 3
SEGUNDOS_ENTRE_TENTATIVAS = 5


def numero_br_para_float(valor):
    texto = str(valor or "").strip()

    if not texto:
        return None

    texto = texto.replace(".", "").replace(",", ".")

    try:
        return float(texto)
    except ValueError:
        return None


def normalizar_texto_busca(texto):
    return (
        unicodedata.normalize("NFD", str(texto or ""))
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )


def texto_busca_compacto(texto):
    return re.sub(
        r"\s+",
        " ",
        normalizar_texto_busca(texto),
    ).strip()


def pagina_tem_sinais_nfce(texto):
    texto_normalizado = texto_busca_compacto(texto)

    marcadores = (
        "cnpj:",
        "chave de acesso",
        "valor a pagar",
        "qtd. total de itens",
        "emissao:",
        "numero:",
        "serie:",
    )

    quantidade = sum(
        marcador in texto_normalizado
        for marcador in marcadores
    )

    return quantidade >= 2


class ErroTemporarioSefaz(RuntimeError):
    pass


def diagnosticar_resposta_sefaz(texto):
    texto_normalizado = texto_busca_compacto(texto)

    if not texto_normalizado:
        raise ErroTemporarioSefaz(
            "A SEFAZ-RJ respondeu sem conteúdo legível."
        )

    if (
        "servico de seguranca da informacao bloqueia acessos"
        in texto_normalizado
        or (
            "enderecos ip"
            in texto_normalizado
            and "mensagem de bloqueio"
            in texto_normalizado
        )
    ):
        raise RuntimeError(
            "A SEFAZ-RJ bloqueou este acesso pela camada "
            "de segurança do serviço."
        )

    erros_rede = {
        "err_connection_refused":
            "A conexão com a SEFAZ-RJ foi recusada.",
        "err_timed_out":
            "A conexão com a SEFAZ-RJ expirou.",
        "err_name_not_resolved":
            "Não foi possível localizar o endereço da SEFAZ-RJ.",
        "err_network_changed":
            "A conexão de rede mudou durante o acesso à SEFAZ-RJ.",
        "err_internet_disconnected":
            "O computador ficou sem conexão com a internet.",
    }

    for codigo, mensagem in erros_rede.items():
        if codigo in texto_normalizado:
            raise ErroTemporarioSefaz(mensagem)

    if (
        "service unavailable" in texto_normalizado
        or "503 service unavailable" in texto_normalizado
    ):
        raise ErroTemporarioSefaz(
            "A SEFAZ-RJ está temporariamente indisponível."
        )


def abrir_nota_com_retentativas(
    pagina,
    link,
    chave,
):
    texto = ""
    html = ""

    for tentativa in range(
        1,
        MAX_TENTATIVAS_SEFAZ + 1,
    ):
        print(
            "Abrindo a SEFAZ-RJ..."
            if tentativa == 1
            else (
                "Tentando novamente a SEFAZ-RJ "
                f"({tentativa}/{MAX_TENTATIVAS_SEFAZ})..."
            )
        )

        try:
            try:
                texto, html = abrir_nota_como_celular(
                    pagina,
                    link,
                )
            except PlaywrightTimeoutError as erro:
                raise ErroTemporarioSefaz(
                    "A SEFAZ-RJ não respondeu dentro "
                    "do tempo esperado."
                ) from erro

            salvar_debug(
                chave,
                texto,
                html,
            )

            diagnosticar_resposta_sefaz(
                texto
            )

            return texto, html

        except ErroTemporarioSefaz as erro:
            if tentativa >= MAX_TENTATIVAS_SEFAZ:
                raise

            print(
                "Problema temporário: "
                f"{erro}"
            )
            print(
                "Nova tentativa em "
                f"{SEGUNDOS_ENTRE_TENTATIVAS} segundos..."
            )

            time.sleep(
                SEGUNDOS_ENTRE_TENTATIVAS
            )

    raise RuntimeError(
        "A abertura da NFC-e terminou de forma inesperada."
    )

def localizar_valor_resumo(texto, rotulo_regex):
    texto_normalizado = normalizar_texto_busca(texto)

    correspondencia = re.search(
        rf"{rotulo_regex}\s*r?\$?\s*:?\s*([0-9][0-9.,]*)",
        texto_normalizado,
        flags=re.IGNORECASE,
    )

    if not correspondencia:
        return None, ""

    valor_original = correspondencia.group(1).strip()

    return (
        numero_br_para_float(valor_original),
        valor_original,
    )


def somar_valores_itens(nota):
    valores = []

    for item in nota.get("itens", []):
        valor = numero_br_para_float(
            item.get("valor_total", "")
        )

        if valor is not None:
            valores.append(valor)

    if not valores:
        return None

    return round(sum(valores), 2)


def extrair_resumo_financeiro(texto, nota):
    valor_total_bruto, total_original = (
        localizar_valor_resumo(
            texto,
            r"valor\s+total",
        )
    )

    valor_descontos, descontos_original = (
        localizar_valor_resumo(
            texto,
            r"descontos?",
        )
    )

    valor_pagar_texto, pagar_original_texto = (
        localizar_valor_resumo(
            texto,
            r"valor\s+a\s+pagar",
        )
    )

    valor_pagar_nota = numero_br_para_float(
        nota.get("valor_pagar", "")
    )

    valor_pagar = (
        valor_pagar_texto
        if valor_pagar_texto is not None
        else valor_pagar_nota
    )

    if valor_total_bruto is None:
        valor_total_bruto = somar_valores_itens(nota)

    if (
        valor_descontos is None
        and valor_total_bruto is not None
        and valor_pagar is not None
    ):
        diferenca = round(
            valor_total_bruto - valor_pagar,
            2,
        )

        valor_descontos = (
            diferenca if diferenca > 0 else 0.0
        )

    return {
        "valorTotalBruto": valor_total_bruto,
        "valorTotalBrutoOriginal": total_original,
        "valorDescontos": valor_descontos,
        "valorDescontosOriginal": descontos_original,
        "valorPagar": valor_pagar,
        "valorPagarOriginal": (
            pagar_original_texto
            or str(nota.get("valor_pagar", "")).strip()
        ),
        "temDesconto": (
            valor_descontos is not None
            and valor_descontos > 0.005
        ),
    }


def variavel_verdadeira(nome, padrao=False):
    valor = os.getenv(nome)

    if valor is None:
        return padrao

    return str(valor).strip().lower() in {
        "1", "true", "sim", "yes", "on"
    }


def carregar_credencial_firebase():
    credencial_json = os.getenv(
        "FIREBASE_SERVICE_ACCOUNT",
        "",
    ).strip()

    if credencial_json:
        try:
            dados_credencial = json.loads(credencial_json)
        except json.JSONDecodeError as erro:
            raise ValueError(
                "O segredo FIREBASE_SERVICE_ACCOUNT não contém "
                "um JSON válido."
            ) from erro

        return credentials.Certificate(dados_credencial)

    if ARQUIVO_CHAVE.exists():
        return credentials.Certificate(str(ARQUIVO_CHAVE))

    raise FileNotFoundError(
        "Não encontrei a credencial do Firebase. No PC, mantenha "
        "serviceAccountKey.json na pasta do projeto. No GitHub "
        "Actions, crie o segredo FIREBASE_SERVICE_ACCOUNT."
    )


def iniciar_firestore():
    if not firebase_admin._apps:
        firebase_admin.initialize_app(
            carregar_credencial_firebase()
        )

    return firestore.client()


def recuperar_processamentos_travados(db):
    limite = datetime.now(timezone.utc) - timedelta(
        minutes=MINUTOS_PROCESSAMENTO_TRAVADO
    )
    recuperadas = 0

    consulta = (
        db.collection(COLECAO_FILA)
        .where("estado", "==", "processando")
    )

    for documento in consulta.stream():
        dados = documento.to_dict() or {}
        iniciado_em = dados.get("iniciadoEm")

        esta_travada = (
            not isinstance(iniciado_em, datetime)
            or iniciado_em < limite
        )

        if not esta_travada:
            continue

        documento.reference.update({
            "estado": "aguardando",
            "erro": (
                "Uma execução anterior foi interrompida. "
                "A nota voltou automaticamente para a fila."
            ),
            "atualizadoEm": firestore.SERVER_TIMESTAMP,
        })
        recuperadas += 1

    if recuperadas:
        print(
            f"Processamentos interrompidos recuperados: {recuperadas}"
        )


def buscar_solicitacoes_aguardando(db):
    consulta = (
        db.collection(COLECAO_FILA)
        .where("estado", "==", "aguardando")
    )

    return list(consulta.stream())


def mensagem_erro_publica(erro, chave="", link=""):
    mensagem = str(erro).strip() or "Erro não identificado."

    if link:
        mensagem = mensagem.replace(link, "[endereço ocultado]")

    if chave:
        mensagem = mensagem.replace(chave, "[chave ocultada]")

    mensagem = re.sub(
        r"https?://\S+",
        "[endereço ocultado]",
        mensagem,
        flags=re.IGNORECASE,
    )
    mensagem = re.sub(
        r"(?<!\d)\d{44}(?!\d)",
        "[chave ocultada]",
        mensagem,
    )

    return mensagem[:600]


def preparar_itens(nota):
    itens = []

    for indice, item in enumerate(nota.get("itens", []), start=1):
        itens.append({
            "indice": indice,
            "produtoOriginal": str(item.get("produto", "")).strip(),
            "codigo": str(item.get("codigo", "")).strip(),
            "quantidadeComprada": str(item.get("quantidade", "")).strip(),
            "unidadeCompra": str(item.get("unidade", "")).strip(),
            "valorUnitarioNota": numero_br_para_float(
                item.get("valor_unitario", "")
            ),
            "valorTotal": numero_br_para_float(
                item.get("valor_total", "")
            ),
            "quantidadeOriginal": str(
                item.get("quantidade", "")
            ).strip(),
            "valorUnitarioOriginal": str(
                item.get("valor_unitario", "")
            ).strip(),
            "valorTotalOriginal": str(
                item.get("valor_total", "")
            ).strip(),
        })

    return itens


def validar_nota(
    nota,
    chave_esperada,
    texto_pagina="",
):
    chave_extraida = str(nota.get("chave", "")).strip()
    mercado = str(nota.get("mercado", "")).strip()
    itens = nota.get("itens", [])

    parece_nfce = pagina_tem_sinais_nfce(
        texto_pagina
    )

    if not chave_extraida:
        if not parece_nfce:
            raise ValueError(
                "A SEFAZ-RJ respondeu, mas a página recebida "
                "não parece ser uma NFC-e."
            )

        raise ValueError(
            "A NFC-e foi recebida, mas não foi possível "
            "identificar sua chave."
        )

    if chave_extraida != chave_esperada:
        raise ValueError(
            "A chave extraída da página é diferente da chave enviada pelo app."
        )

    if not mercado:
        if not parece_nfce:
            raise ValueError(
                "A SEFAZ-RJ respondeu, mas a página recebida "
                "não parece ser uma NFC-e."
            )

        raise ValueError(
            "A NFC-e foi recebida, mas não foi possível "
            "identificar o mercado no formato atual da página."
        )

    if not itens:
        if not parece_nfce:
            raise ValueError(
                "A SEFAZ-RJ respondeu, mas a página recebida "
                "não parece ser uma NFC-e."
            )

        raise ValueError(
            "A NFC-e foi recebida, mas nenhum item pôde ser "
            "extraído no formato atual da página."
        )


def deve_salvar_debug():
    valor_configurado = os.getenv("SALVAR_DEBUG")

    if valor_configurado is not None:
        return variavel_verdadeira("SALVAR_DEBUG")

    # No GitHub Actions, os arquivos de debug podem conter dados
    # pessoais da nota e não são necessários para a rotina normal.
    return not variavel_verdadeira("CI")


def salvar_debug(chave, texto, html):
    if not deve_salvar_debug():
        return

    PASTA_DEBUG.mkdir(exist_ok=True)

    (PASTA_DEBUG / f"{chave}.txt").write_text(
        texto,
        encoding="utf-8",
    )

    (PASTA_DEBUG / f"{chave}.html").write_text(
        html,
        encoding="utf-8",
    )


def salvar_nota_capturada(
    db,
    chave,
    link,
    nota,
    texto_pagina,
):
    itens = preparar_itens(nota)
    resumo_financeiro = extrair_resumo_financeiro(
        texto_pagina,
        nota,
    )

    dados_nota = {
        "chave": chave,
        "linkOriginal": link,
        "mercadoOriginal": str(nota.get("mercado", "")).strip(),
        "cnpj": str(nota.get("cnpj", "")).strip(),
        "dataEmissao": str(nota.get("data_emissao", "")).strip(),
        "numero": str(nota.get("numero", "")).strip(),
        "serie": str(nota.get("serie", "")).strip(),
        "valorTotalBruto":
            resumo_financeiro["valorTotalBruto"],
        "valorTotalBrutoOriginal":
            resumo_financeiro["valorTotalBrutoOriginal"],
        "valorDescontos":
            resumo_financeiro["valorDescontos"],
        "valorDescontosOriginal":
            resumo_financeiro["valorDescontosOriginal"],
        "valorPagar":
            resumo_financeiro["valorPagar"],
        "valorPagarOriginal":
            resumo_financeiro["valorPagarOriginal"],
        "temDesconto":
            resumo_financeiro["temDesconto"],
        "qtdTotalItens": str(
            nota.get("qtd_total_itens", "")
        ).strip(),
        "quantidadeItensExtraidos": len(itens),
        "itens": itens,
        "estadoConferencia": "pendente",
        "capturadaEm": firestore.SERVER_TIMESTAMP,
        "atualizadoEm": firestore.SERVER_TIMESTAMP,
    }

    db.collection(COLECAO_NOTAS).document(chave).set(
        dados_nota,
        merge=True,
    )

    return dados_nota

def marcar_processando(referencia):
    referencia.update({
        "estado": "processando",
        "erro": "",
        "iniciadoEm": firestore.SERVER_TIMESTAMP,
        "atualizadoEm": firestore.SERVER_TIMESTAMP,
    })


def marcar_extraida(referencia, dados_nota):
    referencia.update({
        "estado": "extraida",
        "erro": "",
        "mercado": dados_nota["mercadoOriginal"],
        "dataEmissao": dados_nota["dataEmissao"],
        "quantidadeItensExtraidos":
            dados_nota["quantidadeItensExtraidos"],
        "valorTotalBruto":
            dados_nota.get("valorTotalBruto"),
        "valorDescontos":
            dados_nota.get("valorDescontos"),
        "valorPagar":
            dados_nota["valorPagar"],
        "temDesconto":
            dados_nota.get("temDesconto", False),
        "notaCapturadaId": dados_nota["chave"],
        "finalizadoEm": firestore.SERVER_TIMESTAMP,
        "atualizadoEm": firestore.SERVER_TIMESTAMP,
    })

def marcar_erro(referencia, erro):
    mensagem = str(erro).strip() or "Erro não identificado."

    referencia.update({
        "estado": "erro",
        "erro": mensagem[:1500],
        "finalizadoEm": firestore.SERVER_TIMESTAMP,
        "atualizadoEm": firestore.SERVER_TIMESTAMP,
    })


def encontrar_edge():
    candidatos = []

    if os.name == "nt":
        for variavel in ("ProgramFiles(x86)", "ProgramFiles"):
            base = os.environ.get(variavel)

            if base:
                candidatos.append(
                    Path(base)
                    / "Microsoft"
                    / "Edge"
                    / "Application"
                    / "msedge.exe"
                )
    else:
        for nome in (
            "microsoft-edge",
            "microsoft-edge-stable",
        ):
            caminho = shutil.which(nome)

            if caminho:
                candidatos.append(Path(caminho))

    for caminho in candidatos:
        if caminho.exists():
            return caminho

    raise FileNotFoundError(
        "Não foi possível localizar o Microsoft Edge."
    )


def escolher_porta_livre():
    with socket.socket(
        socket.AF_INET,
        socket.SOCK_STREAM,
    ) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def encerrar_edge_externo(processo, pasta_perfil):
    if processo is not None and processo.poll() is None:
        processo.terminate()

        try:
            processo.wait(timeout=5)
        except subprocess.TimeoutExpired:
            processo.kill()
            processo.wait(timeout=5)

    if pasta_perfil is not None:
        shutil.rmtree(
            pasta_perfil,
            ignore_errors=True,
        )


def iniciar_edge_externo(modo_headless):
    edge = encontrar_edge()
    porta = escolher_porta_livre()

    pasta_perfil = Path(
        tempfile.mkdtemp(prefix="edge_nfce_")
    )

    argumentos = [
        str(edge),
        f"--remote-debugging-port={porta}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={pasta_perfil}",
        "--no-first-run",
        "--no-default-browser-check",
    ]

    if modo_headless:
        argumentos.append("--headless=new")

    argumentos.append("about:blank")

    processo = subprocess.Popen(
        argumentos,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    endereco_cdp = f"http://127.0.0.1:{porta}"

    for _ in range(40):
        if processo.poll() is not None:
            shutil.rmtree(
                pasta_perfil,
                ignore_errors=True,
            )

            raise RuntimeError(
                "O Microsoft Edge foi iniciado, "
                "mas encerrou inesperadamente."
            )

        try:
            with urllib.request.urlopen(
                f"{endereco_cdp}/json/version",
                timeout=1,
            ):
                return (
                    processo,
                    pasta_perfil,
                    endereco_cdp,
                )

        except Exception:
            time.sleep(0.5)

    encerrar_edge_externo(
        processo,
        pasta_perfil,
    )

    raise RuntimeError(
        "O Microsoft Edge abriu, mas a conexão "
        "de depuração não ficou disponível."
    )


def main():
    print("=" * 70)
    print("PROCESSADOR DA FILA DE NOTAS NFC-e")
    print("=" * 70)

    try:
        db = iniciar_firestore()
    except Exception as erro:
        print()
        print(f"ERRO: {erro}")
        sys.exit(1)

    recuperar_processamentos_travados(db)
    solicitacoes = buscar_solicitacoes_aguardando(db)

    if not solicitacoes:
        print()
        print("Nenhuma nota aguardando processamento.")
        return

    print()
    print(f"Notas aguardando: {len(solicitacoes)}")

    if deve_salvar_debug():
        PASTA_DEBUG.mkdir(exist_ok=True)

    sucessos = 0
    falhas = 0

    modo_headless = variavel_verdadeira(
        "MODO_HEADLESS",
        padrao=variavel_verdadeira("CI"),
    )

    print(
        "Modo do navegador: "
        + (
            "sem interface gráfica"
            if modo_headless
            else "com interface gráfica"
        )
    )

    with sync_playwright() as p:
        processo_edge, pasta_perfil_edge, endereco_cdp = (
            iniciar_edge_externo(modo_headless)
        )

        print(
            "Edge externo iniciado. "
            "Conectando o Playwright..."
        )

        navegador = p.chromium.connect_over_cdp(
            endereco_cdp
        )

        if not navegador.contexts:
            encerrar_edge_externo(
                processo_edge,
                pasta_perfil_edge,
            )

            raise RuntimeError(
                "O Playwright conectou ao Edge, "
                "mas não encontrou um contexto de navegação."
            )

        contexto = navegador.contexts[0]

        for numero, documento in enumerate(solicitacoes, start=1):
            referencia = documento.reference
            solicitacao = documento.to_dict() or {}
            chave = documento.id
            link = str(
                solicitacao.get("linkOriginal", "")
            ).strip()

            print()
            print("-" * 70)
            print(f"NOTA {numero}/{len(solicitacoes)}")

            if not link:
                erro = "A solicitação não possui linkOriginal."
                print(f"ERRO: {erro}")
                marcar_erro(referencia, erro)
                falhas += 1
                continue

            nota_existente = (
                db.collection(COLECAO_NOTAS)
                .document(chave)
                .get()
            )

            if nota_existente.exists:
                dados_existentes = nota_existente.to_dict() or {}

                possui_resumo_financeiro = (
                    "valorTotalBruto" in dados_existentes
                    and "valorDescontos" in dados_existentes
                )

                if possui_resumo_financeiro:
                    print(
                        "A nota já estava capturada com o resumo "
                        "financeiro completo. Atualizando a fila."
                    )
                    marcar_extraida(
                        referencia,
                        dados_existentes,
                    )
                    sucessos += 1
                    continue

                print(
                    "A nota já estava capturada, mas ainda não "
                    "possui os campos de desconto. Reprocessando."
                )

            pagina = contexto.new_page()

            try:
                marcar_processando(referencia)

                texto, html = abrir_nota_com_retentativas(
                    pagina,
                    link,
                    chave,
                )

                print("Extraindo os dados...")

                print("Extraindo os dados...")
                nota = processar_texto_da_nota(
                    texto,
                    link,
                )

                validar_nota(
                    nota,
                    chave,
                    texto,
                )

                dados_nota = salvar_nota_capturada(
                    db,
                    chave,
                    link,
                    nota,
                    texto,
                )

                marcar_extraida(
                    referencia,
                    dados_nota,
                )

                print(
                    "OK: "
                    f"{dados_nota['quantidadeItensExtraidos']} item(ns) "
                    "extraído(s)."
                )

                sucessos += 1

            except Exception as erro:
                print(
                    "ERRO: "
                    + mensagem_erro_publica(erro, chave, link)
                )
                marcar_erro(referencia, erro)
                falhas += 1

            finally:
                pagina.close()

        try:
            navegador.close()
        finally:
            encerrar_edge_externo(
                processo_edge,
                pasta_perfil_edge,
            )

    print()
    print("=" * 70)
    print("PROCESSAMENTO ENCERRADO")
    print("=" * 70)
    print(f"Notas capturadas: {sucessos}")
    print(f"Notas com erro: {falhas}")
    print()
    print(
        "Os dados extraídos foram gravados na coleção "
        f"{COLECAO_NOTAS}."
    )
    if deve_salvar_debug():
        print(
            "A pasta debug_fila_notas contém o texto e o HTML das notas. "
            "Não compartilhe esses arquivos."
        )
    else:
        print(
            "Arquivos de debug não foram gravados neste ambiente."
        )


if __name__ == "__main__":
    main()
