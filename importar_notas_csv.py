from pathlib import Path
from urllib.parse import urlparse, parse_qs
import re
import csv

from playwright.sync_api import sync_playwright


ARQUIVO_LINKS = Path("links_notas.txt")
PASTA_DEBUG = Path("debug_notas")
ARQUIVO_NOTAS_CSV = Path("notas_importadas.csv")
ARQUIVO_ITENS_CSV = Path("itens_importados.csv")


def limpar_linha(linha):
    return re.sub(r"\s+", " ", linha).strip()


def normalizar_linhas(texto):
    return [limpar_linha(linha) for linha in texto.splitlines() if limpar_linha(linha)]


def extrair_chave_do_qrcode(link):
    url = urlparse(link)
    parametros = parse_qs(url.query)

    if "p" not in parametros:
        return ""

    valor_p = parametros["p"][0]
    chave = valor_p.split("|")[0]

    if re.fullmatch(r"\d{44}", chave):
        return chave

    return ""


def extrair_primeiro_cnpj(linhas):
    for linha in linhas:
        if linha.startswith("CNPJ:"):
            return linha.replace("CNPJ:", "").strip()
    return ""


def extrair_nome_mercado(linhas):
    for i, linha in enumerate(linhas):
        if linha.startswith("CNPJ:") and i > 0:
            return linhas[i - 1].strip()
    return ""


def extrair_chave_de_acesso(texto):
    pos = texto.lower().find("chave de acesso")
    if pos == -1:
        return ""

    trecho = texto[pos:pos + 500]
    apenas_digitos = re.sub(r"\D", "", trecho)

    if len(apenas_digitos) >= 44:
        return apenas_digitos[:44]

    return ""


def extrair_data_emissao(texto):
    m = re.search(
        r"Emissão:\s*([0-9]{2}/[0-9]{2}/[0-9]{4}\s+[0-9]{2}:[0-9]{2}:[0-9]{2})",
        texto
    )
    if m:
        return m.group(1)

    return ""


def extrair_numero_serie(texto):
    numero = ""
    serie = ""

    m = re.search(r"Número:\s*([0-9]+)\s+Série:\s*([0-9]+)", texto)
    if m:
        numero = m.group(1)
        serie = m.group(2)

    return numero, serie


def linha_parece_valor(linha):
    return re.fullmatch(
        r"[0-9]{1,3}(?:\.[0-9]{3})*,[0-9]{2}|[0-9]+,[0-9]{2}",
        linha
    ) is not None


def extrair_valor_apos_rotulo(linhas, rotulo):
    rotulo_min = rotulo.lower()

    for i, linha in enumerate(linhas):
        if rotulo_min in linha.lower():
            depois = linha.lower().split(rotulo_min, 1)[-1]

            m = re.search(
                r"([0-9]{1,3}(?:\.[0-9]{3})*,[0-9]{2}|[0-9]+,[0-9]{2}|[0-9]+)",
                depois
            )
            if m:
                return m.group(1)

            for j in range(i + 1, min(i + 5, len(linhas))):
                if re.fullmatch(r"[0-9]+", linhas[j]) or linha_parece_valor(linhas[j]):
                    return linhas[j]

    return ""


def extrair_quantidade_unidade_unitario(bloco):
    quantidade = ""
    unidade = ""
    valor_unitario = ""

    m_completo = re.search(
        r"Qtde\.?\s*:\s*([0-9]+(?:[,.][0-9]+)?)\s*"
        r"UN\s*:\s*([A-Za-zÀ-ÿ0-9]+)\s*"
        r"Vl\.\s*Unit\.?\s*:\s*([0-9]+(?:[,.][0-9]+)?)",
        bloco,
        re.IGNORECASE
    )

    if m_completo:
        quantidade = m_completo.group(1)
        unidade = m_completo.group(2)
        valor_unitario = m_completo.group(3)
    else:
        m_qtd = re.search(
            r"Qtde\.?\s*:\s*([0-9]+(?:[,.][0-9]+)?)",
            bloco,
            re.IGNORECASE
        )
        if m_qtd:
            quantidade = m_qtd.group(1)

        m_un = re.search(
            r"UN\s*:\s*([A-Za-zÀ-ÿ0-9]+?)(?=Vl\.|Vl\s|$)",
            bloco,
            re.IGNORECASE
        )
        if m_un:
            unidade = m_un.group(1)

        m_unit = re.search(
            r"Vl\.\s*Unit\.?\s*:\s*([0-9]+(?:[,.][0-9]+)?)",
            bloco,
            re.IGNORECASE
        )
        if m_unit:
            valor_unitario = m_unit.group(1)

    unidade = unidade.strip()

    if unidade.upper() == "KG":
        unidade = "kg"
    elif unidade.upper() == "UN":
        unidade = "un"

    return quantidade, unidade, valor_unitario


def extrair_valor_total_do_item(bloco_linhas):
    bloco = " ".join(bloco_linhas)

    m_total = re.search(
        r"Vl\.\s*Total\s*([0-9]{1,3}(?:\.[0-9]{3})*,[0-9]{2}|[0-9]+,[0-9]{2})",
        bloco,
        re.IGNORECASE
    )
    if m_total:
        return m_total.group(1)

    valores = re.findall(
        r"[0-9]{1,3}(?:\.[0-9]{3})*,[0-9]{2}|[0-9]+,[0-9]{2}",
        bloco
    )

    if valores:
        return valores[-1]

    return ""


def extrair_itens(linhas):
    itens = []
    i = 0

    while i < len(linhas):
        linha = linhas[i]

        if "(Código:" not in linha:
            i += 1
            continue

        m_item = re.search(r"^(.*?)\s*\(Código:\s*([0-9]+)", linha)
        if not m_item:
            i += 1
            continue

        nome = m_item.group(1).strip()
        codigo = m_item.group(2).strip()

        bloco_linhas = [linha]
        j = i + 1

        while j < len(linhas):
            proxima = linhas[j]

            if j != i + 1 and "(Código:" in proxima:
                break

            if proxima.startswith("Qtd. total de itens"):
                break

            if proxima.startswith("Valor a pagar"):
                break

            if proxima.startswith("Forma de pagamento"):
                break

            if proxima.startswith("Informação dos Tributos"):
                break

            bloco_linhas.append(proxima)
            j += 1

        bloco = " ".join(bloco_linhas)

        quantidade, unidade, valor_unitario = extrair_quantidade_unidade_unitario(bloco)
        valor_total = extrair_valor_total_do_item(bloco_linhas)

        itens.append({
            "produto": nome,
            "codigo": codigo,
            "quantidade": quantidade,
            "unidade": unidade,
            "valor_unitario": valor_unitario,
            "valor_total": valor_total,
        })

        i = j

    return itens


def processar_texto_da_nota(texto, link_original):
    linhas = normalizar_linhas(texto)

    mercado = extrair_nome_mercado(linhas)
    cnpj = extrair_primeiro_cnpj(linhas)

    chave = extrair_chave_de_acesso(texto)
    if not chave:
        chave = extrair_chave_do_qrcode(link_original)

    data_emissao = extrair_data_emissao(texto)
    numero, serie = extrair_numero_serie(texto)
    valor_pagar = extrair_valor_apos_rotulo(linhas, "Valor a pagar R$:")
    qtd_total_itens = extrair_valor_apos_rotulo(linhas, "Qtd. total de itens:")
    itens = extrair_itens(linhas)

    return {
        "link_original": link_original,
        "mercado": mercado,
        "cnpj": cnpj,
        "chave": chave,
        "data_emissao": data_emissao,
        "numero": numero,
        "serie": serie,
        "valor_pagar": valor_pagar,
        "qtd_total_itens": qtd_total_itens,
        "itens": itens,
    }


def ler_links():
    if not ARQUIVO_LINKS.exists():
        print(f"ERRO: não encontrei o arquivo {ARQUIVO_LINKS}")
        print("Crie esse arquivo e coloque um link de QR Code por linha.")
        return []

    links = []

    for linha in ARQUIVO_LINKS.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()

        if not linha:
            continue

        if linha.startswith("#"):
            continue

        links.append(linha)

    return links


def abrir_nota_como_celular(pagina, link):
    link_para_abrir = link.replace("|", "%7C")

    pagina.goto(link_para_abrir, wait_until="domcontentloaded", timeout=60000)
    pagina.wait_for_timeout(7000)

    texto = pagina.locator("body").inner_text(timeout=15000)
    html = pagina.content()

    return texto, html


def salvar_csv_notas(notas):
    campos = [
        "mercado",
        "cnpj",
        "data_emissao",
        "numero",
        "serie",
        "chave",
        "qtd_total_itens",
        "valor_pagar",
        "link_original",
    ]

    with ARQUIVO_NOTAS_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        escritor = csv.DictWriter(f, fieldnames=campos, delimiter=";")
        escritor.writeheader()

        for nota in notas:
            escritor.writerow({
                "mercado": nota["mercado"],
                "cnpj": nota["cnpj"],
                "data_emissao": nota["data_emissao"],
                "numero": nota["numero"],
                "serie": nota["serie"],
                "chave": nota["chave"],
                "qtd_total_itens": nota["qtd_total_itens"],
                "valor_pagar": nota["valor_pagar"],
                "link_original": nota["link_original"],
            })


def salvar_csv_itens(notas):
    campos = [
        "mercado",
        "cnpj",
        "data_emissao",
        "numero",
        "serie",
        "chave",
        "produto",
        "codigo",
        "quantidade",
        "unidade",
        "valor_unitario",
        "valor_total",
    ]

    with ARQUIVO_ITENS_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        escritor = csv.DictWriter(f, fieldnames=campos, delimiter=";")
        escritor.writeheader()

        for nota in notas:
            for item in nota["itens"]:
                escritor.writerow({
                    "mercado": nota["mercado"],
                    "cnpj": nota["cnpj"],
                    "data_emissao": nota["data_emissao"],
                    "numero": nota["numero"],
                    "serie": nota["serie"],
                    "chave": nota["chave"],
                    "produto": item["produto"],
                    "codigo": item["codigo"],
                    "quantidade": item["quantidade"],
                    "unidade": item["unidade"],
                    "valor_unitario": item["valor_unitario"],
                    "valor_total": item["valor_total"],
                })


def main():
    print("=" * 70)
    print("IMPORTADOR DE NOTAS SEFAZ-RJ PARA CSV")
    print("=" * 70)

    links = ler_links()

    if not links:
        return

    print()
    print(f"Links encontrados em {ARQUIVO_LINKS}: {len(links)}")

    PASTA_DEBUG.mkdir(exist_ok=True)

    notas_importadas = []
    chaves_ja_vistas = set()

    with sync_playwright() as p:
        navegador = p.chromium.launch(
            headless=False,
            slow_mo=200,
        )

        contexto = navegador.new_context(
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            viewport={"width": 412, "height": 915},
            is_mobile=True,
            has_touch=True,
            user_agent=(
                "Mozilla/5.0 (Linux; Android 16; SM-S926B) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Mobile Safari/537.36"
            ),
        )

        for indice, link in enumerate(links, start=1):
            print()
            print("-" * 70)
            print(f"NOTA {indice}")
            print("-" * 70)
            print(link)

            pagina = contexto.new_page()

            try:
                texto, html = abrir_nota_como_celular(pagina, link)

                nota = processar_texto_da_nota(texto, link)

                chave = nota["chave"]

                if chave and chave in chaves_ja_vistas:
                    print("Nota duplicada. Pulando.")
                    pagina.close()
                    continue

                if chave:
                    chaves_ja_vistas.add(chave)

                arquivo_txt = PASTA_DEBUG / f"nota_{indice}.txt"
                arquivo_html = PASTA_DEBUG / f"nota_{indice}.html"
                arquivo_txt.write_text(texto, encoding="utf-8")
                arquivo_html.write_text(html, encoding="utf-8")

                notas_importadas.append(nota)

                print(f"Mercado: {nota['mercado']}")
                print(f"CNPJ: {nota['cnpj']}")
                print(f"Data: {nota['data_emissao']}")
                print(f"Número: {nota['numero']}")
                print(f"Série: {nota['serie']}")
                print(f"Chave: {nota['chave']}")
                print(f"Qtd. total de itens: {nota['qtd_total_itens']}")
                print(f"Valor a pagar: {nota['valor_pagar']}")
                print(f"Itens encontrados: {len(nota['itens'])}")

                for item in nota["itens"]:
                    print(
                        f"  - {item['produto']} | "
                        f"qtd {item['quantidade']} {item['unidade']} | "
                        f"unit {item['valor_unitario']} | "
                        f"total {item['valor_total']}"
                    )

            except Exception as e:
                print("ERRO ao importar esta nota:")
                print(e)

            finally:
                pagina.close()

        navegador.close()

    if not notas_importadas:
        print()
        print("Nenhuma nota foi importada.")
        return

    salvar_csv_notas(notas_importadas)
    salvar_csv_itens(notas_importadas)

    print()
    print("=" * 70)
    print("IMPORTAÇÃO CONCLUÍDA")
    print("=" * 70)
    print(f"Notas importadas: {len(notas_importadas)}")
    print(f"Arquivo de notas: {ARQUIVO_NOTAS_CSV}")
    print(f"Arquivo de itens: {ARQUIVO_ITENS_CSV}")
    print()
    print("Observação: a pasta debug_notas guarda textos/HTML completos da nota.")
    print("Não me envie esses arquivos se eles tiverem CPF, nome ou endereço.")
    print("=" * 70)


if __name__ == "__main__":
    main()