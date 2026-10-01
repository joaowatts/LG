"""
Bot de avisos da Luminosity (CS2) por e-mail (Gmail), usando dados do HLTV.

Avisos:
  1. Jogo novo marcado (e mudança de horário)
  2. Lembretes antes do jogo (padrão: 1 dia antes e 60 min antes)
  3. Resultado final
  4. Novo evento/campeonato

Roda a cada X minutos (GitHub Actions) e guarda o que já avisou em state.json.
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

# ----------------------------- Configuração ---------------------------------
TEAM_ID = os.getenv("TEAM_ID", "6290")
TEAM_SLUG = os.getenv("TEAM_SLUG", "luminosity")
TEAM_NAME = os.getenv("TEAM_NAME", "Luminosity")
TEAM_URL = f"https://www.hltv.org/team/{TEAM_ID}/{TEAM_SLUG}"
HLTV = "https://www.hltv.org"

# Lembretes em minutos antes do jogo (padrão: 1 dia antes e 1 hora antes)
REMINDERS = sorted({int(x) for x in os.getenv("REMINDERS", "1440,60").split(",") if x.strip()},
                   reverse=True)
TZ = ZoneInfo(os.getenv("TIMEZONE", "America/Sao_Paulo"))
STATE_FILE = os.getenv("STATE_FILE", "state.json")
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"  # 1 = só imprime, não envia

# E-mail (Gmail + senha de app)
GMAIL_USER = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "")
# Destinatários separados por vírgula (vão em cópia oculta)
EMAIL_TO = [e.strip() for e in re.split(r"[,;\s]+", os.getenv("EMAIL_TO", "")) if e.strip()]

DIAS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


# ----------------------------- HLTV -----------------------------------------
def fetch_team_page() -> str:
    """Baixa a página do time imitando um Chrome real (o HLTV usa Cloudflare)."""
    from curl_cffi import requests as creq

    last_err = None
    for attempt in range(3):
        try:
            r = creq.get(TEAM_URL, impersonate="chrome", timeout=30,
                         headers={"Accept-Language": "en-US,en;q=0.9"})
            if r.status_code == 200 and "matchesBox" in r.text:
                return r.text
            last_err = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Não consegui acessar o HLTV ({last_err}).")


def _match_id(href: str):
    m = re.search(r"/matches/(\d+)/", href or "")
    return m.group(1) if m else None


def _event_id(href: str):
    m = re.search(r"/events/(\d+)/", href or "")
    return m.group(1) if m else None


def _logo(flex) -> str | None:
    """URL do logo do time (versão clara). Ignora SVG (Gmail não exibe) e o logo genérico."""
    if flex is None:
        return None
    imgs = flex.select("img.team-logo")
    img = next((i for i in imgs if "night-only" not in (i.get("class") or [])), None)
    src = (img.get("src") or "") if img else ""
    path = src.split("?")[0].lower()
    if not src or "placeholder" in path or path.endswith(".svg"):
        return None
    return HLTV + src if src.startswith("/") else src


def _parse_match_table(table) -> list[dict]:
    matches = []
    event_name = ""
    for row in table.find_all("tr"):
        classes = row.get("class") or []
        if "event-header-cell" in classes:
            a = row.find("a")
            event_name = a.get_text(strip=True) if a else row.get_text(strip=True)
            continue
        if "team-row" not in classes:
            continue
        link = row.select_one("a[href*='/matches/']")
        mid = _match_id(link["href"]) if link else None
        if not mid:
            continue
        date_span = row.select_one("td.date-cell [data-unix]")
        unix = int(date_span["data-unix"]) // 1000 if date_span else None
        t1 = row.select_one(".team-1")
        t2 = row.select_one(".team-2")
        scores = [s.get_text(strip=True) for s in row.select(".score-cell .score")]
        flexes = row.select("td.team-center-cell .team-flex")
        matches.append({
            "id": mid,
            "time": unix,
            "team1": t1.get_text(strip=True) if t1 else "?",
            "team2": t2.get_text(strip=True) if t2 else "?",
            "score1": scores[0] if len(scores) > 0 else "-",
            "score2": scores[1] if len(scores) > 1 else "-",
            "event": event_name,
            "url": HLTV + link["href"],
            "logo1": _logo(flexes[0]) if len(flexes) > 0 else None,
            "logo2": _logo(flexes[1]) if len(flexes) > 1 else None,
        })
    return matches


def parse_team_page(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    box = soup.select_one("#matchesBox")
    if box is None:
        raise RuntimeError("Estrutura do HLTV mudou: #matchesBox não encontrado.")

    upcoming, results = [], []
    for h2 in box.find_all("h2"):
        title = h2.get_text(strip=True).lower()
        table = h2.find_next("table")
        if table is None:
            continue
        if title.startswith("upcoming matches"):
            upcoming = _parse_match_table(table)
        elif title.startswith("recent results"):
            results = _parse_match_table(table)

    events = []
    ongoing = soup.select_one("#ongoingEvents")
    if ongoing:
        for a in ongoing.select("a[href*='/events/']"):
            eid = _event_id(a["href"])
            if not eid:
                continue
            name = a.select_one(".eventbox-eventname")
            dates = [int(s["data-unix"]) // 1000 for s in a.select(".eventbox-date [data-unix]")]
            events.append({
                "id": eid,
                "name": name.get_text(strip=True) if name else a.get_text(strip=True),
                "start": dates[0] if dates else None,
                "end": dates[1] if len(dates) > 1 else (dates[0] if dates else None),
                "url": HLTV + a["href"],
            })
    return {"upcoming": upcoming, "results": results, "events": events}


# ----------------------------- Formatação -----------------------------------
def fmt_dt(unix) -> str:
    if not unix:
        return "horário a definir"
    d = datetime.fromtimestamp(unix, TZ)
    return f"{DIAS[d.weekday()]} {d:%d/%m} às {d:%H:%M}"


def fmt_day(unix) -> str:
    return datetime.fromtimestamp(unix, TZ).strftime("%d/%m") if unix else "?"


def opponent(m: dict) -> str:
    return m["team2"] if m["team1"].lower() == TEAM_NAME.lower() else m["team1"]


def logos(m: dict) -> tuple:
    """(logo da Luminosity, logo do adversário)."""
    l1, l2 = m.get("logo1"), m.get("logo2")
    return (l1, l2) if m["team1"].lower() == TEAM_NAME.lower() else (l2, l1)


class Msg(str):
    """Texto simples do aviso (usado no corpo em texto e nos testes) + dados para o e-mail bonito."""

    def __new__(cls, text, **card):
        obj = super().__new__(cls, text)
        obj.card = card
        return obj


def _fmt_date(unix) -> str:
    d = datetime.fromtimestamp(unix, TZ)
    return f"{DIAS[d.weekday()]} {d:%d/%m}"


def _fmt_hour(unix) -> str:
    return datetime.fromtimestamp(unix, TZ).strftime("%H:%M") + " (Brasília)"


def _when_rows(m):
    if not m["time"]:
        return [("Data", "a definir")]
    return [("Data", _fmt_date(m["time"])), ("Horário", _fmt_hour(m["time"]))]


def msg_new_match(m):
    text = (f"🆕 *Jogo marcado: {TEAM_NAME} x {opponent(m)}*\n"
            f"🏆 {m['event']}\n"
            f"🗓️ {fmt_dt(m['time'])} (Brasília)\n{m['url']}")
    return Msg(text, kind="new", label="Jogo marcado", headline="Novo jogo na agenda",
               match=m, rows=[("Campeonato", m["event"])] + _when_rows(m),
               url=m["url"], button="Ver partida no HLTV")


def msg_time_changed(m, old):
    text = (f"⏰ *Horário alterado: {TEAM_NAME} x {opponent(m)}*\n"
            f"Antes: {fmt_dt(old)}\nAgora: *{fmt_dt(m['time'])}* (Brasília)\n{m['url']}")
    return Msg(text, kind="time", label="Horário alterado", headline="O horário do jogo mudou",
               match=m, rows=[("Campeonato", m["event"]),
                              ("Antes", f"~~{fmt_dt(old)}~~"),
                              ("Agora", f"{fmt_dt(m['time'])} (Brasília)")],
               url=m["url"], button="Ver partida no HLTV")


def _when(unix, now) -> str:
    """'hoje às 15:00', 'amanhã às 15:00' ou 'sex 02/10 às 15:00'."""
    d = datetime.fromtimestamp(unix, TZ)
    diff = (d.date() - datetime.fromtimestamp(now, TZ).date()).days
    if diff == 0:
        return f"hoje às {d:%H:%M}"
    if diff == 1:
        return f"amanhã às {d:%H:%M}"
    return fmt_dt(unix)


def msg_reminder(m, minutes_left, now=None):
    now = now or int(time.time())
    vs = f"{TEAM_NAME} x {opponent(m)}"
    if minutes_left >= 120:  # lembrete com antecedência (ex.: 1 dia antes)
        when = _when(m["time"], now)
        when = when[0].upper() + when[1:]
        head = f"📅 *{when}: {vs}*"
        kind, label, headline = "day", "Lembrete", f"{when}"
    else:
        head = f"🔔 *Em {minutes_left} min: {vs}*"
        kind, label, headline = "hour", "Começa já já", f"Começa em {minutes_left} min"
    text = (f"{head}\n"
            f"🏆 {m['event']}\n"
            f"Começa {fmt_dt(m['time'])} (Brasília)\n{m['url']}")
    return Msg(text, kind=kind, label=label, headline=headline, match=m,
               rows=[("Campeonato", m["event"])] + _when_rows(m),
               url=m["url"], button="Assistir / acompanhar no HLTV")


def msg_result(m):
    ours_first = m["team1"].lower() == TEAM_NAME.lower()
    our, their = (m["score1"], m["score2"]) if ours_first else (m["score2"], m["score1"])
    try:
        won = int(our) > int(their)
        head = "✅ Vitória" if won else "❌ Derrota"
        kind = "win" if won else "loss"
        headline = f"Vitória da {TEAM_NAME}!" if won else f"Derrota da {TEAM_NAME}"
    except ValueError:
        head, kind, headline = "🏁 Fim de jogo", "end", "Fim de jogo"
    text = (f"{head}: *{TEAM_NAME} {our} x {their} {opponent(m)}*\n"
            f"🏆 {m['event']}\n{m['url']}")
    return Msg(text, kind=kind, label="Resultado", headline=headline, match=m,
               score=(our, their), rows=[("Campeonato", m["event"])],
               url=m["url"], button="Ver estatísticas no HLTV")


def msg_new_event(e):
    text = (f"🏆 *Novo campeonato: {e['name']}*\n"
            f"📅 {fmt_day(e['start'])} a {fmt_day(e['end'])}\n{e['url']}")
    return Msg(text, kind="event", label="Novo campeonato", headline=e["name"],
               rows=[("Período", f"{fmt_day(e['start'])} a {fmt_day(e['end'])}")],
               url=e["url"], button="Ver campeonato no HLTV")


# ----------------------------- E-mail ---------------------------------------
ACCENTS = {  # cor de destaque, fundo do selo
    "new": ("#7c3aed", "#efe7ff"), "day": ("#7c3aed", "#efe7ff"), "info": ("#7c3aed", "#efe7ff"),
    "hour": ("#d97706", "#fff1dc"), "time": ("#2563eb", "#e3ecff"),
    "win": ("#16a34a", "#dcf5e5"), "loss": ("#dc2626", "#fde4e4"), "end": ("#6b7280", "#eceef1"),
    "event": ("#b7791f", "#fbf0d9"),
}


def _split_message(text: str) -> tuple[str, str]:
    """1ª linha vira o assunto; o resto, o corpo."""
    lines = text.strip().splitlines()
    subject = lines[0].replace("*", "").strip()
    return subject, "\n".join(lines[1:]).strip()


def _to_html(subject: str, body: str, card: dict | None = None) -> str:
    from html import escape as e

    card = dict(card or {})
    if not card:  # mensagens simples (boas-vindas, teste)
        urls = re.findall(r"https?://\S+", body)
        paras = [re.sub(r"\*(.+?)\*", r"<b>\1</b>", e(p)) for p in body.split("\n") if p.strip()
                 and not p.strip().startswith("http")]
        card = {"kind": "info", "label": "Aviso", "headline": re.sub(r"^\W+\s*", "", subject),
                "paras": paras, "url": urls[0] if urls else None, "button": "Abrir no HLTV"}
    accent, soft = ACCENTS.get(card.get("kind"), ACCENTS["info"])
    font = "-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif"

    # Placar / confronto
    board = ""
    m = card.get("match")
    if m:
        mid = (f'{e(card["score"][0])} : {e(card["score"][1])}' if card.get("score")
               else '<span style="font-size:16px;color:#9a92ab;font-weight:600">vs</span>')
        def team(name, logo):
            if logo:
                img = (f'<img src="{e(logo)}" width="56" height="56" alt="" '
                       f'style="display:block;margin:0 auto 8px;width:56px;height:56px;object-fit:contain;border:0">')
            else:  # sem logo (SVG ou adversário indefinido): círculo com a inicial
                letter = "?" if ("/" in name or "winner" in name.lower()) else (name[:1].upper() or "?")
                img = ('<div style="width:56px;height:56px;line-height:56px;margin:0 auto 8px;border-radius:50%;'
                       f'background:#e7e1f2;color:#5b4b7a;font-size:22px;font-weight:800;text-align:center">{e(letter)}</div>')
            return (f'<td width="40%" align="center" valign="middle" style="padding:18px 8px;font-size:17px;'
                    f'font-weight:700;color:#1f1235">{img}{e(name)}</td>')

        our_logo, opp_logo = logos(m)
        board = (
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="margin:18px 0 6px;background:#f7f5fb;border-radius:12px">'
            '<tr>'
            + team(TEAM_NAME, our_logo) +
            f'<td width="20%" align="center" valign="middle" style="padding:18px 4px;font-size:26px;font-weight:800;color:{accent};white-space:nowrap">{mid}</td>'
            + team(opponent(m), opp_logo) +
            '</tr></table>')

    rows = ""
    for label, value in card.get("rows", []):
        strike = value.startswith("~~") and value.endswith("~~")
        value = e(value.strip("~"))
        style = "text-decoration:line-through;color:#9a92ab;font-weight:500" if strike else "color:#1f1235;font-weight:600"
        rows += (f'<tr><td style="padding:10px 0;border-bottom:1px solid #eeeaf4;color:#8a8399;font-size:13px;width:110px">{e(label)}</td>'
                 f'<td style="padding:10px 0;border-bottom:1px solid #eeeaf4;font-size:15px;{style}">{value}</td></tr>')
    if rows:
        rows = f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:8px">{rows}</table>'

    paras = "".join(f'<p style="margin:12px 0 0;font-size:15px;line-height:1.55;color:#3b3350">{p}</p>'
                    for p in card.get("paras", []))

    button = ""
    if card.get("url"):
        button = (f'<tr><td align="center" style="padding:24px 28px 30px">'
                  f'<a href="{e(card["url"])}" style="display:inline-block;background:{accent};color:#ffffff;'
                  f'text-decoration:none;font-weight:700;font-size:15px;padding:13px 26px;border-radius:10px">'
                  f'{e(card.get("button", "Ver no HLTV"))} &rarr;</a>'
                  f'<div style="margin-top:10px;font-size:12px;color:#9a92ab">'
                  f'<a href="{e(card["url"])}" style="color:#9a92ab">{e(card["url"].replace("https://www.", ""))[:70]}</a></div>'
                  f'</td></tr>')
    else:
        button = '<tr><td style="padding:0 28px 26px"></td></tr>'

    return f"""<!doctype html><html><body style="margin:0;padding:0;background:#f1edf8">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f1edf8"><tr><td align="center" style="padding:28px 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;border-radius:16px;overflow:hidden;font-family:{font};box-shadow:0 2px 10px rgba(40,10,80,.08)">
<tr><td style="background:#22093f;padding:18px 28px">
  <span style="color:#ffffff;font-size:15px;font-weight:800;letter-spacing:2px">LUMINOSITY</span>
  <span style="color:#b9a6e6;font-size:13px;font-weight:600;letter-spacing:1px">&nbsp;·&nbsp;CS2</span>
</td></tr>
<tr><td style="height:4px;line-height:4px;font-size:0;background:{accent}">&nbsp;</td></tr>
<tr><td style="padding:26px 28px 0">
  <span style="display:inline-block;background:{soft};color:{accent};font-size:11px;font-weight:800;letter-spacing:1px;text-transform:uppercase;padding:5px 11px;border-radius:999px">{e(card.get("label", ""))}</span>
  <div style="margin-top:14px;font-size:24px;line-height:1.25;font-weight:800;color:#1f1235">{e(card.get("headline", subject))}</div>
  {board}{rows}{paras}
</td></tr>
{button}
<tr><td style="background:#faf8fd;border-top:1px solid #eeeaf4;padding:14px 28px;font-size:12px;line-height:1.5;color:#8a8399">
  Bot de avisos da Luminosity &middot; dados do HLTV &middot; horários de Brasília
</td></tr>
</table></td></tr></table></body></html>"""


def send_email(text: str) -> None:
    subject, body = _split_message(text)
    if DRY_RUN:
        print(f"---- [DRY_RUN] {subject} ----\n{body}\n")
        return
    import smtplib
    from email.message import EmailMessage

    if not (GMAIL_USER and GMAIL_APP_PASSWORD):
        raise RuntimeError("Faltam os segredos GMAIL_USER ou GMAIL_APP_PASSWORD.")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"Bot Luminosity <{GMAIL_USER}>"
    msg["To"] = GMAIL_USER          # dono da conta que envia recebe como destinatário principal
    if EMAIL_TO:                      # demais pessoas em cópia oculta
        msg["Bcc"] = ", ".join(EMAIL_TO)
    msg.set_content(body.replace("*", ""))
    msg.add_alternative(_to_html(subject, body, getattr(text, "card", None)), subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)
    print(f"E-mail enviado: {subject}")


# ----------------------------- Estado ---------------------------------------
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


# ----------------------------- Lógica ---------------------------------------
def compute_notifications(data: dict, state: dict, now: int) -> tuple[list[str], dict]:
    """Compara o que está no HLTV com o estado salvo. Retorna (mensagens, novo_estado)."""
    first_run = not state.get("initialized")
    matches = state.get("matches", {})   # id -> {time, sent (lembretes enviados), result_sent}
    events = state.get("events", {})     # id -> {name}
    out: list[str] = []

    def mark_passed(rec, secs_left):
        """Marca como enviados os lembretes cujo prazo já passou (evita aviso repetido
        logo depois de 'jogo marcado' ou 'horário alterado', que já trazem o horário)."""
        rec["sent"] = [t for t in REMINDERS if secs_left <= t * 60]

    # 1) Jogos futuros: novo jogo / horário alterado / lembretes
    for m in data["upcoming"]:
        rec = matches.get(m["id"])
        if rec is not None and "sent" not in rec:  # estado da versão antiga
            rec["sent"] = list(REMINDERS) if rec.pop("reminded", False) else []
        if rec is None:
            rec = {"time": m["time"], "sent": [], "result_sent": False}
            matches[m["id"]] = rec
            if m["time"]:
                mark_passed(rec, m["time"] - now)
            if not first_run:
                out.append(msg_new_match(m))
            continue
        if m["time"] and rec.get("time") and m["time"] != rec["time"]:
            out.append(msg_time_changed(m, rec["time"]))
            rec["time"] = m["time"]
            mark_passed(rec, m["time"] - now)
            continue
        if m["time"] and not rec.get("time"):
            rec["time"] = m["time"]
            mark_passed(rec, m["time"] - now)
            continue
        if not m["time"]:
            continue

        secs_left = m["time"] - now
        due = [t for t in REMINDERS if secs_left <= t * 60 and t not in rec["sent"]]
        if not due:
            continue
        rec["sent"] = sorted(set(rec["sent"]) | set(due), reverse=True)
        # Se mais de um venceu ao mesmo tempo (ex.: bot ficou parado), manda só o mais próximo.
        if secs_left > 0 and not first_run:
            out.append(msg_reminder(m, max(1, round(secs_left / 60)), now))

    # 2) Resultados: avisa uma vez por partida (só as recentes, até 24h)
    for m in data["results"]:
        if m["score1"] in ("-", "") or m["score2"] in ("-", ""):
            continue
        rec = matches.setdefault(m["id"], {"time": m["time"], "sent": list(REMINDERS), "result_sent": False})
        if rec.get("result_sent"):
            continue
        rec["result_sent"] = True
        recent = m["time"] and now - m["time"] < 24 * 3600
        if not first_run and recent:
            out.append(msg_result(m))

    # 3) Eventos/campeonatos novos
    for e in data["events"]:
        if e["id"] not in events:
            events[e["id"]] = {"name": e["name"]}
            if not first_run:
                out.append(msg_new_event(e))

    # Limpeza: esquece partidas com mais de 30 dias
    cutoff = now - 30 * 24 * 3600
    matches = {k: v for k, v in matches.items() if not v.get("time") or v["time"] > cutoff}

    new_state = {"initialized": True, "matches": matches, "events": events}
    return out, new_state


def main() -> int:
    test_msg = os.getenv("TEST_MESSAGE", "").strip()
    if test_msg:  # botão "Run workflow" com mensagem de teste
        send_email("🧪 *Teste do Bot Luminosity*\n" + test_msg)
        return 0

    state = load_state()
    try:
        html = fetch_team_page()
    except RuntimeError as e:
        # Falha temporária no HLTV não deve “quebrar” o bot; tenta na próxima rodada.
        print(f"AVISO: {e}")
        return 0

    data = parse_team_page(html)
    print(f"HLTV: {len(data['upcoming'])} jogos futuros, {len(data['results'])} resultados, "
          f"{len(data['events'])} eventos.")

    first_run = not state.get("initialized")
    msgs, new_state = compute_notifications(data, state, int(time.time()))

    if first_run:
        print("Primeira execução: estado salvo sem enviar avisos (evita spam).")
        if not DRY_RUN and os.getenv("SEND_WELCOME", "1") == "1":
            nxt = sorted([m for m in data["upcoming"] if m["time"]], key=lambda m: m["time"])
            txt = "🤖 *Bot da Luminosity ativado!*\nVou avisar jogos marcados, lembretes, resultados e campeonatos."
            if nxt:
                txt += f"\n\nPróximo jogo: {TEAM_NAME} x {opponent(nxt[0])} — {fmt_dt(nxt[0]['time'])} (Brasília)"
            send_email(txt)

    for text in msgs:
        send_email(text)

    save_state(new_state)
    print(f"{len(msgs)} aviso(s) enviado(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
