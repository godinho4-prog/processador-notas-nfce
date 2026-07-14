import json
import os
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore


ARQUIVO_CHAVE = Path("serviceAccountKey.json")
COLECAO_FILA = "filaNotas"


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
        "Não encontrei serviceAccountKey.json nem o segredo "
        "FIREBASE_SERVICE_ACCOUNT."
    )


def iniciar_firestore():
    if not firebase_admin._apps:
        firebase_admin.initialize_app(
            carregar_credencial_firebase()
        )

    return firestore.client()


def registrar_saida_github(tem_notas):
    caminho_saida = os.getenv("GITHUB_OUTPUT", "").strip()

    if not caminho_saida:
        return

    with open(caminho_saida, "a", encoding="utf-8") as arquivo:
        arquivo.write(
            f"tem_notas={'true' if tem_notas else 'false'}\n"
        )


def main():
    db = iniciar_firestore()

    consulta_aguardando = (
        db.collection(COLECAO_FILA)
        .where("estado", "==", "aguardando")
        .limit(1)
    )

    tem_aguardando = any(consulta_aguardando.stream())
    tem_processando = False

    if not tem_aguardando:
        consulta_processando = (
            db.collection(COLECAO_FILA)
            .where("estado", "==", "processando")
            .limit(1)
        )
        tem_processando = any(consulta_processando.stream())

    deve_rodar = tem_aguardando or tem_processando
    registrar_saida_github(deve_rodar)

    if tem_aguardando:
        print("Há nota aguardando processamento.")
    elif tem_processando:
        print(
            "Há nota marcada como processando; "
            "o processador verificará se a execução foi interrompida."
        )
    else:
        print("Não há nota aguardando processamento.")


if __name__ == "__main__":
    main()
