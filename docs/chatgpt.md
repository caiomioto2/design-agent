# ChatGPT e terminal

Este fork usa sua assinatura ChatGPT pelo proxy local de
[codex-multi-auth](https://github.com/ndycode/codex-multi-auth), testado na versão 2.23.0.
A assinatura é paga. O consumo fica sujeito às cotas e aos modelos liberados para sua conta;
o caminho validado não usa uma chave faturada da API OpenAI. Serviços opcionais, como Paper,
MDA, voz e música, mantêm seus próprios planos e cobranças.

## Instalar e abrir

Siga o [README](../README.md#começar-com-chatgpt). A TUI oficial do Deep Agents Code fica na versão
0.1.87 para usar a mesma versão do Deep Agents que o núcleo deste projeto.
`./run.sh` abre ou retoma a última conversa; `./run.sh --new` começa uma nova.
Use `./run.sh --thread ID` para escolher uma conversa salva.

`design-agent configure-chatgpt` grava apenas a configuração local em `.env`, com permissão 0600.
A chave aleatória é uma senha do proxy loopback, não uma chave OpenAI. O comando não substitui uma
chave de outro endpoint já configurado. Modelos auxiliares passam a usar o mesmo modelo da assinatura.
OAuth continua no armazenamento do mcodex. O sandbox nunca recebe as credenciais.

O comando `chat` inicia o proxy se a porta configurada estiver livre, e encerra somente o proxy
que ele próprio iniciou. Um proxy já em execução é reutilizado. Para um serviço persistente,
rode `design-agent proxy` separadamente, com o mesmo diretório de trabalho e `.env`.
O adaptador usa um módulo do pacote mcodex que ainda não tem export público; a versão exata é
verificada antes da inicialização para evitar atualizar esse contrato silenciosamente.

## Arquivos na TUI

`upload_files` copia os resultados para `workspace/out/<thread>/`. A TUI não possui um painel de
anexos de chat. A resposta da ferramenta informa o caminho real; `/files` lista essas entregas.

Para acessar as imagens pelo navegador em outro computador, rode um serviço de downloads:

```bash
.venv/bin/design-agent files --host 127.0.0.1 --port 8132
.venv/bin/design-agent config set DESIGN_AGENT_DELIVERY_BASE_URL=http://127.0.0.1:8132
```

Reabra a TUI após configurar. Para uso remoto, escolha o IP Tailscale da máquina ou um proxy reverso
privado como host e URL acessível ao seu navegador. O servidor entrega apenas arquivos em
`workspace/out/`, sem índice nem execução de agentes. Cada link contém uma assinatura específica
para o arquivo; quem recebe o link e consegue chegar à máquina pode abri-lo. Trate os links como
privados. O segredo fica em `workspace/state/delivery.key`; removê-lo invalida os links existentes.
O serviço precisa continuar rodando para os links funcionarem.

## Paper e Figma

A TUI usa os mesmos MCPs do núcleo, configurados em `config/mcp.toml` e `PAPER_*`.
`/mcp` mostra quais carregaram. Abrir um aplicativo depois da TUI exige reabrir a TUI para carregar
a nova conexão. A autenticação de outro cliente Codex não é transferida automaticamente.

O [bridge do Paper](partners/paper.md) permite transferir rasters do sandbox com `paper_place_image`.
Esse importador ainda requer validação visual no desktop. O limite semanal do MCP do Paper é
independente do ChatGPT e não pode ser restaurado por rotação de contas do mcodex.

Comandos da TUI voltados ao servidor de programação, como trocar agentes ou autenticar provedores,
ficam fora desta integração. Use `/help`, `/files`, `/mcp`, `/clear` e `/quit`; altere o modelo pela
configuração do projeto e reinicie.
