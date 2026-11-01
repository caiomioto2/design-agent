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

## Conversas separadas no Slack e no terminal

O serviço compartilhado atende Slack e TUI com as mesmas integrações. Cada conversa mantém seu
histórico, tarefas e sandbox; mensagens de outros projetos não são reunidas automaticamente.
`DESIGN_AGENT_SHARED_RUNTIME_URL` e `DESIGN_AGENT_SHARED_RUNTIME_TOKEN` conectam a TUI ao serviço.
`DESIGN_AGENT_SHARED_THREAD_ID` escolhe somente a conversa inicial padrão. `DESIGN_AGENT_SLACK_OWNER_USER_ID`
identifica o operador que pode retomar pelo Slack conversas criadas localmente.

Na TUI:

- `/threads`: liste conversas anteriores com títulos e IDs.
- `/resume ID` ou `/resume trecho único do título`: retome a conversa escolhida.
- `/projects`: liste projetos anteriores e a conversa à qual pertencem.
- `/project NOME`: retome a conversa que possui os arquivos desse projeto.
- `/new`: inicie uma conversa separada. `design-agent chat --new` faz o mesmo ao abrir.
- `design-agent chat --thread ID`: abra diretamente uma conversa salva.

No Slack, mencione o bot para iniciar a participação em um fio de canal. Depois, respostas nesse
fio dispensam @. O bot ignora os demais fios até ser chamado, inclusive após reiniciar o serviço.
Em mensagens diretas não é necessário mencionar. Para navegar, envie `threads`, `projetos`,
`retomar ID` ou `projeto NOME`. Retomar vincula apenas aquele fio à conversa escolhida; os históricos
permanecem separados. A TUI pode abrir esse mesmo ID e continuar de onde o Slack parou.

O agente dispõe das ferramentas `list_conversations`, `resume_conversation`, `list_previous_projects`
e `open_previous_project`: pedidos como "liste minhas conversas" e "retome a conversa da logo" podem
usar essas ferramentas, sem comandos decorados. A troca acontece ao terminar o turno atual. As
ferramentas de retomada não ficam disponíveis como navegação autônoma dos helpers em segundo plano.
Os projetos são encontrados pelo registro local dos volumes de cada conversa, sem mover ou copiar
seus arquivos para outra. O histórico de cada thread continua salvo em `workspace/state/`.

Respostas e anexos vão ao fio que originou o pedido. Turnos concorrentes da mesma conversa esperam
na mesma fila; conversas diferentes continuam isoladas. O serviço do Slack precisa estar ativo
para usar a TUI conectada. O GBrain permanece uma fonte compartilhada de conhecimento com as
permissões da conexão, sem substituir os históricos separados.
