# Bot da Luminosity por e-mail 🟣

Manda e-mail para você e seus amigos sobre os jogos de CS2 da Luminosity, com dados do HLTV:

| Aviso | Assunto do e-mail (exemplo) |
|---|---|
| Jogo marcado | 🆕 Jogo marcado: Luminosity x FURIA |
| Horário alterado | ⏰ Horário alterado: Luminosity x FURIA |
| Lembrete (1 dia antes) | 📅 Amanhã às 15:00: Luminosity x FURIA |
| Lembrete (60 min antes) | 🔔 Em 60 min: Luminosity x FURIA |
| Resultado | ✅ Vitória: Luminosity 2 x 0 B8 |
| Novo campeonato | 🏆 Novo campeonato: Digital Crusade DraculaN Season 7 |

Como funciona: o GitHub Actions roda o `bot.py` a cada 10 min. O bot lê a página do time no HLTV, compara com o que já avisou (`state.json`) e manda só as novidades pelo seu Gmail. Na primeira vez ele só salva o estado e manda um e-mail de "bot ativado", sem disparar avisos antigos.

---

## Passo 1 — Senha de app do Gmail

1. Ative a **verificação em duas etapas** na conta Google: <https://myaccount.google.com/signinoptions/twosv>
2. Crie uma **senha de app**: <https://myaccount.google.com/apppasswords>. Dê um nome qualquer, como "Bot Luminosity".
3. O Google mostra uma senha de 16 letras. Copie (os espaços não importam).

Essa senha serve só para o bot enviar e-mails. Você pode apagá-la a qualquer momento nessa mesma página, e o bot para.

## Passo 2 — GitHub

1. Crie uma conta em <https://github.com> (se ainda não tiver) e crie um repositório novo, **público** (veja a observação abaixo).
2. Suba todos os arquivos desta pasta, **incluindo a pasta `.github`** (no site: *Add file → Upload files*, arraste a pasta inteira).
3. Vá em **Settings → Secrets and variables → Actions → New repository secret** e crie:

   | Nome | Valor |
   |---|---|
   | `GMAIL_USER` | seu Gmail, ex.: `joao@gmail.com` |
   | `GMAIL_APP_PASSWORD` | a senha de app de 16 letras |
   | `EMAIL_TO` | (opcional) e-mails dos amigos separados por vírgula, ex.: `ana@gmail.com, pedro@hotmail.com`. Sem ele, só você recebe. |

4. Vá em **Actions**, ative os workflows se pedir, abra **Bot Luminosity → Run workflow**. Em ~1 min deve chegar o e-mail "🤖 Bot da Luminosity ativado!".

Pronto: a partir daí ele roda sozinho.

Você recebe como destinatário principal e os amigos vão em **cópia oculta**, então um não vê o e-mail do outro.

**Por que público?** Em repositório público o GitHub Actions é ilimitado. Em privado são 2.000 min/mês grátis, e rodar a cada 10 min gasta ~4.300. A senha de app continua escondida mesmo em repositório público; só o código e o `state.json` ficam visíveis. Se preferir privado, troque o cron em `.github/workflows/bot.yml` para `*/30 * * * *`.

## Dica para seus amigos

- **Para não cair no spam:** na primeira mensagem, marcar "Não é spam" e adicionar o seu Gmail aos contatos.
- **Para não perder o lembrete:** no app do Gmail, criar um filtro com o assunto "Em 60 min" e marcar como importante, ou ativar notificação para esse marcador.

---

## Ajustes

- **Lembretes:** `REMINDERS` no `bot.yml`, em minutos antes do jogo (padrão `1440,60` = 1 dia e 1 hora). Ex.: `1440,180,30` para 1 dia, 3 horas e 30 min.
- **Adicionar/remover amigos:** edite o segredo `EMAIL_TO`.
- **Outro time:** defina `TEAM_ID`, `TEAM_SLUG` e `TEAM_NAME` (estão no link do HLTV: `/team/6290/luminosity`).
- **Testar sem enviar nada:** `DRY_RUN=1 python bot.py`.
- **Testes:** `pip install pytest beautifulsoup4 && pytest tests`.

## Limitações

- **O GitHub atrasa os agendamentos.** Em horários de pico o "a cada 10 min" pode virar 15–30 min. Por isso o último lembrete é de 60 min: mesmo atrasado, ainda chega antes do jogo.
- **HLTV e Cloudflare.** O HLTV bloqueia robôs. O bot usa `curl_cffi`, que imita um Chrome de verdade, e isso costuma passar. Se o HLTV bloquear, a rodada só pula (aparece `AVISO` no log do Actions) e o bot tenta de novo na próxima.
- **Se o HLTV mudar o layout**, o bot para de ler os jogos (aparece erro no Actions e o GitHub te avisa por e-mail).
- **Sem atividade por 60 dias** (nenhum jogo novo), o GitHub pausa o agendamento. É só reativar em Actions.
